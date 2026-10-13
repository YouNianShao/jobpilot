"""FastAPI 看板。

界面存在的意义：把 **风控状态（剩余额度 / 冷却锁）** 和 **待确认岗位** 摆到眼前，
让"人工确认门"真的能点。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .. import readable
from ..ai.llm import build_llm
from ..ai.providers import provider_options, resolve_provider
from ..config import (
    CONFIG_PATH,
    DATA_DIR,
    AiCfg,
    AppConfig,
    config_problems,
    field_rules,
    load_config,
    save_config,
)
from ..models import Status
from ..nature import nature_labels
from ..platforms import build_default_registry
from ..safety.throttle import in_send_window
from ..services import confirmer
from ..services.context import ServiceContext
from ..services.orchestrator import TaskNotRunnable
from ..services.progress import INTERVIEW_STATES
from ..services.progress import state_labels as progress_state_labels
from ..tasks import TaskAlreadyRunning, TaskRunner, run_problem

STATIC_DIR = Path(__file__).resolve().parent / "static"
# /api/status 里待确认岗位只带这些字段（概览「待确认」卡片：标题 / 公司 / 分数 / 平台 / 薪资 / 招呼语 / 打开 / 详情）
PENDING_FIELDS = ("id", "title", "company", "score", "platform", "salary", "greeting", "url")


class ProgressBody(BaseModel):
    state: str = ""
    note: str = ""


class MarkAppliedBody(BaseModel):
    note: str = ""


class ResumeBody(BaseModel):
    text: str = ""


class TaskRequest(BaseModel):
    mode: str
    platform: str = "boss"
    limit: int | None = None
    # 「运行前配置」：这些是**这一次**的覆盖值，不会写回 config.yaml
    keywords: list[str] | None = None
    cities: list[str] | None = None
    max_pages: int | None = None


class JobIds(BaseModel):
    platform: str = "boss"
    ids: list[str] | None = None


# 与前端 common.js 的 STATUS_LABEL 一致（导出给人看的文件用）
STATUS_NAMES = {
    "new": "待评分", "filtered": "未通过", "pending_confirm": "待确认", "approved": "已确认",
    "applied": "已投递", "rejected": "被拒", "already": "已投过", "failed": "失败",
    "need_resume": "待补简历", "unsupported": "不支持",
}

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _bare_host(netloc: str) -> str:
    """'127.0.0.1:8700' -> '127.0.0.1'；'[::1]:8700' -> '[::1]'。"""
    netloc = (netloc or "").strip().lower()
    if netloc.startswith("["):
        return netloc.split("]", 1)[0] + "]"
    return netloc.split(":", 1)[0]


def _local_only_problem(method: str, headers) -> str:
    """看板只给本机用。返回拒绝原因；空串 = 放行。

    只监听 127.0.0.1 挡得住局域网，挡不住浏览器里别的网页：
    - DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1，就能当成「同源」读 /api/resume、下载 /api/backup
      （里面有 config.yaml 和真实 API Key）。这时请求头里的 Host 是它的域名 —— 只认本机 Host 就挡住了。
    - 跨站提交：别的网页可以直接往 127.0.0.1:8700 发 POST（拿不到响应，但动作会生效：停任务、确认岗位、
      开页面预演）。浏览器给这类请求带 Origin 头 —— 写操作只认本机 Origin。
    命令行脚本（tools/、curl）不带 Origin，不受影响。
    """
    host = _bare_host(headers.get("host", ""))
    if host not in LOCAL_HOSTS:
        return f"拒绝：只接受本机访问（Host: {host or '空'}）"
    if method.upper() in _WRITE_METHODS:
        origin = (headers.get("origin") or "").strip().lower()
        if origin and origin != "null":
            scheme_host = origin.split("://", 1)[-1]
            if _bare_host(scheme_host) not in LOCAL_HOSTS:
                return f"拒绝：写操作只接受本机页面发起（Origin: {origin}）"
        elif origin == "null":
            return "拒绝：写操作只接受本机页面发起（Origin: null）"
    return ""


def _history_row(row: dict) -> dict:
    """概览「最近任务」的一条：只带界面要用的字段 + 一句人话。"""
    try:
        result = json.loads(row.get("result") or "null")
    except (ValueError, TypeError):
        result = None
    error = row.get("error") or ""
    return {
        "id": row["id"], "mode": row["mode"], "platform": row["platform"], "status": row["status"],
        "error": run_problem(row), "created_at": row["created_at"], "finished_at": row["finished_at"] or "",
        "summary": readable.run_summary(row["mode"], result, error),
        "not_done": row["status"] == "completed" and readable.run_failed(result, error),
    }


def _field_message(err: dict) -> str:
    """pydantic 的单条校验错误 -> 一句中文（只覆盖配置里真会出现的几类）。"""
    ctx = err.get("ctx") or {}
    kind = err.get("type", "")
    if kind == "greater_than_equal":
        return f"不能小于 {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"不能大于 {ctx.get('le')}"
    if kind == "greater_than":
        return f"必须大于 {ctx.get('gt')}"
    if kind == "less_than":
        return f"必须小于 {ctx.get('lt')}"
    if kind in ("int_parsing", "int_from_float", "int_type"):
        return "需要填整数"
    if kind in ("float_parsing", "float_type"):
        return "需要填数字"
    if kind in ("bool_parsing", "bool_type"):
        return "只能是开或关"
    if kind == "missing":
        return "必填"
    return str(err.get("msg") or "格式不对")


def create_app(ctx_factory: Callable[[], ServiceContext], *, runner: TaskRunner | None = None) -> FastAPI:
    INLINE_RESUME = DATA_DIR / "resumes" / "master.md"

    app = FastAPI(title="JobPilot", version="2.0.0")

    @app.middleware("http")
    async def _revalidate_static(request, call_next):
        """静态资源每次都回源校验。

        不设这个头，浏览器会按「启发式新鲜度」缓存 app.css / app.js —— 改完样式重新打开
        页面可能还是旧样子（真踩过：命令面板的样式改了，看的却是旧缓存）。
        用 no-cache（意思是「每次校验」，不是「不缓存」）：内容没变就是 304，代价极小。
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def _local_only(request, call_next):
        """只接受本机访问（后注册的中间件在最外层：被拒的请求根本到不了路由）。原因见 _local_only_problem。"""
        problem = _local_only_problem(request.method, request.headers)
        if problem:
            return JSONResponse(status_code=403, content={"error": problem})
        return await call_next(request)

    tasks = runner or TaskRunner(ctx_factory)
    registry = build_default_registry()
    # 看门狗用：runner 引用 + 最近一次页面心跳（None = 还没有页面连过）
    app.state.runner = tasks
    app.state.last_seen = None
    app.state.idle_shutdown_minutes = 0.0

    def with_ctx(fn):
        ctx = ctx_factory()
        try:
            return fn(ctx)
        finally:
            ctx.repo.conn.close()

    @app.get("/api/status")
    def status():
        app.state.last_seen = time.time()  # 页面心跳：有页面在轮询就不算闲置

        def build(ctx: ServiceContext):
            platforms = {}
            locks = {}
            for key in registry.keys():
                driver = registry.get(key)
                platforms[key] = {
                    "name": driver.name,
                    "capabilities": sorted(driver.capabilities),
                    # 能不能「投递预演（只看不点）」：智联还没做，界面就不显示这个按钮
                    "can_probe": hasattr(driver, "probe_apply"),
                    "remaining": ctx.policy.remaining(key),
                    # 真正生效的上限（已解析每平台覆盖）+ 滚动窗口用量，界面据此画进度条
                    "limits": ctx.policy.limits_for(key),
                    "rate": ctx.policy.rate_state(key),
                    "penalty": ctx.policy.penalty_state(key),
                    "next_open_in": round(ctx.policy.next_open_in(key), 1),
                }
                lock = ctx.policy.locks.active(key)
                if lock:
                    locks[key] = lock
            pending = ctx.repo.list_jobs(status=Status.PENDING_CONFIRM.value, limit=50)
            all_counts = ctx.repo.status_counts()
            return {
                "running": tasks.running,
                "paused": tasks.paused,
                "task": tasks.snapshot(),
                "platforms": platforms,
                "locks": locks,
                "applied_today": ctx.policy.applied_today(),
                "daily_limit": ctx.config.apply.daily_limit,
                # 一条 GROUP BY 拿全部状态（以前 4 次 COUNT），只回界面用到的 4 个键
                "counts": {
                    s: all_counts.get(s, 0)
                    for s in (Status.NEW.value, Status.PENDING_CONFIRM.value,
                              Status.APPROVED.value, Status.APPLIED.value)
                },
                # 岗位池真实总数（所有状态）。以前概览「岗位池」用上面 4 个状态相加，
                # 没算「未通过 / 被拒 / 已投过」：库里 31 个岗位，界面只写 4
                "pool_total": sum(all_counts.values()),
                # 只回概览「待确认」卡片用到的字段：这个接口每 2 秒轮询一次，
                # 以前把整个岗位（含 JD 正文）都塞进来，2 万岗位的库里一次就是 111KB
                "pending_confirm": [{key: getattr(job, key) for key in PENDING_FIELDS} for job in pending],
                "history": [_history_row(row) for row in ctx.repo.list_task_runs(10)],
"nature_labels": nature_labels(),
"progress_states": progress_state_labels(),
                "active_window_open": in_send_window(ctx.config.safety.active_windows),
                "active_windows": list(ctx.config.safety.active_windows or []),
                "idle_shutdown_minutes": app.state.idle_shutdown_minutes,
            }

        return with_ctx(build)

    @app.get("/api/resume")
    def get_resume():
        """简历正文 + 可用性状态。评分/招呼语都依赖它，读不到必须让用户看见。"""

        def build(ctx: ServiceContext):
            text, problem = ctx.resume_text()
            raw = (ctx.config.profile.resume_path or "").strip()
            limit = 100_000
            return {
                "path": raw,
                "ok": not problem,
                "problem": problem,
                "chars": len(text),
                "truncated": len(text) > limit,
                "text": text[:limit],
                "store_path": str(INLINE_RESUME),
            }

        return with_ctx(build)

    @app.post("/api/resume")
    def put_resume(body: ResumeBody):
        """把界面里编辑好的简历存进项目（data/resumes/master.md），并把配置指过去。

        只写到固定路径：不接受任意路径，避免变成"用接口写任意文件"。
        """
        text = (body.text or "").replace("\r\n", "\n").strip()
        if len(text) < 50:
            return JSONResponse(status_code=400, content={"error": "简历太短了（至少 50 字），先把正文贴进来"})
        INLINE_RESUME.parent.mkdir(parents=True, exist_ok=True)
        INLINE_RESUME.write_text(text + "\n", encoding="utf-8")
        config = load_config()
        config.profile.resume_path = str(INLINE_RESUME)
        save_config(config, CONFIG_PATH)
        return {"ok": True, "path": str(INLINE_RESUME), "chars": len(text)}

    @app.post("/api/resume/import")
    async def import_resume(request: Request, filename: str = ""):
        """上传 Word / PDF / Markdown / 纯文本简历，只**抽取正文**返回给界面，不落盘。

        正文先放进编辑框给用户过目（PDF 抽出来的换行常常不理想），用户点保存才走上面的
        POST /api/resume 写进 data/resumes/master.md —— 评分 / 招呼语只认那一份。
        请求体就是文件原始字节：不为一个上传框引入 multipart 依赖。
        """
        from ..resume_io import SUPPORTED, extract_text, format_of

        name = Path(filename or "").name
        if Path(name).suffix.lower() not in (*SUPPORTED, ".doc"):
            return JSONResponse(status_code=400, content={"error": "只支持 .md / .txt / .docx / .pdf 简历"})
        limit = 10 * 1024 * 1024
        # 先看声明的长度：超限的文件不必整份读进内存再拒绝
        if int(request.headers.get("content-length") or 0) > limit:
            return JSONResponse(status_code=413, content={"error": "文件太大（上限 10 MB）"})
        data = await request.body()
        if len(data) > limit:
            return JSONResponse(status_code=413, content={"error": "文件太大（上限 10 MB）"})
        if not data:
            return JSONResponse(status_code=400, content={"error": "文件是空的"})
        text, problem = extract_text(data, name)
        if problem:
            return JSONResponse(status_code=400, content={"error": problem})
        return {"ok": True, "text": text, "chars": len(text), "format": format_of(name), "filename": name}

    @app.post("/api/suggest-keywords")
    def suggest_keywords():
        """让模型看着简历 + 你实际采到的结果，推荐搜索关键词（不碰浏览器）。"""
        from ..services.keywords import suggest_keywords as suggest

        if tasks.running:
            return JSONResponse(status_code=409, content={"error": "有任务正在跑，先等它结束"})

        def build(ctx: ServiceContext):
            result = suggest(ctx)
            if result.get("error"):
                return JSONResponse(status_code=409, content=result)
            return result

        return with_ctx(build)

    @app.get("/api/cities")
    def cities(platform: str = ""):
        """该平台认识哪些城市。

        BOSS / 51job 的搜索用的是平台自己的城市编码，填错城市会被**跳过**——
        界面必须能提前告诉用户，而不是跑完一圈发现什么都没采到。
        猎聘 / 智联按城市名直接搜，属于自由文本。
        """
        driver = registry.get(platform)
        if driver is None:
            return JSONResponse(status_code=404, content={"error": f"没有 {platform} 的平台驱动"})
        codes = dict(getattr(driver, "supported_cities", None) or {})
        hot = [name for name in (getattr(driver, "hot_cities", None) or ()) if name in codes]
        return {
            "platform": platform,
            "restricted": bool(codes),
            "count": len(codes),
            "hot": hot,
            "cities": sorted(codes.keys()),
        }

    @app.get("/api/activity")
    def activity(days: int = 7, runs_page: int = 1, events_page: int = 1, page_size: int = 15):
        """记录页：今日/近 N 天的用量、任务历史、风控事件。

        任务历史和风控事件会越攒越多：各自分页（``runs_page`` / ``events_page``，每页 ``page_size`` 条），
        返回 ``runs_pager`` / ``events_pager`` = ``{page, pages, total, limit}``。

        "被风控过几次"这种事必须能查到——locks 表只存当前锁，所以历史写在 events 里。
        """
        import json as _json
        from datetime import datetime as _dt
        from datetime import timedelta as _td

        span = max(1, min(int(days or 7), 30))

        def build(ctx: ServiceContext):
            repo = ctx.repo
            kinds = ("collected", "scored", "greeting", "applied", "confirmed",
                     "prefilter_rejected", "risk", "page_error", "risk_lock", "rate_stop")
            today = {kind: repo.count_events_today(kind) for kind in kinds}
            pages = repo.pages_by_stage()
            by_day = repo.pages_by_day(span)
            seen = {row["day"]: int(row["count"]) for row in by_day}
            filled = []
            for offset in range(span - 1, -1, -1):
                day = (_dt.now() - _td(days=offset)).strftime("%Y-%m-%d")
                filled.append({"day": day, "count": seen.get(day, 0)})

            size = max(5, min(int(page_size), 50))

            def pager(total: int, want: int) -> dict:
                pages = max(1, -(-total // size))
                return {"page": min(max(1, int(want)), pages), "pages": pages, "total": total, "limit": size}

            runs_pager = pager(repo.count_task_runs(), runs_page)
            events_pager = pager(repo.count_system_events(), events_page)
            runs = []
            for row in repo.list_task_runs(size, (runs_pager["page"] - 1) * size):
                result = None
                if row.get("result"):
                    try:
                        result = _json.loads(row["result"])
                    except (ValueError, TypeError):
                        result = None
                runs.append({
                    "id": row["id"], "mode": row["mode"], "platform": row["platform"],
                    "status": row["status"], "error": run_problem(row),
                    "created_at": row["created_at"], "finished_at": row["finished_at"] or "",
                    "result": result,
                    # 给人看的一句话 + 「跑完了但没做成」：界面直接显示，不再自己拼 new=… seen=…
                    "summary": readable.run_summary(row["mode"], result, row["error"] or ""),
                    "not_done": row["status"] == "completed" and readable.run_failed(result, row["error"] or ""),
                })

            return {
                "days": span,
                "today": {
                    "pages_total": sum(pages.values()),
                    "pages": pages,
                    "events": today,
                },
                "week": {
                    "pages_total": sum(item["count"] for item in filled),
                    "risk_lock": repo.count_events_since("risk_lock", span),
                    "rate_stop": repo.count_events_since("rate_stop", span),
                    "page_error": repo.count_events_since("page_error", span),
                    "applied": repo.count_events_since("applied", span),
                    "collected": repo.count_events_since("collected", span),
                },
"funnel": {
                    **repo.pool_funnel(ctx.config.scoring.threshold),
                    "interview": repo.count_interactions_by_states(INTERVIEW_STATES),
                    "offer": repo.count_interactions_by_states(("offer",)),
                },
                "keywords": repo.keyword_stats(ctx.config.scoring.threshold),
                "companies": repo.company_stats(ctx.config.scoring.threshold),
                "pages_by_hour": repo.pages_by_hour(),
                "pages_by_day": filled,
                "runs": runs,
                "runs_pager": runs_pager,
                "events": [
                    {**item, "label": readable.EVENT_NAMES.get(item["kind"], item["kind"]),
                     "text": readable.event_text(item["kind"], item["detail"])}
                    for item in repo.list_recent_events(size, job_id_null_only=True,
                                                        offset=(events_pager["page"] - 1) * size)
                ],
                "events_pager": events_pager,
            }

        return with_ctx(build)

    @app.get("/api/jobs")
    def jobs(platform: str | None = None, status: str | None = None, limit: int = 100,
             offset: int = 0, deleted: int = 0, page: int = 0, q: str = "", sort: str = "created",
             dir: str = "desc", hide_nature: int = 0):  # noqa: A002 - 查询参数名就叫 dir
        """岗位列表。

        - 不带 ``page``：老格式（直接返回数组），给脚本 / 冒烟测试用，行为不变
        - ``page=N``（从 1 开始）：岗位池分页。筛选、搜索、排序、计数都在库里对**全量**做，
          返回 ``{items, total, page, pages, limit, counts}``。以前岗位池一次只取 400 条、
          再在浏览器里筛：超过 400 条之后，最新采到、还没评分的岗位直接看不见
        """
        from ..nature import detect

        def with_nature(job) -> dict:
            # 岗位性质是"算出来的"，不进库：规则改了立刻生效，也不会和旧数据不一致
            return {**job.__dict__, "nature": list(detect(job.title, job.company, job.jd))}

        if deleted:
            if not page:
                return with_ctx(lambda ctx: [job.__dict__ for job in ctx.repo.list_deleted(limit)])

            def build_bin(ctx: ServiceContext):
                size = max(1, min(int(limit), 200))
                total = ctx.repo.count_deleted()
                pages = max(1, -(-total // size))
                current = min(max(1, int(page)), pages)
                rows = ctx.repo.list_deleted(size, (current - 1) * size)
                return {"items": [with_nature(job) for job in rows], "total": total,
                        "page": current, "pages": pages, "limit": size, "counts": {}}

            return with_ctx(build_bin)

        if not page:
            def build_list(ctx: ServiceContext):
                return [with_nature(job) for job in
                        ctx.repo.list_jobs(platform=platform, status=status, limit=limit, offset=offset)]

            return with_ctx(build_list)

        def build_page(ctx: ServiceContext):
            size = max(1, min(int(limit), 200))
            exclude = ctx.repo.flagged_nature_ids() if hide_nature else None
            current = max(1, int(page))
            items, total, counts = ctx.repo.query_jobs(
                status=status or None, q=q, platform=platform or None, sort=sort, direction=dir,
                limit=size, offset=(current - 1) * size, exclude_ids=exclude,
            )
            pages = max(1, -(-total // size))
            if current > pages:          # 删到最后一页空了：退回真正的最后一页
                current = pages
                items, total, counts = ctx.repo.query_jobs(
                    status=status or None, q=q, platform=platform or None, sort=sort, direction=dir,
                    limit=size, offset=(current - 1) * size, exclude_ids=exclude,
                )
            platform_counts = ctx.repo.platform_counts(status=status or None, q=q, exclude_ids=exclude)
            return {"items": [with_nature(job) for job in items], "total": total, "page": current,
                    "pages": pages, "limit": size, "counts": counts, "platform_counts": platform_counts}

        return with_ctx(build_page)

    @app.post("/api/jobs/delete")
    def delete_jobs(body: JobIds):
        if not body.ids:
            return JSONResponse(status_code=400, content={"error": "请选择要删除的岗位"})
        return with_ctx(lambda ctx: {"deleted": ctx.repo.soft_delete(body.ids)})

    @app.post("/api/jobs/restore")
    def restore_jobs(body: JobIds):
        if not body.ids:
            return JSONResponse(status_code=400, content={"error": "请选择要恢复的岗位"})
        return with_ctx(lambda ctx: {"restored": ctx.repo.restore(body.ids)})

    @app.post("/api/jobs/purge")
    def purge_jobs(body: JobIds):
        if not body.ids:
            return JSONResponse(status_code=400, content={"error": "请选择要彻底删除的岗位"})
        return with_ctx(lambda ctx: {"purged": ctx.repo.purge(body.ids)})

    @app.get("/api/export")
    def export_jobs(format: str = "csv", status: str | None = None, platform: str | None = None,
                    q: str = "", hide_nature: int = 0):
        """导出岗位池为 CSV（带 UTF-8 BOM，Excel 直接打开不乱码）。

        和岗位池用同一套筛选（状态 / 搜索词 / 只看正常岗）：界面上看到什么就导出什么。
        平台、状态写中文：这份文件是给人在 Excel 里看的。
        """
        import csv
        import io

        def build(ctx: ServiceContext):
            exclude = ctx.repo.flagged_nature_ids() if hide_nature else None
            jobs, _, _ = ctx.repo.query_jobs(status=status or None, q=q or "", platform=platform or None,
                                             limit=20000, exclude_ids=exclude)
            registry = build_default_registry()
            names = {key: registry.get(key).name for key in registry.keys()}
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["平台", "标题", "公司", "城市", "薪资", "评分", "状态",
                             "链接", "招呼语", "定制简历", "跟进话术"])
            for job in jobs:
                writer.writerow([
                    names.get(job.platform, job.platform), job.title, job.company, job.city, job.salary,
                    "" if job.score is None else job.score, STATUS_NAMES.get(job.status, job.status), job.url,
                    job.greeting, job.resume_path, job.follow_up,
                ])
            return Response(
                content="﻿" + buffer.getvalue(),
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": 'attachment; filename="jobpilot-jobs.csv"'},
            )

        return with_ctx(build)

    @app.get("/api/follow-ups")
    def follow_ups():
        from ..services.followup import find_due

        def build(ctx: ServiceContext):
            due_ids = {job.id for job in find_due(ctx, limit=200)}
            applied = ctx.repo.list_jobs(status=Status.APPLIED.value, limit=500)
            return [
                {**job.__dict__, "due": job.id in due_ids}
                for job in applied if job.follow_up or job.id in due_ids
            ]

        return with_ctx(build)

    @app.get("/api/interactions")
    def interactions(limit: int = 200):
        return with_ctx(lambda ctx: ctx.repo.list_interactions(limit=limit))

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        def build(ctx: ServiceContext):
            job = ctx.repo.get_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="岗位不存在")
            return {
                **job.__dict__,
                "events": ctx.repo.list_events(job_id, limit=50),
                "trace": ctx.repo.get_score_trace(job_id),
            }

        return with_ctx(build)

    @app.post("/api/jobs/{job_id}/resume")
    def make_resume(job_id: str):
        from ..services.resume import generate_resume

        return with_ctx(lambda ctx: generate_resume(ctx, job_id))

    @app.post("/api/jobs/confirm")
    def confirm(body: JobIds):
        return with_ctx(lambda ctx: confirmer.approve(ctx, body.platform, job_ids=body.ids))

    @app.post("/api/jobs/reject")
    def reject(body: JobIds):
        return with_ctx(lambda ctx: confirmer.reject(ctx, body.platform, job_ids=body.ids))

    @app.post("/api/jobs/{job_id}/probe-apply")
    def probe_job_apply(job_id: str):
        """投递预演：只打开岗位页看按钮在不在，不点、不发。"""
        from ..services.probe import probe_apply

        if tasks.running:
            return JSONResponse(status_code=409, content={"error": "有任务正在跑，先等它结束再预演"})

        def build(ctx: ServiceContext):
            result = probe_apply(ctx, job_id)
            if result.get("error"):
                return JSONResponse(status_code=409, content=result)
            return result

        return with_ctx(build)

    @app.post("/api/jobs/{job_id}/interview-prep")
    def interview_prep(job_id: str):
        """面试准备：针对这个岗位预测问题，并指出该用简历里的哪段经历回答。"""
        from ..services.interview import prepare_interview

        def build(ctx: ServiceContext):
            result = prepare_interview(ctx, job_id)
            if result.get("error"):
                return JSONResponse(status_code=409, content=result)
            return result

        return with_ctx(build)

    @app.post("/api/jobs/{job_id}/progress")
    def record_job_progress(job_id: str, body: ProgressBody):
        """记录投递之后的进展：已查看 / 已回复 / 约面试 / 面试完 / offer / 被拒。"""
        from ..services.progress import record_progress

        def build(ctx: ServiceContext):
            result = record_progress(ctx, job_id, body.state, note=body.note)
            if result.get("error"):
                return JSONResponse(status_code=409, content=result)
            return result

        return with_ctx(build)

    @app.post("/api/jobs/{job_id}/applied")
    def mark_job_applied(job_id: str, body: MarkAppliedBody):
        """手动投递回执（自动投递未开放时的真实路径）。"""
        from ..services.applier import mark_applied

        def build(ctx: ServiceContext):
            result = mark_applied(ctx, job_id, note=body.note)
            if result.get("error"):
                return JSONResponse(status_code=409, content=result)
            return result

        return with_ctx(build)

    @app.post("/api/tasks")
    def start_task(body: TaskRequest):
        try:
            task = tasks.start(
                body.mode, body.platform, limit=body.limit,
                keywords=body.keywords, cities=body.cities, max_pages=body.max_pages,
            )
        except TaskAlreadyRunning as exc:
            return JSONResponse(status_code=409, content={"error": str(exc)})
        except TaskNotRunnable as exc:
            # 能力门：这个平台/模式现在做不了任何事，直接说清原因
            return JSONResponse(status_code=409, content={"error": str(exc)})
        return task.snapshot()

    @app.post("/api/tasks/stop")
    def stop_task():
        return tasks.stop()

    @app.post("/api/tasks/pause")
    def pause_task():
        return tasks.pause()

    @app.post("/api/tasks/resume")
    def resume_task():
        return tasks.resume()

    @app.get("/api/config")
    def get_config():
        data = load_config().model_dump()
        key = (data.get("ai") or {}).get("api_key") or ""
        if key:
            data["ai"]["api_key"] = key[:4] + "****" + key[-4:]
        data["_config_path"] = str(CONFIG_PATH)
        data["_rules"] = field_rules()   # 界面据此做实时校验（上下限 / 整数 / 小数），与后端同一份规则
        data["_providers"] = provider_options()   # 模型服务商预设（不含任何 Key）
        data["ai"]["provider"] = resolve_provider(data["ai"].get("provider") or "", data["ai"].get("base_url") or "")
        return data

    @app.post("/api/ai/test")
    def test_ai(payload: dict[str, Any]):
        """用配置页**当前填的**值发一次极小的请求，确认 Key / 地址 / 模型名都对。不保存配置。

        Key 输入框里是掩码（没改过）时用已保存的 Key —— Key 不会经过浏览器回传。
        """
        saved = load_config().ai.model_dump()
        merged = dict(saved)
        for key, value in (payload or {}).items():
            if key in saved and value not in (None, ""):
                if key == "api_key" and isinstance(value, str) and "*" in value:
                    continue
                merged[key] = value
        try:
            cfg = AiCfg.model_validate({**merged, "max_attempts": 1})
        except ValidationError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": _field_message(exc.errors()[0])})
        result = build_llm(cfg).complete(
            system="你是连通性测试。", user='只回复 JSON：{"ok": true}', schema={"type": "object"}, max_tokens=64,
        )
        if result.error and result.error_kind != "parse":   # 能回话就算通，JSON 格式不是这里要测的
            hints = {"auth": "Key 不对，或这个 Key 不属于该服务商 / 地域",
                     "request": "多半是模型名不对（或该账号没开通这个模型）",
                     "network": "连不上接口地址（地址写错，或需要代理）",
                     "rate_limit": "被限流了，稍后再试"}
            return {"ok": False, "error": result.error, "hint": hints.get(result.error_kind, "")}
        return {"ok": True, "model": result.model, "latency_ms": result.latency_ms}

    @app.post("/api/config")
    def post_config(payload: dict[str, Any]):
        data = load_config().model_dump()
        for section, values in (payload or {}).items():
            if isinstance(values, dict) and isinstance(data.get(section), dict):
                for key, value in values.items():
                    if section == "ai" and key == "api_key" and isinstance(value, str) and "*" in value:
                        continue  # 掩码值不回写
                    data[section][key] = value
        try:
            config = AppConfig.model_validate(data)
        except ValidationError as exc:
            # 以前直接回 str(exc)：一大段英文的 pydantic 报错，用户根本不知道改哪一格。
            # 现在按字段路径给出中文原因，界面把它标在对应输入框下面。
            fields = {".".join(str(part) for part in err["loc"]): _field_message(err) for err in exc.errors()}
            return JSONResponse(status_code=400, content={"error": f"有 {len(fields)} 项配置不合法", "fields": fields})
        problems = config_problems(config)
        if problems:
            return JSONResponse(status_code=400, content={"error": f"有 {len(problems)} 项配置互相矛盾", "fields": problems})
        save_config(config, CONFIG_PATH)
        return {"ok": True}

    @app.get("/api/checks")
    def checks():
        """系统体检：一次回答"现在能不能干活，不能先修哪儿"。"""
        from ..services.health import run_checks

        return with_ctx(run_checks)

    @app.get("/api/backup")
    def download_backup():
        """把整条流水线打包下载：库（在线一致性备份）+ 配置 + 简历。"""
        import tempfile
        from datetime import datetime as _dt

        from ..services.backup import build_backup

        if tasks.running:
            return JSONResponse(status_code=409, content={"error": "有任务正在跑，等它结束再备份（库正在写）"})

        tmp = Path(tempfile.mkdtemp()) / "jobpilot-backup.zip"
        info = build_backup(tmp)
        data = tmp.read_bytes()
        tmp.unlink(missing_ok=True)
        stamp = _dt.now().strftime("%Y%m%d-%H%M")
        return Response(
            content=data,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="jobpilot-backup-{stamp}.zip"',
                "X-Backup-Files": str(len(info["files"])),
            },
        )

    @app.post("/api/browser/launch")
    def launch_browser():
        """替用户开一个带调试端口的 Chrome（登录态存在 data/chrome-profile，下次还在）。"""
        from ..browser.launcher import launch

        cfg = load_config()
        return launch(cfg.browser.cdp_url, DATA_DIR / "chrome-profile")

    @app.get("/api/health")
    def health():
        from ..browser import create_browser

        cfg = load_config()
        browser = create_browser(cfg.browser.cdp_url)
        try:
            return {"cdp": cfg.browser.cdp_url, "ok": browser.health()}
        finally:
            browser.close()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

    return app


def run_server(host: str = "127.0.0.1", port: int = 8700, idle_shutdown_minutes: float | None = None) -> None:
    """启动看板。

    看门狗：页面全部关闭后闲置 ``server.idle_shutdown_minutes`` 分钟自动退出
    （0 = 常驻）。**只要还有任务在跑就绝不退出**。
    """
    import os
    import threading

    import uvicorn

    from ..bootstrap import build_context

    app = create_app(build_context)

    if idle_shutdown_minutes is None:
        raw = os.environ.get("JOBPILOT_IDLE_MINUTES")
        idle_shutdown_minutes = float(raw) if raw else float(load_config().server.idle_shutdown_minutes)
    app.state.idle_shutdown_minutes = idle_shutdown_minutes

    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            access_log=False, log_config=None)
    server = uvicorn.Server(config)

    def watchdog() -> None:
        if idle_shutdown_minutes <= 0:
            return
        while not server.should_exit:
            time.sleep(2)
            runner = getattr(app.state, "runner", None)
            if runner is not None and runner.running:
                continue  # 任务进行中，绝不退出
            last = getattr(app.state, "last_seen", None)
            if last is None:
                continue  # 从没有页面连过（命令行起的），保持常驻
            if time.time() - last > idle_shutdown_minutes * 60:
                server.should_exit = True
                return

    threading.Thread(target=watchdog, daemon=True).start()
    server.run()
