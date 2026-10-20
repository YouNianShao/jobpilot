"""自动监测：看板开着的时候，按 monitor.interval_minutes 在 monitor.working_hours 里自动跑「监测全部平台」。

以前 MonitorCfg(auto / interval_minutes / working_hours) 是一段没人读的死配置 —— 监测从来没自己跑过，
interactions 表一直是空的（2026-10-20 账号 B 核对）。

规则（每 tick 读一次**当前**配置，所以设置页保存之后不用重启就生效）：
- monitor.enabled 和 monitor.auto 都开着才跑
- 现在在 working_hours 里（多段逗号分隔，"08:00-00:00" = 到半夜；空 = 不限）
- 距上次自动监测 >= interval_minutes（上次时间存在 runtime_kv 'monitor:auto:last'，重启也记得）
- 没有别的任务正占着任何一个能监测的平台（监测会在那些平台上开页面）
- 没有跨平台整体冷却
都满足就 runner.start("monitor", "")；被拒（TaskAlreadyRunning / TaskNotRunnable）就下一 tick 再看。
真正开页面时照样过 Policy 的全部闸门（额度 / 节奏 / 开页面时段 / 冷却锁），这里不绕开任何一道。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..safety.policy import GLOBAL_LOCK_KEY
from ..safety.throttle import parse_windows

LAST_KEY = "monitor:auto:last"
TICK_SECONDS = 30.0
_TS = "%Y-%m-%d %H:%M:%S"


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.strptime(str(value), _TS)
    except (TypeError, ValueError):
        return None


def _in_windows(windows: list[tuple[int, int]], at: datetime) -> bool:
    if not windows:
        return True
    minutes = at.hour * 60 + at.minute
    return any(start <= minutes < end for start, end in windows)


def next_window_start(spec: str, now: datetime) -> datetime | None:
    """从 now 起最近一个落在时段里的时刻（now 本身在时段里就返回 now）；时段全空 = None。"""
    windows = parse_windows(spec)
    if not windows:
        return now
    if _in_windows(windows, now):
        return now
    for day in range(0, 2):
        base = (now + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
        for start, end in sorted(windows):
            if end <= start:
                continue                       # 空区间（开始 = 结束）一分钟都不开
            at = base + timedelta(minutes=start)
            if at > now:
                return at
    return None


def monitor_platforms(config: Any, registry: Any) -> set[str]:
    """开放了监测、而且启用了的平台（「监测全部」会在这些平台上开页面）。"""
    from ..platforms import supports

    return {key for key in registry.keys()
            if supports(registry.get(key), "monitor")
            and (config.platforms.get(key) is None or config.platforms[key].enabled)}


def _busy(runner: Any, platforms: set[str]) -> bool:
    if runner is None:
        return False
    getter = getattr(runner, "running_platforms", None)
    if callable(getter):
        return bool(set(getter()) & platforms)
    return bool(getattr(runner, "running", False))


def schedule(config: Any, repo: Any, runner: Any, *, registry: Any = None,
             now: datetime | None = None) -> dict:
    """现在该不该自动监测，以及下一次大概什么时候（给 /api/monitor/schedule 和调度线程共用）。

    返回 {auto, interval_minutes, working_hours, last_run, next_run, reason, due}；
    reason 是给人看的「为什么现在不跑」，能跑时为空串。
    """
    from ..platforms import build_default_registry

    now = (now or datetime.now()).replace(microsecond=0)
    cfg = config.monitor
    hours = (cfg.working_hours or "").strip()
    last_raw = repo.get_kv(LAST_KEY)
    last = _parse(last_raw)
    info = {
        "auto": bool(cfg.enabled and cfg.auto),
        "interval_minutes": int(cfg.interval_minutes),
        "working_hours": hours,
        "last_run": last.strftime(_TS) if last else None,
        "next_run": None,
        "reason": "",
        "due": False,
    }
    if not cfg.enabled:
        return {**info, "reason": "监测已关闭（设置 → 监测）"}
    if not cfg.auto:
        return {**info, "reason": "没开自动监测，只在你点「监测」时跑"}

    registry = registry or build_default_registry()
    platforms = monitor_platforms(config, registry)
    if not platforms:
        return {**info, "reason": "没有开放监测、而且启用了的平台"}

    earliest = last + timedelta(minutes=int(cfg.interval_minutes)) if last else now
    candidate = max(earliest, now)
    start = next_window_start(hours, candidate)
    if start is None:
        return {**info, "reason": f"监测时段 {hours} 里一分钟都没有"}
    info["next_run"] = start.strftime(_TS)

    if not _in_windows(parse_windows(hours), now):
        return {**info, "reason": f"不在监测时段 {hours}"}
    if earliest > now:
        return {**info, "reason": f"距上次自动监测不到 {int(cfg.interval_minutes)} 分钟"}
    lock = repo.get_lock(GLOBAL_LOCK_KEY)
    until = _parse((lock or {}).get("until"))
    if until is not None and until > now:
        info["next_run"] = max(until, start).strftime(_TS)
        return {**info, "reason": f"整体冷却中：{lock.get('reason') or '多个平台接连被拦'}"}
    if _busy(runner, platforms):
        return {**info, "reason": "有任务在跑，等它结束"}
    return {**info, "due": True}


class AutoMonitor:
    """后台守护线程：每 tick 看一眼 schedule()，到点就让 runner 跑一次「监测全部」。

    ``clock`` / ``sleep`` 可注入：测试里直接调 ``tick()``，不起线程、不等真时间。
    """

    def __init__(self, ctx_factory: Callable[[], Any], runner: Any, *,
                 clock: Callable[[], datetime] = datetime.now,
                 sleep: Callable[[float], Any] | None = None,
                 tick_seconds: float = TICK_SECONDS, registry: Any = None) -> None:
        self._ctx_factory = ctx_factory
        self._runner = runner
        self._clock = clock
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._tick_seconds = tick_seconds
        self._registry = registry
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def tick(self) -> dict:
        """检查一次；到点就启动监测任务。返回这次的判断（带 started / task_id）。"""
        from ..services.orchestrator import TaskNotRunnable
        from ..tasks import TaskAlreadyRunning

        ctx = self._ctx_factory()
        try:
            now = self._clock()
            plan = schedule(ctx.config, ctx.repo, self._runner, registry=self._registry, now=now)
            if not plan["due"]:
                return {**plan, "started": False}
            try:
                task = self._runner.start("monitor", "")
            except (TaskAlreadyRunning, TaskNotRunnable) as exc:
                return {**plan, "started": False, "reason": str(exc)}
            # 启动成功才记时间：被拒的话下一 tick 再试，不白等一个间隔
            ctx.repo.set_kv(LAST_KEY, now.strftime(_TS))
            return {**plan, "started": True, "task_id": getattr(task, "id", None)}
        finally:
            try:
                ctx.repo.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - 调度线程不能因为一次异常就永久停掉
                self.last_error = str(exc)
            self._sleep(self._tick_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="auto-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

