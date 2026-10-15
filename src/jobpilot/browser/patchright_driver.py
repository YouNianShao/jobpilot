"""patchright 实现的真实浏览器驱动：连接用户**已在运行**的 Chrome（CDP）。

原则：
- 只连接，不启动、不关闭用户的浏览器（close() 只断开 Playwright 驱动）
- 复用用户的登录态（connect_over_cdp 拿到的就是真实 profile）
- 不做指纹伪装；风控交给 safety 层
"""

from __future__ import annotations

from typing import Any

from .driver import Page

NAV_TIMEOUT_MS = 20000
CLICK_TIMEOUT_MS = 8000
# 连浏览器的超时：默认 3 分钟，浏览器状态异常时任务就干等 3 分钟才报错（2026-10-15 真踩过）
CONNECT_TIMEOUT_MS = 20000

# 驱动进程挂了 / 连接断了时 patchright 抛出的原文：碰到这些就重连一次再试
_DEAD = ("Connection closed", "Target page, context or browser has been closed", "Browser has been closed",
         "browser has disconnected", "Target closed")
_PATCH_CHECKED = False


def is_dead_connection(exc: BaseException) -> bool:
    return any(word in str(exc) for word in _DEAD)


class PatchrightPage:
    def __init__(self, page: Any) -> None:
        self._page = page

    @property
    def url(self) -> str:
        return self._page.url or ""

    @property
    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:  # noqa: BLE001 - 页面可能已关闭
            return ""

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    def text(self) -> str:
        return self._page.evaluate(
            "(document.body && document.body.innerText || '').slice(0, 4000)"
        ) or ""

    def html(self) -> str:
        return self._page.content()

    def eval(self, js: str) -> Any:
        return self._page.evaluate(js)

    def click(self, selector: str, timeout_ms: int = CLICK_TIMEOUT_MS) -> None:
        self._page.locator(selector).first.click(timeout=timeout_ms)

    def fill(self, selector: str, value: str) -> None:
        self._page.locator(selector).first.fill(value)

    def inner_text(self, selector: str) -> str:
        try:
            return self._page.locator(selector).first.inner_text(timeout=3000)
        except Exception:  # noqa: BLE001 - 元素不存在时返回空
            return ""

    def exists(self, selector: str) -> bool:
        return bool(self._page.evaluate("s => Boolean(document.querySelector(s))", selector))

    def wait_for(self, selector: str, timeout_ms: int = 8000) -> bool:
        """等待元素出现（SPA 必须靠等待，不能靠 sleep）。"""
        try:
            self._page.wait_for_selector(selector, timeout=timeout_ms, state="attached")
            return True
        except Exception:  # noqa: BLE001 - 超时即视为未出现
            return False

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=False)

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:  # noqa: BLE001
            pass


class PatchrightBrowser:
    """连接一个已开启远程调试的 Chrome；不会启动或关闭它。"""

    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure(self):
        global _PATCH_CHECKED
        if self._context is not None:
            return self._context
        from patchright.sync_api import sync_playwright

        if not _PATCH_CHECKED:
            # 每个进程第一次连浏览器前检查一次驱动补丁（见 driver_patch.py：标签页刚开就关会把驱动杀掉）
            from .driver_patch import ensure_patched

            _PATCH_CHECKED = True
            try:
                ensure_patched()
            except OSError:
                pass
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=CONNECT_TIMEOUT_MS)
        except Exception as exc:
            self.close()
            if "Timeout" in str(exc):
                raise RuntimeError(
                    f"连不上浏览器：{CONNECT_TIMEOUT_MS // 1000} 秒内没有响应。调试 Chrome 可能卡住了"
                    "（比如有标签页一直在加载）：把它整个关掉，再点左下角「启动浏览器」") from exc
            raise
        if not self._browser.contexts:
            raise RuntimeError(f"已连上 CDP 但没有浏览器上下文（请确认 Chrome 已登录）: {self.cdp_url}")
        self._context = self._browser.contexts[0]
        return self._context

    def health(self) -> bool:
        """CDP 端口活着吗？

        这里**故意不去连 Playwright**：connect_over_cdp 在浏览器状态异常时会卡住几十秒
        到几分钟，而 health() 会被 /api/health 和「系统体检」调用 —— 结果是「浏览器连不上」
        这条信息本身表现成「接口卡死」，连启动自检都被拖垮（真踩过）。
        打一下 /json/version 就行，2 秒超时，永远给得出答案。
        """
        import urllib.request

        base = self.cdp_url.rstrip("/")
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001 - 连不上就是没连上
            return False

    def open(self, url: str) -> Page:
        try:
            page = self._ensure().new_page()
        except Exception as exc:  # noqa: BLE001
            if not is_dead_connection(exc):
                raise
            # 驱动进程挂了（浏览器还开着）：断开、重连一次。任务不必因为这一下就整个失败
            self.close()
            page = self._ensure().new_page()
        wrapped = PatchrightPage(page)
        wrapped.goto(url)
        return wrapped

    def find(self, url_contains: str) -> Page | None:
        for page in self._ensure().pages:
            if url_contains in (page.url or ""):
                return PatchrightPage(page)
        return None

    def close(self) -> None:
        """只断开 Playwright 驱动，不动用户的 Chrome。"""
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._browser = None
        self._pw = None
