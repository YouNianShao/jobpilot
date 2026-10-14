"""★ 安全内核：所有副作用的唯一闸门。

任何会改变外部世界的动作（开页面 / 投递 / 发消息）都**只能**通过
``Policy.authorize(action)`` 获得许可。驱动拿不到"直接开页面"的能力，只能申请。

闸门顺序（fail-closed）：
  0. 平台冷却锁      —— 命中验证码/限流/封禁后该平台整体拒绝（按平台隔离）
  1. 时间窗          —— 仅投递
  2. 随机休息日      —— 仅投递
  3. 每日投递上限    —— 仅投递
  4. 采集→投递冷却   —— 仅投递；刚采完不能马上发
5. **活跃时段**    —— 开页面；不在 safety.active_windows 内直接拒绝（半夜刷招聘是机器特征）
  6. **速率窗口**    —— 开页面；滚动 10 分钟 / 1 小时上限，超速先等，等太久就停手
  6. 页面预算        —— 开页面；按 平台+阶段+本地日期 计数（放在速率窗口之后：被速率挡下不该白吃额度）
  7. **开页面节流**  —— 每个平台独立计时，两次开页面之间强制随机间隔

第 7 条是防封号的第一道也是最重要的一道：机器人最明显的特征就是"零间隔连续开页面"。
第 5 条管的是另一种机器特征：**把一天的额度在短时间内猛刷完**。日预算是"今天最多开多少页"，
它管不了"这些页面是摊在一整天里开的，还是十分钟里全开完的"。
"""

from __future__ import annotations

import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..config import AppConfig
from ..db import Repo
from ..models import BudgetExceeded
from .budget import Budget
from .locks import RiskLock
from .throttle import Backoff, human_interval, in_send_window, should_take_day_off

# 预期内的"优雅停手"：这些不是崩溃，而是闸门按设计让任务停在这里。
# 驱动遇到它们要记录原因并正常收尾，否则一次"被风控保护性停下"会变成一条吓人的任务失败。
GRACEFUL_STOP_KINDS = frozenset({
    "budget",        # 页面预算用完
    "locked",        # 平台处于风险冷却
    "rate",          # 滚动窗口超速、且等不起
    "daily_limit",   # 今日投递到顶
    "outside_window",  # 不在发送时间窗
    "day_off",       # 随机休息日
"cooldown",      # 采集→投递冷却中
    "outside_active_window",   # 不在允许开页面的时段
})

# 账号级整体锁的保留键（复用 locks 表；平台名不会叫这个）
GLOBAL_LOCK_KEY = "__all__"

PAGE_OPEN = "page_open"
APPLY_ONE = "apply_one"
SEND_MESSAGE = "send_message"

_TS = "%Y-%m-%d %H:%M:%S"


class Denied(RuntimeError):
    """闸门拒绝。``kind`` 用于分类，``reason`` 用于展示。"""

    def __init__(self, kind: str, reason: str, platform: str = "") -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.platform = platform


def is_graceful_stop(exc: BaseException) -> bool:
    """这个拒绝是不是"按设计停手"（而不是需要冒泡上去的异常）。"""
    return isinstance(exc, Denied) and exc.kind in GRACEFUL_STOP_KINDS


@dataclass(frozen=True)
class Action:
    kind: str
    platform: str
    stage: str = ""
    detail: str = ""


@dataclass
class Permit:
    action: Action
    remaining: dict[str, int] = field(default_factory=dict)
    waited_seconds: float = 0.0



