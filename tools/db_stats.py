"""看一眼数据库现状（排查用）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.config import DB_PATH  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402

repo = Repo(connect(DB_PATH))
version = repo.conn.execute("SELECT version FROM schema_version").fetchone()[0]
tables = sorted(row[0] for row in repo.conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall())

print("db =", DB_PATH)
print("schema_version =", version)
print("tables =", ", ".join(tables))
print("jobs =", len(repo.list_jobs(limit=1000)))
print("interactions =", len(repo.list_interactions(limit=1000)))
print("task_runs =", len(repo.list_task_runs(50)))
for sql, label in (
    ("SELECT COUNT(*) FROM jobs WHERE id LIKE '%demo%'", "残留 demo 岗位"),
    ("SELECT COUNT(*) FROM events WHERE job_id LIKE '%demo%'", "残留 demo 事件"),
    ("SELECT COUNT(*) FROM interactions WHERE key LIKE '%demo%'", "残留 demo 互动"),
):
    print(label, "=", repo.conn.execute(sql).fetchone()[0])
print("kv(冷却/计数) =", repo.conn.execute("SELECT COUNT(*) FROM runtime_kv").fetchone()[0])
print("score_traces =", repo.conn.execute("SELECT COUNT(*) FROM score_traces").fetchone()[0])
repo.conn.close()
