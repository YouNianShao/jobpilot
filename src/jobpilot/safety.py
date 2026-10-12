"""风控与安全护栏（JobPilot）。

目标：把"自动投递"从"一直点"变成"有护栏地慢点"，降低账号被风控/封禁的概率。
设计上保持 JobPilot 的轻量风格——纯逻辑可单测，不依赖真实浏览器。

提供：
- 投递时间窗：只在配置的时间段内自动投递。
- 随机休息日：按概率整天跳过，模拟真人作息。
- 渐进退避：连续失败时逐级拉长等待，而不是机械重试。
- 风险冷却锁：检测到验证码/限流/拦截后，持久化锁定一段时间，跨进程生效。
- 风险信号识别：从页面文本与 URL 识别 captcha / rate_limit / blocked / login_required。
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE = ROOT / "data" / "safety_lock.json"

_WINDOW_RE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")

# 平台风控/拦截页面的正文信号。只放足够具体、不易误伤正常页面的词。
RISK_TEXT_SIGNALS: dict[str, tuple[str, ...]] = {
    "blocked": (
        "账号存在异常", "账号异常", "账号已被限制", "账号被限制", "访问被拒绝",
        "访问受限", "访问被拦截", "请求被拦截", "已被封禁",
    ),
    "captcha": (
        "请完成验证", "安全验证", "拖动滑块", "点击完成验证", "完成验证后继续",
        "人机验证", "滑块验证", "验证码校验",
    ),
    "rate_limit": (
        "操作过于频繁", "操作频繁", "访问过于频繁", "请求过于频繁",
        "访问频率", "请稍后再试", "系统繁忙，请稍后",
    ),
}

# URL 信号：验证码/登录墙通常体现在地址栏，比正文更可靠。
RISK_URL_SIGNALS: dict[str, tuple[str, ...]] = {
    "captcha": ("/captcha", "/verify", "security-check", "geetest", "/sec_verify"),
    "login_required": ("login", "passport", "uplogin", "signin", "sso"),
}

RISK_JS = r"""
(() => {
  const text = (document.body ? document.body.innerText : '') || '';
  return JSON.stringify({ url: location.href || '', text: text.replace(/\s+/g, ' ').slice(0, 600) });
})()
"""

RISK_LABELS = {
    "captcha": "验证码/人机校验",
    "rate_limit": "频率限制",
    "blocked": "账号或请求被拦截",
    "login_required": "登录态失效",
}


class PlatformRiskError(RuntimeError):
    """检测到平台风控信号时抛出，交由编排层统一做冷却处理。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail

    @property
    def label(self) -> str:
        return RISK_LABELS.get(self.kind, self.kind)


# --------------------------------------------------------------------------
# 风险信号识别
# --------------------------------------------------------------------------
def classify_risk_text(text: str) -> str | None:
    """按优先级识别正文风控信号，未命中返回 None。"""
    blob = text or ""
    for kind in ("blocked", "captcha", "rate_limit"):
        if any(kw in blob for kw in RISK_TEXT_SIGNALS[kind]):
            return kind
    return None


def classify_risk_url(url: str) -> str | None:
    """按 URL 识别验证码/登录墙，未命中返回 None。"""
    low = (url or "").lower()
    if not low:
        return None
    for kind in ("captcha", "login_required"):
        if any(sig in low for sig in RISK_URL_SIGNALS[kind]):
            return kind
    return None


def classify_risk(text: str = "", url: str = "") -> str | None:
    """综合 URL 与正文判定风控类型（URL 优先，其次正文）。"""
    return classify_risk_url(url) or classify_risk_text(text)


def _parse_probe(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def detect_page_risk(browser: Any, target_id: str) -> str | None:
    """在已打开的页面里探测风控信号，返回类型或 None。

    先看地址栏（快且准），再看正文文本；浏览器桥返回 None 时静默放行。
    """
    info = browser.info(target_id) or {}
    kind = classify_risk_url(info.get("url") or "")
    if kind:
        return kind
    probe = _parse_probe(browser.evaluate(target_id, RISK_JS))
    if not probe:
        return None
    return classify_risk(probe.get("text") or "", probe.get("url") or "")


# --------------------------------------------------------------------------
# 投递时间窗
# --------------------------------------------------------------------------
def parse_windows(spec: Any) -> list[tuple[int, int]]:
    """把 "09:00-21:00" 或 ["09:00-12:00","14:00-21:00"] 解析为分钟区间。"""
    if not spec:
        return []
    if isinstance(spec, str):
        parts = [p for p in re.split(r"[,;，；\s]+", spec) if p]
    elif isinstance(spec, (list, tuple)):
        parts = [str(p) for p in spec]
    else:
        return []
    windows: list[tuple[int, int]] = []
    for part in parts:
        for m in _WINDOW_RE.finditer(part):
            sh, sm, eh, em = (int(m.group(i)) for i in range(1, 5))
            if sh <= 23 and eh <= 23 and sm <= 59 and em <= 59:
                windows.append((sh * 60 + sm, eh * 60 + em))
    return windows


def in_send_window(spec: Any, now: datetime | None = None) -> bool:
    """当前时间是否落在任一投递窗口内；未配置窗口视为不限制。"""
    windows = parse_windows(spec)
    if not windows:
        return True
    cur = now or datetime.now()
    cur_min = cur.hour * 60 + cur.minute
    return any(start <= cur_min < end for start, end in windows)


# --------------------------------------------------------------------------
# 随机休息 / 渐进退避
# --------------------------------------------------------------------------
def should_take_day_off(probability: Any, rng: random.Random | None = None) -> bool:
    """按概率返回"今天是否整体休息"，probability<=0 永不休息。"""
    try:
        p = float(probability)
    except (TypeError, ValueError):
        return False
    if p <= 0:
        return False
    if p >= 1:
        return True
    return (rng or random).random() < p


class ProgressiveBackoff:
    """连续失败时逐级拉长等待，成功即清零。"""

    def __init__(self) -> None:
        self.errors = 0

    def record_success(self) -> None:
        self.errors = 0

    def record_error(self) -> float:
        """记录一次失败并返回建议等待秒数（60 / 120 / 300）。"""
        self.errors += 1
        if self.errors >= 3:
            return 300.0
        if self.errors == 2:
            return 120.0
        return 60.0

    @property
    def should_stop(self) -> bool:
        return self.errors >= 3


def jitter(seconds: float, spread: float = 0.25) -> float:
    """给固定等待加上 ±spread 的随机抖动，避免机械节奏。"""
    if seconds <= 0:
        return 0.0
    delta = seconds * max(0.0, spread)
    return max(0.0, random.uniform(seconds - delta, seconds + delta))


# --------------------------------------------------------------------------
# 持久化风险冷却锁（跨 CLI / Web / 定时器进程共享）
# --------------------------------------------------------------------------
def set_lock(reason: str, minutes: float, kind: str = "") -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    until = time.time() + max(0.0, float(minutes)) * 60
    payload = {
        "reason": str(reason),
        "kind": str(kind),
        "until": until,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    LOCK_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def get_lock() -> dict[str, Any] | None:
    """返回当前仍生效的锁；过期/不存在时返回 None，并顺手清理过期文件。"""
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        until = float(data.get("until") or 0)
    except (TypeError, ValueError):
        until = 0.0
    if until <= time.time():
        clear_lock()
        return None
    data["remaining_minutes"] = max(0, int((until - time.time()) // 60) + 1)
    return data


def is_locked() -> bool:
    return get_lock() is not None


def clear_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass