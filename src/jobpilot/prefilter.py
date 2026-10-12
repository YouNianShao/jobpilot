"""硬规则预筛：AI 评分之前挡掉明显不匹配的岗位（不消耗 AI 额度）。"""

from __future__ import annotations

import re

from .config import AppConfig
from .models import Job
from .nature import describe, detect

_INTERN = re.compile(r"实习|intern", re.IGNORECASE)
_SALARY_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至]\s*(\d+(?:\.\d+)?)")
_SINGLE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """解析薪资为 (月薪下限K, 上限K)。支持 `15-25K`、`20-30万/年`、`300-500元/天`。"""
    raw = (text or "").replace(",", "").strip()
    if not raw:
        return None, None

    money = _SALARY_RANGE.search(raw)
    pair = (float(money.group(1)), float(money.group(2))) if money else None
    if pair is None:
        single = _SINGLE.search(raw)
        pair = (float(single.group(1)), float(single.group(1))) if single else None
    if pair is None:
        return None, None

    low, high = pair
    if "万" in raw and "年" in raw:
        return low * 10 / 12, high * 10 / 12
    if "元/天" in raw or "元天" in raw:
        return low * 21.75 / 1000, high * 21.75 / 1000
    return low, high


def prefilter(job: Job, config: AppConfig) -> tuple[bool, str]:
    """返回 (是否剔除, 原因)。"""
    title = job.title or ""
    profile = config.profile

    if not profile.allow_internship and _INTERN.search(title):
        return True, "实习岗（已屏蔽）"

    block = {str(item).strip() for item in (profile.block_natures or []) if str(item).strip()}
    if block:
        hits = [key for key in detect(title, job.company, job.jd) if key in block]
        if hits:
            return True, f"岗位性质命中：{describe(hits)}"

    for word in profile.deal_breakers:
        if word and word.strip() and word.strip().lower() in title.lower():
            return True, f"命中黑名单词: {word.strip()}"

    salary_min = float(profile.salary_min or 0)
    salary_max = float(profile.salary_max or 0)
    if salary_min > 0 or salary_max > 0:
        low, high = parse_salary(job.salary)
        if low is not None and high is not None:
            if salary_min > 0 and high < salary_min:
                return True, f"薪资低于下限: 上限{high:.0f}K < {salary_min:.0f}K"
            if salary_max > 0 and low > salary_max:
                return True, f"薪资高于上限: 下限{low:.0f}K > {salary_max:.0f}K"

    return False, ""
