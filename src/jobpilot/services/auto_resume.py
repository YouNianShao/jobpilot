"""自动续采：采集被**速率窗口**（10 分钟 / 1 小时开页上限）拦在半路时，等窗口重新打开后自动接着采没采完的组合。

实测（账号 B）：每次采集 / 全流程都被「最近 1 小时开页面已达上限」停在半路，要等二三十分钟，
以前只能你自己记着回来再点一次。现在停手时把「还没采完的组合」记进 runtime_kv
``collect:resume:{platform}``，看板开着时后台每 30 秒看一眼，到点、条件都满足就自动起一个只采这些组合的任务。

只认速率窗口：当日额度用完（明天再说，没采到的组合进「下次先补」）、风控锁、你点了停止 —— 都不续采。
续采任务和你手动点的一样过 Policy 的全部闸门（额度 / 节奏 / 速率 / 开页面时段 / 冷却锁），这里不绕开任何一道。

续采条件（每 tick 读一次**当前**配置和状态）：
- 现在 >= not_before（窗口重新打开的时刻）
- 在 safety.active_windows 里
- 这个平台没有任务在跑（runner.running_platforms()）
- 没有整体冷却 / 平台冷却锁
- 这个平台今天的搜索页额度还有剩
超过 12 小时还没续上 / 当日额度已经用完：丢掉这条续采（原因记进事件），没采到的组合留在「下次先补」里。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from ..platforms.base import read_carry
from ..safety.policy import GLOBAL_LOCK_KEY
from ..safety.throttle import in_send_window

RESUME_KEY = "collect:resume:{platform}"
MAX_AGE_HOURS = 12.0
TICK_SECONDS = 30.0
_TS = "%Y-%m-%d %H:%M:%S"


def _key(platform: str) -> str:
    return RESUME_KEY.format(platform=platform)


def _parse(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), _TS)
    except (TypeError, ValueError):
        return None


def read_resume(repo: Any, platform: str) -> dict | None:
    raw = repo.get_kv(_key(platform))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("slots"):
        return None
    return data


def clear_resume(repo: Any, platform: str) -> None:
    repo.set_kv(_key(platform), "")


def record_resume(repo: Any, policy: Any, platform: str, *, mode: str, slots: list[str], labels: list[str],
                  options: dict | None = None, now: datetime | None = None) -> str:
    """记一条待续采；返回大约几点接着采（YYYY-MM-DD HH:MM:SS）。"""
    now = (now or datetime.now()).replace(microsecond=0)
    safety = policy.config.safety
    ten = policy._window_wait(platform, 600, safety.per_10min_for(platform))
    hour = policy._window_wait(platform, 3600, safety.per_hour_for(platform))
    wait = max(ten, hour)
    window = "1 小时" if hour >= ten else "10 分钟"
    if wait <= 0:
        wait = 60.0        # 理论上不会：被速率拦下时窗口一定是满的；保守地等一分钟
    at = now + timedelta(seconds=int(wait + 0.999))
    opts = {k: v for k, v in (options or {}).items() if v}
    if mode == "full":
        opts["apply_after"] = False       # 自动续采只采集 + 评分 + 写招呼语，绝不自动投递
    record = {
        "mode": mode, "slots": list(slots), "labels": list(labels), "options": opts,
        "window": window, "not_before": int(at.timestamp()), "created_at": int(now.timestamp()),
    }
    repo.set_kv(_key(platform), json.dumps(record, ensure_ascii=False))
    return at.strftime(_TS)


def start_args(platform: str, record: dict) -> tuple[str, str, dict]:
    """把续采记录翻成 runner.start(mode, platform, **options) 的参数。"""
    opts = dict(record.get("options") or {})
    slots = list(record.get("slots") or [])
    search = {k: opts[k] for k in ("keywords", "cities", "max_pages") if opts.get(k)}
    if record.get("mode") == "full":
        plan = {"platform": platform, **search, "slots": slots}
        return "full", "", {"plans": [plan], "apply_after": False}
    return "collect", platform, {**search, "slots": slots}


def decide(ctx: Any, runner: Any, platform: str, record: dict, now: datetime) -> dict:
    """要不要现在续采：{"action": "start" | "wait" | "drop", "reason": 人话}。"""
    try:
        age = now.timestamp() - float(record.get("created_at"))
    except (TypeError, ValueError):
        age = MAX_AGE_HOURS * 3600 + 1
    if age > MAX_AGE_HOURS * 3600:
        return {"action": "drop", "reason": f"超过 {MAX_AGE_HOURS:g} 小时还没续上，不再自动续采（没采到的组合下次先补）"}
    if ctx.policy.budget.remaining(platform, "collect_search") == 0:
        return {"action": "drop", "reason": "今天的搜索页额度已经用完，不再自动续采（没采到的组合下次先补）"}
    try:
        not_before = float(record.get("not_before") or 0)
    except (TypeError, ValueError):
        not_before = 0.0
    if now.timestamp() < not_before:
        at = datetime.fromtimestamp(not_before).strftime("%H:%M")
        return {"action": "wait", "reason": f"最近 {record.get('window') or '1 小时'} 开页面到上限，{at} 后自动接着采"}
    if not in_send_window(ctx.config.safety.active_windows, now):
        return {"action": "wait", "reason": "不在允许开页面的时段，进了时段再接着采"}
    getter = getattr(runner, "running_platforms", None)
    if callable(getter) and platform in set(getter()):
        return {"action": "wait", "reason": "这个平台有任务在跑，等它结束再接着采"}
    for key, label in ((GLOBAL_LOCK_KEY, "整体冷却中"), (platform, "这个平台在冷却")):
        lock = ctx.repo.get_lock(key)
        until = _parse((lock or {}).get("until"))
        if until is not None and until > now:
            return {"action": "wait", "reason": f"{label}，{until.strftime('%H:%M')} 之后再接着采"}
    return {"action": "start", "reason": "到点了，接着采"}


def resume_status(repo: Any, platforms: Any, *, now: datetime | None = None,
                  waiting: dict[str, str] | None = None) -> dict[str, dict]:
    """/api/status 的 collect_resume：{platform: {at, in_seconds, labels, mode, reason}}。"""
    now = now or datetime.now()
    out: dict[str, dict] = {}
    for platform in platforms:
        record = read_resume(repo, platform)
        if record is None:
            continue
        try:
            not_before = datetime.fromtimestamp(float(record.get("not_before") or 0))
        except (TypeError, ValueError, OSError):
            not_before = now
        labels = list(record.get("labels") or record.get("slots") or [])
        reason = (waiting or {}).get(platform) or (
            f"最近 {record.get('window') or '1 小时'} 开页面到上限，"
            f"{not_before.strftime('%H:%M')} 后自动接着采 {len(labels)} 个组合")
        out[platform] = {
            "at": not_before.strftime(_TS),
            "in_seconds": max(0, int((not_before - now).total_seconds())),
            "labels": labels,
            "mode": str(record.get("mode") or "collect"),
            "reason": reason,
        }
    return out


def carry_status(repo: Any, platforms: Any) -> dict[str, dict]:
    """/api/status 的 collect_carry：{platform: {labels, since}}（上次没采到、下次先补的组合）。"""
    out: dict[str, dict] = {}
    for platform in platforms:
        carry = read_carry(repo, platform)
        if carry:
            out[platform] = {"labels": list(carry["labels"]), "since": str(carry.get("since") or "")}
    return out


class AutoResume:
    """后台守护线程：每 tick 看一眼每个平台的待续采，到点、条件都满足就让 runner 起任务。

    ``clock`` / ``sleep`` 可注入：测试里直接调 ``tick()``，不起线程、不等真时间。
    只由 run_server 启动（TestClient 建的 app 不会自己开真浏览器）。
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
        self.waiting: dict[str, str] = {}     # 每个平台上一次「为什么还没续」（界面显示）

    def _platforms(self) -> list[str]:
        from ..platforms import build_default_registry

        return list((self._registry or build_default_registry()).keys())

    def tick(self) -> dict[str, dict]:
        """检查一次；返回每个有待续采的平台这次的判断（带 started / task_id）。"""
        from ..services.orchestrator import TaskNotRunnable
        from ..tasks import TaskAlreadyRunning

        ctx = self._ctx_factory()
        out: dict[str, dict] = {}
        try:
            now = self._clock()
            for platform in self._platforms():
                record = read_resume(ctx.repo, platform)
                if record is None:
                    self.waiting.pop(platform, None)
                    continue
                verdict = decide(ctx, self._runner, platform, record, now)
                if verdict["action"] == "drop":
                    clear_resume(ctx.repo, platform)
                    self.waiting.pop(platform, None)
                    ctx.repo.add_event(None, "collect_resume", f"{platform} 不再自动续采：{verdict['reason']}")
                    out[platform] = {**verdict, "started": False}
                    continue
                if verdict["action"] == "wait":
                    self.waiting[platform] = verdict["reason"]
                    out[platform] = {**verdict, "started": False}
                    continue
                mode, plat, options = start_args(platform, record)
                # 先清再起：起来的任务自己又被速率拦下时会写一条新的续采，不能被这里随后清掉
                clear_resume(ctx.repo, platform)
                try:
                    task = self._runner.start(mode, plat, **options)
                except (TaskAlreadyRunning, TaskNotRunnable) as exc:
                    ctx.repo.set_kv(_key(platform), json.dumps(record, ensure_ascii=False))  # 被拒：留着下一 tick 再试
                    self.waiting[platform] = f"这次没续上：{exc}；等一会儿再试"
                    out[platform] = {**verdict, "action": "wait", "started": False, "reason": str(exc)}
                    continue
                self.waiting.pop(platform, None)
                labels = "、".join(record.get("labels") or [])
                ctx.repo.add_event(None, "collect_resume", f"{platform} 自动接着采：{labels}")
                out[platform] = {**verdict, "started": True, "task_id": getattr(task, "id", None)}
            return out
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
        self._thread = threading.Thread(target=self._loop, name="auto-resume", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
