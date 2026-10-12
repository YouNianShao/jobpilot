from .driver import BrowserDriver, Page
from .fake import FakeBrowser, FakePage


def create_browser(cdp_url: str) -> BrowserDriver:
    """真实浏览器（懒加载 patchright，避免没装时影响纯逻辑测试）。"""
    from .patchright_driver import PatchrightBrowser

    return PatchrightBrowser(cdp_url)


__all__ = ["BrowserDriver", "Page", "FakeBrowser", "FakePage", "create_browser"]
