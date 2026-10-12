"""投递预演：只打开岗位页"看"，不点、不发。

自动投递要不要开放，取决于两件事：
1. 页面结构支不支持（按钮在不在、选择器对不对）—— 这件事**不需要发消息**就能验；
2. 你愿不愿意让程序替你发第一条消息 —— 这件事只有你能决定。

这个服务负责第 1 件：走和采集详情页完全相同的额度与闸门，打开页面看一眼就关掉。
"""

from __future__ import annotations

from ..models import LoginRequired, PlatformRisk
from ..platforms import build_default_registry
from .context import ServiceContext


def probe_apply(ctx: ServiceContext, job_id: str, *, registry=None) -> dict:
    job = ctx.repo.get_job(job_id)
    if job is None:
        return {"error": f"没有这个岗位：{job_id}"}

    registry = registry or build_default_registry()
    driver = registry.get(job.platform)
    if driver is None:
        return {"error": f"没有 {job.platform} 的平台驱动"}
    probe = getattr(driver, "probe_apply", None)
    if probe is None:
        return {"error": f"{job.platform} 还没有实现投递预演", "platform": job.platform}

    browser = ctx.create_browser()
    try:
        report = probe(ctx.driver_context(browser, job.platform), job)
    except LoginRequired as exc:
        return {"error": f"登录态失效：{exc}", "platform": job.platform, "job_id": job_id,
                "need_login": True, "sent_anything": False}
    except PlatformRisk as exc:
        ctx.policy.record_risk(job.platform, exc.kind, exc.detail)
        return {"error": f"页面命中风控：{exc.kind}", "platform": job.platform,
                "job_id": job_id, "sent_anything": False}
    except Exception as exc:  # noqa: BLE001 - 预演失败不该带走别的功能
        return {"error": f"{type(exc).__name__}: {exc}", "platform": job.platform,
                "job_id": job_id, "sent_anything": False}
    finally:
        try:
            browser.close()      # 必须关：这是连到你自己 Chrome 的 CDP 连接
        except Exception:  # noqa: BLE001
            pass

    ctx.repo.add_event(job.id, "probe_apply", str(report.get("verdict", ""))[:120])
    return report
