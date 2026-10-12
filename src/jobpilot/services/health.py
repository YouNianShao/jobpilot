"""体检：一次回答"这台机器现在能不能干活，不能的话先修哪儿"。

都是确定性检查（不发 AI 请求、不发消息）；唯一"真的碰一下"的是浏览器连接和
数据库写入探针 —— 这两件事正是最常坏、也最难从报错里看出来的。
"""

from __future__ import annotations

import shutil

from ..platforms import build_default_registry
from ..safety.throttle import in_send_window
from .context import ServiceContext


def _row(key, label, ok, detail="", fix="", level="error"):
    return {"key": key, "label": label, "ok": bool(ok), "level": level, "detail": detail, "fix": fix}


def run_checks(ctx: ServiceContext) -> dict:
    cfg = ctx.config
    checks = []

    # 1) AI key
    has_key = bool((cfg.ai.api_key or "").strip())
    checks.append(_row(
        "ai_key", "AI Key", has_key,
        f"{cfg.ai.model} @ {cfg.ai.base_url}" if has_key else "没配 key",
        "设置 → 模型 → API Key（没有它，评分/招呼语/面试准备/跟进全线不可用）",
    ))

    # 2) 简历
    text, problem = ctx.resume_text()
    checks.append(_row(
        "resume", "简历可读", not problem,
        f"{len(text)} 字" if not problem else problem,
        problem and "设置 → 简历正文（可以直接粘贴，或从文件导入）",
    ))

    # 3) 浏览器（真的连一下 CDP）
    try:
        browser = ctx.create_browser()
        try:
            ok = browser.health()
        finally:
            browser.close()
        checks.append(_row("browser", "浏览器连接", ok,
                           "已连上你登录好的 Chrome" if ok else "连不上 CDP",
                           "Chrome 要以 --remote-debugging-port=9222 启动，并保持登录"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_row("browser", "浏览器连接", False, f"{type(exc).__name__}: {exc}",
                           "检查 config.yaml 的 browser.cdp_url 与 Chrome 是否在跑"))

    # 4) 数据库写得进去吗（只写一个探针键）
    try:
        ctx.repo.set_kv("health:probe", "1")
        ctx.repo.conn.execute("DELETE FROM runtime_kv WHERE key = 'health:probe'")
        ctx.repo.conn.commit()
        checks.append(_row("db", "数据库可写", True, "读写正常"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_row("db", "数据库可写", False, f"{type(exc).__name__}: {exc}",
                           "检查 data/ 是否只读或磁盘是否写满"))

    # 5) 磁盘空间
    try:
        usage = shutil.disk_usage(str(ctx.repo.conn.execute("PRAGMA database_list").fetchone()[2] or "."))
        free_gb = usage.free / 1024 / 1024 / 1024
        checks.append(_row("disk", "磁盘空间", free_gb > 0.5, f"剩余 {free_gb:.1f} GB",
                           "清理磁盘", level="warn"))
    except Exception:  # noqa: BLE001 - 拿不到就别挡路
        pass

    # 6) 活跃时段
    open_now = in_send_window(cfg.safety.active_windows)
    checks.append(_row(
        "active_window", "活跃时段", open_now,
        "，".join(cfg.safety.active_windows or []) or "不限",
        "不在时段内不会开页面；要半夜跑就在设置 → 风控里清空这一项",
        level="warn",
    ))

    # 7) 平台：启用的是否都有驱动 + 城市能不能解析
    registry = build_default_registry()
    enabled = [key for key, plat in cfg.platforms.items() if plat.enabled]
    unknown_city = []
    for key in enabled:
        driver = registry.get(key)
        supported = dict(getattr(driver, "supported_cities", None) or {}) if driver else {}
        if not supported:
            continue
        for city in cfg.platforms[key].cities or []:
            if city.strip() and city.strip() not in supported and not city.strip().isdigit():
                unknown_city.append(f"{key}/{city}")
    checks.append(_row(
        "platforms", "平台配置", bool(enabled) and not unknown_city,
        f"启用 {len(enabled)} 个：" + "、".join(enabled) + (f"；城市不在内置表：{unknown_city}" if unknown_city else ""),
        "这些城市的页面不会被打开，等于白配；设置 → 平台 → 城市（表外城市会标红）",
        level="warn",
    ))

    # 8) 投递开关 vs 驱动能力（诚实提示，不算错）
    claimed = []
    for key in enabled:
        driver = registry.get(key)
        if cfg.platforms[key].apply and driver is not None and "apply" not in driver.capabilities:
            claimed.append(key)
    checks.append(_row(
        "apply_switch", "投递开关口径", not claimed,
        "投递开关打开但驱动未核验：" + "、".join(claimed) if claimed else "开关与能力一致",
        "打开也不会真的投递；关掉只是少一个误解",
        level="warn",
    ))

    failed = [row for row in checks if not row["ok"] and row["level"] == "error"]
    warned = [row for row in checks if not row["ok"] and row["level"] == "warn"]
    return {"ok": not failed, "checks": checks,
            "failed": [row["key"] for row in failed], "warned": [row["key"] for row in warned]}
