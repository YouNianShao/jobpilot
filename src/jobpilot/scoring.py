"""AI 评分编排：预筛 → 并发评分 → 串行入库。

- 预筛命中直接 rejected，不消耗 AI 额度。
- 支持并发调用 AI（scoring.concurrency，1-3），数据库仍串行写入。
- 评分失败回退 new，下一轮可重新评分。
- 通过评分的岗位进入 pending_confirm（人工确认门）或 approved（关闭确认时）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from jobpilot.ai.scorer import load_resume, score_job
from jobpilot.control import yield_point
from jobpilot.db import (
    count_status,
    get_conn,
    log_history,
    pending_scoring,
    set_score,
    set_status,
)
from jobpilot.prefilter import prefilter_job
from jobpilot.progress import job as prog_job
from jobpilot.progress import phase as prog_phase
from jobpilot.progress import score_failed as prog_score_failed

_FAIL_PREFIXES = ("评分失败", "未配置")


def _clamp_int(value: Any, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = low
    return max(low, min(parsed, high))


def _passed_status(cfg: dict) -> str:
    """评分通过后的落库状态：开启人工确认门则 pending_confirm，否则直接 approved。"""
    required = bool((cfg.get("confirm", {}) or {}).get("required", True))
    return "pending_confirm" if required else "approved"


def score_pending(cfg: dict[str, Any], platform: str) -> dict[str, Any]:
    """对某平台的待评分岗位执行预筛 + AI 评分。返回统计汇总。"""
    conn = get_conn()
    try:
        return _score_pending(cfg, platform, conn)
    finally:
        conn.close()


def _score_pending(cfg: dict[str, Any], platform: str, conn) -> dict[str, Any]:
    resume = load_resume((cfg.get("profile", {}) or {}).get("resume_path", ""))
    threshold = int((cfg.get("scoring", {}) or {}).get("threshold") or 60)
    profile_cfg = cfg.get("profile", {}) or {}
    pend = pending_scoring(conn, platform)
    total = len(pend)
    prog_phase("score", total, 0, "评分中…")

    prefiltered = 0
    ready: list[dict] = []
    for job in pend:
        yield_point()
        rejected, preason = prefilter_job(dict(job), profile_cfg)
        if rejected:
            set_status(conn, job["id"], "rejected")
            log_history(conn, job["id"], "prefilter_rejected", preason)
            prefiltered += 1
            continue
        ready.append(dict(job))

    results: list[tuple[str, int, str]] = []
    concurrency = _clamp_int((cfg.get("scoring", {}) or {}).get("concurrency", 1), 1, 3)
    if concurrency <= 1:
        for i, job in enumerate(ready, 1):
            yield_point()
            prog_job(i, total, f"{job['title']}@{job['company']}")
            score, reason = score_job(cfg, job, resume)
            results.append((job["id"], score, reason))
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = {pool.submit(score_job, cfg, job, resume): job for job in ready}
            for fut in as_completed(futures):
                job = futures[fut]
                try:
                    score, reason = fut.result()
                except Exception as exc:  # noqa: BLE001
                    score, reason = 0, f"评分失败: {exc}"
                done += 1
                prog_job(done, total, f"{job['title']}@{job['company']}")
                results.append((job["id"], score, reason))

    passed_status = _passed_status(cfg)
    scored = 0
    failed = 0
    fail_reason = ""
    for job_id, score, reason in results:
        set_score(conn, job_id, score, reason)
        if score >= threshold:
            set_status(conn, job_id, passed_status)
            scored += 1
        else:
            set_status(conn, job_id, "filtered")
        if reason and str(reason).startswith(_FAIL_PREFIXES):
            failed += 1
            if not fail_reason:
                fail_reason = str(reason)
            # 评分失败回退 new，避免永久滞留
            set_status(conn, job_id, "new")

    if results and failed == len(results):
        prog_score_failed(True, fail_reason)

    return {
        "score": scored,
        "prefiltered": prefiltered,
        "failed": failed,
        "fail_reason": fail_reason,
        "pending_confirm": count_status(conn, "pending_confirm", platform),
        "scored_total": len(results),
    }