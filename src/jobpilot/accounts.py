"""多账号：两个求职身份分开投递，一次只用一个。

每个账号各有一套：配置（简历 / 搜索关键词 / 城市 / 评分筛选 / 各平台开关）、数据库（岗位池 /
投递记录 / 今日额度 / 风控锁）、调试 Chrome 的登录资料、定制简历。切换账号 = 换这一整套。

两个账号**共用**（用户 2026-10-14 选的）：AI 模型和 API Key、风控规则（页面预算、时间窗、冷却、
投递间隔、每日上限的数值）。共用的意思是「规则一样」，**计数仍然各算各的**：
A 今天在 BOSS 投了 30 个，不影响 B 在 BOSS 还能投 30 个 —— 平台那边本来就是按账号算的。

目录：账号 A 沿用原来的位置（config.yaml / data/jobpilot.db / data/chrome-profile），
这样已有数据和已登录的浏览器都不用搬；账号 B 在 data/accounts/B/ 下。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import config as cfgmod

REGISTRY = cfgmod.DATA_DIR / "accounts.json"
DEFAULT_ACCOUNTS = [{"id": "A", "name": "账号 A"}, {"id": "B", "name": "账号 B"}]
# 每个账号的调试 Chrome 用不同端口：A 开着的时候切到 B，B 绝不会连到 A 的浏览器（那等于用 A 的身份操作）。
# 不用 9222：那是 Chrome 调试端口的默认值，别的自动化工具（BossHunter 的 DrissionPage 等）也抢它，
# 2026-10-14 就被一个无头 Chrome 占着，点「启动浏览器」没反应。登录态在 chrome-profile 目录里，和端口无关。
CDP_PORTS = {"A": 9333, "B": 9334}
# 以前用过的端口：配置里还是这些就自动换成上面的新端口（用户自己改成别的端口的不动）
LEGACY_PORTS = {9222, 9223, 9224}
# 两个账号共用的配置段（见模块说明）；平台段里只有 daily_limit 是共用的，其余（城市 / 关键词 / 开关）各管各的
SHARED_SECTIONS = ("ai", "safety", "apply")


@dataclass(frozen=True)
class AccountPaths:
    id: str
    config: Path
    db: Path
    data_dir: Path          # 这个账号自己的数据目录：定制简历、浏览器登录资料
    chrome_profile: Path
    resumes: Path


def paths_for(account_id: str) -> AccountPaths:
    if account_id == "A":
        data = cfgmod.DATA_DIR
        return AccountPaths("A", cfgmod.ROOT / "config.yaml", data / "jobpilot.db", data,
                            data / "chrome-profile", data / "resumes")
    data = cfgmod.DATA_DIR / "accounts" / account_id
    return AccountPaths(account_id, data / "config.yaml", data / "jobpilot.db", data,
                        data / "chrome-profile", data / "resumes")


def _read() -> dict:
    try:
        data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    known = {a["id"] for a in data.get("accounts") or []}
    accounts = list(data.get("accounts") or []) + [a for a in DEFAULT_ACCOUNTS if a["id"] not in known]
    active = data.get("active") if data.get("active") in {a["id"] for a in accounts} else "A"
    return {"active": active, "accounts": accounts}


def _write(data: dict) -> None:
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_account(account_id: str) -> AccountPaths:
    """第一次用某个账号时给它建好目录和配置：共用段照抄账号 A，自己的部分给空白起点。"""
    p = paths_for(account_id)
    if account_id != "A" and not p.config.exists():
        base = cfgmod.load_config(paths_for("A").config)
        fresh = cfgmod.AppConfig()
        for section in SHARED_SECTIONS:
            setattr(fresh, section, getattr(base, section).model_copy(deep=True))
        for key, plat in fresh.platforms.items():
            if key in base.platforms:
                plat.daily_limit = base.platforms[key].daily_limit
        fresh.profile.resume_output_dir = str(p.resumes)
        fresh.browser.cdp_url = default_cdp_url(account_id)
        p.data_dir.mkdir(parents=True, exist_ok=True)
        cfgmod.save_config(fresh, p.config)
    return p


def listing() -> dict:
    data = _read()
    out = []
    for acc in data["accounts"]:
        p = paths_for(acc["id"])
        out.append({**acc, "active": acc["id"] == data["active"], "ready": p.config.exists()})
    return {"active": data["active"], "accounts": out}


def active_id() -> str:
    return _read()["active"]


def active_name() -> str:
    data = _read()
    return next((a["name"] for a in data["accounts"] if a["id"] == data["active"]), data["active"])


def activate(account_id: str | None = None) -> AccountPaths:
    """把整个进程切到某个账号：所有读写配置 / 数据库的地方都指向它的那一套。"""
    data = _read()
    account_id = account_id or data["active"]
    if account_id not in {a["id"] for a in data["accounts"]}:
        raise ValueError(f"没有这个账号：{account_id}")
    p = ensure_account(account_id)
    migrate_port(account_id)
    if data["active"] != account_id or not REGISTRY.exists():
        data["active"] = account_id
        _write(data)
    _rebind(p)
    return p


def default_cdp_url(account_id: str) -> str:
    return f"http://127.0.0.1:{CDP_PORTS.get(account_id, 9340)}"


def migrate_port(account_id: str) -> bool:
    """配置里还是旧的本机端口（9222/9223/9224）就换成这个账号的新端口；返回是否改了。

    只改 cdp_url 这一行（文本替换），不整份重写配置。
    """
    import re
    from urllib.parse import urlsplit

    path = paths_for(account_id).config
    if not path.exists():
        return False
    parts = urlsplit(cfgmod.load_config(path).browser.cdp_url or "")
    if parts.hostname not in ("127.0.0.1", "localhost") or parts.port not in LEGACY_PORTS:
        return False
    text = path.read_text(encoding="utf-8")
    new, count = re.subn(r"(?m)^(\s*cdp_url:\s*).*$", lambda m: m.group(1) + default_cdp_url(account_id), text, count=1)
    if not count:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _rebind(p: AccountPaths) -> None:
    """模块里 ``from ..config import CONFIG_PATH`` 拿到的是导入时的值，所以每个用到的模块都要改一遍。"""
    import importlib

    cfgmod.CONFIG_PATH, cfgmod.DB_PATH, cfgmod.ACCOUNT_DIR = p.config, p.db, p.data_dir
    for name in ("jobpilot.bootstrap", "jobpilot.web.app", "jobpilot.services.backup", "jobpilot.cli"):
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        for attr, value in (("CONFIG_PATH", p.config), ("DB_PATH", p.db), ("ACCOUNT_DIR", p.data_dir)):
            if hasattr(mod, attr):
                setattr(mod, attr, value)


def sync_shared(source: cfgmod.AppConfig, account_id: str) -> list[str]:
    """一个账号保存配置后，把共用的部分（AI、风控规则、各平台每日上限的数值）写进其它账号。"""
    touched = []
    for acc in _read()["accounts"]:
        if acc["id"] == account_id:
            continue
        p = paths_for(acc["id"])
        if not p.config.exists():
            continue
        other = cfgmod.load_config(p.config)
        for section in SHARED_SECTIONS:
            setattr(other, section, getattr(source, section).model_copy(deep=True))
        for key, plat in other.platforms.items():
            if key in source.platforms:
                plat.daily_limit = source.platforms[key].daily_limit
        cfgmod.save_config(other, p.config)
        touched.append(acc["id"])
    return touched
