from __future__ import annotations

import json
import re
import time
from typing import Any

from jobpilot.ai.scorer import generate_text, load_resume
from jobpilot.browser import Browser
from jobpilot.control import yield_point
from jobpilot.db import (
    get_conn,
    get_hr_interactions,
    log_history,
    match_job_by_title_company,
    update_job_hr,
    upsert_hr_interaction,
)
from jobpilot.progress import (
    done as prog_done,
    error as prog_error,
    job as prog_job,
    phase as prog_phase,
    start as prog_start,
)

MY_APPLY_URL = "https://i.51job.com/userset/my_apply.php?lang=c"

# 逐个申请项提取 HR 信息（基于真实 DOM：.li.l1 / .zhn.gt / .gs / .at / .xz / span 活跃标签）
JS_MONITOR = r"""
(() => {
  const items = Array.from(document.querySelectorAll('.li.l1'));
  const out = [];
  for (const it of items) {
    const link = it.querySelector('.zhn.gt');
    const href = link ? link.getAttribute('href') : '';
    const title = link ? (link.innerText||'').trim() : '';
    const comp = (it.querySelector('.gs')||{}).innerText || '';
    const ats = Array.from(it.querySelectorAll('.at')).map(e=>e.innerText.trim()).filter(Boolean);
    const hrName = ats[0]||'';
    const hrTitle = ats.slice(1).join(' ');
    const salary = (it.querySelector('.xz')||{}).innerText || '';
    const spans = Array.from(it.querySelectorAll('span')).map(e=>e.innerText.trim()).filter(Boolean);
    const activity = spans.filter(s=>/回复|活跃|在线|刚刚|分钟前|小时前|昨天|今天/.test(s)).join(' ');
    // status_text：innerText 会丢失部分状态时间线/隐藏元素，这里额外收集状态容器文本
    const statusEls = it.querySelectorAll('.state,.status,.tips,.ilabel,[class*=state],[class*=status],[class*=tip],[class*=State],[class*=Status]');
    const statusExtra = Array.from(statusEls).map(e=>(e.innerText||e.textContent||'')).join(' ');
    const full = ((it.innerText||'') + ' ' + statusExtra).replace(/\s+/g,' ');
    let appId='';
    const m = (href||'').match(/all[/](\d+)\.html/);
    if (m) appId = m[1];
    out.push({app_id:appId, title, company:comp, hr_name:hrName, hr_title:hrTitle,
              salary, activity, status_text:full, deeplink:href});
  }
  return JSON.stringify(out);
})()
"""

JS_NEXT = r"""
(() => {
  const btns = Array.from(document.querySelectorAll('a,button')).filter(e=>/下一页/.test(e.innerText||''));
  if (!btns.length) return JSON.stringify({ok:false});
  btns[0].click();
  return JSON.stringify({ok:true});
})()
"""

# 状态优先级（从高到低）与对应事件
STATE_RULES = [
    ("interview", "interview_invite", ["邀面试", "面试邀请", "邀您面试", "面试邀约"]),
    ("rejected", "hr_rejected", ["不合适", "不匹配", "不考虑", "拒绝", "暂不合适", "已招满", "岗位关闭", "未通过"]),
    ("interested", "hr_interested", ["感兴趣"]),
    ("replied", "hr_replied", ["回复", "已回复", "看过你的消息"]),
    ("viewed", "hr_viewed", ["已查收", "已查阅", "已查看", "查阅", "查看了你的简历"]),
]

# 仅这些状态写入 history 时间线（避免 viewed/interested 刷屏）
LOGGED_EVENTS = {"interview_invite", "hr_replied", "hr_rejected"}


def _detect_state(status_text: str) -> tuple[str | None, str | None]:
    t = status_text or ""
    for state, event, kws in STATE_RULES:
        if any(k in t for k in kws):
            return state, event
    return None, None


