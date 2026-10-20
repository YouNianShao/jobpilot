"""监测服务：抓取已投递岗位的 HR 互动。"""

from __future__ import annotations

from ..models import LoginRequired, PlatformRisk
from ..platforms import build_default_registry, supports
from ..safety import attention
from ..safety.policy import Denied
from .context import ServiceContext


def run_monitor(ctx: ServiceContext, platform: str, *, registry=None) -> dict:
    registry = registry or build_default_registry()
    driver = registry.get(platform)
    if driver is None or not supports(driver, "monitor"):
        return {"platform": platform, "note": f"{platform} 未开放监测"}

    browser = ctx.create_browser()
    try:
        result = driver.monitor(ctx.driver_context(browser, platform))
    except PlatformRisk as exc:
        ctx.policy.record_risk(platform, exc.kind, exc.detail)
        attention.flag(ctx.repo, platform, exc, browser)
        return {"platform": platform, "blocked": exc.kind, "error": str(exc)}
    except LoginRequired as exc:
        attention.flag(ctx.repo, platform, exc, browser)
        return {"platform": platform, "login_required": True, "error": str(exc)}
    except Denied as exc:
        # 闸门拒绝（不在开页面时段 / 额度用完 / 冷却中）：这个平台这次不查，说清楚原因；
        # 「所有平台一起查」时别的平台照常查（以前整个任务直接报错停下）
        return {"platform": platform, "error": str(exc)}
    finally:
        browser.close()

    attention.clear(ctx.repo, platform)   # 列表正常读到了：之前的「待你处理」已经过去了
    ctx.repo.add_event(None, "monitor", f"{platform} checked={result.checked}")
    return {"platform": platform, "checked": result.checked, "events": result.events,
            "progressed": sum(result.events.values())}


def monitor_all(ctx: ServiceContext, *, registry=None) -> dict:
    """所有开放监测、而且启用了的平台逐个查（一个接一个，每个平台只开一个页面）。"""
    registry = registry or build_default_registry()
    per, notes = {}, []
    for key in registry.keys():
        cfg = ctx.config.platforms.get(key)
        if not supports(registry.get(key), "monitor"):
            continue
        if cfg is not None and not cfg.enabled:
            continue
        ctx.stop.check()
        res = run_monitor(ctx, key, registry=registry)
        per[key] = res
        if res.get("error") or res.get("blocked"):
            notes.append(f"{key}：{res.get('error') or res.get('blocked')}")
    out = {
        "checked": sum(r.get("checked", 0) for r in per.values()),
        "progressed": sum(r.get("progressed", 0) for r in per.values()),
        "per_platform": per,
    }
    if notes:
        out["note"] = "；".join(notes)
    return out
