from __future__ import annotations

from pathlib import Path

import sqlite3

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "jobpilot.db"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            title TEXT, company TEXT, salary TEXT, city TEXT,
            experience TEXT, jd TEXT, hr_name TEXT,
            url TEXT, search_url TEXT,
            score INTEGER, score_reason TEXT,
            status TEXT DEFAULT 'new',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # 兼容旧库：缺列则补齐
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "search_url" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN search_url TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT, action TEXT, detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    # HR 互动监测表（独立于 jobs，记录 51job 全部申请项的 HR 状态）
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hr_interactions (
            app_id TEXT PRIMARY KEY,
            title TEXT, company TEXT, hr_name TEXT, hr_title TEXT,
            salary TEXT, activity TEXT, hr_state TEXT, deeplink TEXT,
            last_event TEXT, last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP, job_id TEXT
        )"""
    )
    # 兼容旧库：jobs 补齐 HR 状态列
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    for col, ctype in [
        ("hr_state", "TEXT"), ("hr_title", "TEXT"), ("hr_activity", "TEXT"),
        ("hr_deeplink", "TEXT"), ("last_hr_check", "TEXT"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ctype}")
    conn.commit()
    return conn


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() is not None


def insert_job(conn: sqlite3.Connection, rec: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO jobs
        (id, platform, title, company, salary, city, experience, jd, hr_name, url, search_url, status)
        VALUES (:id, :platform, :title, :company, :salary, :city, :experience, :jd, :hr_name, :url, :search_url, 'new')""",
        rec,
    )
    conn.commit()


def pending_scoring(conn: sqlite3.Connection, platform: str | None = None):
    sql = "SELECT * FROM jobs WHERE status='new'"
    if platform:
        sql += f" AND platform='{platform}'"
    return conn.execute(sql).fetchall()


def pending_apply(conn: sqlite3.Connection, platform: str, threshold: int):
    return conn.execute(
        "SELECT * FROM jobs WHERE platform=? AND score>=? AND status='approved'",
        (platform, threshold),
    ).fetchall()


def set_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    conn.commit()


def set_score(conn: sqlite3.Connection, job_id: str, score: int, reason: str) -> None:
    conn.execute(
        "UPDATE jobs SET score=?, score_reason=?, status='scored' WHERE id=?",
        (score, reason, job_id),
    )
    conn.commit()


def log_history(conn: sqlite3.Connection, job_id: str | None, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO history (job_id, action, detail) VALUES (?, ?, ?)",
        (job_id, action, detail),
    )
    conn.commit()


def count_applied_today(conn: sqlite3.Connection) -> int:
    """今日已成功投递数量（用于节流日上限）。"""
    row = conn.execute(
        "SELECT COUNT(*) FROM history WHERE action='apply' AND detail='applied' AND DATE(created_at)=DATE('now')"
    ).fetchone()
    return int(row[0]) if row else 0


def upsert_hr_interaction(conn: sqlite3.Connection, rec: dict) -> None:
    """插入或更新一条 HR 互动记录（按 app_id 去重）。"""
    conn.execute(
        """INSERT INTO hr_interactions
           (app_id,title,company,hr_name,hr_title,salary,activity,hr_state,deeplink,last_event,last_seen,job_id)
           VALUES (:app_id,:title,:company,:hr_name,:hr_title,:salary,:activity,:hr_state,:deeplink,:last_event,datetime('now'),:job_id)
           ON CONFLICT(app_id) DO UPDATE SET
             title=excluded.title, company=excluded.company, hr_name=excluded.hr_name,
             hr_title=excluded.hr_title, salary=excluded.salary, activity=excluded.activity,
             hr_state=excluded.hr_state, deeplink=excluded.deeplink, last_event=excluded.last_event,
             last_seen=datetime('now'), job_id=excluded.job_id""",
        rec,
    )
    conn.commit()


def get_hr_interactions(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM hr_interactions ORDER BY last_seen DESC").fetchall()


def match_job_by_title_company(conn: sqlite3.Connection, title: str, company: str) -> str | None:
    """按 公司名精确 + 标题包含 匹配 JobPilot 已采集岗位（用于关联 hr_state）。"""
    t = (title or "").strip()
    c = (company or "").strip()
    if not c:
        return None
    rows = conn.execute("SELECT id,title,company,hr_state FROM jobs").fetchall()
    for r in rows:
        rc = (r["company"] or "").strip()
        rt = (r["title"] or "").strip()
        if rc == c and (t in rt or rt in t):
            return r["id"]
    return None


def update_job_hr(conn: sqlite3.Connection, job_id: str, hr_state: str,
                  hr_name: str, hr_title: str, deeplink: str) -> None:
    conn.execute(
        "UPDATE jobs SET hr_state=?, hr_name=?, hr_title=?, hr_deeplink=?, last_hr_check=datetime('now') WHERE id=?",
        (hr_state, hr_name, hr_title, deeplink, job_id),
    )
    conn.commit()
