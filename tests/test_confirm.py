"""人工确认门单测。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobpilot import confirm, db


def _job(job_id, platform="51job", title="数据分析师", company="示例公司"):
    return {
        "id": job_id, "platform": platform, "title": title, "company": company,
        "salary": "20-30K", "city": "上海", "experience": "", "jd": "",
        "hr_name": "", "url": "", "search_url": "",
    }


class ConfirmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH", Path(self.tmp.name) / "jp.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_approve_all_pending(self):
        conn = db.get_conn()
        for i in range(3):
            rec = _job(f"51job-{i}")
            db.insert_job(conn, rec)
            db.set_status(conn, rec["id"], "pending_confirm")
        conn.close()

        res = confirm.approve({}, "51job")
        self.assertEqual(res["confirmed"], 3)

        conn = db.get_conn()
        self.assertEqual(db.count_status(conn, "approved", "51job"), 3)
        self.assertEqual(db.count_status(conn, "pending_confirm", "51job"), 0)
        conn.close()

    def test_reject_single(self):
        conn = db.get_conn()
        db.insert_job(conn, _job("51job-x"))
        db.set_status(conn, "51job-x", "pending_confirm")
        conn.close()

        res = confirm.reject({}, "51job", job_ids=["51job-x"], all_pending=False)
        self.assertEqual(res["rejected"], 1)

        conn = db.get_conn()
        self.assertEqual(db.count_status(conn, "rejected", "51job"), 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()