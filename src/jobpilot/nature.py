"""岗位性质识别：在花 AI 额度之前，先看出这个岗位大概是什么。

为什么需要它：招聘平台上一大半噪音不是"技能不匹配"，而是**性质不对**——
外包/驻场、培训招生、以及话术包装的高提成销售。这类岗位的共同点是
标题和 JD 里有很稳定的用词，用规则就能识别，不需要模型，也不该浪费一次 AI 调用。

这里只做**标记**（`/api/jobs` 会带上 tags，界面显示徽章），是否直接剔除由
`profile.block_natures` 决定，默认空 —— 不替用户做决定。
"""

from __future__ import annotations

import re

# 每一种性质：key -> (中文名, 命中就说明是这个性质的正则)
RULES: dict[str, tuple[str, tuple[str, ...]]] = {
    "outsourcing": ("外包/派遣", (
        r"外包", r"外派", r"驻场", r"派遣", r"劳务", r"人力服务", r"人才服务",
        r"众包", r"外服", r"乙方现场", r"甲方现场",
    )),
    "training": ("培训/招生", (
        r"包就业", r"保就业", r"零基础", r"转行培训", r"招生", r"实训",
        r"学费", r"先培训", r"培训生", r"就业班",
    )),
    "marketing": ("营销话术", (
        r"新风口", r"躺赚", r"日结", r"无经验月入", r"包吃住", r"提供单人间",
        r"月入过万", r"轻松过万", r"无责底薪", r"高提成", r"电话销售", r"地推",
        r"陌拜", r"电销", r"拓客", r"招商",
    )),
}

_COMPILED = {
    key: (label, re.compile("|".join(patterns), re.IGNORECASE))
    for key, (label, patterns) in RULES.items()
}


def nature_labels() -> dict[str, str]:
    """key -> 中文名（界面/配置里用来提示）。"""
    return {key: label for key, (label, _) in RULES.items()}


def detect(title: str = "", company: str = "", jd: str = "") -> tuple[str, ...]:
    """返回命中的性质 key（可能多个）。招聘用词也算证据，所以公司名一起看。"""
    haystack = " ".join(part for part in (title or "", company or "", (jd or "")[:1500]))
    hits = []
    for key, (_, pattern) in _COMPILED.items():
        if pattern.search(haystack):
            hits.append(key)
    return tuple(hits)


def describe(keys) -> str:
    """把 key 变成人话，用于预筛原因/界面提示。"""
    labels = nature_labels()
    return "、".join(labels.get(key, key) for key in keys)
