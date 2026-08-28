from __future__ import annotations

import random
import time
from typing import Any

from jobpilot.ai.scorer import load_resume, score_job
from jobpilot.browser import Browser
from jobpilot.prefilter import prefilter_job
from jobpilot.db import (
    count_applied_today,
    get_conn,
    log_history,
    pending_apply,
    pending_scoring,
    set_score,
    set_status,
)
from jobpilot.platforms import get_adapter
from jobpilot.monitor import run_monitor
from jobpilot.control import yield_point
from jobpilot.progress import done as prog_done
from jobpilot.progress import error as prog_error
from jobpilot.progress import job as prog_job
from jobpilot.progress import phase as prog_phase
from jobpilot.progress import start as prog_start
from jobpilot.progress import blocked as prog_blocked
from jobpilot.progress import score_failed as prog_score_failed


def run_pipeline(cfg: dict[str, Any], platform: str = "51job", mode: str = "full") -> dict[str, Any]:
    """执行采集/评分/投递流水线，返回汇总结果。被 CLI 与 Web 共用。"""
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
            resume = load_resume(cfg.get("profile", {}).get("resume_path", ""))
            threshold = int(cfg.get("scoring", {}).get("threshold") or 60)
            conn = get_conn()
            pend = pending_scoring(conn, platform)
            total = len(pend)
            prog_phase("score", total, 0, "评分中…")
            count = 0
            prefiltered = 0
            fail_count = 0
            fail_reason = ""
            profile_cfg = cfg.get("profile", {}) or {}
            for i, job in enumerate(pend, 1):
                yield_point()
                prog_job(i, total, f"{job['title']}@{job['company']}")
                # 硬规则预筛（实习/黑名单/薪资），命中则跳过 AI 评分
                rejected, preason = prefilter_job(dict(job), profile_cfg)
                if rejected:
                    set_status(conn, job["id"], "rejected")
                    log_history(conn, job["id"], "prefilter_rejected", preason)
                    prefiltered += 1
                    continue
                score, reason = score_job(cfg, dict(job), resume)
                set_score(conn, job["id"], score, reason)
                if score >= threshold:
                    set_status(conn, job["id"], "approved")
                count += 1
                # reason 以"评分失败"/"未配置"开头代表 AI 调用失败（非真实低分）
                if reason and str(reason).startswith(("评分失败", "未配置")):
                    fail_count += 1
                    if not fail_reason:
                        fail_reason = str(reason)
                    # 评分失败回退 new 状态，下次运行可重新评分（否则失败岗位永久滞留 scored）
                    set_status(conn, job["id"], "new")
            conn.close()
            # 全部评分失败 → 通常是 AI Key 失效/未配置，标记供前端醒目提示
            if total > 0 and fail_count == total:
                prog_score_failed(True, fail_reason)
            result["score"] = count
            result["prefiltered"] = prefiltered

        if mode in ("apply", "full") and not getattr(adapter, "collect_only", False):
            throttle = cfg.get("throttle", {}) or {}
            daily_limit = int(throttle.get("daily_limit", 30) or 30)
            interval_min = float(throttle.get("interval_min", 60) or 60)
            interval_max = float(throttle.get("interval_max", 180) or 180)
            threshold = int(cfg.get("scoring", {}).get("threshold") or 60)
            conn = get_conn()
            applied_today = count_applied_today(conn)
            remaining = daily_limit - applied_today
            pend = pending_apply(conn, platform, threshold)
            total = min(len(pend), remaining) if remaining > 0 else 0
            prog_phase("apply", total, 0, f"投递中（今日 {applied_today}/{daily_limit}）")
            count = 0
            consecutive_blocked = 0
            if remaining <= 0:
                print(f"[throttle] 今日已投递 {applied_today} 个，已达上限 {daily_limit}，跳过投递。")
            for i, job in enumerate(pend, 1):
                if remaining <= 0:
                    print(f"[throttle] 已达今日上限 {daily_limit}（已投递 {applied_today}），停止。")
                    break
                yield_point()
                prog_job(i, total, f"{job['title']}@{job['company']}")
                status = adapter.apply(cfg, browser, dict(job))
                set_status(conn, job["id"], status)
                log_history(conn, job["id"], "apply", status)
                count += 1
                if status == "applied":
                    applied_today += 1
                    remaining -= 1
                    consecutive_blocked = 0
                    # 仅成功投递后做节流等待，模拟真人节奏，规避风控
                    if remaining > 0:
                        wait = random.uniform(interval_min, interval_max)
                        print(f"[throttle] 投递成功，等待 {wait:.0f}s 后继续（今日 {applied_today}/{daily_limit}）")
                        prog_phase("apply", total, i, f"等待 {wait:.0f}s 后继续（今日 {applied_today}/{daily_limit}）")
                        time.sleep(wait)
                elif status == "unsupported":
                    # 投递被拦截（点击无响应/需登录/未定位），连续多次则判定平台反爬，提前结束
                    consecutive_blocked += 1
                    if consecutive_blocked >= 3:
                        msg = "连续 3 次投递被 51job 拦截（点击无响应或需登录），疑似平台反爬限制，已停止自动投递。"
                        print(f"[apply] {msg}")
                        result["apply_blocked"] = True
                        prog_phase("apply", total, i, "投递被拦截，已停止")
                        prog_blocked(True)
                        break
                else:
                    consecutive_blocked = 0
            conn.close()
            result["apply"] = count
        elif mode in ("apply", "full"):
            # 采集-only 平台：跳过自动投递，提示用户手动
            result["apply"] = 0
            result["apply_note"] = (
                f"{adapter.name} 仅支持采集，自动投递被平台反爬封锁；"
                f"请在「岗位列表」点岗位直达链接到{adapter.name}内手动投递。"
            )

        prog_done(f"完成：{result}")
    except Exception as e:  # noqa: BLE001
        prog_error(str(e))
        raise

    if mode == "monitor":
        prog_start(mode, "monitor", 1, "监测中…")
        result["monitor"] = run_monitor(cfg, browser)
        prog_done(f"监测完成：{result.get('monitor')}")

    return result
