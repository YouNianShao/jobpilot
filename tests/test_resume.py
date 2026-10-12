"""定制简历：写盘、防编造、缺简历。"""
from __future__ import annotations

from pathlib import Path

from jobpilot.ai.llm import LlmResult
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.resume import generate_resume

SOURCE = """# 张三
6 年保险数据分析经验，2023 年用 K-Means 做代理人分层，
A/B 测试把转化率从 3.2% 提升到 5.7%，处理千万级数据。
"""


class ScriptedLlm:
    """按顺序返回预置 markdown；用完就一直返回最后一个。"""

    def __init__(self, *markdowns: str) -> None:
        self.queue = list(markdowns)
        self.calls = 0

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.calls += 1
        text = self.queue.pop(0) if len(self.queue) > 1 else (self.queue[0] if self.queue else "")
        return LlmResult(data={"markdown": text, "highlights": ["A/B 实验"]}, text="{}")


def make_ctx(tmp_path, llm, *, with_source=True):
    cfg = AppConfig()
    if with_source:
        src = tmp_path / "resume.md"
        src.write_text(SOURCE, encoding="utf-8")
        cfg.profile.resume_path = str(src)
    cfg.profile.resume_output_dir = str(tmp_path / "out")
    repo = Repo(connect(tmp_path / "resume.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                        title="资深数据分析师", company="柯莱特集团",
                        salary="20-28K", city="上海", jd="负责经营分析与建模"))
    return ServiceContext(config=cfg, repo=repo, llm=llm, policy=Policy(repo, cfg),
                          stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))


def test_list_numbering_is_not_flagged(tmp_path):
    good = ("# 张三\n"
            "1. 6 年保险数据分析经验\n"
            "2. 2023 年用 K-Means 做代理人分层\n"
            "3. A/B 测试把转化率从 3.2% 提升到 5.7%\n")
    ctx = make_ctx(tmp_path, ScriptedLlm(good))
    result = generate_resume(ctx, "boss-1")
    assert "error" not in result, result          # 列表序号 1./2./3. 不能触发防编造


def test_generates_and_writes_file(tmp_path):
    good = "# 张三\n求职意向：资深数据分析师\n6 年保险数据分析，2023 年用 K-Means 分层，转化率从 3.2% 到 5.7%。"
    ctx = make_ctx(tmp_path, ScriptedLlm(good))
    result = generate_resume(ctx, "boss-1")

    assert "error" not in result, result
    assert Path(result["path"]).exists()
    assert Path(result["path"]).read_text(encoding="utf-8") == good
    assert ctx.repo.get_job("boss-1").resume_path == result["path"]
    assert any(e["kind"] == "resume" for e in ctx.repo.list_events("boss-1"))


def test_fabricated_numbers_trigger_retry_then_succeed(tmp_path):
    fabricated = "# 张三\n8 年经验，管理 40 人团队，转化率提升 99%。"
    good = "# 张三\n6 年保险数据分析经验，2023 年用 K-Means 做代理人分层，转化率从 3.2% 提升到 5.7%。"
    llm = ScriptedLlm(fabricated, good)
    ctx = make_ctx(tmp_path, llm)
    result = generate_resume(ctx, "boss-1")

    assert "error" not in result, result
    assert result["warnings"] == []
    assert llm.calls == 2                      # 触发了带反馈的重试
    assert Path(result["path"]).exists()


def test_still_ungrounded_is_kept_as_warning(tmp_path):
    fabricated = "# 张三\n8 年经验，管理 40 人团队，转化率提升 99%。"
    ctx = make_ctx(tmp_path, ScriptedLlm(fabricated))
    result = generate_resume(ctx, "boss-1")

    assert "error" not in result
    assert result["warnings"]                       # 仍不合规 → 警告
    assert "99" in result["warnings"] or "40" in result["warnings"]
    assert Path(result["path"]).exists()            # 草稿保留，由用户判断


def test_requires_source_resume(tmp_path):
    ctx = make_ctx(tmp_path, ScriptedLlm("whatever"), with_source=False)
    result = generate_resume(ctx, "boss-1")
    assert "未配置简历" in result["error"]


def test_missing_job(tmp_path):
    ctx = make_ctx(tmp_path, ScriptedLlm("x"))
    result = generate_resume(ctx, "nope")
    assert result["error"] == "岗位不存在"
