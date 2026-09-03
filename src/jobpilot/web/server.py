from __future__ import annotations

import importlib.metadata as _md
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs

from jobpilot.browser import Browser
from jobpilot.config import CONFIG_PATH, load_config, save_config
from jobpilot import control
from jobpilot.db import count_applied_today, get_conn, get_hr_interactions
from jobpilot.monitor import draft_reply, run_monitor
from jobpilot.pipeline import run_pipeline
from jobpilot.progress import read as read_progress

# 监测定时调度状态（线程安全）
_MONITOR_STATE: dict[str, Any] = {"running": False, "last_run": None, "next_run": None}

HERE = Path(__file__).resolve().parent
DASHBOARD = HERE / "dashboard.html"


def _app_version() -> str:
    """读取应用版本：优先包元数据，回退到 pyproject.toml，避免与单文件前端漂移。"""
    try:
        return _md.version("jobpilot")
    except Exception:  # noqa: BLE001
        try:
            txt = (HERE.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
            m = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', txt, re.M)
            return m.group(1) if m else "0.0.0"
        except Exception:  # noqa: BLE001
            return "0.0.0"

# 后台任务状态（线程安全）
_TASK: dict[str, Any] = {"running": False, "last": None, "log": []}
_TASK_LOCK = threading.Lock()


def _log(msg: str) -> None:
    with _TASK_LOCK:
        _TASK["log"].append(msg)
        if len(_TASK["log"]) > 200:
            _TASK["log"] = _TASK["log"][-200:]


def _run_in_background(mode: str, platform: str) -> None:
    control.resume()  # 新任务以「继续」状态启动，避免残留暂停导致卡住
    with _TASK_LOCK:
        _TASK["running"] = True
        _TASK["log"] = [f"开始执行 {mode} ({platform}) ..."]
    try:
        cfg = load_config()
        res = run_pipeline(cfg, platform, mode)
        with _TASK_LOCK:
            _TASK["last"] = res
            _TASK["log"].append("完成: " + json.dumps(res, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        with _TASK_LOCK:
            _TASK["last"] = {"error": str(e)}
            _TASK["log"].append("错误: " + str(e))
    finally:
        with _TASK_LOCK:
            _TASK["running"] = False


def _monitor_scheduler() -> None:
    """定时后台监测：按 monitor.auto / interval_minutes 周期运行 run_monitor。"""
    while True:
        try:
            cfg = load_config()
            mc = cfg.get("monitor", {})
            if mc.get("auto", False):
                interval = int(mc.get("interval_minutes", 30) or 30) * 60
                now = time.time()
                if not _MONITOR_STATE["running"] and (
                    _MONITOR_STATE["next_run"] is None or now >= _MONITOR_STATE["next_run"]
                ):
                    _MONITOR_STATE["running"] = True
                    try:
                        run_monitor(cfg, Browser(cfg["browser"]["proxy_url"]))
                        _MONITOR_STATE["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as e:  # noqa: BLE001
                        _MONITOR_STATE["last_run"] = f"error: {e}"
                    finally:
                        _MONITOR_STATE["running"] = False
                        _MONITOR_STATE["next_run"] = time.time() + interval
            else:
                _MONITOR_STATE["next_run"] = None
        except Exception:  # noqa: BLE001
            pass
        time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: Any = None, ctype: str = "application/json") -> None:
        if ctype == "application/json":
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        p = urlparse(self.path)
        path = p.path
        if path in ("/", "/index.html"):
            if DASHBOARD.exists():
                self._send(200, DASHBOARD.read_bytes(), "text/html")
            else:
                self._send(404, {"error": "dashboard.html 缺失"})
            return
        if path == "/api/version":
            self._send(200, {"version": _app_version()})
            return
        if path == "/api/jobs":
            q = parse_qs(p.query)
            platform = q.get("platform", ["51job"])[0]
            status = q.get("status", [""])[0]
            conn = get_conn()
            sql = "SELECT * FROM jobs"
            where, args = [], []
            if platform:
                where.append("platform=?")
                args.append(platform)
            if status:
                where.append("status=?")
                args.append(status)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY created_at DESC LIMIT 500"
            rows = conn.execute(sql, args).fetchall()
            conn.close()
            self._send(200, [dict(r) for r in rows])
            return
        if path == "/api/status":
            with _TASK_LOCK:
                task_running = _TASK["running"]
                task_last = _TASK["last"]
                task_log = _TASK["log"]
            prog = read_progress()
            now = time.time()
            # DB 实时计数（兼容独立 CLI 进程运行）
            conn = get_conn()
            daily_limit = int(load_config().get("throttle", {}).get("daily_limit", 30) or 30)
            today_applied = count_applied_today(conn)
            last = conn.execute(
                "SELECT h.job_id, h.action, h.detail, h.created_at, j.title, j.company "
                "FROM history h LEFT JOIN jobs j ON j.id=h.job_id "
                "ORDER BY h.id DESC LIMIT 1"
            ).fetchone()
            recent = conn.execute(
                "SELECT h.action, h.detail, h.created_at, j.title, j.company "
                "FROM history h LEFT JOIN jobs j ON j.id=h.job_id "
                "ORDER BY h.id DESC LIMIT 30"
            ).fetchall()
            last_apply = conn.execute(
                "SELECT created_at FROM history WHERE action='apply' AND detail='applied' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.close()
            last_action = None
            if last:
                last_action = {
                    "action": last["action"], "detail": last["detail"],
                    "created_at": last["created_at"],
                    "title": last["title"], "company": last["company"],
                }
            last_apply_ts = None
            if last_apply:
                try:
                    last_apply_ts = time.mktime(time.strptime(last_apply["created_at"], "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    last_apply_ts = None
            # 运行中判定：受管进度 / 网页任务 / 近期有真实投递
            running_heuristic = bool(last_apply_ts and (now - last_apply_ts) < 120)
            running = prog["state"] == "running" or task_running or running_heuristic
            self._send(200, {
                "running": running,
                "progress": prog,
                "paused": control.is_paused(),
                "task_running": task_running,
                "last": task_last,
                "log": task_log,
                "today_applied": today_applied,
                "daily_limit": daily_limit,
                "monitor": {
                    "auto": load_config().get("monitor", {}).get("auto", False),
                    "running": _MONITOR_STATE["running"],
                    "last_run": _MONITOR_STATE["last_run"],
                    "next_run": _MONITOR_STATE["next_run"],
                },
                "last_action": last_action,
                "recent": [
                    {"action": r["action"], "detail": r["detail"], "created_at": r["created_at"],
                     "title": r["title"], "company": r["company"]}
                    for r in recent
                ],
            })
            return
        if path == "/api/hr":
            conn = get_conn()
            rows = get_hr_interactions(conn)
            conn.close()
            self._send(200, [dict(r) for r in rows])
            return
        if path == "/api/config":
            cfg = load_config()
            ai = cfg.get("ai", {})
            masked = dict(ai)
            if masked.get("api_key"):
                masked["api_key"] = masked["api_key"][:4] + "****" + masked["api_key"][-4:]
            self._send(200, {
                "ai": masked,
                "platforms": cfg.get("platforms", {}),
                "scoring": cfg.get("scoring", {}),
                "throttle": cfg.get("throttle", {}),
                "profile": cfg.get("profile", {}),
                "monitor": cfg.get("monitor", {}),
            })
            return
        if path == "/api/browser_health":
            cfg = load_config()
            proxy = Browser(cfg["browser"]["proxy_url"])
            health = proxy.health()
            targets = proxy.targets()
            self._send(200, {"proxy": health, "targets": len(targets or [])})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        p = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            body = {}

        if p.path == "/api/run":
            mode = body.get("mode", "full")
            platform = body.get("platform", "51job")
            with _TASK_LOCK:
                if _TASK["running"]:
                    self._send(409, {"error": "已有任务在运行"})
                    return
            threading.Thread(target=_run_in_background, args=(mode, platform), daemon=True).start()
            self._send(200, {"ok": True, "msg": f"已启动 {mode}"})
            return
        if p.path == "/api/config":
            cfg = load_config()
            if "ai" in body:
                new_ai = {**cfg.get("ai", {}), **body["ai"]}
                # 防御：GET 返回的是掩码 key，若前端原样回传则不要覆盖真值
                if isinstance(new_ai.get("api_key"), str) and "*" in new_ai["api_key"]:
                    new_ai["api_key"] = cfg.get("ai", {}).get("api_key", "")
                cfg["ai"] = new_ai
            if "platforms" in body:
                cfg["platforms"] = {**cfg.get("platforms", {}), **body["platforms"]}
            if "scoring" in body:
                cfg["scoring"] = {**cfg.get("scoring", {}), **body["scoring"]}
            if "throttle" in body:
                cfg["throttle"] = {**cfg.get("throttle", {}), **body["throttle"]}
            if "profile" in body:
                cfg["profile"] = {**cfg.get("profile", {}), **body["profile"]}
            if "monitor" in body:
                cfg["monitor"] = {**cfg.get("monitor", {}), **body["monitor"]}
            save_config(cfg, CONFIG_PATH)
            self._send(200, {"ok": True})
            return
        if p.path == "/api/draft_reply":
            cfg = load_config()
            if not cfg.get("monitor", {}).get("draft_reply", True):
                self._send(403, {"error": "建议回复功能未启用"})
                return
            hr_text = (body.get("hr_text") or "").strip()
            if not hr_text:
                self._send(400, {"error": "请粘贴 HR 原话"})
                return
            draft = draft_reply(cfg, hr_text, body.get("job_title", ""), body.get("company", ""))
            self._send(200, {"draft": draft})
            return
        if p.path == "/api/pause":
            control.pause()
            self._send(200, {"ok": True, "paused": True, "task_running": _TASK["running"]})
            return
        if p.path == "/api/resume":
            control.resume()
            self._send(200, {"ok": True, "paused": False})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:  # 静默
        pass


def run_server(host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=_monitor_scheduler, daemon=True).start()
    return srv
