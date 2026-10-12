"""监测服务：抓取已投递岗位的 HR 互动。"""

from __future__ import annotations

from ..models import LoginRequired, PlatformRisk
from ..platforms import build_default_registry, supports
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
        return {"platform": platform, "blocked": exc.kind, "error": str(exc)}
    except LoginRequired as exc:
        return {"platform": platform, "login_required": True, "error": str(exc)}
    finally:
        browser.close()

    ctx.repo.add_event(None, "monitor", f"{platform} checked={result.checked}")
    return {"platform": platform, "checked": result.checked, "events": result.events}
