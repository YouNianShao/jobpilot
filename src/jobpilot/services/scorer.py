"""评分服务：硬规则预筛 → AI 判断 → 状态流转（AI 只出分数，流转由代码决定）。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from ..ai.llm import LlmResult
from ..ai.prompting import render, version
from ..models import Job, Status
from ..platforms.base import StopRequested
from ..prefilter import prefilter
from .context import ServiceContext

PROMPT = "scoring.v1.md"
SCORE_SCHEMA = {
    "type": "object",
    "required": ["score", "reason"],
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
    },
}


def _system() -> str:
    return "你是招聘匹配评估助手。严格按用户给的 JSON Schema 输出。"


def _job_text(job: Job) -> str:
    return (
        f"标题：{job.title}\n公司：{job.company}\n薪资：{job.salary or '未提供'}\n"
        f"城市：{job.city or '未提供'}\nJD：{(job.jd or '')[:2000]}"
    )


def _call(ctx: ServiceContext, job: Job, resume: str) -> LlmResult:
    return ctx.llm.complete(
        system=_system(),
        user=render(PROMPT, resume=resume or "（未提供简历）", job=_job_text(job)),
        schema=SCORE_SCHEMA,
        temperature=0.2,
    )


def _passed_status(ctx: ServiceContext) -> str:
    return Status.PENDING_CONFIRM.value if ctx.config.confirm.required else Status.APPROVED.value


def score_jobs(ctx: ServiceContext, platform: str, *, limit: int = 100, resume_text: str = "") -> dict:
    jobs = ctx.repo.list_jobs(platform=platform, status=Status.NEW.value, limit=limit)
    total = len(jobs)
    resume = resume_text
    if not resume:
        resume, problem = ctx.resume_text()
        if problem:
            # 拿"（未提供简历）"去让模型打分，会产出看起来正常、实际毫无意义的分数，
            # 而且这些分数会一路推着岗位走向投递 —— 所以这里必须直接拒绝。
            return {"platform": platform, "scored": 0, "jobs": total, "error": problem}

    prefiltered = failed = 0
    scored = 0
    fail_reason = ""
    threshold = ctx.config.scoring.threshold
    passed_status = _passed_status(ctx)

    def save(job: Job, result: LlmResult | None) -> None:
        """一条结果回来就立刻落库：中途停止 / 异常时，已经花过 token 的分数不能丢。"""
        nonlocal failed, scored, fail_reason
        if result is None or not result.ok or not result.data:
            failed += 1
            fail_reason = fail_reason or (result.error if result else "未知错误")
            return
        try:
            score = int(result.data.get("score"))
        except (TypeError, ValueError):
            failed += 1
            fail_reason = fail_reason or "模型返回的 score 不是整数"
            return
        reason = str(result.data.get("reason", ""))[:200]
        status = passed_status if score >= threshold else Status.FILTERED.value
        ctx.repo.set_score(job.id, score, reason, status)
        ctx.repo.set_score_trace(
            job.id, model=result.model or "", prompt=f"{PROMPT} {version(PROMPT)}",
            tokens_in=result.tokens_in, tokens_out=result.tokens_out, latency_ms=result.latency_ms,
        )
        ctx.repo.add_event(job.id, "scored", f"{score} {reason} [{PROMPT} {version(PROMPT)}]")
        scored += 1

    # 预筛（不消耗 AI 额度）
    pending: list[Job] = []
    for job in jobs:
        ctx.stop.check()
        rejected, reason = prefilter(job, ctx.config)
        if rejected:
            ctx.repo.set_status(job.id, Status.REJECTED.value)
            ctx.repo.add_event(job.id, "prefilter_rejected", reason)
            prefiltered += 1
        else:
            pending.append(job)

    concurrency = max(1, int(ctx.config.scoring.concurrency))
    if concurrency <= 1:
        for index, job in enumerate(pending, 1):
            ctx.stop.check()
            ctx.progress(index, len(pending), f"{job.title}@{job.company}")
            save(job, _call(ctx, job, resume))
    else:
        # 工作线程只调模型，写库只在当前线程做。停止时：还没开始的请求直接取消；
        # 已经发出去的（最多 concurrency 条）等它们回来，结果照样落库 —— token 已经花了。
        pool = ThreadPoolExecutor(max_workers=concurrency)
        futures = {pool.submit(_call, ctx, job, resume): job for job in pending}
        saved = set()
        try:
            for done, future in enumerate(as_completed(futures), 1):
                job = futures[future]
                ctx.progress(done, len(pending), f"{job.title}@{job.company}")
                save(job, future.result())
                saved.add(future)
                ctx.stop.check()
        except StopRequested:
            pool.shutdown(wait=True, cancel_futures=True)
            for future, job in futures.items():
                if future not in saved and not future.cancelled() and future.exception() is None:
                    save(job, future.result())
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return {
        "platform": platform, "scored": scored, "prefiltered": prefiltered,
        "failed": failed, "fail_reason": fail_reason, "total": total,
        "prompt": f"{PROMPT} {version(PROMPT)}",
    }
