from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "browser": {
        "proxy_url": "http://127.0.0.1:3457",
        "chrome_port": 9223,
    },
    "ai": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key": "",
    },
    "platforms": {
        "51job": {
            "enabled": True,
            "cities": ["上海"],
            "keywords": ["AI", "数据分析"],
            "max_pages": 3,
            "apply": True,
        },
        "liepin": {
            "enabled": True,
            "cities": ["上海"],
            "keywords": ["AI", "数据分析"],
            "max_pages": 3,
            "apply": True,
        },
        "zhaopin": {
            "enabled": True,
            "cities": ["上海"],
            "keywords": ["AI", "数据分析"],
            "max_pages": 3,
            "apply": False,
        },
    },
    "throttle": {
        "daily_limit": 30,
        "interval_min": 60,
        "interval_max": 180,
    },
    "scoring": {"threshold": 60},
    "profile": {
        "resume_path": "",
        "salary_min": 0,       # K/月，硬性下限，低于则预筛剔除
        "salary_max": 0,       # K/月，硬性上限，高于则预筛剔除（0=不限）
        "allow_internship": False,
        "deal_breakers": [],   # 标题黑名单关键词
    },
    "monitor": {
        "enabled": True,
        "auto": False,            # 定时后台运行
        "interval_minutes": 30,
        "working_hours": "09:00-21:00",
        "draft_reply": True,      # 允许粘贴 HR 原话生成建议回复
    },
}

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"


def _deep_merge(base: dict, override: dict | None) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else CONFIG_PATH
    cfg = _deep_merge(DEFAULTS, {})
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user)
    return cfg


def save_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    path = Path(path) if path else CONFIG_PATH
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
