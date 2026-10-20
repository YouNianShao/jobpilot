"""前程无忧 / 猎聘 / 智联的 HR 进展监测：只读「我的投递」列表，不点岗位、不进任何会话。

这些平台的聊天列表还没有核验过，目前只读「我的投递」：页上每一条都写着进展
（已查看 / 感兴趣 / 邀面试 / 不合适…）。2026-10-16 在账号 B 上逐个平台核对过这些文字，
映射成统一的进展状态，和 BOSS 监测写同一张 interactions 表。

安全：每个平台一次只开 1 个页面（过同一道页面闸门）；只读文字、切筛选栏
（切栏只改列表显示，不发出任何东西）；进展只往前走不往回退。
"""

from __future__ import annotations

import re

from ..models import Job, MonitorResult, normalize
from ..safety.attention import release_page
from .base import DriverContext, explain_missing

# 各平台列表上的进展文字 → 统一状态（和 boss.STATE_RANK 同一套）；越靠前优先级越高
STATE_WORDS = [
    ("interview", ("邀面试", "面试邀约", "邀请面试", "邀请应聘")),
    ("rejected", ("暂不合适", "不合适")),
    ("replied", ("感兴趣", "有意向", "企业对你有意")),
    ("viewed", ("已查看", "被查看", "已查阅")),
]
STATE_RANK = {"viewed": 1, "replied": 2, "rejected": 3, "interview": 4, "interviewed": 5, "offer": 6}

# 列表里每一条 = 一个含日期的小块（三个平台都在每条后面写投递 / 状态时间）
JS_ROWS = r"""() => {
  const out = [], seen = new Set();
  // 不靠类名（各平台都不一样，前程无忧的行连 li / item 都不是）：任何一个「自己含日期、
  // 但没有哪个子块也含日期且够长」的元素就是一条记录
  for (const n of document.querySelectorAll('body *')) {
    const t = (n.innerText || '').replace(/\s+/g, ' ').trim();
    if (t.length < 12 || t.length > 400 || !/\d{4}-\d{2}-\d{2}/.test(t)) continue;
    if ([...n.children].some((c) => /\d{4}-\d{2}-\d{2}/.test(c.innerText || '') && (c.innerText || '').length > 30)) continue;
    if (seen.has(t)) continue;
    seen.add(t); out.push(t);
  }
  return out.slice(0, 200);
}"""


def state_of(row_text: str) -> str:
    for state, words in STATE_WORDS:
        if any(w in row_text for w in words):
            return state
    return ""


def match_rows(rows: list[str], jobs: list[Job]) -> list[tuple[Job, str, str]]:
    """把列表行对上已投递岗位：标题和公司都在同一行里才算（避免同名岗位串号）。"""
    found = []
    for job in jobs:
        # 猎聘采到的标题前面常带「招聘」、公司后面带行业 / 规模（「某品牌 日化2000-5000人」）：
        # 去掉这些再比，公司只比前 4 个字（列表里公司名常被截短）
        title = normalize(re.sub(r"^招聘", "", job.title or ""))
        company = normalize(re.split(r"\s", (job.company or "").strip())[0])[:4]
        if not title or not company:
            continue
        best: tuple[str, str] | None = None
        for row in rows:
            norm_row = normalize(row)
            if title in norm_row and company in norm_row:
                state = state_of(row)
                # 同一个岗位会在好几栏里出现（智联：投递成功栏 + 被查看栏）：取走得最远的那一条
                if state and (best is None or STATE_RANK.get(state, 0) > STATE_RANK.get(best[0], 0)):
                    best = (state, row[:160])
        if best:
            found.append((job, best[0], best[1]))
    return found


