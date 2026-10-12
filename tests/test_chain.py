"""L1 全链路测试：采集 → 评分 → 招呼语 → 人工确认 → 投递（不需要 Chrome / AI Key）。"""
from __future__ import annotations

import pytest
from conftest import open_time_gates

from jobpilot.ai.llm import LlmResult
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import ApplyResult, CollectResult, Job, MonitorResult, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.platforms.registry import Registry
from jobpilot.safety.policy import Policy
from jobpilot.services.applier import apply_jobs
from jobpilot.services.collector import collect
from jobpilot.services.confirmer import approve, pending_count, reject
from jobpilot.services.context import ServiceContext
from jobpilot.services.greeter import greet_jobs
from jobpilot.services.orchestrator import check_mode, run
from jobpilot.services.scorer import score_jobs


class ScriptedLlm:
    """按 schema 自动回答，避免依赖响应顺序。"""

    def __init__(self, score: int = 90, greeting: str = "你好，我在做 AI 产品，做过推荐系统落地。") -> None:
        self.score = score
        self.greeting = greeting
        self.calls = 0

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.calls += 1
        props = (schema or {}).get("properties", {})
        if "score" in props:
            return LlmResult(data={"score": self.score, "reason": "技能匹配"}, text="{}")
        if "greeting" in props:
            return LlmResult(data={"greeting": self.greeting}, text="{}")
        return LlmResult(text="{}", data={})


class FakeDriver:
    key = "fake"
    name = "假平台"
    capabilities = frozenset({"collect", "apply", "monitor"})

    def collect(self, ctx) -> CollectResult:
        for index, (title, company) in enumerate([("AI 产品经理", "甲公司"), ("销售代表", "乙公司")]):
            ctx.repo.upsert_job(Job(
                id=f"fake-{index}", platform=self.key, external_id=str(index),
                title=title, company=company, salary="20-30K", city="上海",
                url=f"https://example.com/{index}", jd="负责 AI 产品",
            ))
        return CollectResult(platform=self.key, new=2, seen=2)

    def apply(self, ctx, job: Job) -> ApplyResult:
        return ApplyResult(status=Status.APPLIED.value, detail="ok")

    def monitor(self, ctx) -> MonitorResult:
        return MonitorResult(checked=1, events={"hr_replied": 1})


@pytest.fixture
def wired(tmp_path):
    cfg = AppConfig()
    # 简历必须真的存在：服务层现在会拒绝"读不到简历"的评分/招呼语
    resume_file = tmp_path / "resume.md"
    resume_file.write_text("# 张三\n6 年数据分析经验，做过推荐系统与 A/B 实验。\n", encoding="utf-8")
    cfg.profile.resume_path = str(resume_file)
    cfg.profile.deal_breakers = ["销售"]     # 用于验证预筛
    cfg.confirm.required = True
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = 0   # 本用例验证确认门，不验证冷却
    cfg.safety.delivery_cooldown_max_minutes = 0
    # 本用例验证的是链路，不是风控：把"时间窗"和"随机休息日"这两个跟真实时间/随机数
    # 有关的闸门关掉，否则深夜跑测试会失败，白天跑还有 5% 概率随机失败（真出现过）。
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    repo = Repo(connect(tmp_path / "chain.db"))
    registry = Registry()
    registry.register(FakeDriver())
    ctx = ServiceContext(
        config=cfg, repo=repo, llm=ScriptedLlm(),
        policy=Policy(repo, cfg), stop=StopSignal(),
        browser_factory=lambda: FakeBrowser([]),
    )
    return ctx, registry


