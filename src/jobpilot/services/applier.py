"""投递服务：每个岗位都要先过安全内核，再交给平台驱动。"""

from __future__ import annotations

from ..models import LoginRequired, PlatformRisk, Status
from ..platforms import build_default_registry, supports
from ..safety.policy import APPLY_ONE, Action, Denied
from ..safety.throttle import human_interval, jitter
from .context import ServiceContext


def mark_applied(ctx: ServiceContext, job_id: str, *, note: str = "") -> dict:
    """手动投递回执：你在平台里自己投了，回来点一下。

    为什么需要它：自动投递还没核验之前，真实投递是人在平台上点的。
    如果系统不知道这件事，就会出现三种假象——今日投递数偏少（风控上限形同虚设）、
    跟进队列里没有这个岗位、同一岗位下次又会被当成"还没投"再推一遍。
    所以这算是**唯一**一条人工写入 applied 的通道，并且和自动投递共用同一套计数。
    """
    job = ctx.repo.get_job(job_id)
    if job is None:
        return {"error": f"没有这个岗位：{job_id}"}
    if ctx.repo.is_applied(job_id):
        return {"error": f"「{job.title}」已经记过投递了", "job_id": job_id}

    ctx.repo.set_status(job_id, Status.APPLIED.value)
    ctx.repo.add_event(job_id, "apply", f"manual {note}".strip())
    ctx.repo.add_event(job_id, "applied", "manual")   # 与自动投递同一个计数口径
    return {
        "ok": True, "job_id": job_id, "title": job.title,
        "remaining_today": ctx.policy.remaining_applies(job.platform),
    }


def apply_jobs(ctx: ServiceContext, platform: str, *, registry=None, limit: int | None = None) -> dict:
    registry = registry or build_default_registry()
    driver = registry.get(platform)
    if driver is None or not supports(driver, "apply"):
        return {"platform": platform, "applied": 0, "note": f"{platform} 未开放自动投递"}

    platform_cfg = ctx.config.platforms.get(platform)
    if platform_cfg is not None and not platform_cfg.apply:
        # 这个开关以前没人读（点开也不会有任何变化），现在它是真的总闸
        return {"platform": platform, "applied": 0,
                "skipped": f"{platform} 的「允许对该平台投递」是关的（设置 → 平台）"}

    jobs = ctx.repo.list_jobs(platform=platform, status=Status.APPROVED.value, limit=1000)
    if limit is not None:
        jobs = jobs[:limit]

    applied = failed = already = 0
    stopped = ""
    action = Action(kind=APPLY_ONE, platform=platform)
    browser = ctx.create_browser()
    try:
        for index, job in enumerate(jobs, 1):
            ctx.stop.check()
            if ctx.repo.is_applied(job.id):
                already += 1
                continue
            try:
                ctx.policy.authorize(action)
            except Denied as exc:
                stopped = exc.reason
                break

            ctx.progress(index, len(jobs), f"{job.title}@{job.company}")
            try:
                result = driver.apply(ctx.driver_context(browser, platform), job)
            except (PlatformRisk, LoginRequired) as exc:
                # LoginRequired 没有 kind 属性：以前 getattr 兜底成 "blocked"，掉登录也会把平台锁掉
                if isinstance(exc, LoginRequired):
                    stopped = f"{platform} 登录态失效：{exc}"
                else:
                    kind = exc.kind
                    ctx.policy.record_risk(platform, kind, str(exc))
                    stopped = f"{platform} 命中风控：{kind}"
                break

            ctx.repo.set_status(job.id, result.status)
            ctx.repo.add_event(job.id, "apply", f"{result.status} {result.detail}".strip())
            if result.status == Status.APPLIED.value:
                ctx.repo.add_event(job.id, "applied", "applied")   # 计入每日上限
                applied += 1
                ctx.policy.record_outcome(action, True)
                if index < len(jobs):
                    # 间隔在 [min, max] 之间随机取再加抖动：固定节奏是最容易被识别的特征。
                    # 之前只用 interval_min，interval_max 是死配置——界面上有旋钮就必须真的生效。
                    low = max(0.0, float(ctx.config.apply.interval_min))
                    high = max(low, float(ctx.config.apply.interval_max))
                    # 用截断正态而不是均匀随机：均匀随机的投递节奏同样是个可统计的特征
                    pause = jitter(human_interval(low, high))
                    ctx.log(f"[apply] 投递成功，等待 {pause:.0f}s 继续")
                    ctx.stop.sleep(pause)
            elif result.status == Status.ALREADY.value:
                # 早已聊过：既不该算成功（不占额度、不触发投递间隔），
                # 也不该算失败 —— 否则连着几个已聊过的岗位就会把熔断计数推上去。
                already += 1
            else:
                failed += 1
                wait = ctx.policy.record_outcome(action, False)
                if ctx.policy.should_stop(platform):
                    stopped = stopped or f"{platform} 连续失败过多，已停止"
                    break
                ctx.stop.sleep(min(wait, 30.0))
    finally:
        browser.close()

    return {
        "platform": platform, "applied": applied, "failed": failed,
        "already": already, "stopped": stopped,
        "remaining_today": ctx.policy.remaining(platform)["daily_limit"],
    }