def write_observation(ctx: DriverContext, *, key: str, platform: str, job_id: str | None,
                      hr_name: str, state: str, text: str, deeplink: str, company: str = "",
                      unread: bool | None = None, msg_time: str | None = None,
                      known: dict[str, dict] | None = None) -> str:
    """把监测看到的一条写进 interactions；返回 "advanced" / "updated" / ""（没变化）。

    - 状态往前走了 → 整行更新，岗位已知时记一条 hr_reply / progress 事件（每次状态变化一条）
    - 状态没往前走（含「已经走得更远」）但最后一句 / 未读 / 列表时间变了 → 只更新这些，
      **保留更高的状态**。以前这种行直接跳过，HR 后来发的新消息永远写不进来（2026-10-20）
    """
    old = known.get(key) if known is not None else ctx.repo.get_interaction(key)
    old_state = (old or {}).get("state") or ""
    if STATE_RANK.get(old_state, 0) < STATE_RANK.get(state, 0):
        ctx.repo.upsert_interaction(
            key=key, platform=platform, job_id=job_id, hr_name=hr_name, state=state,
            last_text=text, deeplink=deeplink, company=company, unread=unread, msg_time=msg_time,
        )
        if job_id:
            kind = "hr_reply" if state in ("replied", "interview", "offer", "rejected") else "progress"
            ctx.repo.add_event(job_id, kind, f"{state} 监测：{platform}")
        _remember(known, key, state, text, unread, msg_time, job_id)
        return "advanced"
    changed = (
        (text or "") != ((old or {}).get("last_text") or "")
        or (unread is not None and bool(unread) != bool((old or {}).get("unread")))
        or (msg_time is not None and msg_time != ((old or {}).get("msg_time") or ""))
    )
    if not changed:
        return ""
    ctx.repo.upsert_interaction(
        key=key, platform=platform, job_id=job_id or (old or {}).get("job_id"), hr_name=hr_name,
        state=old_state, last_text=text, deeplink=deeplink, company=company,
        unread=unread, msg_time=msg_time,
    )
    _remember(known, key, old_state, text, unread, msg_time, job_id or (old or {}).get("job_id"))
    return "updated"


def _remember(known, key, state, text, unread, msg_time, job_id) -> None:
    """同一次监测里同一个 key 又出现一次（两栏都有这一条）时，按刚写进去的比，别重复记事件。"""
    if known is None:
        return
    row = dict(known.get(key) or {})
    row.update(state=state, last_text=text, job_id=job_id)
    if unread is not None:
        row["unread"] = int(bool(unread))
    if msg_time is not None:
        row["msg_time"] = msg_time
    known[key] = row


def monitor_records(ctx: DriverContext, *, platform: str, url: str, ready: str, label: str,
                    tabs: tuple[str, ...] = ()) -> MonitorResult:
    """打开「我的投递」，把列表上的进展写进 interactions（只读）。

    ``tabs``：要依次切过去看的筛选栏（智联默认那一栏不含「被查看 / 邀面试」）。切栏只是筛选列表。
    """
    result = MonitorResult()
    jobs = [j for j in ctx.repo.list_jobs(platform=platform, status="applied", limit=2000)]
    if not jobs:
        ctx.log(f"[{platform}] 还没有已投递的岗位，跳过监测")
        return result
    ctx.reserve("monitor")
    page = ctx.browser.open(url)
    try:
        if not page.wait_for(ready, timeout_ms=30000):
            explain_missing(page, f"[{platform}] {label}")
        ctx.stop.sleep(3)                         # 列表是异步渲染的
        rows = list(page.eval(JS_ROWS) or [])
        for tab in tabs:
            try:
                page.click(f'text="{tab}"', timeout_ms=4000)   # 只是切筛选栏（精确匹配文字）
            except Exception:  # noqa: BLE001 - 没有这一栏就算了
                continue
            ctx.stop.sleep(2.5)
            # 有的平台（前程无忧）行上不写状态，状态就是所在的筛选栏：把栏名补进这一栏的每一行
            rows += [f"{r} {tab}" for r in (page.eval(JS_ROWS) or []) if f"{r} {tab}" not in rows]
        result.checked = len(rows)
        known = ctx.repo.interactions_by_key(platform)
        for job, state, text in match_rows(rows, jobs):
            outcome = write_observation(
                ctx, key=f"{platform}:{job.id}", platform=platform, job_id=job.id, hr_name="",
                state=state, text=text, deeplink=url, company=job.company, known=known,
            )
            if outcome == "advanced":
                result.events[state] = result.events.get(state, 0) + 1
    finally:
        release_page(page)   # 验证 / 掉登录的那一页留给你处理
    ctx.log(f"[{platform}] 监测「我的投递」{result.checked} 条（只读列表，没打开任何岗位或会话）")
    return result



