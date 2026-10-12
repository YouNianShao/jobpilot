"""看板只给本机用：挡 DNS 重绑定（Host 不是本机）和跨站提交（写操作的 Origin 不是本机）。
另外 /api/status 里的待确认岗位只带界面用到的字段。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobpilot.bootstrap import build_context
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.web.app import PENDING_FIELDS, create_app


@pytest.fixture
def http(tmp_path):
    db = tmp_path / "g.db"
    repo = Repo(connect(db))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1", title="数据分析师", company="某公司",
                        jd="很长的 JD 正文" * 200, greeting="你好", url="https://example.com/1"))
    repo.set_score("boss-1", 88, "匹配", "pending_confirm")
    return TestClient(create_app(lambda: build_context(AppConfig(), repo=Repo(connect(db)))),
                      base_url="http://127.0.0.1:8700")


@pytest.mark.parametrize("host", ["127.0.0.1:8700", "localhost:8700", "[::1]:8700", "LOCALHOST"])
def test_local_hosts_are_allowed(http, host):
    assert http.get("/api/status", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:8700", "127.0.0.1.evil.com", "192.168.1.5:8700"])
def test_foreign_host_is_rejected_even_for_reads(http, host):
    """DNS 重绑定：网页把自己的域名解析到 127.0.0.1，请求头里的 Host 仍是它的域名。"""
    for path in ("/api/status", "/api/resume", "/api/backup", "/"):
        res = http.get(path, headers={"Host": host})
        assert res.status_code == 403 and "本机" in res.json()["error"], path


@pytest.mark.parametrize("origin", ["https://evil.example.com", "http://127.0.0.1.evil.com", "null"])
def test_cross_site_writes_are_rejected(http, origin):
    res = http.post("/api/tasks/stop", headers={"Origin": origin})
    assert res.status_code == 403
    res = http.post("/api/jobs/confirm", json={"platform": "boss", "ids": ["boss-1"]}, headers={"Origin": origin})
    assert res.status_code == 403
    assert http.get("/api/status").json()["pending_confirm"][0]["id"] == "boss-1"   # 没被确认


@pytest.mark.parametrize("origin", [None, "http://127.0.0.1:8700", "http://localhost:8700"])
def test_same_machine_writes_are_allowed(http, origin):
    headers = {"Origin": origin} if origin else {}            # 命令行脚本不带 Origin
    assert http.post("/api/tasks/stop", headers=headers).status_code == 200


def test_status_pending_carries_only_needed_fields(http):
    pending = http.get("/api/status").json()["pending_confirm"]
    assert len(pending) == 1
    assert set(pending[0]) == set(PENDING_FIELDS)              # 不再带 JD 正文等大字段
    assert pending[0]["url"] == "https://example.com/1" and pending[0]["score"] == 88
