"""节流：发送时间窗、随机休息日、随机抖动、渐进退避、可中断等待。"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime
from threading import Event

_WINDOW = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")


def parse_windows(spec) -> list[tuple[int, int]]:
    """把 "09:00-21:00" 或 ["09:00-12:00","14:00-21:00"] 解析成分钟区间。"""
    if not spec:
        return []
    parts = [p for p in re.split(r"[,;，；\s]+", spec) if p] if isinstance(spec, str) else [str(p) for p in spec]
    windows: list[tuple[int, int]] = []
    for part in parts:
        for m in _WINDOW.finditer(part):
            sh, sm, eh, em = (int(m.group(i)) for i in range(1, 5))
            windows.append((sh * 60 + sm, eh * 60 + em))
    return windows


def in_send_window(spec, now: datetime | None = None) -> bool:
    """未配置窗口 = 不限制。"""
    windows = parse_windows(spec)
    if not windows:
        return True
    cur = now or datetime.now()
    minutes = cur.hour * 60 + cur.minute
    return any(start <= minutes < end for start, end in windows)


def should_take_day_off(probability: float) -> bool:
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    return random.random() < probability


def human_interval(low: float, high: float) -> float:
    """在 [low, high] 里抽一个「像人」的间隔。

    为什么不用 random.uniform：均匀分布的期望「每分钟操作数」是一条平直的线，
    这本身就是个可以统计的特征。真人是有中心节奏的 —— 多数时候接近某个值，
    偶尔快一点、偶尔停一下 —— 所以这里用截断正态：均值居中，±3σ 正好覆盖整个区间，
    超出范围就截回来（保证仍然落在配置的上下限内）。
    """
    low, high = min(low, high), max(low, high)
    if high <= low:
        return low
    mean = (low + high) / 2
    sigma = (high - low) / 6
    return min(high, max(low, random.gauss(mean, sigma)))

def jitter(seconds: float, spread: float = 0.25) -> float:
    """给等待时间加 ±spread 抖动，避免机械节奏。"""
    if seconds <= 0:
        return 0.0
    delta = seconds * spread
    return max(0.0, random.uniform(seconds - delta, seconds + delta))


class Backoff:
    """连续失败时逐级拉长等待：60 → 120 → 300 秒。成功即清零。"""

    def __init__(self) -> None:
        self.errors = 0

    def record_success(self) -> None:
        self.errors = 0

    def record_error(self) -> float:
        self.errors += 1
        return {1: 60.0, 2: 120.0}.get(self.errors, 300.0)

    @property
    def should_stop(self) -> bool:
        return self.errors >= 3


def sleep_checked(seconds: float, stop: Event | None = None) -> None:
    """分段睡眠，期间可被停止事件打断。"""
    end = time.time() + max(0.0, seconds)
    while time.time() < end:
        if stop is not None and stop.is_set():
            return
        time.sleep(min(0.5, max(0.0, end - time.time())))
