"""多平台全流程：勾选的平台依次采集 → 评分 / 招呼语 → 有待确认就停；投递只投勾选的平台。"""
from __future__ import annotations

import pytest
from conftest import open_time_gates
from test_chain import ScriptedLlm

from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig, PlatformCfg
from jobpilot.db import Repo, connect
from jobpilot.models import ApplyResult, CollectResult, Job, LoginRequired, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.platforms.registry import Registry
from jobpilot.readable import run_summary
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.orchestrator import check_mode, run


class Driver:
    capabilities = frozenset({"collect", "apply"})

    def __init__(self, key, calls, *, login_fails=False, can_apply=True):
        self.key, self.name, self.calls, self.login_fails = key, key.upper(), calls, login_fails
        if not can_apply:
            self.capabilities = frozenset({"collect"})

    def collect(self, ctx) -> CollectResult:
        cfg = ctx.config.platforms[self.key]
        self.calls.append(("collect", self.key, list(cfg.keywords), list(cfg.cities), cfg.max_pages))
        if self.login_fails:
            raise LoginRequired("没登录")
        ctx.repo.upsert_job(Job(id=f"{self.key}-1", platform=self.key, external_id="1",
                                title=f"{self.key} 产品经理", company=f"{self.key} 公司", jd="AI 产品"))
        return CollectResult(platform=self.key, new=1, seen=1)

    def apply(self, ctx, job):
        self.calls.append(("apply", self.key, job.id))
        return ApplyResult(status=Status.APPLIED.value, detail="ok")


@pytest.fixture
def rig(tmp_path):
    cfg = open_time_gates(AppConfig())
    resume = tmp_path / "r.md"
    resume.write_text("# 简历\n做过 AI 产品。\n", encoding="utf-8")
    cfg.profile.resume_path = str(resume)
    cfg.safety.page_interval_min_seconds = cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = cfg.safety.delivery_cooldown_max_minutes = 0
    cfg.apply.interval_min = cfg.apply.interval_max = 1
    for key in ("pa", "pb", "pc"):
        cfg.platforms[key] = PlatformCfg(enabled=True, apply=True, keywords=["默认词"], cities=["上海"])
    repo = Repo(connect(tmp_path / "f.db"))
    stop = StopSignal()
    stop.sleep = lambda seconds: stop.check()
    ctx = ServiceContext(config=cfg, repo=repo, llm=ScriptedLlm(), policy=Policy(repo, cfg), stop=stop,
                         browser_factory=lambda: FakeBrowser([]))
    return ctx, cfg


def make_registry(calls, **flags):
    reg = Registry()
    for key in ("pa", "pb", "pc"):
        reg.register(Driver(key, calls, **flags.get(key, {})))
    return reg


def test_chosen_platforms_collect_in_order_with_their_own_settings_then_stop_for_confirmation(rig, monkeypatch):
    ctx, cfg = rig
    import jobpilot.config as cfgmod

    saved = []
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: saved.append(a))
    calls: list = []
    plans = [{"platform": "pb", "keywords": ["数据分析"], "cities": ["杭州"], "max_pages": 2},
             {"platform": "pa", "keywords": None, "cities": None, "max_pages": None}]
    out = run(ctx, "full", None, registry=make_registry(calls), plans=plans)

    assert [c[1] for c in calls if c[0] == "collect"] == ["pb", "pa"]          # 按勾选顺序，pc 没勾就不碰
    assert calls[0][2:] == (["数据分析"], ["杭州"], 2)                          # 这一次的搜索条件
    assert calls[1][2:] == (["默认词"], ["上海"], 3)                            # 没改的用配置里的
    assert saved == []                                                          # 这一次的条件不写回 config.yaml
    assert out["steps"]["collect"]["new"] == 2 and out["awaiting_confirmation"] == 2
    assert not [c for c in calls if c[0] == "apply"]                            # 有待确认：停下等人
    text = run_summary("full", out)
    assert "PB" not in text and "采集：" in text and "2 个等你确认" in text


