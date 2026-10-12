"""新增风控：开页面节流、采集→投递冷却、失败熔断、冷却时长随机化。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import open_time_gates

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.safety.policy import APPLY_ONE, PAGE_OPEN, Action, Denied, Policy

_TS = "%Y-%m-%d %H:%M:%S"


def make_policy(tmp_path, **safety):
    cfg = AppConfig()
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.platforms = {}          # 关闭每平台覆盖，本文件专测全局默认
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = 0
    cfg.safety.delivery_cooldown_max_minutes = 0
    for key, value in safety.items():
        setattr(cfg.safety, key, value)
    waits: list[float] = []
    policy = Policy(Repo(connect(tmp_path / "risk.db")), cfg, sleep=waits.append)
    return policy, waits


def open_search(policy, platform="boss"):
    return policy.authorize(Action(kind=PAGE_OPEN, platform=platform, stage="collect_search"))


# ---------------- 开页面节流 ----------------
def test_first_page_open_does_not_wait(tmp_path):
    policy, waits = make_policy(tmp_path, page_interval_min_seconds=2.0,
                                page_interval_max_seconds=2.0, delay_multiplier=1.0)
    open_search(policy)
    assert waits == []


def test_second_page_open_is_paced(tmp_path):
    policy, waits = make_policy(tmp_path, page_interval_min_seconds=2.0,
                                page_interval_max_seconds=2.0, delay_multiplier=1.0)
    open_search(policy)
    permit = open_search(policy)
    assert waits and waits[0] > 0          # 第二次必须等
    assert permit.waited_seconds > 0


def test_pacing_is_per_platform(tmp_path):
    policy, waits = make_policy(tmp_path, page_interval_min_seconds=5.0,
                                page_interval_max_seconds=5.0, delay_multiplier=1.0)
    open_search(policy, "boss")
    open_search(policy, "liepin")          # 换平台不算"连续开页面"
    assert waits == []


def test_delay_multiplier_lengthens_interval(tmp_path):
    policy, waits = make_policy(tmp_path, page_interval_min_seconds=2.0,
                                page_interval_max_seconds=2.0, delay_multiplier=3.0)
    open_search(policy)
    open_search(policy)
    assert 5.9 <= waits[0] <= 6.1          # 2s * 3


# ---------------- 采集 → 投递 冷却 ----------------
def test_apply_blocked_right_after_collect(tmp_path):
    policy, _ = make_policy(tmp_path, delivery_cooldown_min_minutes=10, delivery_cooldown_max_minutes=10)
    policy.note_collect_done("boss")
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=APPLY_ONE, platform="boss"))
    assert exc.value.kind == "cooldown"


def test_apply_allowed_after_cooldown(tmp_path):
    policy, _ = make_policy(tmp_path)
    policy.note_collect_done("boss")
    past = (datetime.now() - timedelta(seconds=5)).strftime(_TS)
    policy.repo.set_kv("cooldown:boss", past)
    policy.authorize(Action(kind=APPLY_ONE, platform="boss"))   # 不抛即通过


def test_cooldown_is_per_platform(tmp_path):
    policy, _ = make_policy(tmp_path, delivery_cooldown_min_minutes=10, delivery_cooldown_max_minutes=10)
    policy.note_collect_done("boss")
    policy.authorize(Action(kind=APPLY_ONE, platform="liepin"))  # 别的平台不受影响


# ---------------- 页面失败熔断 ----------------
def test_page_failure_locks_platform_at_threshold(tmp_path):
    policy, _ = make_policy(tmp_path, max_consecutive_page_failures=3)
    assert policy.record_page_failure("boss") == 1
    assert policy.record_page_failure("boss") == 2
    assert policy.locks.active("boss") is None
    assert policy.record_page_failure("boss") == 3
    assert policy.locks.active("boss") is not None        # 第三次直接锁
    assert policy.locks.active("liepin") is None          # 不连坐


def test_page_success_resets_failure_counter(tmp_path):
    policy, _ = make_policy(tmp_path, max_consecutive_page_failures=3)
    policy.record_page_failure("boss")
    policy.record_page_success("boss")
    assert policy.page_failures("boss") == 0


# ---------------- 冷却时长随机化 ----------------
def test_risk_lock_duration_is_jittered(tmp_path):
    policy, _ = make_policy(tmp_path, risk_lock_minutes=30, risk_lock_jitter=0.35)
    until = policy.record_risk("boss", "captcha")
    remaining = (datetime.strptime(until, _TS) - datetime.now()).total_seconds() / 60
    assert 19 <= remaining <= 41          # 30 * (1 ± 0.35)


def test_risk_lock_can_disable_jitter(tmp_path):
    policy, _ = make_policy(tmp_path, risk_lock_minutes=30, risk_lock_jitter=0)
    until = policy.record_risk("boss", "captcha")
    remaining = (datetime.strptime(until, _TS) - datetime.now()).total_seconds() / 60
    assert 29 <= remaining <= 30.5


def test_page_interval_has_a_human_center(tmp_path):
    """间隔用截断正态而不是均匀随机：均匀随机的「每分钟开页数」平得像一条直线，
    本身就是特征。这里只验两件事 —— 落在区间内、且大量样本聚在中间。
    """
    from jobpilot.safety.throttle import human_interval
    values = [human_interval(10.0, 20.0) for _ in range(500)]
    assert all(10.0 <= value <= 20.0 for value in values)
    middle = [value for value in values if 13.0 <= value <= 17.0]
    assert len(middle) > len(values) * 0.6        # 均匀分布只会有 40%


def test_burst_of_page_opens_adds_extra_wait(tmp_path):
    """连开好几页要额外多等：意外聚集（重试 / 页面秒开）是平台最容易抓的特征。
    """
    policy, waits = make_policy(tmp_path, page_interval_min_seconds=1.0,
                                page_interval_max_seconds=1.0, delay_multiplier=1.0)
    for _ in range(3):
        open_search(policy)
    assert waits[0] < 1.5            # 第 2 次开页面：只有正常间隔（第 1 次不等待，所以没记）
    assert waits[1] >= 1.0 + 1.2     # 第 3 次：叠上突发惩罚（下限 1.2s）


def test_risk_on_two_platforms_stops_all_of_them(tmp_path):
    '''同一天被两个平台分别拦下，说明被盯上的是行为模式 —— 这时候换平台继续跑是最糟的选择。
    '''
    policy, _ = make_policy(tmp_path, cross_platform_risk_platforms=2)

    policy.record_risk('boss', 'captcha', '验证码')
    open_search(policy, 'liepin')          # 只中一个平台：别的平台照常

    policy.record_risk('liepin', 'rate_limit', '操作过于频繁')
    with pytest.raises(Denied) as exc:
        open_search(policy, '51job')
    assert exc.value.kind == 'locked'
    assert '整体' in exc.value.reason


def test_cross_platform_lock_can_be_disabled(tmp_path):
    '''阈值设 0 = 关掉联动：单平台的锁只锁它自己，不连坐。'''
    policy, _ = make_policy(tmp_path, cross_platform_risk_platforms=0)
    policy.record_risk('boss', 'captcha', '验证码')
    policy.record_risk('liepin', 'captcha', '验证码')
    open_search(policy, '51job')             # 不抛即通过


# ---------------- 跨实例的状态（每个请求 / 每个任务都会新建 Policy） ----------------
def test_page_failures_accumulate_across_policy_instances(tmp_path):
    """每次采集都是新任务、新 Policy：连续失败计数必须落库，否则"连续 3 次就锁"永远到不了 3。"""
    first, _ = make_policy(tmp_path, max_consecutive_page_failures=3)
    assert first.record_page_failure("boss") == 1
    second = Policy(first.repo, first.config)
    assert second.record_page_failure("boss") == 2
    third = Policy(first.repo, first.config)
    assert third.record_page_failure("boss") == 3
    assert third.locks.active("boss") is not None
    assert third.locks.active("liepin") is None


def test_page_success_resets_counter_for_later_instances(tmp_path):
    first, _ = make_policy(tmp_path, max_consecutive_page_failures=3)
    first.record_page_failure("boss")
    first.record_page_failure("boss")
    Policy(first.repo, first.config).record_page_success("boss")
    assert Policy(first.repo, first.config).page_failures("boss") == 0


def test_next_open_in_is_visible_to_a_fresh_policy(tmp_path):
    """界面倒计时来自 /api/status 新建的 Policy：它必须看得到任务里刚开过的页面。"""
    task_policy, _ = make_policy(tmp_path, page_interval_min_seconds=5.0,
                                 page_interval_max_seconds=5.0, delay_multiplier=1.0)
    open_search(task_policy)
    status_policy = Policy(task_policy.repo, task_policy.config)
    assert 4.0 < status_policy.next_open_in("boss") <= 5.0
    assert status_policy.next_open_in("liepin") == 0.0


def test_pacing_carries_over_to_the_next_task(tmp_path):
    """两个任务首尾相接：第二个任务的第一页也要等够间隔，不能因为换了实例就零间隔。"""
    first, _ = make_policy(tmp_path, page_interval_min_seconds=5.0,
                           page_interval_max_seconds=5.0, delay_multiplier=1.0)
    open_search(first)
    waits: list[float] = []
    second = Policy(first.repo, first.config, sleep=waits.append)
    open_search(second)
    assert waits and 4.0 < waits[0] <= 5.0
