"""编排服务：把各服务串成 L0~L3 的流程。

L1（默认）：采集 → 评分 → 招呼语 → **停下等人工确认** → 投递
"""

from __future__ import annotations

from ..platforms import build_default_registry, supports
from . import applier, collector, confirmer, followup, greeter, monitor, scorer
from .context import ServiceContext

MODES = ("collect", "score", "greet", "confirm", "apply", "monitor", "followup", "full")

# 每个模式最少需要什么能力；没有就不要假装能跑——
# 一个"跑完了但什么都没做"的任务比直接报错更浪费时间。
MODE_NEEDS = {
    "collect": "collect",
    "full": "collect",
    "apply": "apply",
    "monitor": "monitor",
}


class TaskNotRunnable(RuntimeError):
    """这个模式在当前配置/驱动下做不了任何事。"""


def check_mode(config, mode: str, platform: str | None, *, registry=None, plans=None) -> str | None:
    """返回"为什么跑不了"的人话原因；能跑就返回 None。"""
    if mode not in MODES:
        return f"未知模式：{mode}"
    if mode == "full" and plans is not None:
        # 多平台全流程：每个勾选的平台都要能采集（不能投递的平台只采集，在投递那步说明）
        if not plans:
            return "没有勾选任何平台"
        registry = registry or build_default_registry()
        for plan in plans:
            problem = check_mode(config, "collect", plan.get("platform"), registry=registry)
            if problem:
                return problem
        return None
    if mode in ("confirm", "followup"):
        return None            # 纯数据库 / 纯文本，不开页面
    if mode in ("score", "greet") and not platform:
        return None            # 不开页面：不指定平台 = 所有平台的岗位一起处理
    if mode == "apply" and not platform:
        # 不指定平台 = 所有能投的平台轮流投；一个能投的都没有才拒绝
        registry = registry or build_default_registry()
        reasons = [check_mode(config, "apply", key, registry=registry) for key in registry.keys()]
        return None if any(r is None for r in reasons) else "没有可以自动投递的平台（都没开放，或设置里关掉了「允许对该平台投递」）"
    if mode == "monitor" and not platform:
        return None            # 所有开放监测的平台逐个查（不能查的写进结果）
    if not platform:
        return f"{mode} 需要指定平台"

    registry = registry or build_default_registry()
    driver = registry.get(platform)
    if driver is None:
        return f"没有 {platform} 的平台驱动"

    cfg = config.platforms.get(platform)
    if cfg is not None and not cfg.enabled:
        return f"{platform} 在配置里是停用状态"

    need = MODE_NEEDS.get(mode)
    if need and not supports(driver, need):
        label = {"apply": "自动投递", "monitor": "HR 监测", "collect": "采集"}.get(need, need)
        return f"{platform} 还没有开放「{label}」（驱动的能力未实机核验，界面上只显示已核验的动作）"

    if mode == "apply" and cfg is not None and not cfg.apply:
        return f"{platform} 的「允许对该平台投递」是关的（设置 → 平台）"
    return None


def run(ctx: ServiceContext, mode: str, platform: str | None = None, *, registry=None, **options) -> dict:
    if mode not in MODES:
        return {"error": f"未知模式: {mode}"}
    if mode == "confirm":
        if options.get("reject"):
            return confirmer.reject(ctx, platform, job_ids=options.get("job_ids"))
        return confirmer.approve(ctx, platform, job_ids=options.get("job_ids"))
    if mode == "followup":
        # 纯文本生成，不打开任何页面
        return followup.generate_follow_ups(ctx, limit=options.get("limit") or 10)

    # 评分 / 招呼语不开页面，不该被顶栏选的平台卡住：以前选着 BOSS 点评分，
    # 前程无忧刚采的 20 个岗位就一直不动（用户以为是 bug）。不传平台 = 全部平台。
    if mode == "score" and not platform:
        return scorer.score_jobs(ctx, None, limit=options.get("limit"))
    if mode == "greet" and not platform:
        return greeter.greet_jobs(ctx, None, limit=options.get("limit"))
    if mode == "apply" and not platform:
        # 所有平台轮流投：一个平台等间隔时去投下一个平台（每个平台自己的间隔不变）
        problem = check_mode(ctx.config, "apply", None, registry=registry)
        if problem:
            return {"error": problem, "mode": mode, "platform": "all"}
        return applier.apply_all(ctx, registry=registry, limit=options.get("limit"))

    if mode == "full" and options.get("plans") is not None:
        # 多平台全流程：勾选的平台依次采集（每个平台这一次的搜索条件在 plans 里）
        problem = check_mode(ctx.config, mode, None, registry=registry, plans=options["plans"])
        if problem:
            return {"error": problem, "mode": mode, "platform": "multi"}
        return run_full_multi(ctx, options["plans"], registry=registry, limit=options.get("limit"),
                              apply_after=options.get("apply_after", True))

    if mode == "monitor" and platform is None:
        return monitor.monitor_all(ctx, registry=registry)      # 所有开放监测的平台逐个查
    if platform is None:
        return {"error": f"{mode} 需要指定平台"}

    problem = check_mode(ctx.config, mode, platform, registry=registry)
    if problem:
        return {"error": problem, "mode": mode, "platform": platform}

    if mode == "collect":
        return collector.collect(ctx, platform, registry=registry,
                                 keywords=options.get("keywords"),
                                 cities=options.get("cities"),
                                 max_pages=options.get("max_pages"),
                                 slots=options.get("slots"))
    if mode == "score":
        return scorer.score_jobs(ctx, platform, limit=options.get("limit"))
    if mode == "greet":
        return greeter.greet_jobs(ctx, platform, limit=options.get("limit"))
    if mode == "apply":
        return applier.apply_jobs(ctx, platform, registry=registry, limit=options.get("limit"))
    if mode == "monitor":
        if platform:
            return monitor.run_monitor(ctx, platform, registry=registry)
        return monitor.monitor_all(ctx, registry=registry)

    # full
    summary: dict = {"platform": platform, "steps": {}}
    ctx.stage("collect")
    summary["steps"]["collect"] = collector.collect(
        ctx, platform, registry=registry,
        keywords=options.get("keywords"), cities=options.get("cities"),
        max_pages=options.get("max_pages"), slots=options.get("slots"), resume_mode="full")
    # 评分 / 招呼语只管本平台：别的平台可能正同时跑着自己的全流程，
    # 这里再顺手评一遍会把同一批岗位评两次、花两份 token。别的平台的存量用独立的「评分」任务处理。
    ctx.stage("score")
    summary["steps"]["score"] = scorer.score_jobs(ctx, platform, limit=options.get("limit"))
    ctx.stage("greet")
    summary["steps"]["greet"] = greeter.greet_jobs(ctx, platform, limit=options.get("limit"))

    pending = confirmer.pending_count(ctx, platform)
    if ctx.config.confirm.required and pending:
        summary["awaiting_confirmation"] = pending
        summary["note"] = f"{pending} 个岗位待人工确认；确认后再执行 apply"
        return summary

    ctx.stage("apply")
    summary["steps"]["apply"] = applier.apply_jobs(ctx, platform, registry=registry, limit=options.get("limit"))
    return summary


