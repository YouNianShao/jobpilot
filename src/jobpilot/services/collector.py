"""采集服务：把平台驱动的采集包在风控闸门与异常处理里。"""

from __future__ import annotations

from ..models import LoginRequired, PageReadError, PlatformRisk
from ..platforms import build_default_registry, supports
from ..safety import attention
from ..safety.policy import Denied
from .context import ServiceContext


def collect(ctx: ServiceContext, platform: str, *, registry=None,
            keywords=None, cities=None, max_pages=None) -> dict:
    registry = registry or build_default_registry()
    driver = registry.get(platform)
    if driver is None or not supports(driver, "collect"):
        return {"platform": platform, "error": f"{platform} 未实现采集"}

    # keywords / cities / max_pages 是「这一次」的覆盖值，**不写回 config.yaml**：
    # 界面上想临时搜几个词、试一个城市，不该为此改掉长期配置。覆盖只落在
    # 这个任务自己的 ctx.config 上（每个任务一个 ctx，互不影响）。
    overrides: dict = {}
    cfg = ctx.config.platforms.get(platform)
    if cfg is not None:
        if keywords:
            cfg.keywords = [str(item).strip() for item in keywords if str(item).strip()]
            overrides["keywords"] = list(cfg.keywords)
        if cities:
            cfg.cities = [str(item).strip() for item in cities if str(item).strip()]
            overrides["cities"] = list(cfg.cities)
        if max_pages:
            cfg.max_pages = max(1, int(max_pages))
            overrides["max_pages"] = cfg.max_pages
        if overrides:
            # 写给人看：「关键词 AI、产品经理 · 城市 上海 · 每个组合翻 3 页」，不要直接打印 dict
            parts = []
            if "keywords" in overrides:
                parts.append("关键词 " + "、".join(overrides["keywords"]))
            if "cities" in overrides:
                parts.append("城市 " + "、".join(overrides["cities"]))
            if "max_pages" in overrides:
                parts.append(f"每个组合翻 {overrides['max_pages']} 页")
            ctx.log(f"[{platform}] 这次只用：" + " · ".join(parts) + "（不改长期配置）")

    browser = ctx.create_browser()
    try:
        result = driver.collect(ctx.driver_context(browser, platform))
    except PlatformRisk as exc:
        until = ctx.policy.record_risk(platform, exc.kind, exc.detail)
        ctx.repo.add_event(None, "risk", f"{platform} {exc.kind}")
        # 验证码：页面留在调试浏览器里、切到前台，看板马上提醒你（不等任务结束）
        attention.flag(ctx.repo, platform, exc, browser)
        return {"platform": platform, "blocked": exc.kind, "locked_until": until, "error": str(exc)}
    except LoginRequired as exc:
        attention.flag(ctx.repo, platform, exc, browser)
        return {"platform": platform, "login_required": True, "error": str(exc)}
    except PageReadError as exc:
        # 页面读不到预期内容：计数，连续超阈值自动锁平台（fail-closed）
        failures = ctx.policy.record_page_failure(platform)
        lock = ctx.policy.locks.active(platform)
        ctx.repo.add_event(None, "page_error", f"{platform} 第 {failures} 次：{exc}")
        return {
            "platform": platform, "error": str(exc),
            "page_failures": failures,
            "blocked": "page_failures" if lock else None,
            "locked_until": lock["until"] if lock else None,
        }
    except Denied as exc:
        return {"platform": platform, "denied": exc.kind, "error": exc.reason}
    finally:
        browser.close()

    ctx.policy.record_page_success(platform)
    attention.clear(ctx.repo, platform)   # 页面都正常打开了：之前的「待你处理」已经过去了
    ctx.policy.note_collect_done(platform)  # 采完 → 投递要冷却一段随机时间
    ctx.repo.add_event(None, "collect", f"{platform} new={result.new} seen={result.seen} dup={result.duplicate} filtered={result.filtered} skipped={result.skipped}")
    payload = {
        "platform": platform, "new": result.new, "seen": result.seen,
        "duplicate": result.duplicate, "filtered": result.filtered, "stopped": result.stopped,
        # 翻页省预算：提前结束的组合数 / 少翻的页数 / 每个组合的新岗位率（看哪个组合该换词）
        "early_stopped": result.early_stopped, "pages_saved": result.pages_saved,
        "slots": list(result.slots),
    }
    if result.early_stopped or result.pages_saved:
        ctx.log(f"[{platform}] 有 {result.early_stopped} 个组合提前结束，这次少翻了 {result.pages_saved} 页")
    if overrides:
        payload["options"] = overrides      # 让界面 / 日志能回放这次用的参数
    return payload
