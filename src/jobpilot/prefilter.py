"""硬规则预筛：在 AI 评分之前挡掉明显不匹配的岗位。

参考 BossHunter 的 prefilter 思路，但 JobPilot 是纯 AI 评分流程，
没有预筛层，这里补上三段硬规则：实习屏蔽、标题黑名单、薪资上下限。
命中任一规则即标记 rejected，不消耗 AI 额度。
"""
from __future__ import annotations

import re
from typing import Any

_INTERN_RE = re.compile(r"实习")


def _parse_salary(salary: str) -> tuple[float | None, float | None]:
    """解析薪资字符串为 (月薪下限K, 月薪上限K)。无法解析返回 (None, None)。

    支持形如 '15-25K'、'15-25K·13薪'、'20-30万/年'、'面议'。
    """
    if not salary:
        return (None, None)
    s = salary.replace(",", "").strip()
    # 年薪（万/年）-> 换算成 K/月
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*万\s*/\s*年", s)
    if m:
        lo = float(m.group(1)) * 10 / 12
        hi = float(m.group(2)) * 10 / 12
        return (lo, hi)
    # 月薪区间 K
    m = re.search(r"(\d+(?:\.\d+)?)\s*[-~]\s*(\d+(?:\.\d+)?)\s*K", s, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    # 单一数字 K
    m = re.search(r"(\d+(?:\.\d+)?)\s*K", s, re.IGNORECASE)
    if m:
        return (float(m.group(1)), float(m.group(1)))
    return (None, None)


def prefilter_job(job: dict, profile: dict) -> tuple[bool, str]:
    """返回 (是否剔除, 原因)。"""
    title = (job.get("title") or "").lower()

    # 1. 实习屏蔽
    if not profile.get("allow_internship", False) and _INTERN_RE.search(title):
        return True, "实习岗（已屏蔽）"

    # 2. 标题黑名单
    breakers = [b.strip().lower() for b in (profile.get("deal_breakers") or []) if b.strip()]
    for b in breakers:
        if b and b in title:
            return True, f"命中黑名单词: {b}"

    # 3. 薪资上下限（硬过滤）
    smin = float(profile.get("salary_min") or 0)
    smax = float(profile.get("salary_max") or 0)
    if smin > 0 or smax > 0:
        lo, hi = _parse_salary(job.get("salary") or "")
        if lo is not None and hi is not None:
            if smin > 0 and hi < smin:
                return True, f"薪资低于下限: 上限{hi:.0f}K < {smin:.0f}K"
            if smax > 0 and lo > smax:
                return True, f"薪资高于上限: 下限{lo:.0f}K > {smax:.0f}K"

    return False, ""
