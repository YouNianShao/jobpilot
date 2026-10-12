"""风控：页面预算、冷却锁、节流。"""
from __future__ import annotations

from datetime import datetime

import pytest

from jobpilot.config import DailyPages
from jobpilot.models import BudgetExceeded
from jobpilot.safety.budget import Budget
from jobpilot.safety.locks import RiskLock
from jobpilot.safety.throttle import Backoff, in_send_window, jitter, parse_windows, should_take_day_off


# ---------------- 页面预算 ----------------
def test_budget_blocks_over_limit(repo):
    budget = Budget(repo, DailyPages(collect_search=2, collect_detail=5, apply=5, monitor=5, total=100))
    budget.reserve("51job", "collect_search")
    budget.reserve("51job", "collect_search")
    with pytest.raises(BudgetExceeded):
        budget.reserve("51job", "collect_search")


def test_budget_total_caps_all_stages(repo):
    budget = Budget(repo, DailyPages(collect_search=10, collect_detail=10, apply=10, monitor=10, total=2))
    budget.reserve("51job", "collect_search")
    budget.reserve("51job", "apply")
    with pytest.raises(BudgetExceeded):
        budget.reserve("51job", "monitor")


def test_budget_is_per_platform(repo):
    budget = Budget(repo, DailyPages(collect_search=1, collect_detail=10, apply=10, monitor=10, total=100))
    budget.reserve("51job", "collect_search")
    with pytest.raises(BudgetExceeded):
        budget.reserve("51job", "collect_search")
    budget.reserve("liepin", "collect_search")   # 别的平台不受影响
    assert budget.used("liepin", "collect_search") == 1


def test_budget_zero_means_unlimited(repo):
    budget = Budget(repo, DailyPages(collect_search=0, collect_detail=0, apply=0, monitor=0, total=0))
    for _ in range(5):
        budget.reserve("51job", "collect_search")
    assert budget.used("51job", "collect_search") == 5


# ---------------- 冷却锁 ----------------
def test_lock_active_and_isolated(repo):
    lock = RiskLock(repo, default_minutes=30)
    lock.lock("51job", "命中验证码", "captcha")
    assert lock.active("51job")["kind"] == "captcha"
    assert lock.active("liepin") is None


def test_lock_expired_is_cleared(repo):
    lock = RiskLock(repo, default_minutes=30)
    lock.lock("51job", "过期锁", "rate_limit", minutes=0)
    assert lock.active("51job") is None


# ---------------- 节流 ----------------
def test_parse_windows_and_boundaries():
    assert parse_windows("09:00-18:00") == [(540, 1080)]
    spec = "09:00-18:00"
    assert in_send_window(spec, datetime(2026, 1, 1, 8, 59)) is False
    assert in_send_window(spec, datetime(2026, 1, 1, 9, 0)) is True
    assert in_send_window(spec, datetime(2026, 1, 1, 18, 0)) is False
    assert in_send_window([]) is True


def test_backoff_progression():
    backoff = Backoff()
    assert backoff.record_error() == 60.0
    assert backoff.record_error() == 120.0
    assert backoff.record_error() == 300.0
    assert backoff.should_stop is True
    backoff.record_success()
    assert backoff.errors == 0


def test_day_off_probability():
    assert should_take_day_off(0) is False
    assert should_take_day_off(1) is True


def test_jitter_bounds():
    for _ in range(50):
        value = jitter(100.0, 0.25)
        assert 75.0 <= value <= 125.0


def test_risk_signals_cover_platforms_without_false_positives():
    '''词表要盖住平台改文案后的说法；但泛化短语不能单独触发 ——
    否则一句 JD（「如未接通请稍后再试」）就能把整个平台锁掉。
    '''
    from jobpilot.safety.risk import classify_risk

    # 四个平台各自会出现的说法（改文案之后仍然要认得出）
    assert classify_risk('您的访问速度过快，请稍后再试') == 'rate_limit'
    assert classify_risk('系统检测到您的操作存在异常') == 'rate_limit'
    assert classify_risk('当前访问异常') == 'rate_limit'
    assert classify_risk('请完成安全验证后继续') == 'captcha'
    assert classify_risk('滑动验证') == 'captcha'
    assert classify_risk('您的账号存在风险') == 'blocked'
    assert classify_risk('账号被冻结') == 'blocked'
    assert classify_risk('', 'https://www.zhipin.com/web/user/') == 'login_required'

    # 泛化短语：没有风控语境就不算（普通岗位描述里真的会出现）
    assert classify_risk('如未接通请稍后再试') is None
    assert classify_risk('AI 产品经理 20-25K 上海') is None
