"""监测页两张表（待跟进 / 进展记录）和岗位池「每页 N 条」的服务端分页。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from jobpilot.db import Repo, connect
from jobpilot.models import Job, Status

_TS = "%Y-%m-%d %H:%M:%S"


@pytest.fixture
def http(repo):
    """45 个已投递岗位（前 5 个已到期）+ 45 条进展记录，正好跨三页（每页 20）。"""
    from jobpilot.bootstrap import build_context
    from jobpilot.config import AppConfig
    from jobpilot.web.app import create_app

    now = datetime.now()
    for i in range(45):
        job_id = f"boss-{i:02d}"
        repo.upsert_job(Job(id=job_id, platform="boss", external_id=job_id, title=f"岗位 {i}", company=f"公司{i}"))
        repo.set_status(job_id, Status.APPLIED.value)
        # 前 5 个投了 100 小时（过了默认 48 小时，到期），其余刚投 1 小时
        hours = 100 if i < 5 else 1
        repo.conn.execute("INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
                          (job_id, "applied", "applied", (now - timedelta(hours=hours)).strftime(_TS)))
        # 到期的 5 个只记「已查看」（记了「已回复」会退出跟进队列）
        state = "replied" if i >= 5 and i % 3 == 0 else "viewed"
        repo.upsert_interaction(f"k{i:02d}", "boss", job_id, "HR", state, "", "")
    repo.conn.commit()
    db_path = repo.conn.execute("PRAGMA database_list").fetchone()["file"]
    return TestClient(create_app(lambda: build_context(AppConfig(), repo=Repo(connect(db_path)))),
                      base_url="http://127.0.0.1:8700")


def test_follow_ups_paged(http):
    body = http.get("/api/follow-ups?page=1&limit=20").json()
    assert body["total"] == 45 and body["pages"] == 3 and len(body["rows"]) == 20
    # 计数按全量：待跟进 5 个（都排在第一页），最早到期时间也给出来
    assert body["due_count"] == 5 and body["next_wait_at"]
    assert [row["due"] for row in body["rows"][:6]] == [True] * 5 + [False]
    last = http.get("/api/follow-ups?page=3&limit=20").json()
    assert len(last["rows"]) == 5 and last["due_count"] == 5
    assert http.get("/api/follow-ups?page=99&limit=20").json()["page"] == 3   # 越界页退回最后一页
    ids = {row["id"] for p in (1, 2, 3) for row in http.get(f"/api/follow-ups?page={p}&limit=20").json()["rows"]}
    assert len(ids) == 45                                                       # 翻页不重不漏


def test_follow_ups_legacy_shape(http):
    # 不带 page：老格式，全部行
    body = http.get("/api/follow-ups").json()
    assert len(body["rows"]) == 45 and "page" not in body


def test_interactions_paged(http):
    body = http.get("/api/interactions?page=1&limit=20").json()
    assert body["total"] == 45 and body["pages"] == 3 and len(body["items"]) == 20
    assert body["counts"] == {"replied": 13, "viewed": 32}                     # 全量计数，不是这一页的
    keys = {row["key"] for p in (1, 2, 3) for row in http.get(f"/api/interactions?page={p}&limit=20").json()["items"]}
    assert len(keys) == 45
    assert http.get("/api/interactions?page=9&limit=50").json()["page"] == 1
    assert isinstance(http.get("/api/interactions").json(), list)              # 老格式不变


@pytest.mark.parametrize("size", [20, 50, 100])
def test_jobs_page_sizes(http, size):
    body = http.get(f"/api/jobs?page=1&limit={size}").json()
    assert body["limit"] == size and len(body["items"]) == min(size, 45)
