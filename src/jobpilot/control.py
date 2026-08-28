"""协作式任务暂停/继续控制。

任务在循环的安全点（每个岗位/每页之间）调用 ``yield_point()``。
若已暂停，该调用会阻塞当前任务线程，直到外部调用 ``resume()`` 唤醒。
状态为进程内全局共享，CLI 与 Web 服务共用同一解释器时一致。
"""
from __future__ import annotations

import threading

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