def _per_platform(ctx: ServiceContext, keys: list[str], step, limit: int | None, fields: tuple[str, ...]) -> dict:
    """对几个平台各跑一次评分 / 招呼语，合成一份和单平台同形状的结果（界面摘要照旧能读）。"""
    per = {key: step(ctx, key, limit=limit) for key in keys}
    out: dict = {"platform": ",".join(keys), "per_platform": per}
    for name in fields:
        out[name] = sum(int(r.get(name) or 0) for r in per.values())
    errors = [r["error"] for r in per.values() if r.get("error")]
    if errors:
        # 简历缺失这类错误每个平台都一样，去重后只说一次
        out["error"] = "；".join(dict.fromkeys(errors))
    for key in ("fail_reason", "skipped", "prompt"):
        found = next((r[key] for r in per.values() if r.get(key)), "")
        if found:
            out[key] = found
    return out


def run_full_multi(ctx: ServiceContext, plans: list[dict], *, registry=None, limit: int | None = None,
                   apply_after: bool = True) -> dict:
    """勾选的几个平台依次采集 → 这几个平台的新岗位评分、写招呼语 → 有待确认就停下；
    没有待确认、且勾了「跑完后投递」时，**只在勾选的平台里**轮流投递已确认的岗位。

    平台之间是**依次**采集，不并发：并发开页面会把同一个账号的节奏瞬间拉满。
    某个平台采集失败（没登录 / 被风控锁）不影响后面的平台。
    """
    registry = registry or build_default_registry()
    chosen = [plan["platform"] for plan in plans]
    summary: dict = {"platform": "multi", "platforms": chosen, "steps": {}}
    per: dict[str, dict] = {}
    ctx.stage("collect")
    for plan in plans:
        ctx.stop.check()
        key = plan["platform"]
        ctx.log(f"[full] 采集 {registry.get(key).name}")
        per[key] = collector.collect(ctx, key, registry=registry, keywords=plan.get("keywords"),
                                     cities=plan.get("cities"), max_pages=plan.get("max_pages"),
                                     slots=plan.get("slots"), resume_mode="full")
    collected: dict = {"per_platform": per}
    for field in ("new", "duplicate", "filtered"):
        collected[field] = sum(int(r.get(field) or 0) for r in per.values() if isinstance(r, dict))
    if per and all(isinstance(r, dict) and r.get("error") for r in per.values()):
        collected["error"] = "；".join(f"{registry.get(k).name}：{r['error']}" for k, r in per.items())
    summary["steps"]["collect"] = collected
    # 只评 / 只写勾选的平台：没勾的平台可能正被另一个任务处理（不同平台可以同时跑）
    ctx.stage("score")
    summary["steps"]["score"] = _per_platform(ctx, chosen, scorer.score_jobs, limit,
                                              ("scored", "prefiltered", "failed", "total"))
    ctx.stage("greet")
    summary["steps"]["greet"] = _per_platform(ctx, chosen, greeter.greet_jobs, limit,
                                              ("greeted", "failed", "total"))

    pending = sum(confirmer.pending_count(ctx, key) for key in chosen)
    if ctx.config.confirm.required and pending:
        summary["awaiting_confirmation"] = pending
        summary["note"] = f"{pending} 个岗位待人工确认；确认后再投递"
        return summary
    if apply_after:
        ctx.stage("apply")
        summary["steps"]["apply"] = applier.apply_all(ctx, registry=registry, limit=limit, platforms=chosen)
    return summary
