from __future__ import annotations

import json
import time
from pathlib import Path

STATUS_FILE = Path(__file__).resolve().parents[2] / "data" / "run_status.json"

_DEFAULT = {
    "state": "idle",        # idle | running | done | error
    "mode": "",             # collect | score | apply | full
    "phase": "",            # collect | score | apply
    "current": 0,
    "total": 0,
    "label": "",            # 当前正在处理的岗位（标题@公司）
    "last_status": "",      # 上一次投递结果（applied/unsupported/...）
    "message": "",
    "apply_blocked": False, # 投递被平台拦截（反爬），需醒目提示
    "score_failed": False,  # 评分批量失败（AI 配置/Key 问题），需醒目提示
    "score_reason": "",     # 评分失败的代表性原因
    "started_at": None,
    "updated_at": 0.0,      # epoch 秒，用于判断"是否在近期活动"
}


def _now() -> float:
    return time.time()


def read() -> dict:
    try:
        d = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    base = dict(_DEFAULT)
    base.update(d or {})
    return base


def _write(d: dict) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    d["updated_at"] = _now()
    STATUS_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def start(mode: str, phase: str, total: int, label: str = "") -> None:
    d = dict(_DEFAULT)
    d.update({
        "state": "running",
        "mode": mode,
        "phase": phase,
        "total": total,
        "current": 0,
        "label": label,
        "apply_blocked": False,
        "score_failed": False,
        "score_reason": "",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _write(d)


def blocked(flag: bool) -> None:
    """标记投递被平台拦截（用于前端醒目红色提示）。"""
    d = read()
    d["apply_blocked"] = bool(flag)
    _write(d)


def score_failed(flag: bool, reason: str = "") -> None:
    """标记评分批量失败（AI 配置/Key 问题，用于前端醒目红色提示）。"""
    d = read()
    d["score_failed"] = bool(flag)
    if reason:
        d["score_reason"] = str(reason)
    _write(d)


def phase(phase: str, total: int, current: int = 0, label: str = "") -> None:
    d = read()
    d.update({"state": "running", "phase": phase, "total": total, "current": current, "label": label})
    _write(d)


def job(current: int, total: int, label: str, last_status: str = "") -> None:
    d = read()
    d.update({
        "state": "running",
        "current": current,
        "total": total,
        "label": label,
        "last_status": last_status,
    })
    _write(d)


def done(message: str = "") -> None:
    d = read()
    d.update({"state": "done", "message": message, "label": ""})
    _write(d)


def error(message: str) -> None:
    d = read()
    d.update({"state": "error", "message": message})
    _write(d)


def idle() -> None:
    _write(dict(_DEFAULT))
