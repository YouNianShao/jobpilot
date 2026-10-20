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


BATCH = 100


def score_jobs(ctx: ServiceContext, platform: str, *, limit: int | None = None, resume_text: str = "") -> dict:
    """给待评分的岗位打分。``limit`` 为空 = **全部评完**（按 100 个一批，一批接一批）。

    2026-10-16：以前固定只取 100 个，其中被预筛淘汰的也算在这 100 里，所以 300 个待评分
    点一次只评掉七八十个，要点好几次。现在一直评到没有待评分的为止；
    某一批一个都没处理掉（全部调用失败）就停下，不会对着同一批死循环。
    """
    if limit is not None:
        return _score_batch(ctx, platform, limit=limit, resume_text=resume_text)
    total: dict | None = None
    while True:
        ctx.stop.check()
        batch = _score_batch(ctx, platform, limit=BATCH, resume_text=resume_text)
        if total is None:
            total = batch
        else:
            for key in ("scored", "prefiltered", "failed", "total", "already_scored"):
                total[key] = int(total.get(key) or 0) + int(batch.get(key) or 0)
            total["fail_reason"] = total.get("fail_reason") or batch.get("fail_reason", "")
        if batch.get("error") or batch.get("total", 0) < BATCH:
            break                                     # 出错（例如没简历）或这一批已经是最后一批
        if not (batch.get("scored") or batch.get("prefiltered") or batch.get("already_scored")):
            break                                     # 一个都没处理掉：多半是模型调用全失败，别对着同一批空转
    if total and total.get("error") and total.get("scored"):
        total.pop("error", None)
    return total or {"platform": platform, "scored": 0, "total": 0}


def _score_batch(ctx: ServiceContext, platform: str, *, limit: int, resume_text: str = "") -> dict:
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

    skipped = 0

    def still_new(job: Job) -> bool:
        """调模型前再看一眼：别的任务（另一个平台的全流程 / 独立评分）可能刚评过它，
        再评一次就是白花一份 token。只在当前线程查库（连接不在工作线程里共用）。"""
        nonlocal skipped
        fresh = ctx.repo.get_job(job.id)
        if fresh is None or fresh.status != Status.NEW.value:
            skipped += 1
            return False
        return True

    concurrency = max(1, int(ctx.config.scoring.concurrency))
    if concurrency <= 1:
        for index, job in enumerate(pending, 1):
            ctx.stop.check()
            ctx.progress(index, len(pending), f"{job.title}@{job.company}")
            if still_new(job):
                save(job, _call(ctx, job, resume))
    else:
        # 工作线程只调模型，写库只在当前线程做。请求按需提交（同时最多 concurrency 条），
        # 这样每条在发出前都能重新确认「还没被别的任务评过」；停止时还没提交的直接不发，
        # 已经发出去的等它们回来，结果照样落库 —— token 已经花了。
        pool = ThreadPoolExecutor(max_workers=concurrency)
        queue = list(pending)
        futures: dict = {}
        done = 0

        def fill() -> None:
            while queue and len(futures) < concurrency:
                job = queue.pop(0)
                if still_new(job):
                    futures[pool.submit(_call, ctx, job, resume)] = job

        try:
            fill()
            while futures:
                future = next(as_completed(futures))
                job = futures.pop(future)
                done += 1
                ctx.progress(done, len(pending), f"{job.title}@{job.company}")
                save(job, future.result())
                ctx.stop.check()
                fill()
        except StopRequested:
            pool.shutdown(wait=True, cancel_futures=True)
            for future, job in futures.items():
                if not future.cancelled() and future.exception() is None:
                    save(job, future.result())
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    return {
        "platform": platform, "scored": scored, "prefiltered": prefiltered,
        "failed": failed, "fail_reason": fail_reason, "total": total,
        # 被别的任务抢先评过、这里没再花 token 的岗位数
        "already_scored": skipped,
        "prompt": f"{PROMPT} {version(PROMPT)}",
    }
