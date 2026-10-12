"""评分与采集读取工具单测。"""
import unittest

from jobpilot.platforms.base import BaseAdapter, PageReadError
from jobpilot.scoring import _clamp_int, _passed_status


class ScoringHelperTests(unittest.TestCase):
    def test_passed_status(self):
        self.assertEqual(_passed_status({"confirm": {"required": True}}), "pending_confirm")
        self.assertEqual(_passed_status({"confirm": {"required": False}}), "approved")
        self.assertEqual(_passed_status({}), "pending_confirm")  # 默认开启确认门

    def test_clamp(self):
        self.assertEqual(_clamp_int(5, 1, 3), 3)
        self.assertEqual(_clamp_int(0, 1, 3), 1)
        self.assertEqual(_clamp_int("x", 1, 3), 1)
        self.assertEqual(_clamp_int(2, 1, 3), 2)


class _FakeAdapter(BaseAdapter):
    key = "fake"
    name = "Fake"

    def collect(self, cfg, browser):
        return 0

    def apply(self, cfg, browser, job):
        return "unsupported"


class _FakeBrowser:
    def __init__(self, values):
        self.values = list(values)
        self.calls = 0

    def evaluate(self, target_id, script):
        v = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return v


class ReadListTests(unittest.TestCase):
    def test_empty_list_is_valid_end(self):
        self.assertEqual(_FakeAdapter().read_list(_FakeBrowser(["[]"]), "t", "s"), [])

    def test_none_raises_after_retry(self):
        with self.assertRaises(PageReadError):
            _FakeAdapter().read_list(_FakeBrowser([None, None]), "t", "s", wait=0)

    def test_recovers_after_retry(self):
        jobs = _FakeAdapter().read_list(_FakeBrowser([None, '[{"a": 1}]']), "t", "s", wait=0)
        self.assertEqual(jobs, [{"a": 1}])

    def test_parsed_list_passthrough(self):
        self.assertEqual(_FakeAdapter().read_list(_FakeBrowser([[{"b": 2}]]), "t", "s"), [{"b": 2}])


if __name__ == "__main__":
    unittest.main()