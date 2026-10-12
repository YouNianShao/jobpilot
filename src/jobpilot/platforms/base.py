"""平台驱动协议 + 驱动运行上下文。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Protocol, runtime_checkable

from ..browser.driver import BrowserDriver
from ..config import AppConfig
from ..db import Repo
from ..models import ApplyResult, CollectResult, Job, MonitorResult
from ..safety.policy import PAGE_OPEN, Action, Policy


class StopRequested(Exception):
    """协作式停止：任务被用户请求停止。"""


class StopSignal:
    """停止 + 暂停信号（替代老版的 control.py 全局事件）。"""

    def __init__(self, event: Event | None = None) -> None:
        self._stop = event or Event()
        self._pause = Event()
        self._pause.set()

    def request(self) -> None:
        self._stop.set()

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def check(self) -> None:
        """在安全点调用：若请求停止则抛 StopRequested，若暂停则阻塞。"""
        self._pause.wait()
        if self._stop.is_set():
            raise StopRequested()

    def sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self.check()
            time.sleep(min(0.5, max(0.0, end - time.time())))


@dataclass
class DriverContext:
    """平台驱动能用到的一切（不包含任何平台细节）。

    驱动**不能**自己开页面——必须 ``ctx.reserve(stage)`` 过安全内核拿到许可。
    """

    config: AppConfig
    repo: Repo
    browser: BrowserDriver
    policy: Policy
    stop: StopSignal
    platform: str = ""
    log: Callable[[str], None] = lambda message: None
    progress: Callable[[int, int, str], None] = lambda current, total, label: None

    # ---------------- 采集去重（别为了同样的结果重复开页面） ----------------
    def collect_seen_seconds(self, slot: str, page: int) -> float | None:
        """这一页上次采完是多久以前（秒）。**None = 没有记录**。

        注意：时间戳只精确到秒，所以"同一秒内采过"会算出 0.0 秒 ——
        用 0 当"没采过"的哨兵值会把这种情况漏掉（同一个测试当场抓到的 bug）。
        """
        raw = self.repo.get_kv(f"collect:{self.platform}:{slot}:p{int(page)}")
        if not raw:
            return None
        try:
            when = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return max(0.0, (datetime.now() - when).total_seconds())

    def skip_recently_collected(self, slot: str, page: int) -> float | None:
        """这一页该跳过吗？**None = 该采**；否则返回"多久前采过"的秒数（可能是 0.0）。

        **按页记账**而不是按词：一次采集被打断（点停止 / 崩了 / 网络断）时，
        已采完的页留着水位，下次只重做没做完的页 —— 否则中断一次就等于把整轮
        页面预算再花一遍（这个坑真踩过：白开 10 个详情页）。
        """
        hours = float(self.config.safety.recollect_after_hours or 0)
        if hours <= 0:
            return None
        seen = self.collect_seen_seconds(slot, page)
        if seen is None or seen >= hours * 3600:
            return None
        return seen

    def note_collect(self, slot: str, page: int) -> None:
        """这一页处理完了才记一笔（中途被打断就不记，下次会重做它）。"""
        self.repo.set_kv(f"collect:{self.platform}:{slot}:p{int(page)}",
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def reserve(self, stage: str) -> None:
        """开页面前过闸门；被拒时抛 ``safety.Denied``。

        闸门里可能含"节流"和"速率窗口"的等待，等待时长会写进任务日志——
        否则界面会看起来像卡住了。
        """
        permit = self.policy.authorize(Action(kind=PAGE_OPEN, platform=self.platform, stage=stage))
        if permit.waited_seconds >= 1:
            self.log(f"[safety] 放慢节奏：等了 {permit.waited_seconds:.0f} 秒再开下一页")


def _ago(seconds: float) -> str:
    """把"多久前"说成人话（0 秒是合法的：同一秒内又点了一次采集）。"""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


@runtime_checkable
class PlatformDriver(Protocol):
    key: str
    name: str
    capabilities: frozenset[str]

    def collect(self, ctx: DriverContext) -> CollectResult: ...

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult: ...

    def monitor(self, ctx: DriverContext) -> MonitorResult: ...


def supports(driver: PlatformDriver, capability: str) -> bool:
    return capability in driver.capabilities
