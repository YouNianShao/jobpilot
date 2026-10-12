"""每平台独立风控预算 + 评分依据快照。"""
from __future__ import annotations

import pytest
from conftest import open_time_gates

from jobpilot.ai.llm import LlmResult
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig, DailyPages, PlatformSafetyCfg
from jobpilot.db import Repo, connect
from jobpilot.models import Job, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import PAGE_OPEN, Action, Denied, Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.scorer import score_jobs


def make_policy(tmp_path, cfg) -> Policy:
    return Policy(Repo(connect(tmp_path / "p.db")), cfg, sleep=lambda _seconds: None)


def test_boss_default_is_stricter_than_others():
    cfg = AppConfig()
    assert cfg.safety.pages_for("boss").collect_search < cfg.safety.pages_for("51job").collect_search
    assert cfg.safety.multiplier_for("boss") > cfg.safety.multiplier_for("51job")
    assert cfg.safety.lock_minutes_for("boss") > cfg.safety.lock_minutes_for("51job")

def test_boss_rate_window_is_stricter_than_others():
    """BOSS 的反爬最强：滚动窗口的每小时上限也要更低。"""
    cfg = AppConfig()
    assert cfg.safety.per_hour_for("boss") < cfg.safety.per_hour_for("51job")
    assert cfg.safety.per_10min_for("boss") < cfg.safety.per_10min_for("51job")


def test_per_platform_budget_is_isolated(tmp_path):
    cfg = AppConfig()
    cfg.safety.daily_pages = DailyPages(collect_search=10, collect_detail=10, apply=10, monitor=10, total=100)
    cfg.safety.platforms = {
        "boss": PlatformSafetyCfg(daily_pages=DailyPages(collect_search=1, collect_detail=1,
                                                         apply=1, monitor=1, total=10)),
    }
    cfg.safety.page_interval_min_seconds = 0
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.page_interval_max_seconds = 0
    policy = make_policy(tmp_path, cfg)

    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))   # 用掉 boss 的 1
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert exc.value.kind == "budget"

    # 51job 用全局的 10，完全不受 boss 影响
    policy.authorize(Action(kind=PAGE_OPEN, platform="51job", stage="collect_search"))
    assert policy.remaining("51job")["collect_search"] == 9


class TraceLlm:
    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        return LlmResult(data={"score": 88, "reason": "匹配"}, text="{}",
                         model="test-model", tokens_in=1234, tokens_out=56, latency_ms=999)


def test_score_trace_is_recorded(tmp_path):
    cfg = AppConfig()
    cfg.safety.page_interval_min_seconds = 0
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = 0
    cfg.safety.delivery_cooldown_max_minutes = 0
    repo = Repo(connect(tmp_path / "trace.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                        title="资深数据分析师", company="柯莱特集团", jd="经营分析"))
    ctx = ServiceContext(config=cfg, repo=repo, llm=TraceLlm(), policy=Policy(repo, cfg),
                         stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))

    result = score_jobs(ctx, "boss", resume_text="6 年数据分析经验")
    assert result["scored"] == 1

    trace = repo.get_score_trace("boss-1")
    assert trace["model"] == "test-model"
    assert trace["prompt"] == "scoring.v1.md v1"
    assert trace["tokens_in"] == 1234 and trace["tokens_out"] == 56
    assert trace["latency_ms"] == 999
    assert repo.get_job("boss-1").status == Status.PENDING_CONFIRM.value
