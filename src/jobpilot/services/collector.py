"""采集服务：把平台驱动的采集包在风控闸门与异常处理里。"""

from __future__ import annotations

from ..models import LoginRequired, PageReadError, PlatformRisk
from ..platforms import build_default_registry, supports
from ..safety import attention
from ..safety.policy import Denied
from .context import ServiceContext


def collect(ctx: ServiceContext, platform: str, *, registry=None,
            keywords=None, cities=None, max_pages=None, slots=None, resume_mode: str = "collect") -> dict:
    """``slots``：只采这几个组合（组合键，如 ``"上海|AI"``；自动续采用）。``resume_mode``：被速率窗口
    拦在半路时，自动续采该起什么任务（单独采集 = "collect"，全流程里的采集 = "full"）。"""
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
        dctx = ctx.driver_context(browser, platform)
        if slots:
            dctx.only_slots = [str(item) for item in slots if str(item).strip()]
            ctx.log(f"[{platform}] 这次只采 {len(dctx.only_slots)} 个组合（接着上次没采完的）")
        result = driver.collect(dctx)
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
        # 没轮到的组合（额度 / 速率停手；已进「下次先补」）/ 这次先补的上次没采到的组合
        "missed": list(result.missed),
        "carried": list(result.carried),
        "resume_at": "",
    }
    if result.stop_kind != "rate" and not slots:
        # 完整跑过一轮（或因额度 / 冷却停手）：之前记的待续采已经没有意义，别让它再自己起一个任务
        from .auto_resume import clear_resume, read_resume

        if read_resume(ctx.repo, platform) is not None:
            clear_resume(ctx.repo, platform)
    if result.stop_kind == "rate":
        # 速率窗口（10 分钟 / 1 小时）停手：窗口重新打开后自动接着采没采完的组合（services/auto_resume.py）
        pending = list(dict.fromkeys([*result.partial_slots, *result.missed_slots]))
        names = dict(zip([*result.partial_slots, *result.missed_slots], [*result.partial, *result.missed], strict=False))
        if pending:
            from .auto_resume import read_resume, record_resume

            payload["resume_at"] = record_resume(
                ctx.repo, ctx.policy, platform, mode=resume_mode, slots=pending,
                labels=[names[key] for key in pending], options=overrides)
            payload["resume_slots"] = len(pending)
            record = read_resume(ctx.repo, platform) or {}
            payload["resume_window"] = record.get("window") or "1 小时"
            payload["resume_in_minutes"] = max(1, round(
                (float(record.get("not_before") or 0) - float(record.get("created_at") or 0)) / 60))
            ctx.log(f"[{platform}] 窗口重新打开后（约 {payload['resume_at'][11:16]}）自动接着采 {len(pending)} 个组合")
    if result.early_stopped or result.pages_saved:
        ctx.log(f"[{platform}] 有 {result.early_stopped} 个组合提前结束，这次少翻了 {result.pages_saved} 页")
    if overrides:
        payload["options"] = overrides      # 让界面 / 日志能回放这次用的参数
    return payload