def test_one_platform_logged_out_does_not_stop_the_next_one(rig):
    ctx, _ = rig
    calls: list = []
    reg = make_registry(calls, pa={"login_fails": True})
    out = run(ctx, "full", None, registry=reg, plans=[{"platform": "pa"}, {"platform": "pb"}])
    per = out["steps"]["collect"]["per_platform"]
    assert per["pa"].get("error") and per["pb"]["new"] == 1
    assert "error" not in out["steps"]["collect"]                               # 只有全失败才算整步失败
    assert "登录" in run_summary("full", out)


def test_without_pending_it_applies_only_on_the_chosen_platforms(rig):
    ctx, cfg = rig
    cfg.confirm.required = False
    calls: list = []
    for key in ("pa", "pc"):                                                    # 两个平台都有已确认的岗位
        ctx.repo.upsert_job(Job(id=f"{key}-9", platform=key, external_id="9", title=f"{key} 旧岗", company=f"{key} 旧公司"))
        ctx.repo.set_status(f"{key}-9", Status.APPROVED.value)
    ctx.llm = ScriptedLlm(score=10)                                              # 新岗位都不过线：不产生待确认
    out = run(ctx, "full", None, registry=make_registry(calls), plans=[{"platform": "pa"}])
    applied = [c[1] for c in calls if c[0] == "apply"]
    assert applied == ["pa"]                                                    # pc 没勾：不投
    assert out["steps"]["apply"]["applied"] == 1


def test_full_reports_each_stage_in_order(rig):
    """任务监控台的步骤条：全流程每走到一步就报一次（采集 → 评分 → 招呼语 → 投递）"""
    ctx, cfg = rig
    cfg.confirm.required = False
    seen: list = []
    ctx.stage = seen.append
    ctx.llm = ScriptedLlm(score=10)
    run(ctx, "full", None, registry=make_registry([]), plans=[{"platform": "pa"}])
    assert seen == ["collect", "score", "greet", "apply"]


def test_apply_after_off_stops_after_scoring(rig):
    ctx, cfg = rig
    cfg.confirm.required = False
    calls: list = []
    ctx.repo.upsert_job(Job(id="pa-9", platform="pa", external_id="9", title="旧岗", company="旧公司"))
    ctx.repo.set_status("pa-9", Status.APPROVED.value)
    out = run(ctx, "full", None, registry=make_registry(calls), plans=[{"platform": "pa"}], apply_after=False)
    assert "apply" not in out["steps"] and not [c for c in calls if c[0] == "apply"]


def test_check_mode_for_plans(rig):
    _, cfg = rig
    reg = make_registry([])
    assert check_mode(cfg, "full", None, registry=reg, plans=[]) == "没有勾选任何平台"
    assert check_mode(cfg, "full", None, registry=reg, plans=[{"platform": "pa"}]) is None
    cfg.platforms["pb"].enabled = False
    assert "停用" in check_mode(cfg, "full", None, registry=reg, plans=[{"platform": "pa"}, {"platform": "pb"}])


def test_api_rejects_the_same_platform_twice(tmp_path):
    from fastapi.testclient import TestClient

    from jobpilot.web.app import create_app

    class Idle:
        running = False

        def start(self, *a, **k):
            raise AssertionError("不该启动")

    client = TestClient(create_app(lambda: None, runner=Idle()), base_url="http://127.0.0.1:8700")
    res = client.post("/api/tasks", json={"mode": "full", "plans": [{"platform": "boss"}, {"platform": "boss"}]})
    assert res.status_code == 400 and "两次" in res.json()["error"]


def test_task_log_times_stay_aligned_with_logs():
    """日志时间线：每行日志都带时间；有地方直接 append（没记时间）时补空，不会错位"""
    from jobpilot.tasks import Task
    task = Task(id="t1", mode="full", platform="pa")
    task.log("一")
    task.logs.append("二（没走 log）")
    task.log("三")
    snap = task.snapshot()
    assert len(snap["log_times"]) == len(snap["logs"]) == 3
    assert snap["log_times"][1] == "" and len(snap["log_times"][2]) == 8
    assert snap["stage"] == ""
