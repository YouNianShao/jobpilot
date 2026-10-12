"""安全内核（Policy）测试：闸门顺序、平台隔离、退避。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from conftest import open_time_gates

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.safety.policy import APPLY_ONE, PAGE_OPEN, Action, Denied, Policy


@pytest.fixture
def policy(tmp_path):
    conn = connect(tmp_path / "p.db")
    cfg = AppConfig()
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.platforms = {}          # 关闭每平台覆盖，本文件专测全局默认
    cfg.safety.page_interval_min_seconds = 0   # 测试里不等待
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = 0
    cfg.safety.delivery_cooldown_max_minutes = 0
    return Policy(Repo(conn), cfg)


def test_page_open_consumes_budget(policy):
    permit = policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert permit.remaining["collect_search"] == 59
    assert policy.budget.used("boss", "collect_search") == 1


def test_page_budget_denies_over_limit(policy):
    policy.config.safety.daily_pages.collect_search = 1
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert exc.value.kind == "budget"


def test_budget_is_per_platform(policy):
    policy.config.safety.daily_pages.collect_search = 1
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    # 别的平台不受影响
    policy.authorize(Action(kind=PAGE_OPEN, platform="liepin", stage="collect_search"))


def test_lock_blocks_only_that_platform(policy):
    policy.record_risk("boss", "captcha", "命中验证码")
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert exc.value.kind == "locked"
    policy.authorize(Action(kind=PAGE_OPEN, platform="liepin", stage="collect_search"))


def test_apply_denied_outside_window(policy):
    now = datetime.now()
    start = (now.hour + 1) % 24
    policy.config.safety.send_windows = [f"{start:02d}:00-{(start + 1) % 24:02d}:00"]
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=APPLY_ONE, platform="boss"))
    assert exc.value.kind == "outside_window"


def test_apply_denied_on_day_off(policy):
    policy.config.safety.day_off_probability = 1.0
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=APPLY_ONE, platform="boss"))
    assert exc.value.kind == "day_off"


def test_apply_denied_on_daily_limit(policy):
    policy.config.apply.daily_limit = 1
    policy.repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1", title="t", company="c"))
    policy.repo.add_event("boss-1", "applied", "applied")
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=APPLY_ONE, platform="boss"))
    assert exc.value.kind == "daily_limit"


def test_apply_allowed_reports_remaining(policy):
    policy.config.apply.daily_limit = 5
    permit = policy.authorize(Action(kind=APPLY_ONE, platform="boss"))
    assert permit.remaining["daily_limit"] == 5


def test_backoff_progression_and_reset(policy):
    action = Action(kind=APPLY_ONE, platform="boss")
    assert policy.record_outcome(action, False) == 60.0
    assert policy.record_outcome(action, False) == 120.0
    assert policy.record_outcome(action, False) == 300.0
    assert policy.should_stop("boss") is True
    policy.record_outcome(action, True)
    assert policy.should_stop("boss") is False

def test_rate_window_denies_when_wait_too_long(policy):
    """短时间猛刷页面：等太久就直接停手（kind=rate）。"""
    policy.config.safety.max_pages_per_10min = 2
    policy.config.safety.max_pages_per_hour = 0        # 只留 10 分钟窗
    policy.config.safety.rate_wait_max_seconds = 0     # 不允许等待
    for _ in range(2):
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert exc.value.kind == "rate"
    # 被速率挡下时不能白白吃掉一个页面额度（速率闸门在预算之前）
    assert policy.budget.used("boss", "collect_search") == 2


def test_rate_window_waits_instead_of_giving_up(policy):
    """马上就能滑出窗口时，等一会儿继续跑，而不是把任务打断。"""
    policy.config.safety.max_pages_per_10min = 2
    policy.config.safety.max_pages_per_hour = 0
    policy.config.safety.rate_wait_max_seconds = 3600
    old = (datetime.now() - timedelta(seconds=595)).strftime("%Y-%m-%d %H:%M:%S")
    for _ in range(2):
        policy.repo.conn.execute(
            "INSERT INTO page_access (platform, stage, day, created_at) VALUES (?,?,?,?)",
            ("boss", "collect_search", old[:10], old),
        )
    policy.repo.conn.commit()

    slept: list[float] = []
    policy._sleep = slept.append
    permit = policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    assert slept and 0 < slept[0] <= 20      # 只需等它滑出 10 分钟窗
    assert permit.waited_seconds > 0


def test_rate_window_is_per_platform(policy):
    policy.config.safety.max_pages_per_10min = 1
    policy.config.safety.max_pages_per_hour = 0
    policy.config.safety.rate_wait_max_seconds = 0
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    with pytest.raises(Denied):
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    policy.authorize(Action(kind=PAGE_OPEN, platform="liepin", stage="collect_search"))


def test_limits_for_resolves_per_platform_override(policy):
    policy.config.safety.daily_pages.collect_search = 60
    policy.config.safety.max_pages_per_hour = 60
    limits = policy.limits_for("boss")          # fixture 里 platforms={}，用全局
    assert limits["collect_search"] == 60 and limits["per_hour"] == 60
    assert policy.rate_state("boss")["per_hour"]["limit"] == 60

def test_risk_hit_is_recorded_as_event(policy):
    """锁表只有"当前锁"，所以风控命中的历史必须落到 events 里。"""
    policy.record_risk("boss", "captcha", "命中验证码")
    events = policy.repo.list_recent_events(5, job_id_null_only=True)
    assert [e["kind"] for e in events] == ["risk_lock"]
    assert "boss" in events[0]["detail"] and "captcha" in events[0]["detail"]


def test_rate_stop_is_recorded_as_event(policy):
    policy.config.safety.max_pages_per_10min = 1
    policy.config.safety.max_pages_per_hour = 0
    policy.config.safety.rate_wait_max_seconds = 0
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    with pytest.raises(Denied):
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    kinds = [e["kind"] for e in policy.repo.list_recent_events(5, job_id_null_only=True)]
    assert "rate_stop" in kinds

def test_risk_hit_slows_the_platform_down(policy):
    """被平台警告过就别马上按原速继续：冷却结束后还有一段降速期。"""
    assert policy.penalty_factor("boss") == 1.0
    assert policy.next_open_in("boss") == 0.0

    policy.record_risk("boss", "captcha", "命中验证码")

    assert policy.penalty_factor("boss") == policy.config.safety.risk_penalty_factor
    state = policy.penalty_state("boss")
    assert state and state["factor"] > 1 and state["remaining_minutes"] > 60
    # 别的平台不受影响
    assert policy.penalty_factor("liepin") == 1.0


def test_penalty_can_be_turned_off(policy):
    policy.config.safety.risk_penalty_factor = 1.0
    policy.record_risk("boss", "rate_limit", "太频繁")
    assert policy.penalty_factor("boss") == 1.0
    assert policy.penalty_state("boss") is None


def test_penalty_expires(policy):
    policy.record_risk("boss", "captcha", "命中验证码")
    # 把惩罚期改到过去
    past = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    policy.repo.set_kv("penalty:boss", past)
    assert policy.penalty_factor("boss") == 1.0
    assert policy.penalty_state("boss") is None

def test_penalty_really_doubles_the_page_interval(tmp_path):
    """不是只改个数字：间隔要真的变长（这条才是"降速"的证据）。"""
    import time as _time

    conn = connect(tmp_path / "penalty.db")
    cfg = AppConfig()
    cfg.safety.platforms = {}
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.page_interval_min_seconds = 10
    cfg.safety.page_interval_max_seconds = 10     # 固定 10 秒，方便断言
    cfg.safety.delay_multiplier = 1.0
    slept: list[float] = []
    policy = Policy(Repo(conn), cfg, sleep=slept.append)

    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    slept.clear()
    policy._last_open["boss"] = _time.time()      # 假装"刚开过一页"
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    normal = slept[-1]
    assert 9.5 <= normal <= 10.5

    policy.record_risk("boss", "captcha", "命中验证码")
    policy.clear_risk("boss")                     # 冷却锁去掉，但降速期还在
    slept.clear()
    policy._last_open["boss"] = _time.time()
    policy._recent_open.clear()   # 这条测的是降速倍率，清掉突发惩罚的干扰
    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))
    slowed = slept[-1]
    assert 19.5 <= slowed <= 20.5                 # ×2 真的生效

def test_page_open_respects_active_windows(policy):
    """开页面也受时段约束：半夜刷招聘同样是机器特征。"""
    other = (datetime.now().hour + 1) % 24
    policy.config.safety.active_windows = [f"{other:02d}:00-{(other + 1) % 24:02d}:00"]

    with pytest.raises(Denied) as exc:
        policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))

    assert exc.value.kind == "outside_active_window"
    assert "active_windows" in exc.value.reason          # 告诉用户去哪儿改
    assert policy.budget.used("boss", "collect_search") == 0   # 被挡下时不消耗额度


def test_active_window_allows_page_open_inside(policy):
    hour = datetime.now().hour
    policy.config.safety.active_windows = [f"{hour:02d}:00-{hour:02d}:59"]

    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))

    assert policy.budget.used("boss", "collect_search") == 1


def test_empty_active_windows_means_no_limit(policy):
    policy.config.safety.active_windows = []

    policy.authorize(Action(kind=PAGE_OPEN, platform="boss", stage="collect_search"))

    assert policy.budget.used("boss", "collect_search") == 1
