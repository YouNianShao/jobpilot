"""硬规则预筛：AI 评分之前挡掉明显不匹配的岗位（不消耗 AI 额度）。"""

from __future__ import annotations

import re

from .config import AppConfig
from .models import Job
from .nature import describe, detect

_INTERN = re.compile(r"实习|intern", re.IGNORECASE)
# 一个数字 + 它后面紧跟的单位（千 / 万 / k）。单位是跟着每个数字走的：前程无忧会写「8千-1.2万」
_SALARY_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*(千|万|[kK])?")
_UNIT_K = {"千": 1.0, "k": 1.0, "K": 1.0, "万": 10.0}


def parse_salary(text: str) -> tuple[float | None, float | None]:
    """解析薪资为 (月薪下限K, 上限K)。

    支持 `15-25K`、`2-3.5万`（月薪，前程无忧）、`8千-1.2万`、`20-30万/年`、`300-500元/天`、`14-24K·13薪`。
    2026-10-15 修：以前「2-3.5万」被当成 2-3.5K、「8千-1.2万」被当成 8-8K，
    设了薪资下限就会把这些高薪岗位当成「低于下限」剔掉。
    """
    raw = (text or "").replace(",", "").strip()
    if not raw:
        return None, None
    # 「·13薪」这种是年终月数，不是薪资数字
    body = re.split(r"[·•]", raw)[0]
    nums = [(float(m.group(1)), m.group(2)) for m in _SALARY_NUM.finditer(body)][:2]
    if not nums:
        return None, None
    if len(nums) == 1:
        nums = nums * 2
    (low, low_unit), (high, high_unit) = nums
    yearly = "年" in body
    daily = "天" in body or "日" in body
    if daily:
        return low * 21.75 / 1000, high * 21.75 / 1000
    # 前一个数字没写单位就跟后一个：「2-3.5万」= 2万-3.5万；「8千-1.2万」各算各的
    low_unit = low_unit or high_unit
    low_k, high_k = low * _UNIT_K.get(low_unit or "", 1.0), high * _UNIT_K.get(high_unit or "", 1.0)
    if yearly:
        return low_k / 12, high_k / 12
    return low_k, high_k


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