def test_full_l1_chain(wired):
    ctx, registry = wired

    # 1) 采集
    collected = collect(ctx, "fake", registry=registry)
    assert collected["new"] == 2

    # 2) 评分：黑名单"销售"被预筛，其余进待确认
    scored = score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    assert scored["scored"] == 1
    assert scored["prefiltered"] == 1
    assert pending_count(ctx, "fake") == 1

    # 3) 招呼语
    greeted = greet_jobs(ctx, "fake")
    assert greeted["greeted"] == 1
    pending_job = [j for j in ctx.repo.list_jobs(platform="fake")
                   if j.status == Status.PENDING_CONFIRM.value][0]
    assert "AI 产品" in pending_job.greeting or "推荐" in pending_job.greeting

    # 4) 未确认前不能投递
    blocked = apply_jobs(ctx, "fake", registry=registry)
    assert blocked["applied"] == 0

    # 5) 人工确认 → 投递
    assert approve(ctx, "fake")["confirmed"] == 1
    applied = apply_jobs(ctx, "fake", registry=registry)
    assert applied["applied"] == 1
    assert ctx.repo.get_job(pending_job.id).status == Status.APPLIED.value
    assert ctx.policy.applied_today() == 1


def test_orchestrator_full_stops_for_confirmation(wired):
    ctx, registry = wired
    result = run(ctx, "full", "fake", registry=registry)
    assert result["awaiting_confirmation"] == 1
    assert "apply" not in result["steps"]


def test_confirmer_can_reject(wired):
    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    score_jobs(ctx, "fake", resume_text="x")
    assert reject(ctx, "fake")["rejected"] == 1
    assert pending_count(ctx, "fake") == 0

def test_apply_respects_platform_switch(wired):
    """「允许对该平台投递」以前没人读，现在必须是真闸门。"""
    ctx, registry = wired
    from jobpilot.config import PlatformCfg

    collect(ctx, "fake", registry=registry)
    score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    approve(ctx, "fake")
    ctx.config.platforms["fake"] = PlatformCfg(enabled=True, apply=False)

    result = apply_jobs(ctx, "fake", registry=registry)
    assert result["applied"] == 0
    assert "允许对该平台投递" in result["skipped"]

    ctx.config.platforms["fake"].apply = True
    assert apply_jobs(ctx, "fake", registry=registry)["applied"] == 1

def test_apply_counts_already_as_neither_success_nor_failure(wired):
    """早已聊过既不该算成功（不占额度、不触发投递间隔），也不该算失败 ——
    否则连着几个已聊过的岗位就会把熔断计数推上去。"""
    ctx, _ = wired

    class AlreadyDriver(FakeDriver):
        key = "fake"

        def apply(self, ctx, job: Job) -> ApplyResult:
            return ApplyResult(status=Status.ALREADY.value, detail="已经打过招呼")

    registry = Registry()
    registry.register(AlreadyDriver())
    collect(ctx, "fake", registry=registry)
    score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    approve(ctx, "fake")

    result = apply_jobs(ctx, "fake", registry=registry)
    assert result["applied"] == 0
    assert result["failed"] == 0
    assert result["already"] == 1
    assert ctx.policy.applied_today() == 0
    assert ctx.repo.get_job("fake-0").status == Status.ALREADY.value


def test_resume_loader_reports_problems(wired, tmp_path):
    """读不到简历时必须说清是哪种问题，而不是悄悄用空简历干活。"""
    ctx, _ = wired

    ctx.config.profile.resume_path = ""
    text, problem = ctx.resume_text()
    assert text == "" and "还没有配置" in problem

    ctx.config.profile.resume_path = str(tmp_path / "nope.md")
    text, problem = ctx.resume_text()
    assert text == "" and "不存在" in problem

    blank = tmp_path / "blank.md"
    blank.write_text("   \n", encoding="utf-8")
    ctx.config.profile.resume_path = str(blank)
    text, problem = ctx.resume_text()
    assert text == "" and "空" in problem

    ok = tmp_path / "ok.md"
    ok.write_text("6 年数据分析经验", encoding="utf-8")
    ctx.config.profile.resume_path = str(ok)
    text, problem = ctx.resume_text()
    assert problem == "" and "数据分析" in text