class Policy:
    def __init__(self, repo: Repo, config: AppConfig, sleep: Callable[[float], None] | None = None) -> None:
        self.repo = repo
        self.config = config
        self.budget = Budget(
            repo,
            config.safety.daily_pages,
            {key: value.daily_pages
             for key, value in (config.safety.platforms or {}).items()
             if value.daily_pages is not None},
        )
        self.locks = RiskLock(repo, config.safety.risk_lock_minutes)
        self._backoff: dict[str, Backoff] = {}
        # 每个平台上一次"开页面"的时间。内存是本实例的缓存，真正的来源是 runtime_kv：
        # 每个 HTTP 请求、每个任务都会新建 Policy，只放内存的话界面倒计时永远是 0，
        # 两个任务首尾相接时第二个任务的第一页也不会等（节流被绕开）。
        self._last_open: dict[str, float] = {}
        # 每个平台最近几次开页面的时刻（只留内存）：用来识别突发聚集
        self._recent_open: dict[str, deque[float]] = {}
        # 可注入的 sleep（任务里传 stop.sleep，从而"暂停/停止"能打断节流等待）
        self._sleep = sleep or time.sleep

    # ---------------- 闸门 ----------------
    def authorize(self, action: Action) -> Permit:
        safety = self.config.safety
        platform = action.platform

        # -0. 跨平台联动熔断（账号级）：多个平台在短时间内接连被拦，说明被盯上的是
        # 这个账号的行为模式，而不是某一个平台抽风 —— 这时候换个平台继续跑是最糟的选择。
        global_lock = self.locks.active(GLOBAL_LOCK_KEY)
        if global_lock:
            raise Denied(
                "locked",
                f"整体冷却中：{global_lock['reason']}（约 {global_lock['remaining_minutes']} 分钟）",
                platform,
            )
        # 0. 平台冷却锁
        lock = self.locks.active(platform)
        if lock:
            raise Denied(
                "locked",
                f"{platform} 处于风险冷却：{lock['reason']}（约 {lock['remaining_minutes']} 分钟）",
                platform,
            )

        if action.kind in (APPLY_ONE, SEND_MESSAGE):
            if not in_send_window(safety.send_windows):
                raise Denied("outside_window", "当前不在投递时间窗内", platform)
            if should_take_day_off(safety.day_off_probability):
                raise Denied("day_off", "今日随机休息（防检测）", platform)
            if action.kind == APPLY_ONE:
                applied = self.applied_today()
                if applied >= self.config.apply.daily_limit:
                    raise Denied(
                        "daily_limit",
                        f"今日已投递 {applied} 个，达上限 {self.config.apply.daily_limit}",
                        platform,
                    )
                cap = self.platform_daily_limit(platform)
                if cap is not None:
                    done = self.repo.count_applied_today(platform)
                    if done >= cap:
                        raise Denied("daily_limit", f"{platform} 今日已投递 {done} 个，达这个平台的上限 {cap}", platform)
            cooldown = self.cooldown_remaining(platform)
            if cooldown > 0:
                raise Denied(
                    "cooldown",
                    f"{platform} 采集刚结束，为降低风控需冷却 {cooldown:.0f} 秒后再投递",
                    platform,
                )

        if action.kind == PAGE_OPEN and not in_send_window(safety.active_windows):
            raise Denied(
                "outside_active_window",
                "当前不在允许开页面的时段（safety.active_windows）；"
                "半夜还在刷招聘本身就是机器特征，要改去设置 → 风控",
                platform,
            )

        waited = 0.0
        if action.kind == PAGE_OPEN:
            # 6. 速率窗口：放在预算之前——被速率挡下时不该白白吃掉一个额度
            waited = self._wait_for_rate(platform)
            try:
                self.budget.reserve(platform, action.stage)
            except BudgetExceeded as exc:
                raise Denied("budget", str(exc), platform) from exc
            # 7. 开页面节流（突破不过去——它就挡在唯一的开门动作上）
            waited += self._pace(platform)

        return Permit(action=action, remaining=self.remaining(platform), waited_seconds=waited)

    def _window_wait(self, platform: str, span_seconds: int, limit: int) -> float:
        """滚动窗口：还需要等多少秒才能再开一页（0 = 现在就能开）。

        窗口内已用 ``used`` 次、上限 ``limit``，要让计数降到 ``limit`` 以下，
        必须等最老的那几页滑出窗口；从新到旧数第 ``limit`` 条最早退出。
        """
        if limit <= 0:
            return 0.0
        since = (datetime.now() - timedelta(seconds=span_seconds)).strftime(_TS)
        used = self.repo.count_pages_since(platform, since)
        if used < limit:
            return 0.0
        stamp = self.repo.page_time_at_offset(platform, since, limit - 1)
        if not stamp:
            return 0.0
        try:
            when = datetime.strptime(stamp, _TS)
        except ValueError:
            return 0.0
        remaining = (when + timedelta(seconds=span_seconds) - datetime.now()).total_seconds()
        return max(0.0, remaining) + 1.0   # +1s 缓冲区，避免卡在窗口边界上反复抖动

    def _wait_for_rate(self, platform: str) -> float:
        """速率窗口闸门：超速先等，需要等得比 rate_wait_max_seconds 更久就直接停手。

        为什么不是无限等：等 40 分钟才放行，等于任务"看起来卡住了"，
        不如明确停手并告诉用户原因——明天再来比拿账号试探风控线划算。
        """
        safety = self.config.safety
        windows = (
            (600, safety.per_10min_for(platform), "10 分钟"),
            (3600, safety.per_hour_for(platform), "1 小时"),
        )
        waited = 0.0
        for span, limit, label in windows:
            wait = self._window_wait(platform, span, limit)
            if wait <= 0:
                continue
            cap = float(safety.rate_wait_max_seconds)
            if wait > cap:
                try:
                    self.repo.add_event(
                        None, "rate_stop",
                        f"{platform} 最近 {label} 开页面达上限 {limit} 次，需等约 {wait / 60:.0f} 分钟",
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise Denied(
                    "rate",
                    f"{platform} 最近 {label} 开页面已达上限 {limit} 次，还要等约 {wait / 60:.0f} 分钟。"
                    f"已按保守策略停手：短时间内猛刷页面是平台最容易识别的机器特征",
                    platform,
                )
            self._sleep(wait)
            waited += wait
        return waited

    def _pace(self, platform: str) -> float:
        """两次开页面之间强制一个随机间隔；返回实际等待秒数（倍率按平台解析）。"""
        safety = self.config.safety
        low = float(safety.page_interval_min_seconds)
        high = max(low, float(safety.page_interval_max_seconds))
        target = 0.0
        if high > 0:
            target = (human_interval(low, high)
                      * safety.multiplier_for(platform) * self.penalty_factor(platform))
        last = self._last_open_at(platform)
        now = time.time()
        wait = 0.0
        if last is not None and target > 0:
            wait = max(0.0, target - max(0.0, now - last))
        wait += self._burst_penalty(platform, now)
        if wait > 0:
            self._sleep(wait)
        stamp = time.time()
        self._last_open[platform] = stamp
        # 微秒精度：只留毫秒的话四舍五入可能把时刻"进"到未来，倒计时会比间隔还长一点
        self.repo.set_kv(f"last_open:{platform}", f"{stamp:.6f}")
        self._recent_open.setdefault(platform, deque(maxlen=12)).append(stamp)
        return wait

    def _last_open_at(self, platform: str) -> float | None:
        """上一次开页面的时刻（epoch 秒）：本实例内存与 runtime_kv 取较新的那个。"""
        mem = self._last_open.get(platform)
        raw = self.repo.get_kv(f"last_open:{platform}")
        try:
            stored = float(raw) if raw else None
        except ValueError:
            stored = None
        if mem is None or stored is None:
            return mem if stored is None else stored
        return max(mem, stored)


    def _burst_penalty(self, platform: str, now: float) -> float:
        """短时间连着开页面 -> 额外多等一会儿（只针对这个平台）。

        现实里的「猛刷」往往不是剧本，而是重试 / 页面秒开 / 循环没控制好造成的意外聚集，
        而它恰恰是平台最容易抓的特征 —— 所以这里主动把聚集吃掉。
        """
        times = self._recent_open.get(platform)
        if not times:
            return 0.0
        # +1 = 把「这一次」也算进去：连开 3 页时，窗口里就是 3 次
        last15 = 1 + sum(1 for item in times if now - item <= 15)
        last45 = 1 + sum(1 for item in times if now - item <= 45)
        if last45 >= 6:
            return random.uniform(4.0, 7.0)
        if last15 >= 3:
            return random.uniform(1.2, 2.8)
        return 0.0

    def next_open_in(self, platform: str) -> float:
        """距离下次"可以开页面"还有多少秒（0 = 现在就行）。

        实际间隔是随机的，这里给的是**最快**剩余时间，界面据此显示倒计时。
        """
        safety = self.config.safety
        high = max(float(safety.page_interval_min_seconds), float(safety.page_interval_max_seconds))
        if high <= 0:
            return 0.0
        last = self._last_open_at(platform)
        if last is None:
            return 0.0
        fastest = (float(safety.page_interval_min_seconds) * safety.multiplier_for(platform)
                   * self.penalty_factor(platform))
        # 距上次开页面的秒数不可能是负的（时间戳四舍五入可能落到「现在」之后一点点）
        return max(0.0, fastest - max(0.0, time.time() - last))

    # ---------------- 风控命中后的渐进降速 ----------------
    def penalty_until(self, platform: str) -> datetime | None:
        """这个平台还在不在"惩罚期"里；过期就顺手清掉。"""
        raw = self.repo.get_kv(f"penalty:{platform}")
        if not raw:
            return None
        try:
            until = datetime.strptime(raw, _TS)
        except ValueError:
            return None
        if until <= datetime.now():
            self.repo.conn.execute("DELETE FROM runtime_kv WHERE key = ?", (f"penalty:{platform}",))
            self.repo.conn.commit()
            return None
        return until

    def penalty_factor(self, platform: str) -> float:
        """惩罚期内返回配置的倍率，否则 1.0。"""
        if self.penalty_until(platform) is None:
            return 1.0
        return max(1.0, float(self.config.safety.risk_penalty_factor))

    def penalty_state(self, platform: str) -> dict | None:
        """给界面看的状态。"""
        until = self.penalty_until(platform)
        if until is None:
            return None
        return {
            "factor": self.penalty_factor(platform),
            "remaining_minutes": max(1, int((until - datetime.now()).total_seconds() // 60) + 1),
        }

    # ---------------- 反馈 ----------------
    def record_risk(self, platform: str, kind: str, detail: str = "") -> str:
        """命中验证码/限流/封禁：给该平台上锁（时长按比例随机），返回解锁时间。

        ``locks`` 表每个平台只有一行（当前锁），所以这里额外写一条事件——
        否则"这周被风控过几次"永远查不到，而这恰恰是用户最需要复盘的信息。
        """
        jitter = float(self.config.safety.risk_lock_jitter)
        minutes = float(self.config.safety.lock_minutes_for(platform))
        if jitter > 0:
            minutes = max(1.0, minutes * random.uniform(1 - jitter, 1 + jitter))
        until = self.locks.lock(platform, detail or kind, kind, minutes=minutes)
        penalty_hours = float(self.config.safety.risk_penalty_hours or 0)
        if penalty_hours > 0 and float(self.config.safety.risk_penalty_factor) > 1.0:
            # 冷却结束后别马上按原速继续：会出验证码说明这个节奏已经踩线了
            penalty_until = datetime.now() + timedelta(hours=penalty_hours)
            self.repo.set_kv(f"penalty:{platform}", penalty_until.strftime(_TS))
        try:
            self.repo.add_event(None, "risk_lock", f"{platform}/{kind} · {detail or kind} · {minutes:.0f} 分钟")
        except Exception:  # noqa: BLE001 - 记录失败不该影响上锁本身
            pass
        # 记完之后看一眼：是不是已经有别的平台也中过招了 —— 是的话整体停，而不是换平台接着跑
        self._maybe_trip_global_lock(platform)
        return until

    def _maybe_trip_global_lock(self, platform: str) -> None:
        """近一天内命中过风控的平台数达到阈值 -> 上账号级整体锁（锁到当天结束，最少 4 小时）。

        阈值来自 'safety.cross_platform_risk_platforms'（0 = 关闭）。默认 2：
        真在同一天被两个平台分别拦下，基本可以确定不是平台抽风，而是这个账号的行为模式被盯上了。
        """
        limit = int(getattr(self.config.safety, 'cross_platform_risk_platforms', 0) or 0)
        if limit <= 0:
            return
        key = "risk_platforms:" + datetime.now().strftime("%Y-%m-%d")
        seen = {item for item in (self.repo.get_kv(key) or "").split(",") if item}
        seen.add(platform)
        self.repo.set_kv(key, ",".join(sorted(seen)))
        if len(seen) < limit:
            return
        now = datetime.now()
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=0)
        minutes = max(240.0, (end_of_day - now).total_seconds() / 60.0)
        reason = f"近一天有 {len(seen)} 个平台命中风控：{', '.join(sorted(seen))}"
        self.locks.lock(GLOBAL_LOCK_KEY, reason, "multi_platform", minutes=minutes)
        try:
            self.repo.add_event(None, "risk_lock", f"〔整体〕{reason} · {minutes / 60:.0f} 小时")
        except Exception:  # noqa: BLE001 - 记录失败不影响上锁
            pass

    def clear_risk(self, platform: str) -> None:
        self.locks.clear(platform)

    def record_page_failure(self, platform: str) -> int:
        """页面读不到预期内容时调用；连续失败达阈值直接把该平台上锁。

        计数落在 runtime_kv：每次采集都是一个新任务、一个新 Policy，
        只放内存的话"连续 N 次"永远数不到 N（每次都从 1 开始）。
        """
        count = self.page_failures(platform) + 1
        self.repo.set_kv(f"page_failures:{platform}", str(count))
        limit = int(self.config.safety.max_consecutive_page_failures)
        if count >= limit:
            self.record_risk(platform, "page_failures", f"连续 {count} 次页面异常")
        return count

    def record_page_success(self, platform: str) -> None:
        if self.page_failures(platform):
            self.repo.set_kv(f"page_failures:{platform}", "0")

    def page_failures(self, platform: str) -> int:
        try:
            return int(self.repo.get_kv(f"page_failures:{platform}") or 0)
        except ValueError:
            return 0

    def record_outcome(self, action: Action, ok: bool) -> float:
        backoff = self._backoff.setdefault(action.platform, Backoff())
        if ok:
            backoff.record_success()
            return 0.0
        return backoff.record_error()

    def should_stop(self, platform: str) -> bool:
        backoff = self._backoff.get(platform)
        return bool(backoff and backoff.should_stop)

    # ---------------- 采集→投递 冷却 ----------------
    def note_collect_done(self, platform: str) -> None:
        """一次采集结束后调用：为"投递"设置一个随机冷却窗口。"""
        safety = self.config.safety
        low = float(safety.delivery_cooldown_min_minutes)
        high = max(low, float(safety.delivery_cooldown_max_minutes))
        minutes = random.uniform(low, high) if high > 0 else 0.0
        until = datetime.now() + timedelta(minutes=minutes)
        self.repo.set_kv(f"cooldown:{platform}", until.strftime(_TS))

    def cooldown_remaining(self, platform: str) -> float:
        raw = self.repo.get_kv(f"cooldown:{platform}")
        if not raw:
            return 0.0
        try:
            until = datetime.strptime(raw, _TS)
        except ValueError:
            return 0.0
        return max(0.0, (until - datetime.now()).total_seconds())

    # ---------------- 观测 ----------------
    def applied_today(self) -> int:
        return self.repo.count_events_today("applied")

    def platform_daily_limit(self, platform: str) -> int | None:
        cfg = self.config.platforms.get(platform)
        return getattr(cfg, "daily_limit", None) if cfg is not None else None

    def remaining_applies(self, platform: str) -> int:
        """这个平台今天还能投几个：取「全局剩余」和「平台剩余」里小的那个。"""
        left = max(0, self.config.apply.daily_limit - self.applied_today())
        cap = self.platform_daily_limit(platform)
        if cap is not None:
            left = min(left, max(0, cap - self.repo.count_applied_today(platform)))
        return left

    def remaining(self, platform: str) -> dict[str, int]:
        return {
            "collect_search": self.budget.remaining(platform, "collect_search"),
            "collect_detail": self.budget.remaining(platform, "collect_detail"),
            "apply": self.budget.remaining(platform, "apply"),
            "monitor": self.budget.remaining(platform, "monitor"),
            "daily_limit": self.remaining_applies(platform),
        }

    def limits_for(self, platform: str) -> dict[str, int | float]:
        """该平台**实际生效**的上限（已解析每平台覆盖）。

        界面拿它做分母。以前前端用全局 ``safety.daily_pages`` 当分母，
        BOSS 因为有自己的覆盖，进度条会显示成 40/60 这种错的数。
        """
        pages = self.config.safety.pages_for(platform)
        return {
            "collect_search": pages.collect_search,
            "collect_detail": pages.collect_detail,
            "apply": pages.apply,
            "monitor": pages.monitor,
            "total": pages.total,
            "per_10min": self.config.safety.per_10min_for(platform),
            "per_hour": self.config.safety.per_hour_for(platform),
            # 这个平台真正的每日投递上限：平台自己设了就用它（但不会超过全局），没设就是全局
            "daily_limit": int(min(self.platform_daily_limit(platform) or self.config.apply.daily_limit,
                                   self.config.apply.daily_limit)),
            "risk_lock_minutes": int(self.config.safety.lock_minutes_for(platform)),
            "multiplier": float(self.config.safety.multiplier_for(platform)),
        }

    def rate_state(self, platform: str) -> dict[str, dict[str, int]]:
        """滚动窗口用量（界面显示"近 10 分钟 x / n"）。"""
        safety = self.config.safety
        state: dict[str, dict[str, int]] = {}
        for key, span, limit in (("per_10min", 600, safety.per_10min_for(platform)),
                                 ("per_hour", 3600, safety.per_hour_for(platform))):
            since = (datetime.now() - timedelta(seconds=span)).strftime(_TS)
            used = self.repo.count_pages_since(platform, since)
            state[key] = {
                "used": used,
                "limit": limit,
                "remaining": -1 if limit <= 0 else max(0, limit - used),
            }
        return state
