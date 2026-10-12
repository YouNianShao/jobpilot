"""回收站：软删/恢复/彻底删除，以及"删了之后各处的计数/去重是否正确"。"""
from __future__ import annotations

import pytest

from jobpilot.db import Repo, connect
from jobpilot.models import Job, Status


@pytest.fixture
def repo(tmp_path):
    conn = connect(tmp_path / "bin.db")
    store = Repo(conn)
    store.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                         title="数据分析师", company="甲公司"))
    store.upsert_job(Job(id="liepin-1", platform="liepin", external_id="1",
                         title="数据产品经理", company="乙公司"))
    store.add_event("boss-1", "collected", "x")
    yield store
    conn.close()


def test_soft_delete_hides_from_lists_and_counts(repo):
    assert repo.soft_delete(["boss-1"]) == 1
    assert [j.id for j in repo.list_jobs()] == ["liepin-1"]
    assert repo.count_status(Status.NEW.value, "boss") == 0
    assert [j.id for j in repo.list_deleted()] == ["boss-1"]
    assert repo.get_job("boss-1").deleted_at          # 详情仍可读（回收站里要看）


def test_restore(repo):
    repo.soft_delete(["boss-1"])
    assert repo.restore(["boss-1"]) == 1
    assert "boss-1" in [j.id for j in repo.list_jobs()]
    assert repo.list_deleted() == []
    assert repo.get_job("boss-1").deleted_at == ""


def test_purge_removes_everything(repo):
    repo.soft_delete(["boss-1"])
    assert repo.purge(["boss-1"]) == 1
    assert repo.get_job("boss-1") is None
    assert repo.conn.execute("SELECT COUNT(*) FROM events WHERE job_id='boss-1'").fetchone()[0] == 0


def test_restore_only_affects_deleted(repo):
    # 没被删的岗位调用 restore 不应有副作用
    assert repo.restore(["liepin-1"]) == 0
    assert "liepin-1" in [j.id for j in repo.list_jobs()]


def test_purge_only_affects_deleted(repo):
    assert repo.purge(["liepin-1"]) == 0      # 没进回收站就不能彻底删
    assert repo.get_job("liepin-1") is not None


def test_deleted_job_stays_deleted_on_recollect(repo):
    """回收站里的岗位被重新采到时，保持删除状态——尊重用户的删除决定，也不产生重复行。"""
    repo.soft_delete(["boss-1"])
    inserted, _ = repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                                      title="数据分析师", company="甲公司"))
    assert inserted is False
    assert [j.id for j in repo.list_deleted()] == ["boss-1"]
    assert len(repo.list_jobs()) == 1                 # 线上只剩 liepin-1
    assert repo.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2   # 没有新增重复行