def test_score_refuses_when_resume_is_missing(wired, tmp_path):
    """简历读不到就拒绝评分：假分数会一路把岗位推向投递。"""
    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    ctx.config.profile.resume_path = str(tmp_path / "gone.md")

    result = score_jobs(ctx, "fake")
    assert result["scored"] == 0
    assert "不存在" in result["error"]
    assert all(job.score is None for job in ctx.repo.list_jobs(platform="fake"))


def test_greet_refuses_when_resume_is_missing(wired, tmp_path):
    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    ctx.config.profile.resume_path = str(tmp_path / "gone.md")

    result = greet_jobs(ctx, "fake")
    assert result["greeted"] == 0
    assert "不存在" in result["error"]

def test_manual_mark_applied_counts_and_is_idempotent(wired):
    """手动投递回执：写 applied、计入今日额度，且不会重复计数。"""
    from jobpilot.services.applier import mark_applied

    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    assert ctx.policy.applied_today() == 0

    result = mark_applied(ctx, "fake-0", note="BOSS 网页手动投递")
    assert result["ok"] is True
    assert result["remaining_today"] == ctx.config.apply.daily_limit - 1
    assert ctx.repo.get_job("fake-0").status == Status.APPLIED.value
    assert ctx.policy.applied_today() == 1
    kinds = [event["kind"] for event in ctx.repo.list_events("fake-0")]
    assert "applied" in kinds and "apply" in kinds      # 和自动投递同一套事件口径

    again = mark_applied(ctx, "fake-0")
    assert "已经记过" in again["error"]
    assert ctx.policy.applied_today() == 1              # 不重复计数

    assert "没有这个岗位" in mark_applied(ctx, "nope")["error"]


def test_manual_apply_shares_the_daily_limit_with_auto_apply(wired):
    """手动投递占掉额度后，自动投递也必须被挡下——同一个账号，同一个口径。"""
    from jobpilot.services.applier import mark_applied

    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    ctx.config.profile.deal_breakers = []          # 两个岗位都通过评分，确保还有得投
    score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    approve(ctx, "fake")
    ctx.config.apply.daily_limit = 1

    assert mark_applied(ctx, "fake-0")["ok"] is True
    blocked = apply_jobs(ctx, "fake", registry=registry)
    assert blocked["applied"] == 0
    assert "上限" in blocked["stopped"]           # 手动那一次已经占掉了今天的额度

def test_run_with_explicit_none_limit(wired):
    """从界面启动任务时不带 limit —— 这条路径曾经直接把"评分"按钮搞坏。"""
    ctx, registry = wired
    collect(ctx, "fake", registry=registry)

    result = run(ctx, "score", "fake", registry=registry, limit=None)
    assert result.get("error") is None
    assert result["scored"] == 1

    greeted = run(ctx, "greet", "fake", registry=registry, limit=None)
    assert greeted.get("error") is None
    assert greeted["greeted"] == 1


def test_collect_accepts_per_run_overrides(wired):
    '''「运行前配置」只作用于这一次：没传的字段保持原样，config 的长期值不被改。
    '''
    from jobpilot.config import PlatformCfg

    ctx, registry = wired
    ctx.config.platforms['fake'] = PlatformCfg(
        enabled=True, keywords=['原词'], cities=['上海'], max_pages=1)

    result = collect(ctx, 'fake', registry=registry,
                     keywords=['新词'], max_pages=5)

    assert result['options'] == {'keywords': ['新词'], 'max_pages': 5}
    assert ctx.config.platforms['fake'].keywords == ['新词']
    assert ctx.config.platforms['fake'].max_pages == 5
    assert ctx.config.platforms['fake'].cities == ['上海']   # 没传的保持原样