def _in_window(window: str) -> bool:
    m = re.match(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", window or "")
    if not m:
        return True
    now = time.localtime()
    start = int(m.group(1)) * 60 + int(m.group(2))
    end = int(m.group(3)) * 60 + int(m.group(4))
    cur = now.tm_hour * 60 + now.tm_min
    return start <= cur <= end


def _parse_apps(raw: Any) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def run_monitor(cfg: dict[str, Any], browser: Browser, max_pages: int = 5) -> dict[str, Any]:
    """监测 51job 已投递项目的 HR 互动，返回汇总。被 CLI / Web / 定时任务共用。"""
    monitor_cfg = cfg.get("monitor", {}) or {}
    if not monitor_cfg.get("enabled", True):
        return {"skipped": "monitor disabled"}
    if not _in_window(monitor_cfg.get("working_hours", "09:00-21:00")):
        return {"skipped": "outside working hours"}

    conn = get_conn()
    tid = browser.new_tab(MY_APPLY_URL)
    if not tid:
        conn.close()
        return {"error": "无法打开我的申请页"}
    try:
        time.sleep(4)
        browser.wait_for_load(tid, 8)
        info = browser.info(tid) or {}
        url = (info.get("url") or "").lower()
        if "login" in url or "passport" in url:
            conn.close()
            return {"error": "51job 登录态失效，请重新登录后重试"}

        apps: list[dict] = []
        for _ in range(max_pages):
            yield_point()
            raw = browser.evaluate(tid, JS_MONITOR)
            apps += _parse_apps(raw)
            clicked = browser.evaluate(tid, JS_NEXT)
            if isinstance(clicked, dict) and clicked.get("ok"):
                time.sleep(2)
                browser.wait_for_load(tid, 6)
            else:
                break

        # 去重（优先 app_id，否则按 标题+公司）
        seen: dict[Any, dict] = {}
        for a in apps:
            key = a.get("app_id") or (a.get("title"), a.get("company"))
            if key:
                seen[key] = a
        apps = list(seen.values())

        total = len(apps)
        prog_start("monitor", "monitor", total, "监测中…")
        summary: dict[str, Any] = {"checked": total, "events": {}, "updated": 0}
        existing = {r["app_id"]: r for r in get_hr_interactions(conn) if r["app_id"]}

        for i, a in enumerate(apps, 1):
            yield_point()
            detect_text = (a.get("status_text", "") + " " + a.get("activity", ""))
            state, event = _detect_state(detect_text)
            prev = existing.get(a.get("app_id"))
            prev_state = prev["hr_state"] if prev else None
            job_id = match_job_by_title_company(conn, a.get("title", ""), a.get("company", ""))
            rec = {
                "app_id": a.get("app_id") or "",
                "title": a.get("title", ""),
                "company": a.get("company", ""),
                "hr_name": a.get("hr_name", ""),
                "hr_title": a.get("hr_title", ""),
                "salary": a.get("salary", ""),
                "activity": a.get("activity", ""),
                "hr_state": state or "",
                "deeplink": a.get("deeplink", ""),
                "last_event": event or "",
                "job_id": job_id,
            }
            upsert_hr_interaction(conn, rec)
            if job_id:
                update_job_hr(conn, job_id, state or "", a.get("hr_name", ""), a.get("hr_title", ""), a.get("deeplink", ""))
            if event and event in LOGGED_EVENTS and (prev is None or prev_state != state):
                log_history(conn, job_id, event, f"{a.get('company','')} - {a.get('title','')} : {state}")
                summary["events"][event] = summary["events"].get(event, 0) + 1
            summary["updated"] += 1
            prog_job(i, total, f"{a.get('company','')} - {a.get('title','')}")
            time.sleep(1.5)  # 节流：避免过快触发风控

        prog_done(f"监测完成，检查 {total} 个申请，事件 {summary['events']}")
        conn.close()
        return summary
    except Exception as e:  # noqa: BLE001
        prog_error(str(e))
        conn.close()
        return {"error": str(e)}
    finally:
        if tid:
            browser.close_tab(tid)


DRAFT_SYSTEM = (
    "你是求职助手。用户会把 HR 发来的一条消息原文贴给你，并附上自己的简历要点与应聘岗位。"
    "请据此起草一条礼貌、自然、像真人在手机上打的回复（中文，30-100字）。"
    "不要捏造经历；若 HR 要简历/作品集，回复中表达会尽快发送；若是约定面试时间，积极配合。"
    "只输出回复文本，不要任何标记。"
)


def draft_reply(cfg: dict[str, Any], hr_text: str, job_title: str = "", company: str = "") -> str:
    """根据 HR 原话 + 简历 + 岗位，生成一条建议回复（手动发送，不自动代发）。"""
    resume = load_resume(cfg.get("profile", {}).get("resume_path", ""))
    user = f"""# HR 原话
{hr_text}

# 应聘岗位
{company} - {job_title}

# 我的简历要点
{resume[:1500] if resume else '（未配置简历，请基于岗位通用礼貌回复）'}"""
    out = generate_text(cfg, DRAFT_SYSTEM, user, temperature=0.5)
    return out or "（生成失败，请检查 AI Key 或稍后重试）"
