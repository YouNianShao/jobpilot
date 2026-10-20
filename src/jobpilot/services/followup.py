"""自动跟进：投递后长期无回复 → 生成一条简短的跟进话术。

设计要点（BossHunter 的幂等判断不够严，这里补上）：
- 已经生成过 N 次就不再生成（`follow_up` 事件计数）
- 两次跟进之间强制最小间隔（`follow_up_at`）
- 岗位一旦有 HR 回复（`replied` / `hr_reply` 事件）就退出跟进队列
- 纯文本生成，不碰浏览器 → 不涉及平台风控
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..ai.prompting import render, version
from ..models import Status
from .context import ServiceContext

PROMPT = "follow_up.v1.md"
FOLLOW_UP_SCHEMA = {
    "type": "object",
    "required": ["message"],
    "properties": {"message": {"type": "string"}},
}
_TS = "%Y-%m-%d %H:%M:%S"


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(value), _TS)
    except (TypeError, ValueError):
        return None


def _read_resume(ctx: ServiceContext) -> str:
    # 走统一入口：以前这里直接 read_text，换成 Word / PDF 简历就会读出乱码或直接抛错
    text, _problem = ctx.resume_text()
    return text


# 记过这些进展就说明 HR 已经有动静，不该再发跟进；「已查看」不算回复，照常跟进
_ANSWERED_STATES = frozenset({"replied", "interview", "interviewed", "offer", "rejected"})


def classify(ctx: ServiceContext, job, now: datetime | None = None) -> dict:
    """一个已投递岗位在跟进队列里的位置：due / waiting / answered / done / unknown。

    界面要用它解释"为什么这个岗位还没到跟进"，所以除了结论还带上投递时间和到期时间。
    """
    cfg = ctx.config.follow_up
    now = now or datetime.now()
    applied_at = _parse(ctx.repo.last_event_at(job.id, "applied"))
    info = {"applied_at": applied_at.strftime(_TS) if applied_at else None, "due_at": None}
    if applied_at is None:
        # 没有投递事件（比如旧数据直接改的状态）就算不出到期时间
        return {**info, "state": "unknown"}
    # 有回复就不再跟进：手动「记录进展」写的是 interactions 表，不是 replied 事件，两边都要看
    answered = (ctx.repo.count_events_for_job(job.id, "replied")
                or ctx.repo.count_events_for_job(job.id, "hr_reply")
                or ctx.repo.job_interaction_states(job.id) & _ANSWERED_STATES)
    if answered:
        return {**info, "state": "answered"}
    if ctx.repo.count_events_for_job(job.id, "follow_up") >= cfg.max_times:
        return {**info, "state": "done"}
    due_at = applied_at + timedelta(hours=cfg.after_hours)
    last = _parse(job.follow_up_at)
    if last is not None:
        due_at = max(due_at, last + timedelta(hours=cfg.min_gap_hours))
    info["due_at"] = due_at.strftime(_TS)
    return {**info, "state": "due" if now >= due_at else "waiting"}


def find_due(ctx: ServiceContext, *, limit: int = 20) -> list:
    """找出"该跟进但还没跟进"的岗位。"""
    if not ctx.config.follow_up.enabled:
        return []
    now = datetime.now()
    due = [job for job in ctx.repo.list_jobs(status=Status.APPLIED.value, limit=500)
           if classify(ctx, job, now)["state"] == "due"]
    return due[:limit]


def follow_up_queue(ctx: ServiceContext) -> dict:
    """监测页「待跟进」卡片的数据：全部已投递岗位 + 各自的跟进状态。

    以前接口只返回"已到期或已生成话术"的岗位，刚投完的 48 小时内列表是空的，
    界面就误报"还没有已投递的岗位"。
    """
    cfg = ctx.config.follow_up
    now = datetime.now()
    rows = []
    for job in ctx.repo.list_jobs(status=Status.APPLIED.value, limit=500):
        info = classify(ctx, job, now)
        if not cfg.enabled and info["state"] in ("due", "waiting"):
            info = {**info, "state": "disabled", "due_at": None}
        rows.append({**job.__dict__, **info, "due": info["state"] == "due"})
    # 到期的排前面，其余按到期时间、投递时间排
    order = {"due": 0, "waiting": 1}
    rows.sort(key=lambda r: (order.get(r["state"], 2), r["due_at"] or "9", r["applied_at"] or ""))
    return {"enabled": cfg.enabled, "after_hours": cfg.after_hours, "rows": rows}


def generate_follow_ups(ctx: ServiceContext, *, limit: int = 10) -> dict:
    if not ctx.config.follow_up.enabled:
        return {"skipped": "follow_up disabled"}

    due = find_due(ctx, limit=limit)
    if not due:
        return {"generated": 0, "due": 0}

    resume = _read_resume(ctx)
    if not resume.strip():
        return {"error": "未配置简历（profile.resume_path），无法生成跟进话术"}

    generated = failed = 0
    fail_reason = ""
    for index, job in enumerate(due, 1):
        ctx.stop.check()
        ctx.progress(index, len(due), f"{job.title}@{job.company}")
        result = ctx.llm.complete(
            system="你是求职者本人，严格按 JSON Schema 输出，不要编造经历。",
            user=render(
                PROMPT,
                resume=resume[:4000],
                job=f"职位：{job.title}\n公司：{job.company}\nJD：{(job.jd or '')[:800]}",
            ),
            schema=FOLLOW_UP_SCHEMA,
            temperature=0.5,
        )
        text = str((result.data or {}).get("message") or "").strip() if result.ok else ""
        if not text:
            failed += 1
            fail_reason = fail_reason or (result.error or "模型未返回跟进话术")
            continue
        ctx.repo.set_follow_up(job.id, text)
        ctx.repo.add_event(job.id, "follow_up", f"{text[:40]} [{PROMPT} {version(PROMPT)}]")
        generated += 1

    return {"generated": generated, "failed": failed, "fail_reason": fail_reason,
            "due": len(due), "prompt": f"{PROMPT} {version(PROMPT)}"}
