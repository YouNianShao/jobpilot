"""遇到验证 / 掉登录：把那一页留给你处理，而不是只在任务结束时写一句日志。

验证码、滑块、短信验证、登录过期，都是**只有人能处理**的事。以前平台代码在 finally 里
一律把页面关掉，任务停下后你只能在日志里看到「命中风控」，再自己去找是哪个网址。现在：

1. 抛 PlatformRisk(captcha) / LoginRequired 时，在异常上记下是哪一页（``tag_page``）；
2. 平台代码关页面统一走 ``release_page``：正是那一页、而且是验证 / 掉登录，就不关；
3. 服务层接住异常后 ``flag``：写一条「待你处理」到 runtime_kv，并把那个标签页切到前台。

只切标签页（CDP 的 /json/activate），不在页面上点任何东西、不填任何东西。
限流 / 封禁不留页面：那种情况你在页面上什么都做不了，留着只会让人想点「刷新」。
"""

from __future__ import annotations

import json
from datetime import datetime

from ..db import Repo

PREFIX = "attention:"
_TS = "%Y-%m-%d %H:%M:%S"


def attention_kind(exc: BaseException | None) -> str | None:
    """这个异常是不是要人来处理：验证码 -> "captcha"，掉登录 -> "login"，其它 -> None。"""
    from ..models import LoginRequired, PlatformRisk

    if isinstance(exc, LoginRequired):
        return "login"
    if isinstance(exc, PlatformRisk):
        if exc.kind == "captcha":
            return "captcha"
        if exc.kind == "login_required":
            return "login"
    return None


def tag_page(exc: BaseException, page) -> BaseException:
    """在异常上记下「是哪一页出的事」，返回异常本身（写法：``raise tag_page(Exc(...), page)``）。

    只记第一次：外层代码再包一层时不要把真正出事的那页换掉。
    """
    if attention_kind(exc) and getattr(exc, "page", None) is None:
        exc.page = page
        try:
            exc.url = str(page.url or "")
        except Exception:  # noqa: BLE001 - 页面读不出网址也照样留着
            exc.url = ""
    return exc


def release_page(page) -> None:
    """平台代码 finally 里关页面用它：正在抛的是验证 / 掉登录、而且就是这一页，就留着不关。

    在 finally 里 sys.exc_info() 拿到的就是正在往外抛的异常（Python 3.11 实测）。
    """
    import sys

    if page is None:
        return
    exc = sys.exc_info()[1]
    if attention_kind(exc) and getattr(exc, "page", None) is page:
        return
    try:
        page.close()
    except Exception:  # noqa: BLE001 - 关闭失败不影响结果
        pass


def flag(repo: Repo, platform: str, exc: BaseException, browser=None) -> dict | None:
    """服务层接住验证 / 掉登录后调用：记一条「待你处理」，并把那一页切到前台。不是这两类就什么都不做。"""
    kind = attention_kind(exc)
    if kind is None:
        return None
    url = str(getattr(exc, "url", "") or "")
    item = {
        "platform": platform, "kind": kind,
        "at": datetime.now().strftime(_TS), "url": url,
        "message": str(getattr(exc, "detail", "") or exc) or kind,
    }
    repo.set_kv(PREFIX + platform, json.dumps(item, ensure_ascii=False))
    cdp_url = getattr(browser, "cdp_url", "") if browser is not None else ""
    if url and cdp_url:
        # 切到前台失败不要紧：通知里也写了去调试浏览器里找那一页
        item["activated"] = activate_tab(cdp_url, url)
    return item


def get(repo: Repo, platform: str) -> dict | None:
    raw = repo.get_kv(PREFIX + platform)
    if not raw:
        return None
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    return item if isinstance(item, dict) else None


def clear(repo: Repo, platform: str) -> None:
    repo.conn.execute("DELETE FROM runtime_kv WHERE key = ?", (PREFIX + platform,))
    repo.conn.commit()


def active(repo: Repo, policy, platforms) -> list[dict]:
    """还没处理的提醒。验证码那条跟着平台锁走：锁到期了说明已经冷却过，提醒也就不再挂着。"""
    items = []
    for platform in platforms:
        item = get(repo, platform)
        if item is None:
            continue
        if item.get("kind") == "captcha" and not policy.locks.active(platform):
            clear(repo, platform)
            continue
        items.append(item)
    return items


def resume(repo: Repo, policy, platform: str) -> tuple[bool, str]:
    """「我已处理，继续」：只解除验证码锁和掉登录提醒；降速惩罚**保留**（出过验证码说明节奏已经踩线）。

    限流 / 封禁 / 连续页面异常的锁不让手动解：那是平台在说「慢点」，马上重试最容易被封号。
    整体锁（同一天两个平台都被拦）也不让解：那说明是账号行为被盯上了，不是某一页的问题。
    返回 (成功与否, 给人看的话)。
    """
    from .policy import GLOBAL_LOCK_KEY

    whole = policy.locks.active(GLOBAL_LOCK_KEY)
    if whole:
        return False, (f"现在是整体冷却（{whole['reason']}），不能手动解除："
                       f"同一天几个平台都被拦，马上继续很容易封号，请等锁到期（还剩 {whole['remaining_minutes']} 分钟）")
    item = get(repo, platform)
    lock = policy.locks.active(platform)
    if lock and lock.get("kind") != "captcha":
        return False, (f"这是平台限流 / 封禁（{lock.get('reason') or lock.get('kind')}），马上重试容易封号，"
                       f"请等锁到期（还剩 {lock['remaining_minutes']} 分钟）")
    if not lock and not (item and item.get("kind") == "login"):
        clear(repo, platform)
        return False, "这个平台现在没有等你处理的验证或登录"
    policy.clear_risk(platform)
    clear(repo, platform)
    what = "验证" if lock else "登录"
    try:
        repo.add_event(None, "risk_resume", f"{platform} 你已处理{what}，解除冷却（降速保留）")
    except Exception:  # noqa: BLE001 - 记录失败不影响解除
        pass
    return True, "已解除，可以重新开始这个平台的任务（降速还在，节奏会慢一些）"


def activate_tab(cdp_url: str, url: str, timeout: float = 3.0) -> bool:
    """在调试 Chrome 里把这个网址所在的标签页切到前台（CDP HTTP /json/activate）。找不到就返回 False。"""
    from urllib.parse import urlsplit
    from urllib.request import urlopen

    from ..browser.launcher import debug_port

    host, port = debug_port(cdp_url)
    base = f"http://{host}:{port}"
    try:
        with urlopen(base + "/json/list", timeout=timeout) as resp:
            targets = [t for t in json.loads(resp.read().decode("utf-8")) if t.get("type") == "page"]
    except Exception:  # noqa: BLE001 - 浏览器连不上就算了
        return False
    target = next((t for t in targets if t.get("url") == url), None)
    if target is None:
        # 验证页可能在我们记下网址之后又跳了一下：退一步按同一个网站找。
        # /json/list 最新开的标签页排在最前（2026-10-19 用无头 Chrome 实测），所以取第一个
        site = urlsplit(url).hostname or ""
        same = [t for t in targets if site and (urlsplit(t.get("url") or "").hostname or "") == site]
        target = same[0] if same else None
    if target is None or not target.get("id"):
        return False
    try:
        with urlopen(base + "/json/activate/" + str(target["id"]), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False
