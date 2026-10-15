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


def _apply_one(ctx: ServiceContext, driver, browser, platform: str, job, action) -> tuple[str, str, float]:
    """投一个岗位：过闸门 → 交给驱动 → 写结果。单平台投递和多平台轮流投递共用这一步。

    返回 ``(kind, stop_reason, backoff)``，kind 是 applied / already / failed / stop。
    stop = 这个平台今天不能再投了（闸门拒绝 / 掉登录 / 风控 / 连续失败），原因在第二项；
    failed 时 backoff 是安全内核给的退避秒数。
    """
    if ctx.repo.is_applied(job.id):
        return "already", "", 0.0
    try:
        ctx.policy.authorize(action)
    except Denied as exc:
        return "stop", exc.reason, 0.0
    try:
        result = driver.apply(ctx.driver_context(browser, platform), job)
    except (PlatformRisk, LoginRequired) as exc:
        # LoginRequired 没有 kind 属性：以前 getattr 兜底成 "blocked"，掉登录也会把平台锁掉
        if isinstance(exc, LoginRequired):
            return "stop", f"{platform} 登录态失效：{exc}", 0.0
        ctx.policy.record_risk(platform, exc.kind, str(exc))
        return "stop", f"{platform} 命中风控：{exc.kind}", 0.0

    ctx.repo.set_status(job.id, result.status)
    ctx.repo.add_event(job.id, "apply", f"{result.status} {result.detail}".strip())
    if result.status == Status.APPLIED.value:
        ctx.repo.add_event(job.id, "applied", "applied")   # 计入每日上限
        ctx.policy.record_outcome(action, True)
        return "applied", "", 0.0
    if result.status == Status.ALREADY.value:
        # 早已聊过：既不该算成功（不占额度、不触发投递间隔），
        # 也不该算失败 —— 否则连着几个已聊过的岗位就会把熔断计数推上去。
        return "already", "", 0.0
    wait = ctx.policy.record_outcome(action, False)
    if ctx.policy.should_stop(platform):
        return "stop", f"{platform} 连续失败过多，已停止", 0.0
    return "failed", "", float(wait or 0.0)


def _apply_pause(ctx: ServiceContext) -> float:
    # 间隔在 [min, max] 之间随机取再加抖动：固定节奏是最容易被识别的特征。
    # 用截断正态而不是均匀随机：均匀随机的投递节奏同样是个可统计的特征
    low = max(0.0, float(ctx.config.apply.interval_min))
    high = max(low, float(ctx.config.apply.interval_max))
    return jitter(human_interval(low, high))


def _check(ctx: ServiceContext, registry, platform: str):
    """这个平台能不能投：返回 (driver, 不能投的原因)。"""
    driver = registry.get(platform)
    if driver is None or not supports(driver, "apply"):
        return None, f"{platform} 未开放自动投递"
    platform_cfg = ctx.config.platforms.get(platform)
    if platform_cfg is not None and not platform_cfg.enabled:
        return None, f"{platform} 在配置里是停用状态"
    if platform_cfg is not None and not platform_cfg.apply:
        # 这个开关以前没人读（点开也不会有任何变化），现在它是真的总闸
        return None, f"{platform} 的「允许对该平台投递」是关的（设置 → 平台）"
    return driver, ""


def apply_jobs(ctx: ServiceContext, platform: str, *, registry=None, limit: int | None = None) -> dict:
    registry = registry or build_default_registry()
    driver, problem = _check(ctx, registry, platform)
    if driver is None:
        key = "note" if "未开放" in problem else "skipped"
        return {"platform": platform, "applied": 0, key: problem}

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
            ctx.progress(index, len(jobs), f"{job.title}@{job.company}")
            kind, reason, wait = _apply_one(ctx, driver, browser, platform, job, action)
            if kind == "stop":
                stopped = reason
                break
            if kind == "already":
                already += 1
            elif kind == "applied":
                applied += 1
                if index < len(jobs):
                    pause = _apply_pause(ctx)
                    ctx.log(f"[apply] 投递成功，等待 {pause:.0f}s 继续")
                    ctx.stop.sleep(pause)
            else:
                failed += 1
                ctx.stop.sleep(min(wait, 30.0))
    finally:
        browser.close()

    return {
        "platform": platform, "applied": applied, "failed": failed,
        "already": already, "stopped": stopped,
        "remaining_today": ctx.policy.remaining(platform)["daily_limit"],
    }


