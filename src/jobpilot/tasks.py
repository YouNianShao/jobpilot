"""后台任务运行器：单任务互斥、可暂停、可停止、带日志与进度。

编排层是同步的；这里把一次 run 放到后台线程，并把 ctx 的 log/progress
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
    """同一时间只允许一个任务（避免多任务同时开页面把风控拉满）。"""


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
    logs: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def snapshot(self) -> dict:
        return {
            "id": self.id, "mode": self.mode, "platform": self.platform,
            # 多平台全流程：勾选了哪几个平台（界面在任务卡片上写出来）
            "platforms": [plan["platform"] for plan in (self.options.get("plans") or [])],
            "status": self.status, "progress": dict(self.progress),
            "logs": self.logs[-80:], "result": self.result, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class TaskRunner:
    def __init__(self, ctx_factory: Callable[[], Any]) -> None:
        self._ctx_factory = ctx_factory
        self._task: Task | None = None
        self._ctx: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._task and self._task.status == "running")

    @property
    def paused(self) -> bool:
        return bool(self._ctx and self._ctx.stop.paused)

    def snapshot(self) -> dict | None:
        return self._task.snapshot() if self._task else None

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
        with self._lock:
            if self.running:
                raise TaskAlreadyRunning(f"已有任务在运行：{self._task.mode}")
            ctx = self._ctx_factory()
            # 开跑之前先问一句"这个模式现在真的能做事吗"：
            # 不能就直接拒绝，而不是跑完一圈再告诉用户什么都没发生。
            problem = check_mode(ctx.config, mode, platform or None, registry=build_default_registry(),
                                 plans=options.get("plans"))
            if problem:
                try:
                    ctx.repo.conn.close()
                except Exception:  # noqa: BLE001
                    pass
                raise TaskNotRunnable(problem)
            task = Task(id=str(uuid4())[:8], mode=mode, platform=platform, options=options)
            ctx.log = lambda message: task.logs.append(humanize(message))
            ctx.progress = lambda current, total, label: task.progress.update(
                {"current": current, "total": total, "label": label}
            )
            self._task, self._ctx = task, ctx
            self._persist(ctx, task)
            self._thread = threading.Thread(target=self._work, args=(ctx, task), daemon=True)
            self._thread.start()
            return task

    def _work(self, ctx: Any, task: Task) -> None:
        try:
            task.result = run_flow(ctx, task.mode, task.platform or None, **task.options)
            task.status = "stopped" if ctx.stop.requested else "completed"
            name = _MODE_NAMES.get(task.mode, task.mode)
            problem = problem_of(task.result)
            if problem:
                task.error = problem
                task.logs.append(f"{name}没有做成：{problem}")
            else:
                summary = summarize(task.result)
                task.logs.append(f"{name}结束：{summary}" if summary else f"{name}结束，这一轮没有可处理的岗位")
        except StopRequested:
            task.status = "stopped"
            task.logs.append("任务已停止")
        except Exception as exc:  # noqa: BLE001 - 任务失败要落到任务对象上
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.logs.append(f"任务出错停下了：{humanize(exc)}（详细报错已写入 data/serve.log）")
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

    def stop(self) -> dict:
        if self._ctx is not None:
            self._ctx.stop.request()
            if self._task is not None:
                self._task.logs.append("已请求停止")
        return self.snapshot() or {}

    def pause(self) -> dict:
        if self._ctx is not None:
            self._ctx.stop.pause()
        return {"paused": True}

    def resume(self) -> dict:
        if self._ctx is not None:
            self._ctx.stop.resume()
        return {"paused": False}
