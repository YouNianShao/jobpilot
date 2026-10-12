"""safety 模块单测：风险识别、时间窗、退避、冷却锁。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from jobpilot import safety


class RiskClassifyTests(unittest.TestCase):
    def test_captcha_text(self):
        self.assertEqual(safety.classify_risk_text("请稍后完成验证后继续访问"), "captcha")

    def test_rate_limit_text(self):
        self.assertEqual(safety.classify_risk_text("操作过于频繁，请稍后再试"), "rate_limit")

    def test_blocked_text(self):
        self.assertEqual(safety.classify_risk_text("您的账号存在异常"), "blocked")

    def test_clean_text(self):
        self.assertIsNone(safety.classify_risk_text("高级数据分析师 20-30K 上海"))

    def test_url_login(self):
        self.assertEqual(safety.classify_risk_url("https://passport.51job.com/login"), "login_required")

    def test_url_captcha(self):
        self.assertEqual(safety.classify_risk_url("https://x.com/verify?t=1"), "captcha")

    def test_url_precedence(self):
        self.assertEqual(safety.classify_risk("正文没有信号", "https://x.com/sec_verify"), "captcha")


class WindowTests(unittest.TestCase):
    def test_parse_single(self):
        self.assertEqual(safety.parse_windows("09:00-18:00"), [(540, 1080)])

    def test_parse_multi(self):
        self.assertEqual(
            safety.parse_windows(["09:00-12:00", "14:00-18:00"]),
            [(540, 720), (840, 1080)],
        )

    def test_empty_unrestricted(self):
        self.assertTrue(safety.in_send_window([]))
        self.assertTrue(safety.in_send_window(None))

    def test_boundaries(self):
        spec = "09:00-18:00"
        self.assertFalse(safety.in_send_window(spec, datetime(2026, 1, 1, 8, 59)))
        self.assertTrue(safety.in_send_window(spec, datetime(2026, 1, 1, 9, 0)))
        self.assertTrue(safety.in_send_window(spec, datetime(2026, 1, 1, 17, 59)))
        self.assertFalse(safety.in_send_window(spec, datetime(2026, 1, 1, 18, 0)))


class BackoffTests(unittest.TestCase):
    def test_progression_and_reset(self):
        b = safety.ProgressiveBackoff()
        self.assertEqual(b.record_error(), 60.0)
        self.assertEqual(b.record_error(), 120.0)
        self.assertEqual(b.record_error(), 300.0)
        self.assertTrue(b.should_stop)
        b.record_success()
        self.assertEqual(b.errors, 0)
        self.assertFalse(b.should_stop)


class DayOffTests(unittest.TestCase):
    def test_zero_never(self):
        self.assertFalse(safety.should_take_day_off(0))

    def test_one_always(self):
        self.assertTrue(safety.should_take_day_off(1))

    def test_jitter_bounds(self):
        for _ in range(50):
            v = safety.jitter(100.0, 0.25)
            self.assertGreaterEqual(v, 75.0)
            self.assertLessEqual(v, 125.0)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(safety, "LOCK_FILE", Path(self.tmp.name) / "lock.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_set_and_get(self):
        safety.set_lock("测试锁定", 10, kind="captcha")
        lock = safety.get_lock()
        self.assertIsNotNone(lock)
        self.assertEqual(lock["kind"], "captcha")
        self.assertGreaterEqual(lock["remaining_minutes"], 1)
        self.assertTrue(safety.is_locked())

    def test_expired(self):
        safety.set_lock("已过期", -1)
        self.assertIsNone(safety.get_lock())
        self.assertFalse(safety.is_locked())

    def test_clear(self):
        safety.set_lock("x", 5)
        safety.clear_lock()
        self.assertFalse(safety.is_locked())


if __name__ == "__main__":
    unittest.main()