def apply_all(ctx: ServiceContext, *, registry=None, limit: int | None = None, clock=None,
              platforms: list[str] | None = None) -> dict:
    """多平台轮流投递：一个平台在等投递间隔时，去下一个已经到点的平台投一个。

    为什么不开多个线程同时投：浏览器、数据库、停止按钮都只有一套，并行出问题很难查。
    轮流投的速度和并行差不多 —— 投递本来就大部分时间在等间隔 —— 而且**每个平台自己的
    间隔一秒都不少**：间隔、页面额度、每日上限、风控锁仍是按平台算的，和单平台投递完全一样。
    某个平台被闸门拒绝 / 掉登录 / 风控 / 连续失败，只停那一个平台，其它平台继续；
    两个平台接连被拦时，安全内核的跨平台熔断会让所有平台一起停（authorize 那一步）。
    ``platforms``：只投这些平台（全流程里勾选的那几个）；不传 = 所有平台。
    """
    import time as _time

    clock = clock or _time.monotonic
    registry = registry or build_default_registry()
    queues: dict[str, list] = {}
    skipped: dict[str, str] = {}
    for platform in registry.keys():
        if platforms is not None and platform not in platforms:
            continue
        jobs = ctx.repo.list_jobs(platform=platform, status=Status.APPROVED.value, limit=1000)
        if not jobs:
            continue
        driver, problem = _check(ctx, registry, platform)
        if driver is None:
            skipped[platform] = problem
            continue
        queues[platform] = jobs[:limit] if limit is not None else jobs

    per: dict[str, dict] = {p: {"applied": 0, "failed": 0, "already": 0, "stopped": ""} for p in queues}
    total = sum(len(q) for q in queues.values())
    ready_at = {p: 0.0 for p in queues}          # 这个平台下一次可以投的时刻（单调时钟）
    done = 0
    if not queues:
        return {"platform": "all", "applied": 0, "per_platform": {}, "skipped": skipped,
                "note": "没有已确认、待投递的岗位" if not skipped else ""}

    actions = {p: Action(kind=APPLY_ONE, platform=p) for p in queues}
    browser = ctx.create_browser()
    try:
        while queues:
            ctx.stop.check()
            # 选最早到点的平台；都没到点就等到最早的那个（等待可被停止按钮打断）
            platform = min(queues, key=lambda p: ready_at[p])
            wait = ready_at[platform] - clock()
            if wait > 0:
                ctx.stop.sleep(wait)
                ctx.stop.check()
            # 刚采集完的平台有一段「采集→投递」冷却：轮流投时让它先排到后面，到点再投，
            # 不能像单平台那样直接停掉（那样这个平台今天就白确认了）
            cooling = ctx.policy.cooldown_remaining(platform)
            if cooling > 0:
                ready_at[platform] = clock() + cooling
                ctx.log(f"[apply] {registry.get(platform).name} 采集后冷却中，约 {cooling / 60:.0f} 分钟后再投")
                continue
            job = queues[platform].pop(0)
            done += 1
            ctx.progress(done, total, f"{registry.get(platform).name}：{job.title}@{job.company}")
            kind, reason, backoff = _apply_one(ctx, registry.get(platform), browser, platform, job, actions[platform])
            stats = per[platform]
            if kind == "stop":
                stats["stopped"] = reason
                total -= len(queues[platform])            # 剩下的不再算进进度
                ctx.log(f"[apply] {registry.get(platform).name} 停止：{reason}（其它平台继续）")
                queues.pop(platform)
                continue
            stats[kind] += 1
            if kind == "applied":
                pause = _apply_pause(ctx)
                ready_at[platform] = clock() + pause
                ctx.log(f"[apply] {registry.get(platform).name} 投递成功，这个平台 {pause:.0f}s 后再投下一个")
            elif kind == "failed":
                ready_at[platform] = clock() + min(backoff, 30.0)
            if not queues[platform]:
                queues.pop(platform)
    finally:
        browser.close()

    out = {
        "platform": "all",
        "applied": sum(s["applied"] for s in per.values()),
        "failed": sum(s["failed"] for s in per.values()),
        "already": sum(s["already"] for s in per.values()),
        "per_platform": {p: {**s, "remaining_today": ctx.policy.remaining(p)["daily_limit"]} for p, s in per.items()},
        "skipped": skipped,
    }
    stops = [f"{registry.get(p).name}：{s['stopped']}" for p, s in per.items() if s["stopped"]]
    if stops:
        out["stopped"] = "；".join(stops)
    if skipped:
        out["note"] = "没投的平台 —— " + "；".join(f"{registry.get(p).name}：{why}" for p, why in skipped.items())
    return out
