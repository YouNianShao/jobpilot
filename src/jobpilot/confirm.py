"""人工确认门：pending_confirm → approved / rejected。

默认开启（confirm.required=true）。评分通过的岗位先进入 pending_confirm，
用户在仪表盘确认后才允许自动投递，避免"评分一过就自动发出去"。
"""
from __future__ import annotations

from typing import Any

from jobpilot.db import (
    count_status,
    get_conn,
    jobs_by_status,
    log_history,
    set_status,
)


def _target_jobs(conn, platform: str, job_ids: list[str] | None) -> list[dict]:
    return jobs_by_status(conn, "pending_confirm", platform=platform, job_ids=job_ids)


def approve(
    cfg: dict[str, Any],
    platform: str,
    *,
    job_ids: list[str] | None = None,
    all_pending: bool = True,
) -> dict[str, Any]:
    """确认投递：pending_confirm → approved。"""
    conn = get_conn()
    try:
        jobs = _target_jobs(conn, platform, None if all_pending else job_ids)
        for job in jobs:
            set_status(conn, job["id"], "approved")
            log_history(conn, job["id"], "confirmed", "人工确认投递")
        return {"confirmed": len(jobs), "job_ids": [job["id"] for job in jobs]}
    finally:
        conn.close()


def reject(
    cfg: dict[str, Any],
    platform: str,
    *,
    job_ids: list[str] | None = None,
    all_pending: bool = True,
) -> dict[str, Any]:
    """拒绝投递：pending_confirm → rejected。"""
    conn = get_conn()
    try:
        jobs = _target_jobs(conn, platform, None if all_pending else job_ids)
        for job in jobs:
            set_status(conn, job["id"], "rejected")
            log_history(conn, job["id"], "confirm_rejected", "人工拒绝投递")
        return {"rejected": len(jobs), "job_ids": [job["id"] for job in jobs]}
    finally:
        conn.close()


def summary(conn, platform: str | None = None) -> dict[str, Any]:
    return {
        "pending_confirm": count_status(conn, "pending_confirm", platform),
        "approved": count_status(conn, "approved", platform),
    }