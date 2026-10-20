"""概览「趋势」：每天采集 / 投递数（Repo.daily_counts + /api/trends）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job


def _day(offset: int) -> str:
    return (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")


def _seed(repo: Repo) -> None:
    # 今天：boss 2 个、51job 1 个；3 天前：liepin 1 个（之后被删进回收站 → 不算采集）
    rows = [("boss-1", "boss", 0), ("boss-2", "boss", 0), ("51-1", "51job", 0), ("lp-1", "liepin", 3)]
    for jid, platform, off in rows:
        repo.upsert_job(Job(id=jid, platform=platform, external_id=jid, title=f"岗位{jid}",
                            company=f"公司{jid}", created_at=f"{_day(off)} 10:00:00"))
    repo.soft_delete(["lp-1"])
    stamp_today = f"{_day(0)} 11:00:00"
    stamp_old = f"{_day(3)} 12:00:00"
    for jid, stamp in (("boss-1", stamp_today), ("51-1", stamp_today), ("lp-1", stamp_old),
                       ("gone-1", stamp_today)):  # gone-1：岗位已被彻底删除的投递
        repo.conn.execute("INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
                          (jid, "applied", "", stamp))
    # 不是 applied 的事件不算投递；30 天前的数据不在 14 天窗口里
    repo.conn.execute("INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
                      ("boss-2", "greeted", "", stamp_today))
    repo.upsert_job(Job(id="old-1", platform="boss", external_id="old-1", title="老岗位",
                        company="老公司", created_at=f"{_day(30)} 09:00:00"))
    repo.conn.commit()


def test_daily_counts_zero_filled_and_grouped(tmp_path):
    repo = Repo(connect(tmp_path / "t.db"))
    _seed(repo)
    rows = repo.daily_counts(14)
    assert [r["date"] for r in rows] == [_day(i) for i in range(13, -1, -1)]  # 连续、从旧到新、含今天
    today, three = rows[-1], rows[-4]
    assert today["collected"] == {"boss": 2, "51job": 1}
    assert today["applied"] == {"boss": 1, "51job": 1, "other": 1}
    # 回收站里的岗位不算采集（2026-10-20：坏数据会把那一天顶得老高）；它当时的投递照样算
    assert three["collected"] == {} and three["applied"] == {"liepin": 1}
    assert all(not r["collected"] and not r["applied"] for r in rows[:-4])
    # 今天的投递总数和风控口径一致
    assert sum(today["applied"].values()) == repo.count_events_today("applied")


def test_daily_counts_clamps(tmp_path):
    repo = Repo(connect(tmp_path / "t.db"))
    assert len(repo.daily_counts(0)) == 1
    assert len(repo.daily_counts(500)) == 90


def test_trends_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from jobpilot.bootstrap import build_context
    from jobpilot.web.app import create_app

    db = tmp_path / "t.db"
    _seed(Repo(connect(db)))
    cfg = AppConfig()
    http = TestClient(create_app(lambda: build_context(cfg, repo=Repo(connect(db)))),
                      base_url="http://127.0.0.1:8700")
    data = http.get("/api/trends?days=7").json()
    assert len(data["days"]) == 7
    assert data["days"][-1]["collected"] == {"boss": 2, "51job": 1}
    assert data["platforms"][:4] == ["boss", "51job", "liepin", "zhaopin"]
    assert data["platforms"][-1] == "other"
    assert data["daily_limit"] == cfg.apply.daily_limit
    assert len(http.get("/api/trends").json()["days"]) == 14   # 默认 14 天
    assert data["unit"] == "day"
    # days <= 1 = 「今天」：按小时分桶，00 点到当前小时
    hour_rows = datetime.now().hour + 1
    for q in ("1", "0", "-5"):
        today = http.get(f"/api/trends?days={q}").json()
        assert today["unit"] == "hour" and len(today["days"]) in (hour_rows, hour_rows + 1)  # 跨整点容错
        assert today["daily_limit"] == cfg.apply.daily_limit
    big = http.get("/api/trends?days=999").json()
    assert len(big["days"]) == 90 and big["unit"] == "day"


def _seed_hours(repo: Repo, day: str) -> None:
    # 03 点 boss 2 个；15 点 51job 1 个 + liepin 1 个（进回收站 → 不算采集）
    for jid, platform, h in (("b1", "boss", 3), ("b2", "boss", 3), ("f1", "51job", 15), ("l1", "liepin", 15)):
        repo.upsert_job(Job(id=jid, platform=platform, external_id=jid, title=f"岗位{jid}",
                            company=f"公司{jid}", created_at=f"{day} {h:02d}:20:00"))
    repo.soft_delete(["l1"])
    for jid, stamp in (("b1", "03:59:59"), ("l1", "15:00:00"), ("gone", "15:30:00"), ("f1", "23:10:00")):
        repo.conn.execute("INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
                          (jid, "applied", "", f"{day} {stamp}"))
    repo.conn.execute("INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
                      ("b2", "greeted", "", f"{day} 03:30:00"))
    # 前一天同一小时的不算进这一天
    repo.upsert_job(Job(id="prev", platform="boss", external_id="prev", title="前一天", company="前一天公司",
                        created_at=f"{(datetime.strptime(day, '%Y-%m-%d') - timedelta(days=1)):%Y-%m-%d} 03:00:00"))
    repo.conn.commit()


def test_hourly_counts_buckets_past_day(tmp_path):
    repo = Repo(connect(tmp_path / "t.db"))
    day = _day(1)
    _seed_hours(repo, day)
    rows = repo.hourly_counts(day)
    assert [r["hour"] for r in rows] == list(range(24))  # 过去的日子给满 24 小时
    assert rows[0]["date"] == f"{day} 00:00" and rows[15]["date"] == f"{day} 15:00"
    assert rows[3]["collected"] == {"boss": 2} and rows[3]["applied"] == {"boss": 1}
    # 回收站里的不算采集，但它的投递照算；岗位彻底删掉的投递归 other
    assert rows[15]["collected"] == {"51job": 1}
    assert rows[15]["applied"] == {"liepin": 1, "other": 1}
    assert rows[23]["applied"] == {"51job": 1} and rows[23]["collected"] == {}
    busy = {3, 15, 23}
    assert all(not r["collected"] and not r["applied"] for r in rows if r["hour"] not in busy)
    # 和按天的口径对得上
    daily = {r["date"]: r for r in repo.daily_counts(3)}[day]
    assert totals_of(rows, "collected") == daily["collected"]
    assert totals_of(rows, "applied") == daily["applied"]


def totals_of(rows: list[dict], key: str) -> dict:
    out: dict = {}
    for r in rows:
        for p, n in r[key].items():
            out[p] = out.get(p, 0) + n
    return out


def test_hourly_counts_today_stops_at_current_hour(tmp_path):
    repo = Repo(connect(tmp_path / "t.db"))
    now = datetime.now()
    repo.upsert_job(Job(id="t1", platform="boss", external_id="t1", title="今天岗位", company="今天公司",
                        created_at=now.strftime("%Y-%m-%d %H:%M:%S")))
    repo.conn.commit()
    rows = repo.hourly_counts()
    assert len(rows) in (now.hour + 1, now.hour + 2)  # 跨整点容错
    assert rows[0]["hour"] == 0 and rows[0]["date"] == now.strftime("%Y-%m-%d") + " 00:00"
    assert rows[now.hour]["collected"] == {"boss": 1}
    assert sum(sum(r["applied"].values()) for r in rows) == repo.count_events_today("applied") == 0
