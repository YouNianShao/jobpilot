"""硬规则预筛单测。"""
import unittest

from jobpilot.prefilter import prefilter_job


class PrefilterTests(unittest.TestCase):
    def test_intern_blocked(self):
        rejected, reason = prefilter_job({"title": "数据分析实习生"}, {})
        self.assertTrue(rejected)
        self.assertIn("实习", reason)

    def test_intern_allowed(self):
        rejected, _ = prefilter_job({"title": "数据分析实习生"}, {"allow_internship": True})
        self.assertFalse(rejected)

    def test_deal_breaker(self):
        rejected, _ = prefilter_job({"title": "销售代表"}, {"deal_breakers": ["销售"]})
        self.assertTrue(rejected)

    def test_salary_low(self):
        rejected, _ = prefilter_job({"title": "工程师", "salary": "8-10K"}, {"salary_min": 15})
        self.assertTrue(rejected)

    def test_salary_high(self):
        rejected, _ = prefilter_job({"title": "工程师", "salary": "40-60K"}, {"salary_max": 30})
        self.assertTrue(rejected)

    def test_salary_ok(self):
        rejected, _ = prefilter_job(
            {"title": "工程师", "salary": "15-25K"}, {"salary_min": 15, "salary_max": 30}
        )
        self.assertFalse(rejected)

    def test_salary_annual_low(self):
        rejected, _ = prefilter_job(
            {"title": "工程师", "salary": "18-24万/年"}, {"salary_min": 25}
        )
        self.assertTrue(rejected)


if __name__ == "__main__":
    unittest.main()