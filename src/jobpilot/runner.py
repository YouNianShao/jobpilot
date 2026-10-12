"""投递编排：时间窗、随机休息、风险冷却、渐进退避与节流。

从 pipeline 中抽出，便于单测与复用；pipeline 只按 mode 调用。
护栏顺序：平台能力 → 风险冷却锁 → 投递时间窗 → 随机休息 → 每日上限 → 逐个投递。
"""
from __future__ import annotations

import random
from typing import Any

from jobpilot import safety
from jobpilot.control import sleep_checked, yield_point
from jobpilot.db import (
    count_applied_today,
    get_conn,
    log_history,
    pending_apply,
    set_status,
)
from jobpilot.platforms.base import BaseAdapter
from jobpilot.progress import blocked as prog_blocked
from jobpilot.progress import job as prog_job
from jobpilot.progress import phase as prog_phase


def _lock_and_report(adapter: BaseAdapter, reason: str, kind: str, cfg: dict, conn, out: dict) -> None:
    minutes = (cfg.get("safety", {}) or {}).get("risk_lock_minutes", 30)
    safety.set_lock(f"{adapter.name} {reason}", minutes, kind=kind)
    log_history(conn, None, "apply_blocked", f"{adapter.name}: {reason}")
    out["apply_blocked"] = True
    out["apply_note"] = f"{adapter.name} 触发风控（{reason}），已锁定 {minutes} 分钟"
    prog_blocked(True)
    print(f"[safety] {out['apply_note']}")


def apply_platform(
    cfg: dict[str, Any],
    browser: Any,
    adapter: BaseAdapter,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """对单个平台执行自动投递，返回 {'apply': 成功数, ...} 汇总。

    ``force=True`` 跳过风险冷却/投递时间窗/随机休息（仅调试或手动兜底用）。
    """
    out: dict[str, Any] = {"apply": 0}
    prog_blocked(False)  # 新一轮投递先清掉上一轮遗留的"被拦截"红条
    if not adapter.supports("apply"):
        out["apply_note"] = (
            f"{adapter.name} 仅支持采集，自动投递已跳过；"
            f"请在「岗位列表」点岗位直达链接到{adapter.name}内手动投递。"
        )
        return out

    throttle = cfg.get("throttle", {}) or {}
    conn = get_conn()
    try:
        if not force:
            lock = safety.get_lock()
            if lock:
                out["apply_blocked"] = True
                out["apply_note"] = (
                    f"{adapter.name} 处于风险冷却：{lock.get('reason') or lock.get('kind', '')}"
                    f"（约 {lock.get('remaining_minutes', 0)} 分钟后可重试）"
                )
                prog_blocked(True)
                return out
            if not safety.in_send_window(throttle.get("send_windows")):
                out["apply_note"] = "当前不在投递时间窗内，已跳过自动投递"
                log_history(conn, None, "apply_skipped", "outside_send_window")
                return out
            if safety.should_take_day_off(throttle.get("day_off_probability", 0)):
                out["apply_note"] = "今日随机休息（防检测），已跳过自动投递"
                log_history(conn, None, "apply_skipped", "day_off")
                return out

        daily_limit = int(throttle.get("daily_limit", 30) or 30)
        interval_min = float(throttle.get("interval_min", 60) or 60)
        interval_max = float(throttle.get("interval_max", 180) or 180)
        max_blocked = max(int(throttle.get("max_consecutive_blocked", 3) or 3), 1)
        threshold = int(cfg.get("scoring", {}).get("threshold") or 60)

        applied_today = count_applied_today(conn)
        remaining = daily_limit - applied_today
        if remaining <= 0:
            out["apply_note"] = f"今日已投递 {applied_today} 个，已达上限 {daily_limit}"
            print(f"[throttle] {out['apply_note']}，跳过投递。")
            return out

        pend = pending_apply(conn, adapter.key, threshold)
        total = min(len(pend), remaining)
        prog_phase("apply", total, 0, f"投递中（今日 {applied_today}/{daily_limit}）")

        count = 0
        consecutive_blocked = 0
        backoff = safety.ProgressiveBackoff()

        for i, job in enumerate(pend, 1):
            if remaining <= 0:
                print(f"[throttle] 已达今日上限 {daily_limit}（已投递 {applied_today}），停止。")
                break
            yield_point()
            prog_job(i, total, f"{job['title']}@{job['company']}")

            try:
                status = adapter.apply(cfg, browser, dict(job))
            except safety.PlatformRiskError as exc:
                if exc.kind == "login_required":
                    out["apply_note"] = f"{adapter.name} 登录态失效，请在该 Chrome 窗口重新登录后重试"
                    print(f"[apply] {out['apply_note']}")
                    break
                _lock_and_report(adapter, exc.label, exc.kind, cfg, conn, out)
                break

            set_status(conn, job["id"], status)
            log_history(conn, job["id"], "apply", status)
            count += 1

            if status == "applied":
                backoff.record_success()
                consecutive_blocked = 0
                applied_today += 1
                remaining -= 1
                if remaining > 0:
                    wait = safety.jitter(random.uniform(interval_min, interval_max))
                    print(f"[throttle] 投递成功，等待 {wait:.0f}s 后继续（今日 {applied_today}/{daily_limit}）")
                    prog_phase(
                        "apply", total, i,
                        f"等待 {wait:.0f}s 后继续（今日 {applied_today}/{daily_limit}）",
                    )
                    sleep_checked(wait)
            elif status in ("unsupported", "failed"):
                consecutive_blocked += 1
                if consecutive_blocked >= max_blocked:
                    _lock_and_report(
                        adapter,
                        f"连续 {max_blocked} 次投递被拦截",
                        "blocked",
                        cfg, conn, out,
                    )
                    break
                pause = min(backoff.record_error(), 120.0)
                print(
                    f"[apply] {adapter.name} 投递未成功（{status}），"
                    f"退避 {pause:.0f}s（连续 {consecutive_blocked}/{max_blocked}）"
                )
                sleep_checked(pause)
            else:
                backoff.record_success()
                consecutive_blocked = 0
                if status == "need_login":
                    out["apply_note"] = f"{adapter.name} 登录态失效，请重新登录后重试"
                    print(f"[apply] {out['apply_note']}")
                    break
                if status in ("need_resume", "need_setup"):
                    out["apply_note"] = f"{adapter.name} 需要先在平台完善简历/城市设置"
                    print(f"[apply] {out['apply_note']}，已停止本平台自动投递")
                    break

        out["apply"] = count
        return out
    finally:
        conn.close()