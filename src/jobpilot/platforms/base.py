from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any

from jobpilot.browser import Browser
from jobpilot.db import get_conn


class PageReadError(RuntimeError):
    """页面脚本返回 None（加载失败/被拦截）时抛出，与"列表为空"区分开。"""


class BaseAdapter(ABC):
    """站点适配器基类。每个招聘平台实现一个子类。"""

    key: str          # 配置中的平台标识，如 "51job"
    name: str         # 中文名，如 "前程无忧"
    # 平台能力集：collect / apply / monitor。用能力集而非单一布尔，便于表达
    # "可投递但没有监听" 这类组合。默认支持采集 + 投递。
    capabilities: frozenset[str] = frozenset({"collect", "apply"})

    def supports(self, capability: str) -> bool:
        """该平台是否具备某项能力（collect / apply / monitor）。"""
        return capability in self.capabilities

    @property
    def collect_only(self) -> bool:
        """仅采集平台（不支持自动投递）——由能力集推导，保留旧接口语义。"""
        return "apply" not in self.capabilities

    def read_list(
        self,
        browser: Browser,
        target_id: str,
        script: str,
        *,
        retries: int = 1,
        wait: float = 2.0,
        label: str = "页面列表",
    ) -> list:
        """读取页面列表，区分"JS 返回 None（失败）"与"空列表（无更多结果）"。

        失败时重试；仍失败抛 ``PageReadError``（fail-closed），避免把加载失败
        当成"没有更多结果"而静默截断采集。
        """
        last: Exception | None = None
        for attempt in range(max(1, retries + 1)):
            raw = browser.evaluate(target_id, script)
            if isinstance(raw, list):
                return raw
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                    return data if isinstance(data, list) else []
                except (json.JSONDecodeError, ValueError) as exc:
                    last = exc
            elif raw is not None:
                last = ValueError(f"脚本返回类型异常: {type(raw).__name__}")
            if attempt < retries:
                time.sleep(wait)
        raise PageReadError(f"{label}读取失败（页面加载异常或被拦截）：{last}")

    @abstractmethod
    def collect(self, cfg: dict, browser: Browser) -> int:
        """采集岗位，返回新增数量。"""

    @abstractmethod
    def apply(self, cfg: dict, browser: Browser, job: dict) -> str:
        """对单个岗位执行投递，返回状态字符串：
        applied / unsupported / skipped / failed / need_login。"""
