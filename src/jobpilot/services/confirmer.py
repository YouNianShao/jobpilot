"""人工确认门：pending_confirm → approved / rejected。"""

from __future__ import annotations

from ..models import Status
from .context import ServiceContext


def _pending(ctx: ServiceContext, platform: str, job_ids: list[str] | None):
    """返回 (可确认的岗位, 跳过的 {状态: 个数})。

    点名的 id 不按平台过滤：岗位池是全平台混排的，勾了前程无忧的岗位、请求里却带着
    默认平台 boss，以前就会被静默跳过 —— 表现就是「点了确认投递没反应」。
    """
    if not job_ids:
        return ctx.repo.list_jobs(platform=platform, status=Status.PENDING_CONFIRM.value, limit=1000), {}
    jobs, skipped = [], {}
    for job_id in dict.fromkeys(str(j) for j in job_ids):
        job = ctx.repo.get_job(job_id)
        if job is None:
            skipped["missing"] = skipped.get("missing", 0) + 1
        elif job.status != Status.PENDING_CONFIRM.value:
            skipped[job.status] = skipped.get(job.status, 0) + 1
        else:
            jobs.append(job)
    return jobs, skipped


def approve(ctx: ServiceContext, platform: str, *, job_ids: list[str] | None = None) -> dict:
    jobs, skipped = _pending(ctx, platform, job_ids)
    for job in jobs:
        ctx.repo.set_status(job.id, Status.APPROVED.value)
        ctx.repo.add_event(job.id, "confirmed", "人工确认投递")
    return {"platform": platform, "confirmed": len(jobs), "job_ids": [j.id for j in jobs], "skipped": skipped}


def reject(ctx: ServiceContext, platform: str, *, job_ids: list[str] | None = None) -> dict:
    jobs, skipped = _pending(ctx, platform, job_ids)
    for job in jobs:
        ctx.repo.set_status(job.id, Status.REJECTED.value)
        ctx.repo.add_event(job.id, "confirm_rejected", "人工拒绝投递")
    return {"platform": platform, "rejected": len(jobs), "job_ids": [j.id for j in jobs], "skipped": skipped}


def pending_count(ctx: ServiceContext, platform: str | None = None) -> int:
    return ctx.repo.count_status(Status.PENDING_CONFIRM.value, platform)
