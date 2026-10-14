"""配置加载与校验（pydantic）。"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobpilot.db"


class BrowserCfg(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    cdp_url: str = "http://127.0.0.1:9223"


class AiCfg(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    # 服务商预设（见 ai/providers.py）：决定走哪种协议；留空 = 按 base_url 自动认
    provider: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-flash"
    api_key: str = ""
    timeout_seconds: float = 30.0
    max_attempts: int = 2


class ProfileCfg(BaseModel):
    resume_path: str = ""
    salary_min: float = 0.0
    salary_max: float = 0.0
    allow_internship: bool = False
    deal_breakers: list[str] = Field(default_factory=list)
    # 直接剔除的岗位性质（见 jobpilot.nature）：outsourcing / training / marketing。
    # 默认空 —— 只做标记不替你做决定；填进来就会在 AI 评分之前直接拒掉。
    block_natures: list[str] = Field(default_factory=list)
    greeting_preference: str = ""
    # 定制简历的输出目录（相对项目根或绝对路径）
    resume_output_dir: str = "./data/resumes"


class PlatformCfg(BaseModel):
    enabled: bool = False
    cities: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=3, ge=1, le=20)
    apply: bool = False
    # 这个平台每天最多投几个（空 = 只受全局「每日投递上限」约束）。
    # 账号风险是按平台算的：新开放的平台先给小额度，跑稳了再加；全局上限仍然是所有平台合计的硬顶。
    daily_limit: int | None = Field(default=None, ge=1, le=200)
    # 是否打开详情页抓 JD。每个岗位 = 一次详情页开页（占页面预算），所以默认关闭。
    # 开了它 AI 评分明显更准；不开就只能靠标题+标签+薪资判断。
    fetch_detail: bool = False


class ScoringCfg(BaseModel):
    threshold: int = Field(default=60, ge=0, le=100)
    concurrency: int = Field(default=1, ge=1, le=3)


class ConfirmCfg(BaseModel):
    required: bool = True


class AutonomyCfg(BaseModel):
    """自治等级：L0 手动 / L1 建议（默认）/ L2 半自动 / L3 全自动。"""

    level: str = "L1"

    @field_validator("level")
    @classmethod
    def _check_level(cls, value: str) -> str:
        if value not in {"L0", "L1", "L2", "L3"}:
            raise ValueError("autonomy.level 必须是 L0 / L1 / L2 / L3")
        return value


class GreetingCfg(BaseModel):
    enabled: bool = True


class FollowUpCfg(BaseModel):
    """自动跟进：投递后多久没动静、最多跟进几次、两次之间至少隔多久。

    面试官不会因为你"跟得太勤"给分，但平台会因为你"发得太勤"给你风控——
    所以这里既有次数上限，也有最小间隔。
    """

    enabled: bool = True
    after_hours: float = Field(default=48, ge=1)        # 投递后多久开始跟进
    max_times: int = Field(default=1, ge=1)             # 同一岗位最多跟进几次
    min_gap_hours: float = Field(default=72, ge=1)      # 两次跟进之间的最小间隔


class ApplyCfg(BaseModel):
    daily_limit: int = Field(default=30, ge=1)
    interval_min: int = Field(default=60, ge=1)
    interval_max: int = Field(default=180, ge=1)
    max_consecutive_failures: int = Field(default=3, ge=1)


class ServerCfg(BaseModel):
    """看板服务：页面全部关闭后闲置多久自动退出（0 = 常驻）。

    这是为了匹配直觉——关掉网页就该停，而不是留一个孤儿进程。
    """

    host: str = "127.0.0.1"
    port: int = 8700
    idle_shutdown_minutes: float = Field(default=2.0, ge=0)


class MonitorCfg(BaseModel):
    enabled: bool = True
    auto: bool = False
    interval_minutes: int = Field(default=30, ge=1)
    working_hours: str = "09:00-21:00"


class DailyPages(BaseModel):
    collect_search: int = Field(default=60, ge=0)
    collect_detail: int = Field(default=300, ge=0)
    apply: int = Field(default=60, ge=0)
    monitor: int = Field(default=40, ge=0)
    total: int = Field(default=500, ge=0)


class PlatformSafetyCfg(BaseModel):
    """单个平台的风控覆盖项；只写想覆盖的字段，其余继承全局默认。"""

    daily_pages: DailyPages | None = None
    delay_multiplier: float | None = None
    risk_lock_minutes: int | None = None
    max_pages_per_10min: int | None = None
    max_pages_per_hour: int | None = None


class SafetyCfg(BaseModel):
    # 全局默认（所有平台的兜底）
    daily_pages: DailyPages = Field(default_factory=DailyPages)
    # 每平台覆盖：BOSS 这类高风控平台设得更保守（见 _default_safety_overrides）
    platforms: dict[str, PlatformSafetyCfg] = Field(default_factory=lambda: _default_safety_overrides())

    # 开页面之间的间隔（每个平台独立计时）。这是最容易被判定为机器人的特征，
    # 也是本项目第一条硬约束：任何开页面动作都要先经过它。
    page_interval_min_seconds: float = Field(default=2.5, ge=0)
    page_interval_max_seconds: float = Field(default=7.0, ge=0)
    # 全局保守系数：把上面的间隔整体拉长（BOSS 这类高风控平台建议 2.0+）
    delay_multiplier: float = Field(default=1.5, ge=1.0, le=5.0)

    # 采集结束到开始投递之间必须冷却（避免"刚翻完页就立刻发消息"这种机器节奏）
    delivery_cooldown_min_minutes: float = Field(default=5.0, ge=0)
    delivery_cooldown_max_minutes: float = Field(default=15.0, ge=0)

    # 连续多少次"页面读不到预期内容"就把该平台停掉（fail-closed 的兜底）
    max_consecutive_page_failures: int = Field(default=3, ge=1)

    send_windows: list[str] = Field(default_factory=lambda: ["08:00-22:00"])
    # 允许**开页面**的时段（采集/投递/监测都算）。空列表 = 不限。
    # 为什么算风控：机器人最容易被识别的另一面是"在人不会活动的时间活动" ——
    # 凌晨三点翻 200 个岗位页，比白天同样动作可疑得多。默认留 06:00-24:00（只排除真正在睡觉的那 6 小时），
    # 覆盖正常作息；要半夜跑就去设置里改成空。
    active_windows: list[str] = Field(default_factory=lambda: ["06:00-24:00"])
    day_off_probability: float = Field(default=0.05, ge=0, le=1)
    # 冷却时长按比例随机化（避免每次都精确 30 分钟这种机器特征）
    # 跨平台联动熔断：24 小时内 >= N 个不同平台命中风控 -> 整体冷却到当天结束（0 = 关闭）。
    # 为什么需要：单平台的锁只保护那一个平台；而同一个账号被多个平台接连拦下说明被盯上的是行为模式。
    cross_platform_risk_platforms: int = Field(default=2, ge=0, le=4)
    risk_lock_minutes: int = Field(default=30, ge=1)
    risk_lock_jitter: float = Field(default=0.35, ge=0, le=1)

    # 被平台警告过（验证码/限流）之后，冷却结束也别马上按原速继续：
    # 在接下来 risk_penalty_hours 小时内，这个平台的开页面间隔额外乘以 risk_penalty_factor。
    # 理由很直接——会出验证码说明当前节奏已经踩线了，原样恢复等于再踩一次。
    # 1 或 0 小时 = 关闭这个惩罚。
    risk_penalty_hours: float = Field(default=24.0, ge=0)
    risk_penalty_factor: float = Field(default=2.0, ge=1.0, le=10.0)

    # ── 滚动窗口速率上限 ─────────────────────────────────────────────
    # 每日预算只回答"今天最多开多少页"，不回答"这些页面是在 1 小时里开完的，
    # 还是摊在一整天里开的"。而"短时间猛翻几百页"恰恰是机器最明显的节奏。
    # 所以再叠一层滚动窗口：10 分钟窗防突发，1 小时窗防持续高速。
    # 0 = 不限。超速时先尝试"等窗口滑过去"，需要等得比 rate_wait_max_seconds
    # 更久就直接停手——宁可今天少跑，也不要拿账号去试探平台的风控线。
    # 同一个「城市+关键词」多少小时内不重复采。
    # 为什么算风控：重复点一次"采集"会把同样的搜索页再开一遍，一个新岗位都多不出来，
    # 却实打实地多花一次页面预算 —— 纯粹的账号风险，没有任何收益。
    # 0 = 每次都采（想手动强制刷新时用）。
    recollect_after_hours: float = Field(default=6.0, ge=0)

    max_pages_per_10min: int = Field(default=15, ge=0)
    max_pages_per_hour: int = Field(default=60, ge=0)
    rate_wait_max_seconds: float = Field(default=900.0, ge=0)

    def pages_for(self, platform: str) -> DailyPages:
        override = (self.platforms or {}).get(platform)
        if override is not None and override.daily_pages is not None:
            return override.daily_pages
        return self.daily_pages

    def multiplier_for(self, platform: str) -> float:
        override = (self.platforms or {}).get(platform)
        if override is not None and override.delay_multiplier is not None:
            return float(override.delay_multiplier)
        return float(self.delay_multiplier)

    def lock_minutes_for(self, platform: str) -> int:
        override = (self.platforms or {}).get(platform)
        if override is not None and override.risk_lock_minutes is not None:
            return int(override.risk_lock_minutes)
        return int(self.risk_lock_minutes)

    def per_10min_for(self, platform: str) -> int:
        override = (self.platforms or {}).get(platform)
        if override is not None and override.max_pages_per_10min is not None:
            return int(override.max_pages_per_10min)
        return int(self.max_pages_per_10min)

    def per_hour_for(self, platform: str) -> int:
        override = (self.platforms or {}).get(platform)
        if override is not None and override.max_pages_per_hour is not None:
            return int(override.max_pages_per_hour)
        return int(self.max_pages_per_hour)


def _default_safety_overrides() -> dict[str, PlatformSafetyCfg]:
    """BOSS 的反爬最强，默认就更保守；其它平台用全局默认。"""
    return {
        "boss": PlatformSafetyCfg(
            daily_pages=DailyPages(collect_search=40, collect_detail=200, apply=30, monitor=30, total=300),
            delay_multiplier=2.0,
            risk_lock_minutes=60,
            max_pages_per_10min=8,
            max_pages_per_hour=30,
        ),
    }


def _default_platforms() -> dict[str, PlatformCfg]:
    return {
# BOSS 的列表页没有薪资、也没有 JD，所以默认开详情页；关掉就只剩标题/公司/城市
        "boss": PlatformCfg(enabled=True, cities=["上海"], keywords=["AI"], apply=False, fetch_detail=True),
        "51job": PlatformCfg(enabled=True, cities=["上海"], keywords=["AI"], apply=True),
        "liepin": PlatformCfg(enabled=True, cities=["上海"], keywords=["AI"], apply=True),
        "zhaopin": PlatformCfg(enabled=True, cities=["上海"], keywords=["AI"], apply=False),
    }


class AppConfig(BaseModel):
    browser: BrowserCfg = Field(default_factory=BrowserCfg)
    ai: AiCfg = Field(default_factory=AiCfg)
    profile: ProfileCfg = Field(default_factory=ProfileCfg)
    platforms: dict[str, PlatformCfg] = Field(default_factory=_default_platforms)
    scoring: ScoringCfg = Field(default_factory=ScoringCfg)
    autonomy: AutonomyCfg = Field(default_factory=AutonomyCfg)
    confirm: ConfirmCfg = Field(default_factory=ConfirmCfg)
    greeting: GreetingCfg = Field(default_factory=GreetingCfg)
    follow_up: FollowUpCfg = Field(default_factory=FollowUpCfg)
    apply: ApplyCfg = Field(default_factory=ApplyCfg)
    monitor: MonitorCfg = Field(default_factory=MonitorCfg)
    safety: SafetyCfg = Field(default_factory=SafetyCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)


def load_config(path: Path | None = None) -> AppConfig:
    """读取 config.yaml；不存在时返回默认配置。字段错误会直接抛错（不做静默兜底）。

    平台段支持"只写想覆盖的字段"：文件里没写的键，用**该平台的默认值**补齐。
    不这么做的话，漏写一个键就会掉到 PlatformCfg 的字段默认值，跟"全新默认配置"
    不一致 —— 例如 BOSS 需要开详情页才有薪资和 JD，被漏写就会静默变成不开。
    """
    path = Path(path) if path else CONFIG_PATH
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        defaults = _default_platforms()
        platforms = data.get("platforms")
        if isinstance(platforms, dict):
            for key, value in list(platforms.items()):
                base = defaults.get(key)
                if base is not None and isinstance(value, dict):
                    platforms[key] = {**base.model_dump(), **value}
        return AppConfig.model_validate(data)
    return AppConfig()


def field_rules() -> dict[str, dict]:
    """每个配置字段的类型与上下限（点号路径 -> 规则），给界面做实时校验用。

    规则从 pydantic 模型里读出来，而不是在前端再写一遍：模型是「配置即契约」的唯一来源，
    抄一份到 JS 里，改了这边忘了那边，前端就会放过后端一定会拒的值（或者反过来）。
    dict[str, Model] 字段（如 platforms）用 * 表示任意键：``platforms.*.max_pages``。
    """
    import types
    import typing

    import annotated_types as at

    rules: dict[str, dict] = {}

    def base_type(annotation):
        # 去掉 Optional / X | None，只留真正的类型
        if typing.get_origin(annotation) in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            return base_type(args[0]) if len(args) == 1 else annotation
        return annotation

    def walk(model: type[BaseModel], prefix: str) -> None:
        for name, info in model.model_fields.items():
            path = f"{prefix}{name}"
            annotation = base_type(info.annotation)
            origin = typing.get_origin(annotation)
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                walk(annotation, path + ".")
                continue
            if origin is dict:
                value_type = base_type(typing.get_args(annotation)[1])
                if isinstance(value_type, type) and issubclass(value_type, BaseModel):
                    walk(value_type, path + ".*.")
                continue
            kind = ("list" if origin is list else
                    {bool: "bool", int: "int", float: "float", str: "str"}.get(annotation, "str"))
            rule: dict = {"type": kind}
            for meta in info.metadata:
                if isinstance(meta, at.Ge):
                    rule["min"] = meta.ge
                elif isinstance(meta, at.Gt):
                    rule["min"] = meta.gt
                    rule["exclusive_min"] = True
                elif isinstance(meta, at.Le):
                    rule["max"] = meta.le
                elif isinstance(meta, at.Lt):
                    rule["max"] = meta.lt
                    rule["exclusive_max"] = True
            rules[path] = rule

    walk(AppConfig, "")
    return rules


def config_problems(config: AppConfig) -> dict[str, str]:
    """跨字段的业务规则（点号路径 -> 人话）。

    只在保存时检查，**不放进模型校验**：放进去的话，一份历史上就填反了的 config.yaml
    会让整个看板启动失败；而这些问题只该拦住「这一次保存」，并告诉用户改哪一项。
    """
    problems: dict[str, str] = {}
    if config.apply.interval_min > config.apply.interval_max:
        problems["apply.interval_max"] = "最大间隔不能小于最小间隔"
    safety = config.safety
    if safety.page_interval_min_seconds > safety.page_interval_max_seconds:
        problems["safety.page_interval_max_seconds"] = "最长间隔不能小于最短间隔"
    if config.profile.salary_max and config.profile.salary_min > config.profile.salary_max:
        problems["profile.salary_max"] = "薪资上限不能低于下限（0 = 不限）"
    return problems


def save_config(config: AppConfig, path: Path | None = None) -> None:
    path = Path(path) if path else CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="python")
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
