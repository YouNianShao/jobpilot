from __future__ import annotations

from typing import Any

import httpx


class Browser:
    """浏览器桥接客户端：对接 BossHunter 风格的运行时代理 (默认 3457)。

    JobPilot 使用独立的 Chrome 实例(9223)与代理(3457)，与 BossHunter(9222/3456)隔离，
    但复用同一套 HTTP 桥接协议（new_tab / eval / click / scroll ...）。
    """

    def __init__(self, proxy_url: str = "http://127.0.0.1:3457") -> None:
        self.base = proxy_url.rstrip("/")
        self._c = httpx.Client(base_url=self.base, trust_env=False, timeout=30)

    # --- lifecycle ---
    def health(self) -> dict | None:
        return self._get_json("/health", timeout=3)

    def targets(self) -> list[dict]:
        data = self._get_json("/targets", timeout=5)
        return data if isinstance(data, list) else []

    def new_tab(self, url: str) -> str | None:
        data = self._get_json("/new", params={"url": url}, timeout=20)
        return data.get("targetId") if isinstance(data, dict) else None

    def close_tab(self, target_id: str) -> bool:
        return self._ok("GET", "/close", params={"target": target_id}, timeout=5)

    def navigate(self, target_id: str, url: str) -> bool:
        return self._ok("GET", "/navigate", params={"target": target_id, "url": url}, timeout=15)

    def info(self, target_id: str) -> dict | None:
        return self._get_json("/info", params={"target": target_id}, timeout=5)

    def evaluate(self, target_id: str, expression: str, timeout: float = 30) -> Any:
        try:
            r = self._c.post("/eval", params={"target": target_id}, content=expression, timeout=timeout)
            if r.status_code == 200 and r.content:
                data = r.json()
                return data.get("value") if isinstance(data, dict) else None
        except httpx.HTTPError:
            return None
        return None

    def click(self, target_id: str, selector: str) -> bool:
        return self._post_ok("/click", params={"target": target_id}, content=selector, timeout=10)

    def click_at(self, target_id: str, selector_or_xy: str) -> bool:
        return self._post_ok("/clickAt", params={"target": target_id}, content=selector_or_xy, timeout=10)

    def type_text(self, target_id: str, text: str) -> bool:
        return self._post_ok("/type", params={"target": target_id}, content=text, timeout=10)

    def scroll(self, target_id: str, y: int = 0, direction: str = "") -> bool:
        params: dict[str, Any] = {"target": target_id}
        if direction:
            params["direction"] = direction
        else:
            params["y"] = y
        return self._ok("GET", "/scroll", params=params, timeout=5)

    def wait_for_load(self, target_id: str, timeout: float = 10.0) -> bool:
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.info(target_id)
            if info and info.get("ready") == "complete":
                return True
            time.sleep(0.5)
        return False

    # --- internals ---
    def _get_json(self, path: str, params: dict | None = None, timeout: float = 5) -> Any:
        try:
            r = self._c.get(path, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
        except (httpx.HTTPError, ValueError):
            return None
        return None

    def _ok(self, method: str, path: str, params: dict | None = None, timeout: float = 5) -> bool:
        try:
            r = self._c.request(method, path, params=params, timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def _post_ok(self, path: str, params: dict, content: str, timeout: float = 10) -> bool:
        try:
            r = self._c.post(path, params=params, content=content, timeout=timeout)
            return r.status_code == 200
        except httpx.HTTPError:
            return False
