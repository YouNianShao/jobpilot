"""招呼语服务：为待确认/已通过岗位生成招呼语。"""

from __future__ import annotations

from ..ai.prompting import render, version
from ..models import Status
from .context import ServiceContext

PROMPT = "greeting.v1.md"
GREETING_SCHEMA = {
    "type": "object",
    "required": ["greeting"],
    "properties": {"greeting": {"type": "string"}},
}


def greet_jobs(ctx: ServiceContext, platform: str, *, limit: int = 100) -> dict:
    if not ctx.config.greeting.enabled:
        return {"platform": platform, "greeted": 0, "skipped": "greeting disabled"}

    statuses = (Status.PENDING_CONFIRM.value, Status.APPROVED.value)
    jobs = [
        job for job in ctx.repo.list_jobs(platform=platform, limit=limit)
        if job.status in statuses and not (job.greeting or "").strip()
    ]

    resume, problem = ctx.resume_text()
    if problem:
        return {"platform": platform, "greeted": 0, "error": problem}

    greeted = failed = 0
    fail_reason = ""
    preference = ctx.config.profile.greeting_preference
    preference_block = f"\n# 用户偏好\n{preference}\n" if preference else ""

    for index, job in enumerate(jobs, 1):
        ctx.stop.check()
        ctx.progress(index, len(jobs), f"{job.title}@{job.company}")
        result = ctx.llm.complete(
            system="你是求职者本人，按 JSON Schema 输出。",
            user=render(
                PROMPT,
                preference=preference_block,
                resume=resume or "（未提供简历）",
                job=f"标题：{job.title}\n公司：{job.company}\nJD：{(job.jd or '')[:1500]}",
            ),
            schema=GREETING_SCHEMA,
            temperature=0.6,
        )
        text = ""
        if result.ok and result.data:
            text = str(result.data.get("greeting", "")).strip()
        if not text:
            failed += 1
            fail_reason = fail_reason or result.error or "模型未返回招呼语"
            continue
        ctx.repo.set_greeting(job.id, text)
        ctx.repo.add_event(job.id, "greeting", f"{text[:40]} [{PROMPT} {version(PROMPT)}]")
        greeted += 1

    return {
        "platform": platform, "greeted": greeted, "failed": failed,
        "fail_reason": fail_reason, "total": len(jobs),
        "prompt": f"{PROMPT} {version(PROMPT)}",
    }
