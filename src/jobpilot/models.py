"""领域模型与错误类型。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# --------------------------------------------------------------------------
# 错误
# --------------------------------------------------------------------------
class JobPilotError(RuntimeError):
    """所有业务异常的基类。"""


class BudgetExceeded(JobPilotError):
    """页面预算耗尽（当天该平台停止）。"""

    def __init__(self, platform: str, stage: str, limit: int) -> None:
        super().__init__(f"{platform} 的 {stage} 已达当日上限 {limit}")
        self.platform = platform
        self.stage = stage
        self.limit = limit


class PlatformRisk(JobPilotError):
    """命中验证码 / 限流 / 封禁，需停止该平台并进入冷却。"""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class LoginRequired(JobPilotError):
    """登录态失效（停止但不冷却）。"""


class PageReadError(JobPilotError):
    """页面读取失败（区别于"列表为空"）。"""


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
class Status(str, Enum):
    NEW = "new"
    REJECTED = "rejected"
    FILTERED = "filtered"
    PENDING_CONFIRM = "pending_confirm"
    APPROVED = "approved"
    APPLIED = "applied"
    ALREADY = "already"
    NEED_RESUME = "need_resume"
    FAILED = "failed"
    REPLIED = "replied"
    INTERVIEW = "interview"


#: 投递结束态（不可重复投递）
APPLIED_STATUSES = {
    Status.APPLIED.value,
    Status.ALREADY.value,
    Status.REPLIED.value,
    Status.INTERVIEW.value,
}

#: 可被"重新评分/重新确认"的状态
SCORABLE_STATUSES = {Status.NEW.value}


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def normalize(value: str) -> str:
    """公司/标题归一化（跨平台去重用）。"""
    return re.sub(r"[\s\-—_·・,，.。()（）\[\]【】/|]+", "", str(value or "")).lower()


# --------------------------------------------------------------------------
# 实体
# --------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    platform: str
    external_id: str
    title: str
    company: str
    salary: str = ""
    city: str = ""
    url: str = ""
    jd: str = ""
    score: int | None = None
    score_reason: str = ""
    greeting: str = ""
    resume_path: str = ""
    follow_up: str = ""
    follow_up_at: str = ""
    deleted_at: str = ""
    status: str = Status.NEW.value
    dup_of: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @property
    def norm_title(self) -> str:
        return normalize(self.title)

    @property
    def norm_company(self) -> str:
        return normalize(self.company)


@dataclass
class CollectResult:
    platform: str
    new: int = 0
    seen: int = 0
    duplicate: int = 0
    filtered: int = 0
    failed: int = 0
    skipped: int = 0        # 因为"刚采过"而跳过的 城市+关键词 数量
    stopped: str = ""
    early_stopped: int = 0  # 连续几页没有新岗位、提前结束的 关键词×城市 组合数
    pages_saved: int = 0    # 因提前结束 / 按上次深度封顶而少翻的页数
    # 每个组合一条：{slot, label, seen, new, pages}（看哪个组合几乎采不到新岗位）
    slots: list[dict] = field(default_factory=list)
    # 这次没轮到的组合（额度 / 速率窗口停手）：给人看的说法「数据分析 · 上海」
    missed: list[str] = field(default_factory=list)
    missed_slots: list[str] = field(default_factory=list)   # 同上，组合键（续采 / 补采用）
    # 开过页、但被停在半路还没翻完的组合（速率窗口停手时一起续采；不进「下次先补」）
    partial: list[str] = field(default_factory=list)
    partial_slots: list[str] = field(default_factory=list)
    stop_kind: str = ""     # 停手原因分类：Denied.kind（budget / rate / locked …），没停 = ""
    resume_at: str = ""     # 安排了自动续采时：大约几点接着采（YYYY-MM-DD HH:MM:SS，由采集服务填）
    carried: list[str] = field(default_factory=list)   # 这次先补的、上次没采到的组合（给人看的说法）


@dataclass
class ApplyResult:
    status: str
    detail: str = ""


@dataclass
class MonitorResult:
    checked: int = 0
    events: dict[str, int] = field(default_factory=dict)
