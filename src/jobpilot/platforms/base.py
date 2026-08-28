from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from jobpilot.browser import Browser
from jobpilot.db import get_conn


class BaseAdapter(ABC):
    """站点适配器基类。每个招聘平台实现一个子类。"""

    key: str          # 配置中的平台标识，如 "51job"
    name: str         # 中文名，如 "前程无忧"
    collect_only: bool = False  # True 表示仅支持采集，投递需用户手动（平台反爬封锁）

    @abstractmethod
    def collect(self, cfg: dict, browser: Browser) -> int:
        """采集岗位，返回新增数量。"""

    @abstractmethod
    def apply(self, cfg: dict, browser: Browser, job: dict) -> str:
        """对单个岗位执行投递，返回状态字符串：
        applied / unsupported / skipped / failed / need_login。"""
