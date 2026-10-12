"""页面预算：账号级的"每天最多打开多少页面"硬约束。

采集、投递、监测开页面前都必须先 reserve()，超限直接抛 BudgetExceeded。
这是防封号最有效的一条，不能靠各驱动自觉。
"""

from __future__ import annotations

from ..config import DailyPages
from ..db import Repo
from ..models import BudgetExceeded

STAGES = ("collect_search", "collect_detail", "apply", "monitor")


class Budget:
    def __init__(self, repo: Repo, limits: DailyPages,
                 overrides: dict[str, DailyPages] | None = None) -> None:
        self.repo = repo
        self.limits = limits
        self.overrides = overrides or {}

    def limits_for(self, platform: str) -> DailyPages:
        return self.overrides.get(platform, self.limits)

    def _limit(self, stage: str, platform: str = "") -> int:
        return int(getattr(self.limits_for(platform), stage, 0) or 0)

    def used(self, platform: str, stage: str | None = None) -> int:
        return self.repo.count_pages(platform, stage=stage)

    def remaining(self, platform: str, stage: str) -> int:
        limit = self._limit(stage, platform)
        if limit <= 0:
            return -1  # -1 = 不限
        return max(0, limit - self.used(platform, stage))

    def reserve(self, platform: str, stage: str) -> None:
        """申请一次开页面额度；超限抛 BudgetExceeded（上限按平台解析）。"""
        limit = self._limit(stage, platform)
        if limit and self.used(platform, stage) >= limit:
            raise BudgetExceeded(platform, stage, limit)
        total = int(self.limits_for(platform).total or 0)
        if total and self.used(platform) >= total:
            raise BudgetExceeded(platform, "total", total)
        self.repo.record_page(platform, stage)