# ---------------- 智联「消息」聊天列表（2026-10-20 在账号 B 上核对过页面结构） ----------------
# https://i.zhaopin.com/im 左侧会话列表：.im-session-item 里有 __name / __company / __preview-text / __time / __badge。
# 只读列表上的文字，不点会话、不发任何东西。预览文字带 --muted 类 = 已读；__badge 有数字 = 未读条数。
# 列表上看不出最后一句是谁发的：同一句话出现在很多会话里（平台替我发的打招呼模板）就当成没有新进展。
ZP_IM_URL = "https://i.zhaopin.com/im"
ZP_JS_SESSIONS = r"""() => [...document.querySelectorAll('.im-session-item')].slice(0, 100).map((n) => {
  const t = (s) => { const x = n.querySelector(s); return x ? (x.textContent || '').replace(/\s+/g, ' ').trim() : ''; };
  const pv = n.querySelector('.im-session-item__preview-text');
  return { name: t('.im-session-item__name'), company: t('.im-session-item__company'), last: t('.im-session-item__preview-text'),
           time: t('.im-session-item__time'), badge: t('.im-session-item__badge'),
           muted: !!(pv && /--muted/.test(pv.className)) };
})"""


def classify_im_rows(rows: list[dict]) -> list[tuple[dict, str, bool]]:
    """会话行 -> [(行, 状态, 未读)]。模板话（≥3 个会话里一模一样）和空预览跳过；
    有关键词按 boss 的规则判 interview / rejected / offer；其余只有未读（没变灰）的算 replied，已读的普通话跳过。"""
    from collections import Counter

    from .boss import CHAT_STATE_RULES

    counts = Counter(r.get("last") or "" for r in rows)
    out = []
    for row in rows:
        text = (row.get("last") or "").strip()
        unread = bool(re.sub(r"\D", "", row.get("badge") or "")) and (row.get("badge") or "").strip() != "0"
        if not text or counts[text] >= 3:
            continue
        low = text.lower()
        state = next((s for s, words in CHAT_STATE_RULES if any(w in low for w in words)), "")
        if not state:
            # 已读（灰）的普通一句看不出是谁说的：不当成进展（宁可漏记，不把自己发的话记成「HR 回复」）
            if not (unread or not row.get("muted")):
                continue
            state = "replied"
        out.append((row, state, unread))
    return out


def monitor_zhaopin_chat(ctx: DriverContext, result: MonitorResult) -> None:
    """智联消息列表：按公司对上智联已投递岗位（公司在已投递里唯一才填 job_id），写进 interactions。"""
    jobs = ctx.repo.list_jobs(platform="zhaopin", status="applied", limit=2000)
    by_company: dict[str, list[Job]] = {}
    for job in jobs:
        by_company.setdefault(normalize(job.company or "")[:6], []).append(job)
    ctx.reserve("monitor")
    page = ctx.browser.open(ZP_IM_URL)
    try:
        if not page.wait_for(".im-session-item", timeout_ms=30000):
            explain_missing(page, "[zhaopin] 消息列表")
            return
        ctx.stop.sleep(2)
        rows = list(page.eval(ZP_JS_SESSIONS) or [])
        known = ctx.repo.interactions_by_key("zhaopin")
        for row, state, unread in classify_im_rows(rows):
            company = (row.get("company") or "").strip()
            hits = by_company.get(normalize(company)[:6], []) if company else []
            job = hits[0] if len(hits) == 1 else None
            key = f"zhaopin:{job.id}" if job else f"zhaopin:im:{row.get('name', '')}|{company}"
            outcome = write_observation(
                ctx, key=key, platform="zhaopin", job_id=job.id if job else None, hr_name=row.get("name") or "",
                state=state, text=row.get("last") or "", deeplink=ZP_IM_URL, company=company or (job.company if job else ""),
                unread=unread, msg_time=row.get("time") or "", known=known,
            )
            if outcome == "advanced":
                result.events[state] = result.events.get(state, 0) + 1
        result.checked += len(rows)
        ctx.log(f"[zhaopin] 监测消息列表 {len(rows)} 个会话（只读列表，没打开任何会话）")
    finally:
        release_page(page)
