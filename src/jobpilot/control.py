"""协作式任务暂停/继续控制。

任务在循环的安全点（每个岗位/每页之间）调用 ``yield_point()``。
若已暂停，该调用会阻塞当前任务线程，直到外部调用 ``resume()`` 唤醒。
状态为进程内全局共享，CLI 与 Web 服务共用同一解释器时一致。
"""
from __future__ import annotations

import threading
import time

_pause_event = threading.Event()
_pause_event.set()  # 默认未暂停（事件置位=放行）


def pause() -> None:
    """暂停：清空事件，使后续 yield_point 阻塞。"""
    _pause_event.clear()


def resume() -> None:
    """继续：置位事件，唤醒所有阻塞中的 yield_point。"""
    _pause_event.set()


def is_paused() -> bool:
    """当前是否处于暂停状态。"""
    return not _pause_event.is_set()


def yield_point() -> None:
    """在安全点调用：若已暂停则阻塞，直至继续。"""
    _pause_event.wait()


def sleep_checked(seconds: float) -> None:
    """可被暂停打断的分段睡眠：每 1 秒经过一个 yield_point。

    用于投递间隔/退避等待——暂停时不会傻等整段时长。
    """
    remaining = float(seconds)
    while remaining > 0:
        yield_point()
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
