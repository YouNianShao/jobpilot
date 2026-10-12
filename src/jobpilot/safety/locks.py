"""按平台的冷却锁：某个平台命中验证码/限流后只锁它，不连坐其它平台。"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..db import Repo

RISK_KINDS = ("captcha", "rate_limit", "blocked")


class RiskLock:
    def __init__(self, repo: Repo, default_minutes: int = 30) -> None:
        self.repo = repo
        self.default_minutes = default_minutes

    def lock(self, platform: str, reason: str, kind: str, minutes: int | None = None) -> str:
        until = datetime.now() + timedelta(minutes=minutes if minutes is not None else self.default_minutes)
        stamp = until.strftime("%Y-%m-%d %H:%M:%S")
        self.repo.set_lock(platform, reason, kind, stamp)
        return stamp

    def active(self, platform: str) -> dict | None:
        """返回仍生效的锁；过期自动清除。"""
        lock = self.repo.get_lock(platform)
        if not lock:
            return None
        try:
            until = datetime.strptime(lock["until"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            self.repo.clear_lock(platform)
            return None
        if until <= datetime.now():
            self.repo.clear_lock(platform)
            return None
        lock["remaining_minutes"] = max(1, int((until - datetime.now()).total_seconds() // 60) + 1)
        return lock

    def clear(self, platform: str) -> None:
        self.repo.clear_lock(platform)
