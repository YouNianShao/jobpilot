"""面试准备：解析、去重、简历读不到时拒绝、无 JD 时拒绝。"""
from __future__ import annotations

from jobpilot.ai.llm import LlmResult
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.interview import prepare_interview


class StubLlm:
    def __init__(self, data):
        self.data = data
        self.seen = ""

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.seen = user
        return LlmResult(data=self.data, text="{}", model="stub", tokens_in=11, tokens_out=22, latency_ms=7)


def make_ctx(tmp_path, data, *, resume=True, jd="负责经营分析、指标体系与 A/B 实验，熟练 SQL/Python。"):
    cfg = AppConfig()
    if resume:
        path = tmp_path / "resume.md"
        path.write_text("6 年数据分析经验，做过续保流失预警模型（AUC 0.82）。", encoding="utf-8")
        cfg.profile.resume_path = str(path)
    else:
        cfg.profile.resume_path = ""
    repo = Repo(connect(tmp_path / "i.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                        title="高级数据分析师", company="某保险公司", city="上海",
                        salary="20-30K", jd=jd))
    llm = StubLlm(data)
    ctx = ServiceContext(config=cfg, repo=repo, llm=llm, policy=Policy(repo, cfg), stop=StopSignal())
    return ctx, llm, repo


def test_prepare_interview_parses_and_grounds_in_resume(tmp_path):
    ctx, llm, repo = make_ctx(tmp_path, {
        "questions": [
            {"q": "说说你做的流失预警模型", "why": "验证建模能力", "angle": "续保流失预警（AUC 0.82）"},
            {"q": "  ", "why": "空问题会被丢掉", "angle": ""},
        ],
        "watchouts": ["行业跨度：从保险到互联网"],
    })
    ctx.repo.upsert_interaction(key="manual:boss-1", platform="boss", job_id="boss-1",
                                hr_name="", state="interview", last_text="约了周三", deeplink="")

    out = prepare_interview(ctx, "boss-1")

    assert [q["q"] for q in out["questions"]] == ["说说你做的流失预警模型"]
    assert out["watchouts"] == ["行业跨度：从保险到互联网"]
    assert out["prompt"].startswith("interview.v1.md")
    # 提示词里必须带：真实简历、岗位、JD、已记录进展
    assert "续保流失预警" in llm.seen
    assert "高级数据分析师 @ 某保险公司" in llm.seen
    assert "A/B 实验" in llm.seen
    assert "约面试：约了周三" in llm.seen
    assert [e["kind"] for e in repo.list_events("boss-1")] == ["interview_prep"]


def test_prepare_interview_refuses_without_resume(tmp_path):
    ctx, _, _ = make_ctx(tmp_path, {"questions": []}, resume=False)
    assert "还没有配置简历" in prepare_interview(ctx, "boss-1")["error"]


def test_prepare_interview_refuses_without_jd(tmp_path):
    """没有 JD 就只能给通用题 —— 与其假装有用，不如说清怎么补上。"""
    ctx, _, _ = make_ctx(tmp_path, {"questions": []}, jd="")
    out = prepare_interview(ctx, "boss-1")
    assert "没有 JD" in out["error"]
    assert "打开详情页抓 JD" in out["error"]


def test_prepare_interview_unknown_job(tmp_path):
    ctx, _, _ = make_ctx(tmp_path, {"questions": []})
    assert "没有这个岗位" in prepare_interview(ctx, "nope")["error"]
