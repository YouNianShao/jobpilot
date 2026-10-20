"""自动监测调度 + 进展记录的筛选接口 + interactions 新列的迁移。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from jobpilot.config import AppConfig, MonitorCfg
from jobpilot.db import SCHEMA_VERSION, Repo, connect
from jobpilot.models import Job, Status
from jobpilot.services.auto_monitor import LAST_KEY, AutoMonitor, next_window_start, schedule
from jobpilot.services.orchestrator import TaskNotRunnable
from jobpilot.tasks import TaskAlreadyRunning

AT_10 = datetime(2026, 10, 20, 10, 0, 0)


class FakeRunner:
    def __init__(self, busy=(), refuse=None):
        self.busy = set(busy)
        self.refuse = refuse
        self.started = []

    def running_platforms(self):
        return set(self.busy)

    def start(self, mode, platform="", **options):
        if self.refuse:
            raise self.refuse
        self.started.append((mode, platform))
        return type("T", (), {"id": "t1"})()


@pytest.fixture
def repo(tmp_path):
    return Repo(connect(tmp_path / "a.db"))


def cfg(**monitor):
    config = AppConfig()
    for key, value in monitor.items():
        setattr(config.monitor, key, value)
    return config


def test_defaults():
    m = MonitorCfg()
    assert m.auto is True and m.interval_minutes == 60 and m.working_hours == "08:00-22:00"
    with pytest.raises(ValueError):
        MonitorCfg(interval_minutes=5)
    with pytest.raises(ValueError):
        MonitorCfg(interval_minutes=721)


def test_due_in_window_when_never_ran(repo):
    plan = schedule(cfg(), repo, FakeRunner(), now=AT_10)
    assert plan["due"] and plan["reason"] == "" and plan["next_run"] == "2026-10-20 10:00:00"
    assert plan["last_run"] is None and plan["auto"] is True


def test_out_of_window(repo):
    plan = schedule(cfg(), repo, FakeRunner(), now=datetime(2026, 10, 20, 23, 30))
    assert not plan["due"] and plan["reason"] == "不在监测时段 08:00-22:00"
    assert plan["next_run"] == "2026-10-21 08:00:00"


def test_interval_not_elapsed(repo):
    repo.set_kv(LAST_KEY, "2026-10-20 09:30:00")
    plan = schedule(cfg(), repo, FakeRunner(), now=AT_10)
    assert not plan["due"] and "不到 60 分钟" in plan["reason"]
    assert plan["next_run"] == "2026-10-20 10:30:00" and plan["last_run"] == "2026-10-20 09:30:00"
    assert schedule(cfg(), repo, FakeRunner(), now=datetime(2026, 10, 20, 10, 30))["due"]


def test_next_run_rolls_into_next_window(repo):
    repo.set_kv(LAST_KEY, "2026-10-20 21:30:00")
    plan = schedule(cfg(), repo, FakeRunner(), now=datetime(2026, 10, 20, 21, 40))
    assert plan["next_run"] == "2026-10-21 08:00:00"


def test_busy_runner(repo):
    plan = schedule(cfg(), repo, FakeRunner(busy={"boss"}), now=AT_10)
    assert not plan["due"] and plan["reason"] == "有任务在跑，等它结束"
    # 占的不是能监测的平台（比如只有 AI 评分位）就不算忙
    assert schedule(cfg(), repo, FakeRunner(busy={"nope"}), now=AT_10)["due"]


def test_disabled_and_not_auto(repo):
    assert "监测已关闭" in schedule(cfg(enabled=False), repo, FakeRunner(), now=AT_10)["reason"]
    off = schedule(cfg(auto=False), repo, FakeRunner(), now=AT_10)
    assert not off["due"] and off["auto"] is False and off["next_run"] is None


def test_global_lock_blocks(repo):
    from jobpilot.safety.policy import GLOBAL_LOCK_KEY

    repo.set_lock(GLOBAL_LOCK_KEY, "两个平台接连被拦", "multi_platform", "2026-10-20 12:00:00")
    plan = schedule(cfg(), repo, FakeRunner(), now=AT_10)
    assert not plan["due"] and "整体冷却" in plan["reason"]
    assert plan["next_run"] == "2026-10-20 12:00:00"


def test_multi_range_and_midnight():
    spec = "08:00-12:00, 14:00-00:00"
    assert next_window_start(spec, datetime(2026, 10, 20, 13, 0)) == datetime(2026, 10, 20, 14, 0)
    assert next_window_start(spec, datetime(2026, 10, 20, 23, 59)) == datetime(2026, 10, 20, 23, 59)
    assert next_window_start(spec, datetime(2026, 10, 20, 0, 30)) == datetime(2026, 10, 20, 8, 0)
    assert next_window_start("", datetime(2026, 10, 20, 3, 0)) == datetime(2026, 10, 20, 3, 0)


def test_multi_range_schedule(repo):
    config = cfg(working_hours="08:00-12:00,14:00-22:00")
    assert not schedule(config, repo, FakeRunner(), now=datetime(2026, 10, 20, 13, 0))["due"]
    assert schedule(config, repo, FakeRunner(), now=datetime(2026, 10, 20, 15, 0))["due"]


def _ctx_factory(db, config):
    from jobpilot.bootstrap import build_context

    return lambda: build_context(config, repo=Repo(connect(db)))


def test_tick_starts_monitor_and_records_last(tmp_path):
    db = tmp_path / "t.db"
    runner = FakeRunner()
    auto = AutoMonitor(_ctx_factory(db, cfg()), runner, clock=lambda: AT_10)
    out = auto.tick()
    assert out["started"] and runner.started == [("monitor", "")]
    assert Repo(connect(db)).get_kv(LAST_KEY) == "2026-10-20 10:00:00"
    # 间隔没到：不再启动
    assert not auto.tick()["started"] and len(runner.started) == 1


@pytest.mark.parametrize("exc", [TaskAlreadyRunning("busy"), TaskNotRunnable("no")])
def test_tick_refused_is_retried_next_tick(tmp_path, exc):
    db = tmp_path / "t.db"
    auto = AutoMonitor(_ctx_factory(db, cfg()), FakeRunner(refuse=exc), clock=lambda: AT_10)
    assert auto.tick()["started"] is False
    assert Repo(connect(db)).get_kv(LAST_KEY) is None      # 没记时间：下一 tick 还会试


def test_tick_reads_current_config_each_time(tmp_path):
    db = tmp_path / "t.db"
    holder = {"config": cfg(auto=False)}
    from jobpilot.bootstrap import build_context

    runner = FakeRunner()
    auto = AutoMonitor(lambda: build_context(holder["config"], repo=Repo(connect(db))), runner,
                       clock=lambda: AT_10)
    assert not auto.tick()["started"]
    holder["config"] = cfg(auto=True)                      # 设置页保存了
    assert auto.tick()["started"]


def test_create_app_never_starts_the_thread(tmp_path):
    from jobpilot.web.app import create_app

    app = create_app(_ctx_factory(tmp_path / "t.db", cfg()), runner=FakeRunner())
    assert app.state.auto_monitor is not None and app.state.auto_monitor._thread is None
    assert create_app(lambda: None, runner=FakeRunner(), auto_monitor=False).state.auto_monitor is None


# ---------------- 接口 ----------------

@pytest.fixture
def http(tmp_path):
    from jobpilot.web.app import create_app

    db = tmp_path / "w.db"
    repo = Repo(connect(db))
    specs = [
        ("boss-1", "boss", "数据分析师", "润和软件", "苗女士"),
        ("boss-2", "boss", "AI 产品经理", "100%示例", "张女士"),
        ("51job-1", "51job", "数据分析师", "Cognizant", ""),
        ("liepin-1", "liepin", "算法_工程师", "林清轩", ""),
    ]
    for job_id, platform, title, company, hr in specs:
        repo.upsert_job(Job(id=job_id, platform=platform, external_id=job_id, title=title, company=company))
        repo.set_status(job_id, Status.APPLIED.value)
        repo.add_event(job_id, "applied", "applied")
        repo.upsert_interaction(f"k-{job_id}", platform, job_id, hr, "viewed", "看过了", "", company=company)
    repo.upsert_interaction("k-boss-2", "boss", "boss-2", "张女士", "replied", "新消息", "", unread=True,
                            msg_time="10:21")
    return TestClient(create_app(_ctx_factory(db, AppConfig()), runner=FakeRunner()),
                      base_url="http://127.0.0.1:8700")


def test_interactions_filter_and_shape(http):
    body = http.get("/api/interactions?page=1&limit=20").json()
    assert body["total"] == 4 and body["platforms"] == {"boss": 2, "51job": 1, "liepin": 1}
    first = body["items"][0]
    assert first["key"] == "k-boss-2" and first["unread"] is True and first["msg_time"] == "10:21"
    assert {"platform", "unread", "msg_time", "updated_at"} <= set(first)

    boss = http.get("/api/interactions?page=1&platform=boss").json()
    assert boss["total"] == 2 and boss["counts"] == {"viewed": 1, "replied": 1}
    assert boss["platforms"]["51job"] == 1                 # 平台分档不受平台筛选影响

    by_hr = http.get("/api/interactions?page=1&q=苗").json()
    assert [r["key"] for r in by_hr["items"]] == ["k-boss-1"] and by_hr["platforms"] == {"boss": 1}
    assert http.get("/api/interactions?page=1&q=cognizant").json()["total"] == 1   # 不分大小写
    assert http.get("/api/interactions?page=1&q=数据分析").json()["total"] == 2      # 岗位标题


def test_interactions_like_escaping(http):
    assert [r["key"] for r in http.get("/api/interactions?page=1&q=%25").json()["items"]] == ["k-boss-2"]
    assert [r["key"] for r in http.get("/api/interactions?page=1&q=_").json()["items"]] == ["k-liepin-1"]
    assert http.get("/api/interactions?q=%25").json()[0]["key"] == "k-boss-2"     # 老格式也认筛选


def test_interactions_pagination_with_filter(http):
    one = http.get("/api/interactions?page=1&limit=1&platform=boss").json()
    two = http.get("/api/interactions?page=2&limit=1&platform=boss").json()
    assert one["pages"] == 2 and {one["items"][0]["key"], two["items"][0]["key"]} == {"k-boss-1", "k-boss-2"}
    assert http.get("/api/interactions?page=9&limit=1&platform=boss").json()["page"] == 2


def test_follow_ups_filter(http):
    body = http.get("/api/follow-ups?page=1&limit=20&platform=boss").json()
    assert body["total"] == 2 and {r["id"] for r in body["rows"]} == {"boss-1", "boss-2"}
    assert body["platforms"] == {"boss": 2, "51job": 1, "liepin": 1}
    q = http.get("/api/follow-ups?page=1&q=COGNIZANT").json()
    assert q["total"] == 1 and q["rows"][0]["id"] == "51job-1" and q["platforms"] == {"51job": 1}
    assert http.get("/api/follow-ups?page=1&q=_").json()["total"] == 1
    assert len(http.get("/api/follow-ups?platform=liepin").json()["rows"]) == 1


def test_schedule_endpoint(http):
    body = http.get("/api/monitor/schedule").json()
    assert set(body) == {"auto", "interval_minutes", "working_hours", "last_run", "next_run", "reason"}
    assert body["auto"] is True and body["interval_minutes"] == 60 and body["working_hours"] == "08:00-22:00"


# ---------------- 迁移 ----------------

def test_migration_from_v9(tmp_path):
    db = tmp_path / "old.db"
    conn = connect(db)
    conn.close()
    # 造一个 v9 的库：interactions 回到没有新列的样子，里面有一条老数据
    raw = sqlite3.connect(str(db))
    raw.executescript("""
        DROP TABLE interactions;
        CREATE TABLE interactions (key TEXT PRIMARY KEY, platform TEXT NOT NULL, job_id TEXT, hr_name TEXT,
          state TEXT NOT NULL, last_text TEXT, deeplink TEXT, seen_at TEXT NOT NULL, company TEXT);
        INSERT INTO interactions VALUES ('old', 'boss', NULL, 'HR', 'viewed', '旧', '', '2026-10-01 09:00:00', 'X');
        UPDATE schema_version SET version = 9;
    """)
    raw.commit()
    raw.close()

    repo = Repo(connect(db))
    assert repo.conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION == 10
    row = repo.list_interactions()[0]
    assert row["unread"] is False and row["msg_time"] == "" and row["updated_at"] == "2026-10-01 09:00:00"
    # 文字没变：updated_at 沿用旧的 seen_at；变了才更新
    repo.upsert_interaction("old", "boss", None, "HR", "viewed", "旧", "")
    assert repo.get_interaction("old")["updated_at"] == "2026-10-01 09:00:00"
    repo.upsert_interaction("old", "boss", None, "HR", "viewed", "新的一句", "", unread=True)
    fresh = repo.get_interaction("old")
    assert fresh["updated_at"] > "2026-10-01 09:00:00" and fresh["unread"] == 1
    # unread / msg_time 不传 = 不改
    repo.upsert_interaction("old", "boss", None, "HR", "viewed", "新的一句", "")
    assert repo.get_interaction("old")["unread"] == 1
    Repo(connect(db))                                      # 再连一次：不重复迁移
