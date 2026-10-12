"""自动跟进：到期判定、幂等、最小间隔、有回复即退出、缺简历。"""
from __future__ import annotations

from datetime import datetime, timedelta

from jobpilot.ai.llm import LlmResult
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.followup import find_due, generate_follow_ups

_TS = "%Y-%m-%d %H:%M:%S"


class ScriptedLlm:
    def __init__(self, message: str = "还在关注这个岗位，我做过 6 年数据分析。") -> None:
        self.message = message
        self.calls = 0

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.calls += 1
        return LlmResult(data={"message": self.message}, text="{}")


def make_ctx(tmp_path, *, applied_hours_ago: float = 72, with_resume: bool = True):
    cfg = AppConfig()
    cfg.follow_up.after_hours = 48
    cfg.follow_up.max_times = 1
    cfg.follow_up.min_gap_hours = 72
    if with_resume:
        source = tmp_path / "resume.md"
        source.write_text("6 年保险数据分析经验，流失预警 AUC 0.82。", encoding="utf-8")
        cfg.profile.resume_path = str(source)

    repo = Repo(connect(tmp_path / "followup.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                        title="资深数据分析师", company="柯莱特集团", jd="经营分析与建模"))
    repo.set_status("boss-1", Status.APPLIED.value)
    when = (datetime.now() - timedelta(hours=applied_hours_ago)).strftime(_TS)
    repo.conn.execute(
        "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
        ("boss-1", "applied", "applied", when),
    )
    repo.conn.commit()
    return ServiceContext(config=cfg, repo=repo, llm=ScriptedLlm(), policy=Policy(repo, cfg),
                          stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))


def test_due_after_threshold(tmp_path):
    ctx = make_ctx(tmp_path, applied_hours_ago=72)
    assert [job.id for job in find_due(ctx)] == ["boss-1"]


def test_not_due_before_threshold(tmp_path):
    ctx = make_ctx(tmp_path, applied_hours_ago=1)
    assert find_due(ctx) == []


def test_generate_then_no_longer_due(tmp_path):
    ctx = make_ctx(tmp_path)
    result = generate_follow_ups(ctx)
    assert result["generated"] == 1

    job = ctx.repo.get_job("boss-1")
    assert job.follow_up.startswith("还在关注")
    assert job.follow_up_at
    assert ctx.repo.count_events_for_job("boss-1", "follow_up") == 1

    # 幂等：max_times=1，生成过就不该再出现
    assert find_due(ctx) == []


def test_reply_removes_from_queue(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.repo.add_event("boss-1", "replied", "HR 已回复")
    assert find_due(ctx) == []


def test_disabled_does_nothing(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.follow_up.enabled = False
    assert find_due(ctx) == []
    assert generate_follow_ups(ctx)["skipped"] == "follow_up disabled"


def test_requires_resume(tmp_path):
    ctx = make_ctx(tmp_path, with_resume=False)
    result = generate_follow_ups(ctx)
    assert "未配置简历" in result["error"]


def test_min_gap_blocks_second_followup(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.follow_up.max_times = 3          # 允许跟进多次
    ctx.config.follow_up.min_gap_hours = 72
    generate_follow_ups(ctx)
    # 刚跟进过，最小间隔没过 → 不该再次到期
    assert find_due(ctx) == []

    # 把上次跟进时间推到 4 天前 → 又该到期
    past = (datetime.now() - timedelta(hours=96)).strftime(_TS)
    ctx.repo.conn.execute("UPDATE jobs SET follow_up_at = ? WHERE id = ?", (past, "boss-1"))
    ctx.repo.conn.commit()
    assert [job.id for job in find_due(ctx)] == ["boss-1"]