class StopAfter:
    """第 n 次调用模型时，模拟用户正好点了「停止」（结果本身照常返回）。

    ``latency`` 模拟一次真实请求的耗时：并发评分时，主线程得有时间在工作线程
    取走后面的岗位之前把它们取消掉（真实请求要 2 秒多，这里 0.3 秒已经留足余量）。
    """

    def __init__(self, ctx, n: int, latency: float = 0.0) -> None:
        import threading

        self.ctx, self.n, self.latency, self.calls = ctx, n, latency, 0
        self._lock = threading.Lock()

    def complete(self, **_kw) -> LlmResult:
        import time

        with self._lock:
            self.calls += 1
            if self.calls == self.n:
                self.ctx.stop.request()
        if self.latency:
            time.sleep(self.latency)
        return LlmResult(data={"score": 88, "reason": "匹配"}, text="{}")


def _seed_new_jobs(ctx, count: int) -> None:
    for index in range(count):
        ctx.repo.upsert_job(Job(
            id=f"fake-s{index}", platform="fake", external_id=f"s{index}",
            title=f"数据分析师 {index}", company=f"公司{index}", status="new",
        ))


def _scored_count(ctx) -> int:
    return ctx.repo.conn.execute("SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL").fetchone()[0]


def test_stop_mid_scoring_keeps_finished_scores(wired):
    """停止前已经评完（已经花过 token）的分数要落库；以前要等全部评完才统一写，停一下全丢。"""
    from jobpilot.platforms.base import StopRequested

    ctx, _ = wired
    _seed_new_jobs(ctx, 5)
    ctx.llm = StopAfter(ctx, 3)
    with pytest.raises(StopRequested):
        score_jobs(ctx, "fake", resume_text="简历")
    assert ctx.llm.calls == 3
    assert _scored_count(ctx) == 3
    assert ctx.repo.get_score_trace("fake-s0") is not None


def test_stop_mid_concurrent_scoring_keeps_finished_scores(wired):
    from jobpilot.platforms.base import StopRequested

    ctx, _ = wired
    ctx.config.scoring.concurrency = 2
    _seed_new_jobs(ctx, 6)
    ctx.llm = StopAfter(ctx, 2, latency=0.3)
    with pytest.raises(StopRequested):
        score_jobs(ctx, "fake", resume_text="简历")
    # 每一次真正发出去的模型调用都要有落库的分数，一条都不能丢
    assert _scored_count(ctx) == ctx.llm.calls
    assert ctx.llm.calls < 6                     # 还没开始的请求被取消了


def test_apply_stops_without_lock_when_logged_out(wired):
    """投递时掉登录：停下并说明，不能当成风控上锁（LoginRequired 没有 kind，以前被兜底成 blocked）。"""
    from jobpilot.config import PlatformCfg
    from jobpilot.models import LoginRequired

    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    score_jobs(ctx, "fake", resume_text="AI 产品经验 3 年")
    approve(ctx, "fake")
    ctx.config.platforms["fake"] = PlatformCfg(enabled=True, apply=True)
    driver = registry.get("fake")

    def logged_out(ctx, job):
        raise LoginRequired("需要登录")

    driver.apply = logged_out
    result = apply_jobs(ctx, "fake", registry=registry)
    assert "登录态失效" in result["stopped"]
    assert ctx.policy.locks.active("fake") is None
    assert not ctx.repo.get_kv("penalty:fake")


def test_score_without_platform_covers_every_platform(wired):
    """评分不开页面：不传平台就处理所有平台的新岗位（以前选着 BOSS 点评分，前程无忧的新岗位一直不动）。"""
    ctx, registry = wired
    collect(ctx, "fake", registry=registry)
    ctx.repo.upsert_job(Job(id="other-1", platform="other", external_id="1", title="AI 产品经理",
                            company="丙公司", city="上海", jd="负责 AI 产品"))
    assert check_mode(ctx.config, "score", None) is None
    result = run(ctx, "score", None)
    assert result["scored"] + result["prefiltered"] == 3
    assert ctx.repo.get_job("other-1").status != Status.NEW.value
