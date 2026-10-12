"""岗位池：服务端分页 / 筛选 / 搜索 / 计数，跨平台去重走索引，迁移 v9 回填旧数据。"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from jobpilot.db import _MIGRATIONS, SCHEMA_VERSION, Repo, connect
from jobpilot.models import Job


def make(job_id: str, *, platform="boss", title="数据分析师", company="某公司", status="new",
         score=None, created="2026-10-01 10:00:00", jd="") -> Job:
    return Job(id=job_id, platform=platform, external_id=job_id, title=title, company=company,
               status=status, score=score, created_at=created, jd=jd)


@pytest.fixture
def pool(repo):
    """600 个岗位：前 520 个打过分，最新的 80 个还没评分 —— 正好是以前「看不见」的那批。"""
    for i in range(600):
        scored = i < 520
        repo.upsert_job(make(
            f"boss-{i:03d}", title=f"岗位 {i:03d}", company=f"公司{i % 7}",
            status="filtered" if scored else "new", score=(30 + i % 60) if scored else None,
            created=f"2026-10-{1 + i // 100:02d} {10 + (i % 100) // 10:02d}:{(i % 10) * 6:02d}:00",
        ))
    return repo


# ---------------- 分页 / 计数 ----------------
def test_newest_unscored_jobs_are_on_the_first_page(pool):
    items, total, counts = pool.query_jobs(limit=50)
    assert total == 600 and len(items) == 50
    assert items[0].id == "boss-599"                       # 默认按采集时间倒序：刚采的在最上面
    assert counts == {"filtered": 520, "new": 80}


def test_status_filter_counts_whole_pool(pool):
    items, total, counts = pool.query_jobs(status="new", limit=200)
    assert total == 80 and len(items) == 80               # 以前只在前 400 条里筛：这里是 0
    assert {job.status for job in items} == {"new"}
    assert counts["filtered"] == 520                      # 计数不受状态筛选影响


def test_pages_do_not_overlap_and_cover_everything(pool):
    seen = []
    for page in range(12):
        items, _, _ = pool.query_jobs(limit=50, offset=page * 50)
        seen += [job.id for job in items]
    assert len(seen) == 600 and len(set(seen)) == 600


def test_search_title_and_company_and_escape_wildcards(pool):
    _, total, counts = pool.query_jobs(q="公司3")
    assert total == sum(1 for i in range(600) if i % 7 == 3)
    assert sum(counts.values()) == total
    _, total, _ = pool.query_jobs(q="岗位 59")
    assert total == 10                                     # 590..599（「岗位 059」里是「岗位 0」，不含「岗位 59」）
    _, total, _ = pool.query_jobs(q="%")                   # % 是字面量，不是通配符
    assert total == 0


def test_sort_by_score_puts_unscored_last(pool):
    items, _, _ = pool.query_jobs(sort="score", direction="desc", limit=200)
    scores = [job.score for job in items]
    assert scores == sorted(scores, reverse=True) and None not in scores
    tail, _, _ = pool.query_jobs(sort="score", direction="desc", limit=100, offset=550)
    assert all(job.score is None for job in tail)


def test_unknown_sort_key_falls_back_safely(pool):
    items, _, _ = pool.query_jobs(sort="id; DROP TABLE jobs", limit=5)
    assert len(items) == 5 and pool.status_counts()["new"] == 80


def test_exclude_ids_and_deleted_jobs(pool):
    pool.soft_delete(["boss-599"])
    items, total, _ = pool.query_jobs(limit=1, exclude_ids=frozenset({"boss-598"}))
    assert total == 598 and items[0].id == "boss-597"
    assert pool.count_deleted() == 1 and [j.id for j in pool.list_deleted(10)] == ["boss-599"]


def test_status_counts_single_query(pool):
    pool.set_status("boss-000", "applied")
    assert pool.status_counts() == {"filtered": 519, "new": 80, "applied": 1}
    assert pool.status_counts(platform="liepin") == {}


# ---------------- 跨平台去重 ----------------
def test_cross_platform_dup_uses_normalized_columns(repo):
    repo.upsert_job(make("boss-1", title="数据分析师（AI方向）", company="某某 科技"))
    inserted, dup = repo.upsert_job(make("liepin-1", platform="liepin", title="数据分析师(AI方向)",
                                         company="某某科技"))
    assert (inserted, dup) == (False, "boss-1")
    # 同平台不算跨平台重复；被删进回收站的不再参与去重
    assert repo.upsert_job(make("boss-2", title="数据分析师(AI方向)", company="某某科技"))[0] is True
    repo.soft_delete(["boss-1", "boss-2"])
    assert repo.upsert_job(make("zhaopin-1", platform="zhaopin", title="数据分析师（AI方向）",
                                company="某某科技"))[0] is True


def test_migration_v9_backfills_existing_rows(tmp_path):
    """升级前就在库里的岗位：迁移后 norm 列要补上，否则老岗位永远不会被判成重复。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version in range(1, 9):
        conn.executescript(_MIGRATIONS[version])
    conn.execute("INSERT INTO schema_version (version) VALUES (8)")
    conn.execute(
        "INSERT INTO jobs (id, platform, external_id, title, company, status, created_at, updated_at) "
        "VALUES ('boss-old', 'boss', 'old', '数据 分析师', '某某科技', 'new', '2026-10-01 10:00:00', '2026-10-01 10:00:00')"
    )
    conn.commit()
    conn.close()

    repo = Repo(connect(path))
    assert repo.conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    row = repo.conn.execute("SELECT norm_title, norm_company FROM jobs WHERE id = 'boss-old'").fetchone()
    assert tuple(row) == ("数据分析师", "某某科技")
    assert repo.upsert_job(make("liepin-x", platform="liepin", title="数据分析师", company="某某科技")) == (False, "boss-old")
    assert repo.get_job("boss-old").title == "数据 分析师"    # 派生列不会漏进 Job


