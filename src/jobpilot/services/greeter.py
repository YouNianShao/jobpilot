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


def greet_jobs(ctx: ServiceContext, platform: str, *, limit: int | None = None) -> dict:
    if not ctx.config.greeting.enabled:
        return {"platform": platform, "greeted": 0, "skipped": "greeting disabled"}

    # 2026-10-16：以前先从整个岗位池取 100 个（各种状态混在一起）再挑待确认 / 已确认的 ——
    # 岗位池一大，很多待确认的岗位根本排不进那 100 个，点多少次招呼语都轮不到。
    # 现在直接按状态取，limit 为空 = 全部
    jobs = []
    for status in (Status.PENDING_CONFIRM.value, Status.APPROVED.value):
        jobs += [job for job in ctx.repo.list_jobs(platform=platform, status=status, limit=100000)
                 if not (job.greeting or "").strip()]
    if limit is not None:
        jobs = jobs[:limit]

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
        # 别的任务可能刚给它写过招呼语（不同平台的任务可以同时跑），再写一条就是白花 token
        fresh = ctx.repo.get_job(job.id)
        if fresh is None or (fresh.greeting or "").strip():
            continue
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
