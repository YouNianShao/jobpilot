"""平台能力边界单测。"""
import unittest

from jobpilot.platforms import get_adapter


class CapabilityTests(unittest.TestCase):
    def test_platform_capabilities(self):
        self.assertEqual(get_adapter("51job").capabilities, frozenset({"collect", "apply", "monitor"}))
        self.assertEqual(get_adapter("liepin").capabilities, frozenset({"collect", "apply"}))
        self.assertEqual(get_adapter("zhaopin").capabilities, frozenset({"collect"}))

    def test_supports_and_collect_only(self):
        zp = get_adapter("zhaopin")
        self.assertTrue(zp.supports("collect"))
        self.assertFalse(zp.supports("apply"))
        self.assertFalse(zp.supports("monitor"))
        self.assertTrue(zp.collect_only)
        self.assertFalse(get_adapter("liepin").collect_only)
        self.assertTrue(get_adapter("51job").supports("monitor"))

    def test_unknown_platform(self):
        self.assertIsNone(get_adapter("nope"))


if __name__ == "__main__":
    unittest.main()