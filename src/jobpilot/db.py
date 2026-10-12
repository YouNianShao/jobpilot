"""SQLite 存储层（WAL + 版本化迁移）。

只提供仓储方法，不写业务规则。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta
from pathlib import Path

from .models import APPLIED_STATUSES, Job, now_str, today_str

SCHEMA_VERSION = 9

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS jobs (
      id            TEXT PRIMARY KEY,
      platform      TEXT NOT NULL,
      external_id   TEXT NOT NULL,
      title         TEXT NOT NULL,
      company       TEXT NOT NULL,
      salary        TEXT,
      city          TEXT,
      url           TEXT,
      jd            TEXT,
      score         INTEGER,
      score_reason  TEXT,
      greeting      TEXT,
      status        TEXT NOT NULL DEFAULT 'new',
      dup_of        TEXT,
      created_at    TEXT NOT NULL,
      updated_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_jobs_status   ON jobs(status);
    CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);

    CREATE TABLE IF NOT EXISTS events (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id     TEXT,
      kind       TEXT NOT NULL,
      detail     TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_events_job  ON events(job_id);
    CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, created_at);

    CREATE TABLE IF NOT EXISTS interactions (
      key       TEXT PRIMARY KEY,
      platform  TEXT NOT NULL,
      job_id    TEXT,
      hr_name   TEXT,
      state     TEXT NOT NULL,
      last_text TEXT,
      deeplink  TEXT,
      seen_at   TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS page_access (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      platform   TEXT NOT NULL,
      stage      TEXT NOT NULL,
      day        TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_page_access ON page_access(platform, stage, day);

    CREATE TABLE IF NOT EXISTS locks (
      platform  TEXT PRIMARY KEY,
      reason    TEXT NOT NULL,
      kind      TEXT NOT NULL,
      until     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks (
      id         TEXT PRIMARY KEY,
      mode       TEXT NOT NULL,
      status     TEXT NOT NULL,
      progress   TEXT,
      error      TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
    2: """
    CREATE TABLE IF NOT EXISTS runtime_kv (
      key        TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    """,
    3: """
    ALTER TABLE jobs ADD COLUMN resume_path TEXT;
    """,
    4: """
    ALTER TABLE jobs ADD COLUMN follow_up TEXT;
    ALTER TABLE jobs ADD COLUMN follow_up_at TEXT;
    """,
    7: """
    ALTER TABLE jobs ADD COLUMN deleted_at TEXT;
    CREATE INDEX IF NOT EXISTS idx_jobs_deleted ON jobs(deleted_at);
    """,
    6: """
    CREATE TABLE IF NOT EXISTS task_runs (
      id          TEXT PRIMARY KEY,
      mode        TEXT NOT NULL,
      platform    TEXT,
      status      TEXT NOT NULL,
      progress    TEXT,
      logs        TEXT,
      result      TEXT,
      error       TEXT,
      created_at  TEXT NOT NULL,
      finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_task_runs_created ON task_runs(created_at);
    """,
    5: """
    CREATE TABLE IF NOT EXISTS score_traces (
      job_id     TEXT PRIMARY KEY,
      model      TEXT,
      prompt     TEXT,
      tokens_in  INTEGER,
      tokens_out INTEGER,
      latency_ms INTEGER,
      created_at TEXT NOT NULL
    );
    """,
    8: """
    ALTER TABLE interactions ADD COLUMN company TEXT;
    """,
    # 9：跨平台去重以前每入库一条就把其它平台的岗位全读出来、在 Python 里逐条归一化比对
    #   （2 万岗位时每条 113ms）。归一化结果落成两列 + 复合索引，去重变成一次索引查找。
    #   旧数据由 _MIGRATION_HOOKS[9] 回填（归一化规则是 Python 正则，SQL 里写不出来）。
    #   其余索引都是「过滤列在前、排序列在后」的复合索引：岗位池分页总带 deleted_at IS NULL，
    #   只有过滤列的索引会让 SQLite 先取出全部存活岗位再排序（2 万岗位时一页 108ms），
    #   复合索引可以按索引顺序直接取前 50 条（实测 0.1ms 以内）：
    #     live_created         默认排序（采集时间倒序）、搜索
    #     live_status_created  按状态筛选的分页 + 状态分档计数（覆盖索引）
    #     live_score           按分数排序
    #     live_keys            岗位性质缓存只要 (id, updated_at)，覆盖索引不回表
    #     events_kind_job      关键词效果：按 job_id 顺序取每个岗位的第一条采集事件
    9: """
    ALTER TABLE jobs ADD COLUMN norm_title TEXT;
    ALTER TABLE jobs ADD COLUMN norm_company TEXT;
    CREATE INDEX IF NOT EXISTS idx_jobs_norm ON jobs(norm_title, norm_company);
    CREATE INDEX IF NOT EXISTS idx_jobs_live_created ON jobs(deleted_at, created_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_live_status_created ON jobs(deleted_at, status, created_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_live_score ON jobs(deleted_at, score, created_at);
    CREATE INDEX IF NOT EXISTS idx_jobs_live_keys ON jobs(deleted_at, id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_events_kind_job ON events(kind, job_id, detail);
    """,
}


def _backfill_norm(conn: sqlite3.Connection) -> None:
    from .models import normalize

    rows = conn.execute("SELECT id, title, company FROM jobs").fetchall()
    conn.executemany(
        "UPDATE jobs SET norm_title = ?, norm_company = ? WHERE id = ?",
        [(normalize(row["title"]), normalize(row["company"]), row["id"]) for row in rows],
    )


# 某个版本的 SQL 跑完之后，还需要用 Python 补的数据
_MIGRATION_HOOKS = {9: _backfill_norm}

# (岗位 id, updated_at) -> 是否命中岗位性质规则；见 Repo.flagged_nature_ids
_NATURE_CACHE: dict[tuple[str, str], bool] = {}

_JOB_COLUMNS = (
    "id, platform, external_id, title, company, salary, city, url, jd, "
    "score, score_reason, greeting, status, dup_of, created_at, updated_at"
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    current = int(row[0]) if row else 0
    for version in sorted(_MIGRATIONS):
        if version > current:
            conn.executescript(_MIGRATIONS[version])
            if version in _MIGRATION_HOOKS:
                _MIGRATION_HOOKS[version](conn)
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            current = version
    conn.commit()


_TEXT_COLUMNS = ("salary", "city", "url", "jd", "score_reason", "greeting",
                 "resume_path", "follow_up", "follow_up_at", "deleted_at")


_JOB_FIELDS = frozenset(field.name for field in dataclass_fields(Job))


def _row_to_job(row: sqlite3.Row) -> Job:
    """把 SQLite 的 NULL 归一化成空串，避免 None 泄漏到领域层/接口/界面。

    只取 Job 认识的列：表里还有 norm_title / norm_company 这类只给索引用的派生列，
    它们在 Job 上是属性（按 title / company 现算），不能当构造参数传进去。
    """
    data = {k: row[k] for k in row.keys() if k in _JOB_FIELDS}
    for key in _TEXT_COLUMNS:
        if data.get(key) is None:
            data[key] = ""
    return Job(**data)


def _keyword_of(detail: str) -> str:
    """采集事件里的 detail 各平台格式不同：`城市/关键词`、`关键词`、`关键词（第 2 批）`。"""
    text = re.sub(r"（第\s*\d+\s*批）\s*$", "", (detail or "").strip())
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text.strip() or "(未知)"


class Repo:
    """所有数据库读写的唯一出口。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ---------------- jobs ----------------
    def find_cross_platform_dup(self, platform: str, norm_title: str, norm_company: str) -> str | None:
        if not norm_title or not norm_company:
            return None
        # 走 idx_jobs_norm：一次索引查找（以前是把其它平台的岗位全读出来逐条归一化）
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE norm_title = ? AND norm_company = ? "
            "AND platform <> ? AND deleted_at IS NULL ORDER BY rowid LIMIT 1",
            (norm_title, norm_company, platform),
        ).fetchone()
        return str(row["id"]) if row else None

    def upsert_job(self, job: Job) -> tuple[bool, str | None]:
        """插入岗位。返回 (是否新增, 重复指向的原岗位 id)。"""
        existing = self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,)).fetchone()
        if existing:
            return False, job.id

        dup = self.find_cross_platform_dup(job.platform, job.norm_title, job.norm_company)
        if dup:
            self.add_event(dup, "cross_platform_dup", f"{job.platform} 同岗位重复，跳过")
            return False, dup

        stamp = job.created_at or now_str()
        self.conn.execute(
            f"INSERT INTO jobs ({_JOB_COLUMNS}, norm_title, norm_company) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job.id, job.platform, job.external_id, job.title, job.company,
                job.salary, job.city, job.url, job.jd,
                job.score, job.score_reason, job.greeting, job.status, job.dup_of,
                stamp, stamp, job.norm_title, job.norm_company,
            ),
        )
        self.conn.commit()
        return True, None

    def get_job(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def list_jobs(self, platform: str | None = None, status: str | None = None,
                  limit: int = 500, offset: int = 0, include_deleted: bool = False) -> list[Job]:
        # LIMIT/OFFSET 必须是整数：绑 None 时 SQLite 抛的是 "datatype mismatch"，
        # 完全看不出是"参数没传"——仪表盘上的"评分/招呼语"按钮就是这么坏的。
        limit = 500 if limit is None else int(limit)
        offset = 0 if offset is None else int(offset)
        sql = "SELECT * FROM jobs"
        where, args = [], []
        if not include_deleted:
            where.append("deleted_at IS NULL")
        if platform:
            where.append("platform = ?")
            args.append(platform)
        if status:
            where.append("status = ?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY score IS NULL, score DESC, created_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        return [_row_to_job(r) for r in self.conn.execute(sql, args).fetchall()]

    def status_counts(self, platform: str | None = None) -> dict[str, int]:
        """每个状态各有几个岗位（不含回收站）。一条 GROUP BY 代替逐个状态 COUNT ——
        /api/status 每 2 秒轮询一次，以前 4 次 COUNT 在 2 万岗位时就要 66ms。"""
        sql = "SELECT status, COUNT(*) AS n FROM jobs WHERE deleted_at IS NULL"
        args: list = []
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        sql += " GROUP BY status"
        return {str(row["status"]): int(row["n"]) for row in self.conn.execute(sql, args).fetchall()}

    # 岗位池分页：排序键白名单（列名不能拼用户输入）
    _SORTS = {
        "score": "score IS NULL, score {dir}, created_at DESC",
        "created": "created_at {dir}",
        "title": "title {dir}, created_at DESC",
        "company": "company {dir}, created_at DESC",
        "city": "city {dir}, created_at DESC",
        "status": "status {dir}, created_at DESC",
    }

    def query_jobs(self, *, status: str | None = None, q: str = "", platform: str | None = None,
                   sort: str = "created", direction: str = "desc", limit: int = 50, offset: int = 0,
                   exclude_ids: frozenset[str] | None = None) -> tuple[list[Job], int, dict[str, int]]:
        """岗位池的一页：(本页岗位, 符合条件的总数, 各状态计数)。

        状态计数只受搜索词 / 平台影响、不受状态筛选影响 —— 这样状态分档上的数字
        永远在说「当前搜索下每档有几个」，点哪一档之前就知道会看到多少。
        ``exclude_ids`` 给「只看正常岗」用（岗位性质是规则现算的，不在库里）。
        """
        where = ["deleted_at IS NULL"]
        args: list = []
        if platform:
            where.append("platform = ?")
            args.append(platform)
        text = (q or "").strip()
        if text:
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')")
            args += [like, like]
        if exclude_ids:
            # 用 json_each 传一组 id：岗位一多，IN (?, ?, …) 会撞上 SQLite 的变量个数上限
            where.append("id NOT IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sorted(exclude_ids)))
        base = " WHERE " + " AND ".join(where)
        counts = {
            str(row["status"]): int(row["n"])
            for row in self.conn.execute(f"SELECT status, COUNT(*) AS n FROM jobs{base} GROUP BY status", args)
        }
        page_where, page_args = base, list(args)
        if status:
            page_where += " AND status = ?"
            page_args.append(status)
        total = counts.get(status, 0) if status else sum(counts.values())
        order = self._SORTS.get(sort, self._SORTS["created"]).format(dir="ASC" if direction == "asc" else "DESC")
        rows = self.conn.execute(
            f"SELECT * FROM jobs{page_where} ORDER BY {order}, rowid DESC LIMIT ? OFFSET ?",
            [*page_args, max(1, min(int(limit), 200)), max(0, int(offset))],
        ).fetchall()
        return [_row_to_job(row) for row in rows], total, counts

    def platform_counts(self, *, status: str | None = None, q: str = "",
                        exclude_ids: frozenset[str] | None = None) -> dict[str, int]:
        """岗位池平台分档上的数字：受状态 / 搜索词 / 只看正常岗影响，不受平台筛选影响。

        点哪个平台之前就知道会看到多少；和状态分档的计数是同一个思路。
        """
        where = ["deleted_at IS NULL"]
        args: list = []
        if status:
            where.append("status = ?")
            args.append(status)
        text = (q or "").strip()
        if text:
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\')")
            args += [like, like]
        if exclude_ids:
            where.append("id NOT IN (SELECT value FROM json_each(?))")
            args.append(json.dumps(sorted(exclude_ids)))
        sql = "SELECT platform, COUNT(*) AS n FROM jobs WHERE " + " AND ".join(where) + " GROUP BY platform"
        return {str(row["platform"]): int(row["n"]) for row in self.conn.execute(sql, args)}

    def flagged_nature_ids(self) -> frozenset[str]:
        """被识别为外包 / 培训 / 营销话术的岗位 id（「只看正常岗」要排除它们）。

        岗位性质是规则现算的、不进库（规则改了立刻生效）；全量现算 2 万岗位要 200ms，
        所以按 (id, updated_at) 缓存在进程里：只有新岗位或改过的岗位才重新识别。
        """
        from .nature import detect

        # 第一遍只读 (id, updated_at)（走覆盖索引，不读 JD 正文）；缓存里没有的才回表取正文现算
        out, missing = set(), []
        for row in self.conn.execute("SELECT id, updated_at FROM jobs WHERE deleted_at IS NULL"):
            hit = _NATURE_CACHE.get((row["id"], row["updated_at"]))
            if hit is None:
                missing.append(row["id"])
            elif hit:
                out.add(str(row["id"]))
        for start in range(0, len(missing), 500):
            chunk = missing[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in self.conn.execute(
                    f"SELECT id, title, company, jd, updated_at FROM jobs WHERE id IN ({marks})", chunk):
                hit = bool(detect(row["title"], row["company"], row["jd"] or ""))
                _NATURE_CACHE[(row["id"], row["updated_at"])] = hit
                if hit:
                    out.add(str(row["id"]))
        if len(_NATURE_CACHE) > 100_000:      # 只防无限长大（同一岗位每次更新都会留一条旧 key）
            _NATURE_CACHE.clear()
        return frozenset(out)

    def count_status(self, status: str, platform: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM jobs WHERE status = ? AND deleted_at IS NULL"
        args: list = [status]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        return int(self.conn.execute(sql, args).fetchone()[0])

    def set_status(self, job_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
            (status, now_str(), job_id),
        )
        self.conn.commit()

    def set_score(self, job_id: str, score: int, reason: str, status: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET score = ?, score_reason = ?, status = ?, updated_at = ? WHERE id = ?",
            (score, reason, status, now_str(), job_id),
        )
        self.conn.commit()

    def upsert_task_run(self, rec: dict) -> None:
        """任务历史落库：任务开始时写一行，结束时更新同一行。"""
        self.conn.execute(
            "INSERT INTO task_runs (id, mode, platform, status, progress, logs, result, error, created_at, finished_at) "
            "VALUES (:id,:mode,:platform,:status,:progress,:logs,:result,:error,:created_at,:finished_at) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, progress=excluded.progress, "
            "logs=excluded.logs, result=excluded.result, error=excluded.error, finished_at=excluded.finished_at",
            rec,
        )
        self.conn.commit()

    def list_task_runs(self, limit: int = 20, offset: int = 0) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM task_runs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (int(limit), max(0, int(offset))),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_task_runs(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0])

    def set_score_trace(self, job_id: str, *, model: str, prompt: str,
                        tokens_in: int, tokens_out: int, latency_ms: int) -> None:
        """记录"这个分数是怎么算出来的"：模型 / 提示词版本 / token / 耗时。"""
        self.conn.execute(
            "INSERT INTO score_traces (job_id, model, prompt, tokens_in, tokens_out, latency_ms, created_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "model=excluded.model, prompt=excluded.prompt, tokens_in=excluded.tokens_in, "
            "tokens_out=excluded.tokens_out, latency_ms=excluded.latency_ms, created_at=excluded.created_at",
            (job_id, model, prompt, int(tokens_in), int(tokens_out), int(latency_ms), now_str()),
        )
        self.conn.commit()

    def get_score_trace(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM score_traces WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def set_follow_up(self, job_id: str, text: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET follow_up = ?, follow_up_at = ?, updated_at = ? WHERE id = ?",
            (text, now_str(), now_str(), job_id),
        )
        self.conn.commit()

    def set_resume_path(self, job_id: str, path: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET resume_path = ?, updated_at = ? WHERE id = ?",
            (path, now_str(), job_id),
        )
        self.conn.commit()

    def set_greeting(self, job_id: str, greeting: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET greeting = ?, updated_at = ? WHERE id = ?",
            (greeting, now_str(), job_id),
        )
        self.conn.commit()

    # ---------------- 回收站 ----------------
    def _ids_clause(self, job_ids) -> tuple[str, list[str]]:
        ids = [str(i) for i in (job_ids or []) if str(i)]
        return ",".join("?" for _ in ids), ids

    def soft_delete(self, job_ids) -> int:
        marks, ids = self._ids_clause(job_ids)
        if not ids:
            return 0
        cursor = self.conn.execute(
            f"UPDATE jobs SET deleted_at = ?, updated_at = ? WHERE id IN ({marks}) AND deleted_at IS NULL",
            [now_str(), now_str(), *ids],
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def restore(self, job_ids) -> int:
        marks, ids = self._ids_clause(job_ids)
        if not ids:
            return 0
        cursor = self.conn.execute(
            f"UPDATE jobs SET deleted_at = NULL, updated_at = ? WHERE id IN ({marks}) AND deleted_at IS NOT NULL",
            [now_str(), *ids],
        )
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def purge(self, job_ids) -> int:
        """彻底删除：岗位 + 它的事件/互动/评分依据一起清掉。"""
        marks, ids = self._ids_clause(job_ids)
        if not ids:
            return 0
        cursor = self.conn.execute(
            f"DELETE FROM jobs WHERE id IN ({marks}) AND deleted_at IS NOT NULL", ids
        )
        self.conn.execute(f"DELETE FROM events WHERE job_id IN ({marks})", ids)
        self.conn.execute(f"DELETE FROM score_traces WHERE job_id IN ({marks})", ids)
        self.conn.execute(f"DELETE FROM interactions WHERE job_id IN ({marks})", ids)
        self.conn.commit()
        return int(cursor.rowcount or 0)

    def list_deleted(self, limit: int = 200, offset: int = 0) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (int(limit), max(0, int(offset))),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def count_deleted(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE deleted_at IS NOT NULL").fetchone()[0])

    def is_applied(self, job_id: str) -> bool:
        return self.count_status_for_job(job_id, APPLIED_STATUSES) > 0

    def count_status_for_job(self, job_id: str, statuses: set[str]) -> int:
        marks = ",".join("?" for _ in statuses)
        return int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM jobs WHERE id = ? AND status IN ({marks})",
                (job_id, *statuses),
            ).fetchone()[0]
        )

    # ---------------- events ----------------
    def add_event(self, job_id: str | None, kind: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
            (job_id, kind, detail, now_str()),
        )
        self.conn.commit()

    def list_events(self, job_id: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT kind, detail, created_at FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
            (job_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def last_event_at(self, job_id: str, kind: str) -> str | None:
        row = self.conn.execute(
            "SELECT created_at FROM events WHERE job_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (job_id, kind),
        ).fetchone()
        return str(row["created_at"]) if row else None

    def count_events_for_job(self, job_id: str, kind: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE job_id = ? AND kind = ?", (job_id, kind)
            ).fetchone()[0]
        )

    def count_events_today(self, kind: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = ? AND date(created_at) = date('now','localtime')",
                (kind,),
            ).fetchone()[0]
        )

    # ---------------- page budget ----------------
    def record_page(self, platform: str, stage: str) -> None:
        self.conn.execute(
            "INSERT INTO page_access (platform, stage, day, created_at) VALUES (?,?,?,?)",
            (platform, stage, today_str(), now_str()),
        )
        self.conn.commit()

    def count_pages(self, platform: str, day: str | None = None, stage: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM page_access WHERE platform = ? AND day = ?"
        args: list = [platform, day or today_str()]
        if stage:
            sql += " AND stage = ?"
            args.append(stage)
        return int(self.conn.execute(sql, args).fetchone()[0])

    def count_pages_since(self, platform: str, since: str) -> int:
        """滚动窗口：最近 ``since`` 之后开了多少页（created_at 是本地时间字符串，字典序即时间序）。"""
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM page_access WHERE platform = ? AND created_at >= ?",
                (platform, since),
            ).fetchone()[0]
        )

    # ---------------- 可观测性聚合（"记录"页用） ----------------
    def pages_by_hour(self, day: str | None = None, platform: str | None = None) -> list[dict]:
        """今天每个小时开了多少页——一眼看出节奏像不像人在翻页。"""
        sql = ("SELECT substr(created_at, 12, 2) AS hour, COUNT(*) AS count "
               "FROM page_access WHERE day = ?")
        args: list = [day or today_str()]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        sql += " GROUP BY hour ORDER BY hour"
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def pages_by_day(self, days: int = 7, platform: str | None = None) -> list[dict]:
        """近 N 天每天开了多少页（按天聚合，缺的日子补 0 由调用方决定）。"""
        start = (datetime.now() - timedelta(days=max(1, int(days)) - 1)).strftime("%Y-%m-%d")
        sql = "SELECT day, COUNT(*) AS count FROM page_access WHERE day >= ?"
        args: list = [start]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        sql += " GROUP BY day ORDER BY day"
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def pages_by_stage(self, day: str | None = None, platform: str | None = None) -> dict[str, int]:
        sql = "SELECT stage, COUNT(*) AS count FROM page_access WHERE day = ?"
        args: list = [day or today_str()]
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        sql += " GROUP BY stage"
        return {str(row["stage"]): int(row["count"]) for row in self.conn.execute(sql, args).fetchall()}

    # ---------------- 漏斗 / 关键词效果（"记录"页的决策依据） ----------------
    def pool_funnel(self, threshold: int = 0) -> dict:
        """整个岗位池的漏斗：采到 → 打过分 → 过了阈值 → 确认过 → 投出去。"""
        # +deleted_at：一元加号让 SQLite 不对这一列用索引。几乎所有岗位都满足 deleted_at IS NULL，
        # 走 (deleted_at, …) 索引再逐条回表，比直接扫表慢一倍多（2 万岗位 145ms vs 55ms）。
        row = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN score IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS passed, "
            "SUM(CASE WHEN status IN ('approved','applied','already') THEN 1 ELSE 0 END) AS confirmed, "
            "SUM(CASE WHEN status IN ('applied','already') THEN 1 ELSE 0 END) AS applied "
            "FROM jobs WHERE +deleted_at IS NULL",
            (int(threshold),),
        ).fetchone()
        return {key: int(row[key] or 0) for key in ("total", "scored", "passed", "confirmed", "applied")}

    def company_stats(self, threshold: int = 0, limit: int = 12) -> list[dict]:
        """按公司统计：这家公司在我们池子里挂了几个岗位、平均分多少、几个过线。

        为什么值得看：同一家公司批量挂一堆同类岗位，多半是外包 / 中介 / 培训机构，
        而不是真的在招人。它和岗位性质识别互补 —— 那边看 JD 话术，这边看数量与分数的组合。
        """
        # +deleted_at：一元加号让 SQLite 不对这一列用索引。几乎所有岗位都满足 deleted_at IS NULL，
        # 走 (deleted_at, …) 索引再逐条回表，比直接扫表慢一倍多（2 万岗位 145ms vs 55ms）。
        rows = self.conn.execute(
            "SELECT company, COUNT(*) AS jobs, "
            "ROUND(AVG(score), 1) AS avg_score, MAX(score) AS best, "
            "SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS passed "
            "FROM jobs WHERE +deleted_at IS NULL AND COALESCE(TRIM(company), '') <> '' "
            "GROUP BY company ORDER BY jobs DESC, avg_score DESC LIMIT ?",
            (int(threshold), int(limit)),
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            avg = row["avg_score"]
            out.append({
                "company": row["company"],
                "jobs": int(row["jobs"] or 0),
                "avg_score": None if avg is None else float(avg),
                "best": None if row["best"] is None else int(row["best"]),
                "passed": int(row["passed"] or 0),
            })
        return out

    def keyword_stats(self, threshold: int = 0, limit: int = 30) -> list[dict]:
        """按"采集时用的关键词"统计效果：多少个岗位、平均分、最高分、几个过线。

        数据来源是采集事件里记的 `城市/关键词`（猎聘/智联只有关键词），
        再和岗位的分数 join 起来 —— 于是能回答"我这个词到底值不值得采"。
        """
        # 先在 SQL 里按「采集事件的 detail」聚合（join 和计数交给 SQLite），
        # 再把 detail 归一成关键词合并 —— 各平台 detail 的格式不同（城市/词、词（第 N 批）），
        # 但不同的 detail 只有几十种，Python 只处理这几十行，而不是以前的「全部事件 × 全部岗位」。
        rows = self.conn.execute(
            "WITH first AS ("
            "  SELECT job_id, MIN(detail) AS detail FROM events"
            "  WHERE kind = 'collected' AND job_id IS NOT NULL GROUP BY job_id"
            ") "
            "SELECT f.detail AS detail, COUNT(*) AS jobs, COUNT(j.score) AS scored, "
            "SUM(j.score) AS total, MAX(j.score) AS best, "
            "SUM(CASE WHEN j.score >= ? THEN 1 ELSE 0 END) AS passed "
            # CROSS JOIN 固定连接顺序：先走采集事件、再按主键找岗位（让规划器自己选会反过来扫岗位表）
            "FROM first f CROSS JOIN jobs j ON j.id = f.job_id WHERE j.deleted_at IS NULL "
            "GROUP BY f.detail",
            (int(threshold),),
        ).fetchall()
        buckets: dict[str, dict] = {}
        for row in rows:
            keyword = _keyword_of(str(row["detail"] or ""))
            bucket = buckets.setdefault(keyword, {"keyword": keyword, "jobs": 0, "passed": 0,
                                                  "_scored": 0, "_total": 0, "best": None})
            bucket["jobs"] += int(row["jobs"] or 0)
            bucket["passed"] += int(row["passed"] or 0)
            bucket["_scored"] += int(row["scored"] or 0)
            bucket["_total"] += int(row["total"] or 0)
            if row["best"] is not None:
                bucket["best"] = max(int(row["best"]), bucket["best"] if bucket["best"] is not None else -1)
        out = []
        for bucket in buckets.values():
            scored, total = bucket.pop("_scored"), bucket.pop("_total")
            bucket["avg_score"] = round(total / scored, 1) if scored else None
            out.append(bucket)
        out.sort(key=lambda item: (item["avg_score"] is None, -(item["avg_score"] or 0), -item["jobs"]))
        return out[:limit]

    def list_recent_events(self, limit: int = 60, job_id_null_only: bool = False, offset: int = 0) -> list[dict]:
        """最近的全局事件（job_id 为空的那些是风控/系统事件）。"""
        sql = "SELECT id, job_id, kind, detail, created_at FROM events"
        args: list = []
        if job_id_null_only:
            sql += " WHERE job_id IS NULL"
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args += [int(limit), max(0, int(offset))]
        return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def count_system_events(self) -> int:
        """风控 / 系统事件（job_id 为空）一共多少条：记录页翻页用。"""
        return int(self.conn.execute("SELECT COUNT(*) FROM events WHERE job_id IS NULL").fetchone()[0])

    def count_events_since(self, kind: str, days: int = 7) -> int:
        start = (datetime.now() - timedelta(days=max(0, int(days) - 1))).strftime("%Y-%m-%d")
        return int(
            self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = ? AND date(created_at) >= ?",
                (kind, start),
            ).fetchone()[0]
        )

    def page_time_at_offset(self, platform: str, since: str, offset: int) -> str | None:
        """窗口内按时间倒序取第 ``offset`` 条（0 = 最新）的 time。"""
        row = self.conn.execute(
            "SELECT created_at FROM page_access WHERE platform = ? AND created_at >= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1 OFFSET ?",
            (platform, since, max(0, int(offset))),
        ).fetchone()
        return str(row["created_at"]) if row else None

    # ---------------- locks ----------------
    def set_lock(self, platform: str, reason: str, kind: str, until_iso: str) -> None:
        self.conn.execute(
            "INSERT INTO locks (platform, reason, kind, until) VALUES (?,?,?,?) "
            "ON CONFLICT(platform) DO UPDATE SET reason=excluded.reason, kind=excluded.kind, until=excluded.until",
            (platform, reason, kind, until_iso),
        )
        self.conn.commit()

    def get_lock(self, platform: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM locks WHERE platform = ?", (platform,)).fetchone()
        return dict(row) if row else None

    def clear_lock(self, platform: str) -> None:
        self.conn.execute("DELETE FROM locks WHERE platform = ?", (platform,))
        self.conn.commit()

    # ---------------- runtime kv（冷却 / 失败计数等短生命周期状态） ----------------
    def set_kv(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO runtime_kv (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_str()),
        )
        self.conn.commit()

    def get_kv(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM runtime_kv WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    # ---------------- interactions ----------------
    def upsert_interaction(self, key: str, platform: str, job_id: str | None,
                           hr_name: str, state: str, last_text: str, deeplink: str,
                           company: str = "") -> None:
        self.conn.execute(
            "INSERT INTO interactions (key, platform, job_id, hr_name, state, last_text, deeplink, seen_at, company) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state, last_text=excluded.last_text, "
            "seen_at=excluded.seen_at, job_id=excluded.job_id, "
            "company=COALESCE(NULLIF(excluded.company, ''), interactions.company)",
            (key, platform, job_id, hr_name, state, last_text, deeplink, now_str(), company),
        )
        self.conn.commit()

    def list_interactions(self, limit: int = 200) -> list[dict]:
        """带上岗位标题/公司 —— 界面那两列一直都在，但以前 SQL 里根本没查出来（一直显示空白）。"""
        rows = self.conn.execute(
            "SELECT i.key, i.platform, i.job_id, i.hr_name, i.state, i.last_text, "
            "i.deeplink, i.seen_at, j.title AS title, "
            "COALESCE(NULLIF(i.company, ''), j.company) AS company "
            "FROM interactions i LEFT JOIN jobs j ON j.id = i.job_id "
            "ORDER BY i.seen_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def count_interactions_by_states(self, states) -> int:
        """有多少个**不同岗位**处在这些状态（漏斗里的"进面试 / 拿到 offer"）。"""
        marks = ",".join("?" for _ in states)
        return int(
            self.conn.execute(
                f"SELECT COUNT(DISTINCT job_id) FROM interactions WHERE state IN ({marks})",
                tuple(states),
            ).fetchone()[0]
        )
