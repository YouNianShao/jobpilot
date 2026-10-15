"""多平台轮流投递：一个平台等间隔时去投下一个平台；每个平台自己的间隔、额度、风控一点不少。"""
from __future__ import annotations

import pytest
from conftest import open_time_gates

from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig, PlatformCfg
from jobpilot.db import Repo, connect
from jobpilot.models import ApplyResult, Job, LoginRequired, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.platforms.registry import Registry
from jobpilot.safety.policy import Policy
from jobpilot.services.applier import apply_all
from jobpilot.services.context import ServiceContext
from jobpilot.services.orchestrator import check_mode, run


class Driver:
    capabilities = frozenset({"collect", "apply"})

    def __init__(self, key, log, fail_with=None):
        self.key, self.name, self.log, self.fail_with = key, key.upper(), log, fail_with

    def apply(self, ctx, job):
        self.log.append((self.key, job.id, round(ctx_clock[0])))
        if self.fail_with:
            raise self.fail_with
        return ApplyResult(status=Status.APPLIED.value, detail="ok")


ctx_clock = [0.0]          # 假时钟：stop.sleep 只推进它，不真的等


@pytest.fixture
def rig(tmp_path):
    ctx_clock[0] = 0.0
    cfg = open_time_gates(AppConfig())
    cfg.safety.page_interval_min_seconds = cfg.safety.page_interval_max_seconds = 0
    cfg.apply.interval_min = cfg.apply.interval_max = 60
    for key in ("pa", "pb"):
        cfg.platforms[key] = PlatformCfg(enabled=True, apply=True)
    repo = Repo(connect(tmp_path / "r.db"))
    stop = StopSignal()

    def fake_sleep(seconds):
        stop.check()
        ctx_clock[0] += max(0.0, seconds)

    stop.sleep = fake_sleep
    ctx = ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg), stop=stop,
                         browser_factory=lambda: FakeBrowser([]))
    log: list = []

    def add_jobs(key, n):
        for i in range(n):
            repo.upsert_job(Job(id=f"{key}-{i}", platform=key, external_id=str(i), title=f"{key} 岗位 {i}", company=f"{key} 公司"))
            repo.set_status(f"{key}-{i}", Status.APPROVED.value)

    return ctx, log, add_jobs


def registry_of(*drivers):
    reg = Registry()
    for d in drivers:
        reg.register(d)
    return reg


def test_platforms_take_turns_and_each_keeps_its_own_interval(rig, monkeypatch):
    ctx, log, add_jobs = rig
    monkeypatch.setattr("jobpilot.services.applier.jitter", lambda x: x)
    monkeypatch.setattr("jobpilot.services.applier.human_interval", lambda lo, hi: lo)
    add_jobs("pa", 2)
    add_jobs("pb", 2)
    out = apply_all(ctx, registry=registry_of(Driver("pa", log), Driver("pb", log)), clock=lambda: ctx_clock[0])

    assert [entry[0] for entry in log] == ["pa", "pb", "pa", "pb"]          # 轮流，不是一个平台投完再下一个
    times = {k: [t for p, _, t in log if p == k] for k in ("pa", "pb")}
    assert times["pa"][1] - times["pa"][0] >= 60 and times["pb"][1] - times["pb"][0] >= 60   # 各自的间隔一秒不少
    assert ctx_clock[0] < 2 * 60 + 1                                       # 但总耗时 ≈ 一个间隔，不是三个
    assert out["applied"] == 4 and out["per_platform"]["pa"]["applied"] == 2
    assert ctx.policy.applied_today() == 4


def test_one_platform_logged_out_does_not_stop_the_others(rig):
    ctx, log, add_jobs = rig
    add_jobs("pa", 2)
    add_jobs("pb", 2)
    reg = registry_of(Driver("pa", log, fail_with=LoginRequired("掉登录")), Driver("pb", log))
    out = apply_all(ctx, registry=reg, clock=lambda: ctx_clock[0])

    assert [p for p, _, _ in log].count("pa") == 1                         # 掉登录后这个平台不再试
    assert out["per_platform"]["pb"]["applied"] == 2
    assert "登录" in out["stopped"] and out["applied"] == 2
    assert ctx.repo.get_job("pa-1").status == Status.APPROVED.value         # 没投的留着，下次还能投


def test_platform_in_post_collect_cooldown_waits_its_turn(rig):
    ctx, log, add_jobs = rig
    ctx.config.safety.delivery_cooldown_min_minutes = ctx.config.safety.delivery_cooldown_max_minutes = 2
    add_jobs("pa", 1)
    add_jobs("pb", 1)
    ctx.policy.note_collect_done("pa")                                     # pa 刚采集完：冷却约 2 分钟
    real_remaining = ctx.policy.cooldown_remaining
    start = [None]

    def remaining(platform):
        # 冷却是按真实时间记的；测试里按假时钟扣减
        left = real_remaining(platform)
        if platform == "pa" and left > 0:
            start[0] = left if start[0] is None else start[0]
            return max(0.0, start[0] - ctx_clock[0])
        return left

    ctx.policy.cooldown_remaining = remaining
    out = apply_all(ctx, registry=registry_of(Driver("pa", log), Driver("pb", log)), clock=lambda: ctx_clock[0])

    assert [p for p, _, _ in log] == ["pb", "pa"]                           # 先投没冷却的，冷却完再投 pa
    assert out["applied"] == 2 and not out.get("stopped")


def test_apply_without_platform_means_all_platforms(rig):
    ctx, log, add_jobs = rig
    add_jobs("pa", 1)
    reg = registry_of(Driver("pa", log), Driver("pb", log))
    assert check_mode(ctx.config, "apply", None, registry=reg) is None
    out = run(ctx, "apply", None, registry=reg)
    assert out["platform"] == "all" and out["applied"] == 1

    ctx.config.platforms["pa"].apply = ctx.config.platforms["pb"].apply = False
    assert "没有可以自动投递的平台" in check_mode(ctx.config, "apply", None, registry=reg)


def test_disabled_platform_with_approved_jobs_is_reported_not_silently_skipped(rig):
    ctx, log, add_jobs = rig
    add_jobs("pa", 1)
    add_jobs("pb", 1)
    ctx.config.platforms["pb"].apply = False
    out = apply_all(ctx, registry=registry_of(Driver("pa", log), Driver("pb", log)), clock=lambda: ctx_clock[0])
    assert out["applied"] == 1 and "PB" in out["note"] and "允许对该平台投递" in out["note"]


def test_rotation_summary_reads_per_platform():
    from jobpilot.readable import run_summary

    text = run_summary("apply", {"platform": "all", "applied": 3,
                                 "per_platform": {"boss": {"applied": 2}, "liepin": {"applied": 1, "stopped": "liepin 登录态失效：x"}},
                                 "note": "没投的平台 —— 智联招聘：zhaopin 未开放自动投递"})
    assert text.startswith("BOSS直聘：投出 2 个；猎聘：投出 1 个（")
    assert "登录" in text and "智联" in text
