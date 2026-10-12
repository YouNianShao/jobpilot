"""自动跟进：投递后长期无回复 → 生成一条简短的跟进话术。

设计要点（BossHunter 的幂等判断不够严，这里补上）：
- 已经生成过 N 次就不再生成（`follow_up` 事件计数）
- 两次跟进之间强制最小间隔（`follow_up_at`）
- 岗位一旦有 HR 回复（`replied` / `hr_reply` 事件）就退出跟进队列
- 纯文本生成，不碰浏览器 → 不涉及平台风控
"""

from __future__ import annotations

from datetime import datetime

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


def find_due(ctx: ServiceContext, *, limit: int = 20) -> list:
    """找出"该跟进但还没跟进"的岗位。"""
    cfg = ctx.config.follow_up
    if not cfg.enabled:
        return []
    now = datetime.now()
    due = []
    for job in ctx.repo.list_jobs(status=Status.APPLIED.value, limit=500):
        applied_at = _parse(ctx.repo.last_event_at(job.id, "applied"))
        if applied_at is None:
            continue
        if (now - applied_at).total_seconds() < cfg.after_hours * 3600:
            continue
        # 有回复就不再跟进
        if ctx.repo.count_events_for_job(job.id, "replied") or ctx.repo.count_events_for_job(job.id, "hr_reply"):
            continue
        if ctx.repo.count_events_for_job(job.id, "follow_up") >= cfg.max_times:
            continue
        last = _parse(job.follow_up_at)
        if last is not None and (now - last).total_seconds() < cfg.min_gap_hours * 3600:
            continue
        due.append(job)
    return due[:limit]


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