# ---------------- 接口 ----------------
@pytest.fixture
def http(tmp_path, pool):
    from jobpilot.bootstrap import build_context
    from jobpilot.config import AppConfig
    from jobpilot.web.app import create_app

    pool.upsert_job(make("boss-out", title="外包驻场数据分析", company="某人力服务", created="2026-10-09 10:00:00"))
    db_path = pool.conn.execute("PRAGMA database_list").fetchone()["file"]
    return TestClient(create_app(lambda: build_context(AppConfig(), repo=Repo(connect(db_path)))),
                      base_url="http://127.0.0.1:8700")


def test_jobs_page_endpoint(http):
    body = http.get("/api/jobs?page=1&limit=50").json()
    assert body["total"] == 601 and body["pages"] == 13 and len(body["items"]) == 50
    assert body["items"][0]["id"] == "boss-out" and body["items"][0]["nature"]
    body = http.get("/api/jobs?page=1&limit=50&hide_nature=1").json()
    assert body["total"] == 600 and body["items"][0]["id"] == "boss-599"
    body = http.get("/api/jobs?page=99&limit=50&status=new").json()   # 越界页退回最后一页
    assert body["page"] == 2 and body["total"] == 81 and len(body["items"]) == 31   # 80 个未评分 + boss-out


def test_jobs_legacy_array_format_unchanged(http):
    rows = http.get("/api/jobs?limit=5").json()
    assert isinstance(rows, list) and len(rows) == 5 and "nature" in rows[0]


def test_export_follows_pool_filters_and_is_readable(http):
    """导出跟着岗位池的筛选走（状态 / 搜索 / 只看正常岗），平台和状态写中文。"""
    rows = http.get("/api/export?status=new").text.lstrip("﻿").strip().splitlines()
    assert len(rows) == 1 + 81
    assert rows[1].startswith("BOSS直聘,") and ",待评分," in rows[1]
    rows = http.get("/api/export?status=new&hide_nature=1").text.strip().splitlines()
    assert len(rows) == 1 + 80                                  # 去掉了「外包驻场」那条
    rows = http.get("/api/export?q=外包").text.strip().splitlines()
    assert len(rows) == 2 and "外包驻场数据分析" in rows[1]


def test_platform_filter_and_counts(http, pool):
    """岗位池按平台筛选：平台分档的数字受状态影响、不受平台筛选影响。"""
    pool.upsert_job(make("51job-1", platform="51job", status="new"))
    pool.upsert_job(make("51job-2", platform="51job", status="filtered", score=40))
    body = http.get("/api/jobs?page=1&limit=50&platform=51job").json()
    assert body["total"] == 2 and {j["platform"] for j in body["items"]} == {"51job"}
    assert body["platform_counts"] == {"boss": 601, "51job": 2}          # 选了平台，各平台的数照样给全
    body = http.get("/api/jobs?page=1&limit=50&status=new").json()
    assert body["platform_counts"] == {"boss": 81, "51job": 1}          # 跟着状态筛选变
    rows = http.get("/api/export?platform=51job").text.strip().splitlines()
    assert len(rows) == 3 and all(row.startswith("前程无忧,") for row in rows[1:])


def test_activity_pages_runs_and_events(http, pool):
    """任务历史 / 风控事件会越攒越多：各自分页，越界页退回最后一页。"""
    for i in range(23):
        pool.upsert_task_run({"id": f"r{i:02d}", "mode": "collect", "platform": "boss", "status": "completed",
                              "progress": "{}", "logs": "[]", "result": '{"new": 1}', "error": "",
                              "created_at": f"2026-10-12 10:{i:02d}:00", "finished_at": f"2026-10-12 10:{i:02d}:05"})
    for i in range(17):
        pool.add_event(None, "collect", f"boss new={i} seen=20")
    body = http.get("/api/activity?runs_page=2&events_page=9&page_size=10").json()
    assert body["runs_pager"] == {"page": 2, "pages": 3, "total": 23, "limit": 10}
    assert [r["id"] for r in body["runs"]][:2] == ["r12", "r11"]
    assert body["events_pager"]["page"] == 2 and len(body["events"]) == 7
    assert body["runs"][0]["summary"] == "新增 1 个岗位"
