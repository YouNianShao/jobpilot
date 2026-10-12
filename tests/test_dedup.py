"""跨平台去重单测。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobpilot import db


def _job(job_id, platform, title, company):
    return {
        "id": job_id, "platform": platform, "title": title, "company": company,
        "salary": "20-30K", "city": "上海", "experience": "", "jd": "",
        "hr_name": "", "url": "", "search_url": "",
    }


class DedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH", Path(self.tmp.name) / "jp.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cross_platform_duplicate_skipped(self):
        conn = db.get_conn()
        self.assertTrue(db.insert_job(conn, _job("51job-1", "51job", "AI 产品经理", "某某科技")))
        # 归一化后同名同公司（去空格 + 标点），应判为重复
        self.assertFalse(db.insert_job(conn, _job("liepin-9", "liepin", "AI产品经理", "某某科技")))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM history WHERE action='cross_platform_dup'").fetchone()[0],
            1,
        )
        conn.close()

    def test_same_platform_same_id_skipped(self):
        conn = db.get_conn()
        rec = _job("51job-2", "51job", "后端工程师", "甲公司")
        self.assertTrue(db.insert_job(conn, rec))
        self.assertFalse(db.insert_job(conn, dict(rec)))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
        conn.close()

    def test_different_company_kept(self):
        conn = db.get_conn()
        self.assertTrue(db.insert_job(conn, _job("51job-3", "51job", "后端工程师", "甲公司")))
        self.assertTrue(db.insert_job(conn, _job("liepin-3", "liepin", "后端工程师", "乙公司")))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)
        conn.close()


if __name__ == "__main__":
    unittest.main()