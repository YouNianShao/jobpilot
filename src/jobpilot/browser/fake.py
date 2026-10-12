"""测试用假浏览器：按脚本返回页面，不需要 Chrome。"""

from __future__ import annotations

from typing import Any


class FakePage:
    def __init__(self, url: str = "", title: str = "", text: str = "", html: str = "",
                 selector_text: dict[str, str] | None = None,
                 evals: dict[str, Any] | None = None,
                 match_url: str | None = None) -> None:
        # match_url: 模拟重定向——按 A 打开，实际落在 B（page.url 为 B）
        self.match_url = match_url
        self._url = url
        self._title = title
        self._text = text
        self._html = html
        self.selector_text = selector_text or {}
        self.evals = evals or {}
        self.clicks: list[str] = []
        self.fills: dict[str, str] = {}
        self.closed = False

    # --- Page 协议 ---
    @property
    def url(self) -> str:
        return self._url

    @property
    def title(self) -> str:
        return self._title

    def goto(self, url: str) -> None:
        self._url = url

    def text(self) -> str:
        return self._text

    def html(self) -> str:
        return self._html

    def eval(self, js: str) -> Any:
        for key, value in self.evals.items():
            if key in js:
                return value
        return None

    def click(self, selector: str, timeout_ms: int = 8000) -> None:
        self.clicks.append(selector)

    def fill(self, selector: str, value: str) -> None:
        self.fills[selector] = value

    def _any_matches(self, selector: str) -> bool:
        """支持逗号分隔选择器（任何一段命中即算命中），与真实 CSS 语义一致。"""
        parts = [part.strip() for part in str(selector or "").split(",") if part.strip()]
        return any(part in self.selector_text for part in parts)

    def inner_text(self, selector: str) -> str:
        for part in [p.strip() for p in str(selector or "").split(",") if p.strip()]:
            if part in self.selector_text:
                return self.selector_text[part]
        return ""

    def exists(self, selector: str) -> bool:
        return self._any_matches(selector)

    def wait_for(self, selector: str, timeout_ms: int = 8000) -> bool:
        return self._any_matches(selector)

    def screenshot(self, path: str) -> None:
        self.shot = path

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, pages: list[FakePage] | None = None) -> None:
        self.pages = list(pages or [])
        self.opened: list[FakePage] = []

    def health(self) -> bool:
        return True

    def open(self, url: str) -> FakePage:
        for page in self.pages:
            if (page.match_url or page.url) == url:
                return page
        page = FakePage(url=url)
        self.opened.append(page)
        return page

    def find(self, url_contains: str) -> FakePage | None:
        for page in [*self.pages, *self.opened]:
            if url_contains in page.url:
                return page
        return None

    def close(self) -> None:
        for page in [*self.pages, *self.opened]:
            page.close()
