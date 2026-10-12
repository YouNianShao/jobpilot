"""提示词：版本化文本 + 渲染。

提示词是**可版本化的产物**：文件名带版本（`scoring.v1.md`），
每次调用都把版本号记进事件，便于回放对比不同版本的效果。
"""

from __future__ import annotations

import string
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@cache
def load(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def version(name: str) -> str:
    """`scoring.v1.md` → `v1`。"""
    return name.split(".")[-2] if name.count(".") >= 2 else name


def render(name: str, **values: object) -> str:
    """用 `$var` 占位符渲染；缺失变量保持原样（不静默吞掉）。"""
    return string.Template(load(name)).safe_substitute(**values)
