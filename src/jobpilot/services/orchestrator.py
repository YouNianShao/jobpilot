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


def check_mode(config, mode: str, platform: str | None, *, registry=None) -> str | None:
    """返回"为什么跑不了"的人话原因；能跑就返回 None。"""
    if mode not in MODES:
        return f"未知模式：{mode}"
    if mode in ("confirm", "followup"):
        return None            # 纯数据库 / 纯文本，不开页面
    if mode in ("score", "greet") and not platform:
        return None            # 不开页面：不指定平台 = 所有平台的岗位一起处理
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
        return scorer.score_jobs(ctx, None, limit=options.get("limit") or 100)
    if mode == "greet" and not platform:
        return greeter.greet_jobs(ctx, None, limit=options.get("limit") or 100)

    if platform is None:
        return {"error": f"{mode} 需要指定平台"}

    problem = check_mode(ctx.config, mode, platform, registry=registry)
    if problem:
        return {"error": problem, "mode": mode, "platform": platform}

    if mode == "collect":
        return collector.collect(ctx, platform, registry=registry,
                                 keywords=options.get("keywords"),
                                 cities=options.get("cities"),
                                 max_pages=options.get("max_pages"))
    if mode == "score":
        return scorer.score_jobs(ctx, platform, limit=options.get("limit") or 100)
    if mode == "greet":
        return greeter.greet_jobs(ctx, platform, limit=options.get("limit") or 100)
    if mode == "apply":
        return applier.apply_jobs(ctx, platform, registry=registry, limit=options.get("limit"))
    if mode == "monitor":
        return monitor.run_monitor(ctx, platform, registry=registry)

    # full
    summary: dict = {"platform": platform, "steps": {}}
    summary["steps"]["collect"] = collector.collect(
        ctx, platform, registry=registry,
        keywords=options.get("keywords"), cities=options.get("cities"),
        max_pages=options.get("max_pages"))
    # 评分 / 招呼语对所有平台：别的平台之前采到、还没评分的岗位也一起处理
    summary["steps"]["score"] = scorer.score_jobs(ctx, None, limit=options.get("limit") or 100)
    summary["steps"]["greet"] = greeter.greet_jobs(ctx, None, limit=options.get("limit") or 100)

    pending = confirmer.pending_count(ctx, None)
    if ctx.config.confirm.required and pending:
        summary["awaiting_confirmation"] = pending
        summary["note"] = f"{pending} 个岗位待人工确认；确认后再执行 apply"
        return summary

    summary["steps"]["apply"] = applier.apply_jobs(ctx, platform, registry=registry, limit=options.get("limit"))
    return summary
