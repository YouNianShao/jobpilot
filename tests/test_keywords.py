"""搜索关键词建议：解析、去重、简历读不到时拒绝。"""
from __future__ import annotations

from jobpilot.ai.llm import LlmResult
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.keywords import suggest_keywords


class StubLlm:
    def __init__(self, data):
        self.data = data
        self.seen_prompt = ""

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.seen_prompt = user
        return LlmResult(data=self.data, text="{}", model="stub", tokens_in=10, tokens_out=20, latency_ms=5)


def make_ctx(tmp_path, data, *, resume: bool = True):
    cfg = AppConfig()
    if resume:
        path = tmp_path / "resume.md"
        path.write_text("6 年数据分析经验，保险行业，做过续保流失预警模型。", encoding="utf-8")
        cfg.profile.resume_path = str(path)
    else:
        cfg.profile.resume_path = ""
    cfg.platforms["boss"].keywords = ["AI"]
    repo = Repo(connect(tmp_path / "k.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                        title="数据分析师", company="某公司"))
    repo.set_score("boss-1", 88, "匹配", "pending_confirm")
    repo.add_event("boss-1", "collected", "上海/AI")
    llm = StubLlm(data)
    ctx = ServiceContext(config=cfg, repo=repo, llm=llm, policy=Policy(repo, cfg),
                         stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))
    return ctx, llm


def test_suggest_keywords_parses_and_dedupes(tmp_path):
    ctx, llm = make_ctx(tmp_path, {
        "keywords": [
            {"text": "数据分析师", "reason": "岗位名，最通用"},
            {"text": "数据分析师", "reason": "重复的会被去掉"},
            {"text": "  ", "reason": "空的会被去掉"},
            {"text": "BI 工程师", "reason": "偏工具方向"},
        ],
        "avoid": [{"text": "AI", "reason": "平均分低"}],
    })

    out = suggest_keywords(ctx)

    assert [row["text"] for row in out["keywords"]] == ["数据分析师", "BI 工程师"]
    assert out["avoid"][0]["text"] == "AI"
    assert out["model"] == "stub"
    assert out["prompt"].startswith("keywords.v1.md")
    # 提示词里要带上真实证据：当前用的词 + 效果 + 采样岗位
    assert "AI" in llm.seen_prompt
    assert "数据分析师 @ 某公司" in llm.seen_prompt
    assert "平均 88.0 分" in llm.seen_prompt


def test_suggest_keywords_refuses_without_resume(tmp_path):
    ctx, _ = make_ctx(tmp_path, {"keywords": []}, resume=False)
    out = suggest_keywords(ctx)
    assert "还没有配置简历" in out["error"]
