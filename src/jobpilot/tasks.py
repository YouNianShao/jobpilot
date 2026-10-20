"""后台任务运行器：不同平台的任务可以同时跑（同一平台互斥），可暂停、可停止、带日志与进度。

编排层是同步的；这里把每次 run 放到各自的后台线程，并把 ctx 的 log/progress
接进任务对象，供 Web 轮询。
"""

from __future__ import annotations

import json
import re
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from . import readable
from .platforms import build_default_registry
from .platforms.base import StopRequested
from .services.orchestrator import TaskNotRunnable, check_mode
from .services.orchestrator import run as run_flow


class TaskAlreadyRunning(RuntimeError):
    """同一个平台同一时间只允许一个任务（避免同一账号同时开多个页面把风控拉满）。"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 任务日志是给人看的：驱动 / 服务里写的 [boss] [safety] 这类机器前缀，进日志前统一换成中文
_TAGS = {
    "boss": "BOSS直聘", "51job": "前程无忧", "liepin": "猎聘", "zhaopin": "智联招聘",
    "safety": "风控", "apply": "投递",
}
_TAG = re.compile(r"^\[([A-Za-z0-9_]+)\]\s*")


def humanize(message: object) -> str:
    text = str(message)
    match = _TAG.match(text)
    if match and match.group(1) in _TAGS:
        text = f"{_TAGS[match.group(1)]}：{text[match.end():]}"
    # 消息中间的平台代号 / 配置路径（例如 "boss 处于风险冷却"、"safety.active_windows"）
    return readable.clean(text)


_MODE_NAMES = {
    "collect": "采集", "score": "评分", "greet": "生成招呼语", "confirm": "确认",
    "apply": "投递", "monitor": "监测", "followup": "跟进", "full": "全流程",
}
_COUNTS = (
    ("new", "新增 {} 个岗位"), ("duplicate", "{} 个已在库里"), ("filtered", "{} 个被预筛拦下"),
    ("scored", "评分 {} 个"), ("prefiltered", "{} 个预筛淘汰"), ("failed", "{} 个失败"),
    ("greeted", "生成招呼语 {} 条"), ("confirmed", "确认 {} 个"),
    ("applied", "投出 {} 个"), ("already", "{} 个之前投过"), ("awaiting_confirmation", "{} 个等你确认"),
    ("checked", "看了 {} 条投递 / 会话"), ("progressed", "记下 {} 条新进展"),
)


def problem_of(result: object) -> str:
    """任务"跑完了但没做成"的原因：采集被风控锁、没登录、被闸门拒绝……

    这些情况服务层是**返回** error 而不是抛异常（抛了就看不到已经做完的部分），
    所以任务状态仍是 completed —— 不单独拿出来说，界面上就成了"已完成"却什么都没有。
    """
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        text = humanize(result["error"])
        if result.get("locked_until"):
            text += f"。为保护账号，该平台暂停到 {str(result['locked_until'])[5:16]} 再自动恢复"
        elif result.get("login_required"):
            text += "。请先在调试 Chrome 里登录该平台"
        return text
    for value in (result.get("steps") or {}).values():
        found = problem_of(value)
        if found:
            return found
    # 被冷却 / 时段挡住、一个岗位都没处理就停了：也是「没做成」
    if readable.run_failed(result):
        return readable.run_summary("", result)
    return ""


def run_problem(row: dict) -> str:
    """历史任务行（task_runs）的可读失败原因：旧记录里 error 可能是空的、原因藏在 result 里。"""
    error = (row.get("error") or "").strip()
    if error:
        return humanize(error)
    try:
        return problem_of(json.loads(row.get("result") or "null"))
    except (ValueError, TypeError):
        return ""


def summarize(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    sources = [result, *[v for v in (result.get("steps") or {}).values() if isinstance(v, dict)]]
    for source in sources:
        for key, template in _COUNTS:
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value:
                parts.append(template.format(value))
        for key in ("note", "skipped", "stopped"):
            if isinstance(source.get(key), str) and source[key]:
                parts.append(humanize(source[key]))
    return "，".join(dict.fromkeys(parts))


@dataclass
class Task:
    id: str
    mode: str
    platform: str
    options: dict = field(default_factory=dict)
    status: str = "running"  # running / completed / failed / stopped
    progress: dict = field(default_factory=dict)
    # 所有平台轮流投递：platform → {done, total, state, until, label, reason}
    lanes: dict = field(default_factory=dict)
    stage: str = ""  # 全流程当前步骤：collect / score / greet / apply（别的模式留空）
    logs: list[str] = field(default_factory=list)
    # 和 logs 一一对应的时间（HH:MM:SS）：任务监控台把几个平台的日志按时间排成一条
    log_times: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def snapshot(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "platform": self.platform,
            # 多平台全流程：勾选了哪几个平台（界面在任务卡片上写出来）
            "platforms": [plan["platform"] for plan in (self.options.get("plans") or [])],
            "status": self.status, "progress": dict(self.progress), "stage": self.stage,
            "lanes": {k: dict(v) for k, v in self.lanes.items()},
            "logs": self.logs[-80:], "log_times": self._times()[-80:], "result": self.result, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }

    def _times(self) -> list[str]:
        """logs 有的地方直接 append（没记时间）：缺的按空串补齐，保证和 logs 对得上"""
        times = self.log_times
        if len(times) < len(self.logs):
            times.extend([""] * (len(self.logs) - len(times)))
        return times[:len(self.logs)]

    def log(self, message: str) -> None:
        self._times()
        self.logs.append(message)
        self.log_times.append(datetime.now().strftime("%H:%M:%S"))


AI_CLAIM = "ai"
# 内存里留几个跑完的任务给界面看（更早的去「记录」页查 task_runs）
KEEP_FINISHED = 6


def claims_of(config: Any, mode: str, platform: str, options: dict, registry: Any) -> frozenset[str]:
    """这个任务要"占用"哪些平台：会在这些平台上开页面 / 写这些平台的岗位。

    两个任务占用的平台有交集就不能同时跑；没有交集（例如前程无忧在采集后冷却、
    这时去采猎聘）就可以并行 —— 每个平台的节奏 / 额度 / 风控锁本来就是分平台记的。
    """
    plans = options.get("plans")
    if mode in ("confirm", "followup"):
        return frozenset()                      # 纯数据库 / 纯文本，不开页面，随时可以跑
    if mode in ("score", "greet"):
        # 不开页面，按平台占「评分位」：前程无忧在评分的时候，BOSS 照常采集、智联照常投递，
        # 别的平台也能同时评自己的分（2026-10-16 用户要求）。只有同一平台的岗位不能两个任务一起评
        # （会抢同一批岗位、花两份 token）。不指定平台 = 评所有平台 → 占每个平台的评分位
        keys = [platform] if platform else list(registry.keys())
        return frozenset(ai_claim(k) for k in keys)
    if mode == "full" and plans is not None:
        # 全流程也会评 / 写自己平台的岗位：同时占平台和这个平台的评分位
        keys = [plan["platform"] for plan in plans]
        return frozenset(keys) | frozenset(ai_claim(k) for k in keys)
    if mode == "full" and platform:
        return frozenset({platform, ai_claim(platform)})
    if mode == "apply" and not platform:
        # 所有平台轮流投：占住每个现在能投的平台
        return frozenset(key for key in registry.keys()
                         if check_mode(config, "apply", key, registry=registry) is None)
    if mode == "monitor" and not platform:
        # 所有平台的「我的投递 / 聊天列表」逐个看：占住每个开放监测、启用了的平台
        from .platforms import supports

        return frozenset(key for key in registry.keys()
                         if supports(registry.get(key), "monitor")
                         and (config.platforms.get(key) is None or config.platforms[key].enabled))
    return frozenset({platform}) if platform else frozenset()


def ai_claim(platform: str) -> str:
    return f"{AI_CLAIM}:{platform}"


def _claim_names(claims: set[str] | frozenset[str]) -> str:
    plats = {k for k in claims if not k.startswith(AI_CLAIM)}
    ai = sorted(k.split(":", 1)[1] for k in claims if k.startswith(AI_CLAIM + ":") and k.split(":", 1)[1] not in plats)
    names = [readable.platform_name(k) for k in sorted(plats)]
    names += [readable.platform_name(k) + " 的评分 / 招呼语" for k in ai]
    return "、".join(names)


class TaskRunner:
    """可以同时跑多个任务，但同一个平台一次只能有一个任务（见 claims_of）。"""

    def __init__(self, ctx_factory: Callable[[], Any]) -> None:
        self._ctx_factory = ctx_factory
        # 每个任务各有自己的 ctx（自己的库连接 / 停止信号 / 浏览器连接）和线程
        self._tasks: dict[str, Task] = {}
        self._ctxs: dict[str, Any] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._claims: dict[str, frozenset[str]] = {}
        self._order: list[str] = []             # 启动顺序，最新的在最后
        # 可重入：/api/status 轮询读列表时，start() 可能正在登记新任务、清掉旧任务
        self._lock = threading.RLock()

    def _running_ids(self) -> list[str]:
        """正在跑的任务，最新启动的在前。"""
        with self._lock:
            return [tid for tid in reversed(self._order) if self._tasks[tid].status == "running"]

    @property
    def running(self) -> bool:
        return bool(self._running_ids())

    @property
    def paused(self) -> bool:
        return any(self._ctxs[tid].stop.paused for tid in self._running_ids() if tid in self._ctxs)

    def running_platforms(self) -> set[str]:
        busy: set[str] = set()
        with self._lock:
            for tid in self._running_ids():
                busy |= {c for c in self._claims.get(tid, frozenset()) if not c.startswith(AI_CLAIM)}
        return busy

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def _snap(self, tid: str) -> dict:
        snap = self._tasks[tid].snapshot()
        ctx = self._ctxs.get(tid)
        snap["claims"] = sorted(self._claims.get(tid, frozenset()))
        snap["paused"] = bool(snap["status"] == "running" and ctx is not None and ctx.stop.paused)
        return snap

    def snapshot(self) -> dict | None:
        """「主」任务：最近启动的、还在跑的那个；都跑完了就是最近一个跑完的（老界面只看这一个）。"""
        with self._lock:
            running = self._running_ids()
            if running:
                return self._snap(running[0])
            return self._snap(self._order[-1]) if self._order else None

    def snapshots(self) -> list[dict]:
        with self._lock:
            running = self._running_ids()
            finished = [tid for tid in reversed(self._order) if tid not in running]
            ids = running + finished[:max(0, KEEP_FINISHED - len(running))]
            return [self._snap(tid) for tid in ids]

    def _forget_old(self) -> None:
        """只在内存里留最近几个跑完的任务；跑着的一个都不能丢。"""
        finished = [tid for tid in self._order if self._tasks[tid].status != "running"]
        for tid in finished[:max(0, len(finished) - KEEP_FINISHED)]:
            self._order.remove(tid)
            for store in (self._tasks, self._ctxs, self._threads, self._claims):
                store.pop(tid, None)

    @staticmethod
    def _persist(ctx: Any, task: Task) -> None:
        """把任务快照写进 task_runs（失败不影响任务本身）。"""
        try:
            ctx.repo.upsert_task_run({
                # 多平台全流程记成 "boss,liepin"：记录页据此写「BOSS直聘、猎聘」
                "id": task.id, "mode": task.mode,
                "platform": task.platform or ",".join(p["platform"] for p in (task.options.get("plans") or [])),
                "status": task.status,
                "progress": json.dumps(task.progress, ensure_ascii=False),
                "logs": json.dumps(task.logs[-60:], ensure_ascii=False),
                "result": json.dumps(task.result, ensure_ascii=False) if task.result is not None else None,
                "error": task.error, "created_at": task.started_at,
                "finished_at": task.finished_at or None,
            })
        except Exception:  # noqa: BLE001 - 历史记录失败不该影响主流程
            pass

    def start(self, mode: str, platform: str = "", **options) -> Task:
        # 查冲突 + 登记放在同一把锁里：两个请求同时进来时不能都通过检查
        with self._lock:
            ctx = self._ctx_factory()

            def refuse(exc: Exception) -> None:
                try:
                    ctx.repo.conn.close()
                except Exception:  # noqa: BLE001
                    pass
                raise exc

            registry = build_default_registry()
            # 开跑之前先问一句"这个模式现在真的能做事吗"：
            # 不能就直接拒绝，而不是跑完一圈再告诉用户什么都没发生。
            problem = check_mode(ctx.config, mode, platform or None, registry=registry,
                                 plans=options.get("plans"))
            if problem:
                refuse(TaskNotRunnable(problem))
            claims = claims_of(ctx.config, mode, platform, options, registry)
            for tid in self._running_ids():
                clash = claims & self._claims.get(tid, frozenset())
                if clash:
                    other = self._tasks[tid]
                    name = _MODE_NAMES.get(other.mode, other.mode)
                    if all(c.startswith(AI_CLAIM) for c in clash):
                        message = (f"{_claim_names(clash)}正在跑（{name}），同一个平台的岗位一次只能有一个 AI 评分 / 招呼语任务，"
                                   "等它结束再开；别的平台可以照常评")
                    else:
                        message = f"{_claim_names(clash)} 正在跑{name}，同一个平台一次只能跑一个任务"
                    refuse(TaskAlreadyRunning(message))
            task = Task(id=str(uuid4())[:8], mode=mode, platform=platform, options=options)
            ctx.log = lambda message: task.log(humanize(message))
            ctx.progress = lambda current, total, label: task.progress.update(
                {"current": current, "total": total, "label": label}
            )
            ctx.stage = lambda name: setattr(task, "stage", name)
            ctx.lane = lambda platform, info: task.lanes.setdefault(platform, {}).update(info)
            self._tasks[task.id], self._ctxs[task.id], self._claims[task.id] = task, ctx, claims
            self._order.append(task.id)
            self._forget_old()
            self._persist(ctx, task)
            thread = threading.Thread(target=self._work, args=(ctx, task), daemon=True)
            self._threads[task.id] = thread
            thread.start()
            return task

    def _work(self, ctx: Any, task: Task) -> None:
        try:
            task.result = run_flow(ctx, task.mode, task.platform or None, **task.options)
            task.status = "stopped" if ctx.stop.requested else "completed"
            name = _MODE_NAMES.get(task.mode, task.mode)
            problem = problem_of(task.result)
            if problem:
                task.error = problem
                task.log(f"{name}没有做成：{problem}")
            else:
                summary = summarize(task.result)
                task.log(f"{name}结束：{summary}" if summary else f"{name}结束，这一轮没有可处理的岗位")
        except StopRequested:
            task.status = "stopped"
            task.log("任务已停止")
        except Exception as exc:  # noqa: BLE001 - 任务失败要落到任务对象上
            if ctx.stop.requested:
                # 用户已经点了停止，之后才冒出来的错（比如正卡在连浏览器）：算「已停止」，不算出错
                task.status = "stopped"
                task.log("任务已停止")
                return
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.log(f"任务出错停下了：{humanize(exc)}（详细报错已写入 data/serve.log）")
            # 只留一句 str(exc) 的话，出了"datatype mismatch"这种错根本没法查。
            # 栈写进服务日志 + task_runs.error，但不再塞进界面上的任务日志（用户看不懂）。
            traceback.print_exc(file=sys.stderr)
        finally:
            task.finished_at = _now()
            self._persist(ctx, task)
            try:
                ctx.repo.conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _targets(self, task_id: str | None) -> list[str]:
        """给了 id 就只动那一个；没给 = 所有在跑的任务（顶栏的全局按钮）。未知 id 抛 KeyError。"""
        with self._lock:
            if task_id:
                if task_id not in self._tasks:
                    raise KeyError(task_id)
                return [task_id] if self._tasks[task_id].status == "running" else []
            return self._running_ids()

    STOP_GRACE_S = 8.0

    def _force_stop_later(self, tid: str, ctx: Any) -> None:
        """点了停止 8 秒后任务还在跑 = 卡在一句不返回的页面调用里（2026-10-16 智联整页卡死）：
        强制断开它的浏览器驱动，那一句立刻报错返回，任务就能停下；你自己开的标签页也不再被拦着。"""
        def check():
            task = self._tasks.get(tid)
            if task is not None and task.status == "running":
                kill = getattr(ctx, "kill_browsers", None)
                if kill is not None and kill():
                    task.log("任务卡在页面上停不下来：已强制断开它的浏览器连接（浏览器本身不受影响）")

        timer = threading.Timer(self.STOP_GRACE_S, check)
        timer.daemon = True
        timer.start()

    def stop(self, task_id: str | None = None) -> dict:
        targets = self._targets(task_id)
        for tid in targets:
            ctx = self._ctxs.get(tid)
            if ctx is not None:
                # 暂停中的任务卡在 check() 里等恢复：先放开，它才能走到停止点
                ctx.stop.request()
                ctx.stop.resume()
                self._tasks[tid].log("已请求停止")
                self._force_stop_later(tid, ctx)
        if task_id:
            return self._snap(task_id)
        return self.snapshot() or {}

    def pause(self, task_id: str | None = None) -> dict:
        for tid in self._targets(task_id):
            if tid in self._ctxs:
                self._ctxs[tid].stop.pause()
        return {"paused": True, "id": task_id} if task_id else {"paused": True}

    def resume(self, task_id: str | None = None) -> dict:
        for tid in self._targets(task_id):
            if tid in self._ctxs:
                self._ctxs[tid].stop.resume()
        return {"paused": False, "id": task_id} if task_id else {"paused": False}
