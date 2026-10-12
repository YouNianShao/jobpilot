from __future__ import annotations

import random
import time
from typing import Any

from jobpilot.ai.scorer import load_resume, score_job
from jobpilot.browser import Browser
from jobpilot.prefilter import prefilter_job
from jobpilot.db import (
    count_applied_today,
    count_status,
    get_conn,
    log_history,
    pending_apply,
    pending_scoring,
    set_score,
    set_status,
)
from jobpilot.platforms import get_adapter
from jobpilot.monitor import run_monitor
from jobpilot import safety
from jobpilot.runner import apply_platform
from jobpilot.scoring import score_pending
from jobpilot.greeting import ensure_greetings
from jobpilot.confirm import approve as confirm_approve, reject as confirm_reject
from jobpilot.control import yield_point
from jobpilot.progress import done as prog_done
from jobpilot.progress import error as prog_error
from jobpilot.progress import job as prog_job
from jobpilot.progress import phase as prog_phase
from jobpilot.progress import start as prog_start
from jobpilot.progress import blocked as prog_blocked
from jobpilot.progress import score_failed as prog_score_failed


def _confirm_required(cfg: dict) -> bool:
    return bool((cfg.get("confirm", {}) or {}).get("required", True))


def _pending_confirm_count(platform: str) -> int:
    conn = get_conn()
    try:
        return count_status(conn, "pending_confirm", platform)
    finally:
        conn.close()


def run_pipeline(
    cfg: dict[str, Any],
    platform: str = "51job",
    mode: str = "full",
    force: bool = False,
    reject: bool = False,
) -> dict[str, Any]:
    """执行采集/评分/招呼语/确认/投递流水线，返回汇总结果。被 CLI 与 Web 共用。

    ``force=True`` 跳过投递时间窗、随机休息与风险冷却（仅调试或手动兜底用）。
    ``reject=True`` 配合 ``mode='confirm'`` 使用：拒绝而非确认待确认岗位。
    """
    adapter = get_adapter(platform)
    if not adapter:
        return {"error": f"未知平台: {platform}"}

    browser = Browser(cfg["browser"]["proxy_url"])
    result: dict[str, Any] = {"platform": adapter.name, "mode": mode}

    try:
        if mode in ("collect", "full"):
            prog_start(mode, "collect", 1, "采集中…")
            result["collect"] = adapter.collect(cfg, browser)

        if mode in ("score", "full"):
            result.update(score_pending(cfg, platform))

        if mode in ("greet", "full"):
            result.update(ensure_greetings(cfg, platform))

        if mode == "confirm":
            if reject:
                result.update(confirm_reject(cfg, platform))
            else:
                result.update(confirm_approve(cfg, platform))

        if mode in ("apply", "full"):
            pending = _pending_confirm_count(platform) if mode == "full" else 0
            if mode == "full" and _confirm_required(cfg) and pending:
                result["apply"] = 0
                result["apply_note"] = (
                    f"{pending} 个岗位待人工确认，已跳过自动投递；请在仪表盘确认后点「投递」"
                )
                print(f"[confirm] {result['apply_note']}")
            else:
                result.update(apply_platform(cfg, browser, adapter, force=force))

        prog_done(f"完成：{result}")
    except safety.PlatformRiskError as e:  # noqa: PERF203
        # 采集阶段命中风控（投递阶段由 runner 就地处理）：停止当前平台并进入冷却
        if e.kind != "login_required":
            safety.set_lock(
                f"{adapter.name} {e.label}",
                (cfg.get("safety", {}) or {}).get("risk_lock_minutes", 30),
                kind=e.kind,
            )
        result["apply_blocked"] = e.kind != "login_required"
        result["risk"] = e.kind
        result["note"] = f"{adapter.name} 触发风控：{e.label}"
        prog_blocked(True)
        prog_error(result["note"])
    except Exception as e:  # noqa: BLE001
        prog_error(str(e))
        raise

    if mode == "monitor":
        if not adapter.supports("monitor"):
            result["monitor"] = {"skipped": f"{adapter.name} 暂无 HR 监测适配器"}
            prog_done(result["monitor"]["skipped"])
        else:
            prog_start(mode, "monitor", 1, "监测中…")
            result["monitor"] = run_monitor(cfg, browser, platform=adapter.key)
            prog_done(f"监测完成：{result.get('monitor')}")

    return result
