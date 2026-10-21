"""平台驱动协议 + 驱动运行上下文。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Protocol, runtime_checkable

from ..browser.driver import BrowserDriver
from ..config import AppConfig
from ..db import Repo
from ..models import ApplyResult, CollectResult, Job, MonitorResult
from ..safety.policy import PAGE_OPEN, Action, Policy


class StopRequested(Exception):
    """协作式停止：任务被用户请求停止。"""


class StopSignal:
    """停止 + 暂停信号（替代老版的 control.py 全局事件）。"""

    def __init__(self, event: Event | None = None) -> None:
        self._stop = event or Event()
        self._pause = Event()
        self._pause.set()

    def request(self) -> None:
        self._stop.set()

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def check(self) -> None:
        """在安全点调用：若请求停止则抛 StopRequested，若暂停则阻塞。"""
        # 暂停时也要泵浏览器连接：以前是干等 Event，任务一暂停，浏览器里新开的标签页
        # （例如 BOSS 掉登录、你去登录）就一直转圈（2026-10-16 你截图里 BOSS 采集正是「已暂停」）
        while not self._pause.is_set():
            if not self._pump(0.5):
                self._pause.wait(0.5)
            if self._stop.is_set():
                break
        if self._stop.is_set():
            raise StopRequested()

    def sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self.check()
            step = min(0.5, max(0.0, end - time.time()))
            if not self._pump(step):
                time.sleep(step)

    # 等待时「泵」浏览器连接。2026-10-16 实测：任务在等间隔 / 拟人停留 / 冷却时，线程用 time.sleep
    # 睡着，patchright 的驱动消息没人处理 —— 浏览器里新开的标签页（你自己去登录也一样）在等驱动
    # 回话，于是一直转圈；4 个任务一起等的时候最明显。等待期间改成让浏览器连接继续收发消息。
    def add_pump(self, pump) -> None:
        self.__dict__.setdefault("_pumps", []).append(pump)

    def _pump(self, seconds: float) -> bool:
        pumps = self.__dict__.get("_pumps") or []
        if not pumps:
            return False
        share = seconds / len(pumps)
        alive = False
        for pump in list(pumps):
            try:
                if pump(share):
                    alive = True
            except Exception:  # noqa: BLE001 - 连接已经断了：去掉它，退回普通的睡
                pumps.remove(pump)
        return alive


@dataclass
class SlotRun:
    """一个 关键词×城市 组合这一次的翻页账本（提前结束 / 深度记忆 / 新岗位率都记在这）。"""

    slot: str
    label: str          # 日志里的说法：「上海/AI」
    name: str           # 给人看的提示里的说法：「AI · 上海」
    limit: int          # 这次最多翻到第几页
    max_pages: int
    full: bool          # 完整翻一次：翻到 max_pages、不提前结束（顺便更新深度记忆）
    seen: int = 0
    new: int = 0
    pages: int = 0      # 真正打开处理过的页数（"刚采过、跳过"的页不算）
    streak: int = 0     # 连续几页没有新岗位
    deepest_new: int = 0
    last_page: int = 0
    stopped_at: int = 0  # 提前结束停在第几页（0 = 没有提前结束）
    gaps: bool = False   # 中间有「刚采过、跳过」的页：那几页有没有新岗位不知道


# 每个平台上次采集停在哪个组合之后（下一轮从它的下一个开始）/ 上次没轮到、要先补的组合
CURSOR_KEY = "collect:cursor:{platform}"
CARRY_KEY = "collect:carry:{platform}"
CARRY_KEEP_DAYS = 3.0
#: 广度优先第一轮：每个组合先翻前几页（再一个组合一个组合往深翻）
SHALLOW_PAGES = 2
#: 这些停手原因下没轮到的组合记成「没采到」、下次先补（额度用完 / 速率窗口）
MISS_KINDS = frozenset({"budget", "rate"})
_TS = "%Y-%m-%d %H:%M:%S"


@dataclass
class SlotSpec:
    """驱动交给共享调度器的一个 关键词×城市 组合。"""

    slot: str            # 组合键（翻页水位 / 深度记忆 / 游标都用它）
    keyword: str
    city: str = ""       # 平台没有城市维度（猎聘）就留空
    data: object = None  # 驱动自己的东西（例如城市码）

    @property
    def name(self) -> str:
        return f"{self.keyword} · {self.city}" if self.city else self.keyword


def read_carry(repo: Repo, platform: str, *, now: datetime | None = None) -> dict:
    """上次没采到、要先补的组合：{slots: [...], labels: [...], since: str}；过期（3 天）/ 没有 = {}。"""
    raw = repo.get_kv(CARRY_KEY.format(platform=platform))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        since = datetime.strptime(str(data.get("since") or ""), _TS)
    except (ValueError, TypeError, AttributeError):
        return {}
    if ((now or datetime.now()) - since).total_seconds() > CARRY_KEEP_DAYS * 86400:
        return {}
    slots = [str(s) for s in data.get("slots") or []]
    labels = [str(s) for s in data.get("labels") or []]
    if not slots:
        return {}
    labels = (labels + slots[len(labels):])[:len(slots)]
    return {"slots": slots, "labels": labels, "since": data.get("since")}


def write_carry(repo: Repo, platform: str, slots: list[str], labels: list[str], since: str | None = None) -> None:
    """整体写回（空 = 清掉）。"""
    key = CARRY_KEY.format(platform=platform)
    if not slots:
        repo.set_kv(key, "")
        return
    repo.set_kv(key, json.dumps({"slots": list(slots), "labels": list(labels),
                                 "since": since or datetime.now().strftime(_TS)}, ensure_ascii=False))


class SlotSession:
    """一次采集的组合级账本：with 块里被 ``Denied`` 打断时记下没轮到的组合、并进「下次先补」；
    不管怎么结束都把轮转游标停在最后真正开过页的组合上。"""

    def __init__(self, ctx: DriverContext, result: CollectResult, order: list[SlotSpec]) -> None:
        self.ctx, self.result, self.order = ctx, result, order
        self.runs: dict[str, SlotRun] = {}   # crawl_slots 填：停在半路时补记新岗位率 / 深度
        self.done: set[str] = set()
        self._opened: set[str] = set()
        self._cursor = ""

    def opened(self, slot: str) -> None:
        """这个组合这次真的开过一页了。"""
        self._cursor = slot
        if slot not in self._opened:
            self._opened.add(slot)
            self.ctx._carry_done(slot)

    def __enter__(self) -> SlotSession:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        from ..safety.policy import Denied

        try:
            if isinstance(exc, Denied):
                self.result.stop_kind = exc.kind
            if isinstance(exc, Denied) and exc.kind in MISS_KINDS:
                for spec in self.order:
                    if spec.slot in self.runs and spec.slot not in self.done:
                        self.ctx.slot_end(self.result, self.runs[spec.slot], finished=False)  # 停在半路：深度只往深记
                        if spec.slot in self._opened:
                            self.result.partial.append(spec.name)
                            self.result.partial_slots.append(spec.slot)
                    if spec.slot not in self._opened:
                        self.result.missed.append(spec.name)
                        self.result.missed_slots.append(spec.slot)
                self.ctx._carry_add(self.result)
        finally:
            if self._cursor and self.ctx.only_slots is None:
                self.ctx.repo.set_kv(CURSOR_KEY.format(platform=self.ctx.platform), self._cursor)
        return False


@dataclass
class DriverContext:
    """平台驱动能用到的一切（不包含任何平台细节）。

    驱动**不能**自己开页面——必须 ``ctx.reserve(stage)`` 过安全内核拿到许可。
    """

    config: AppConfig
    repo: Repo
    browser: BrowserDriver
    policy: Policy
    stop: StopSignal
    platform: str = ""
    log: Callable[[str], None] = lambda message: None
    progress: Callable[[int, int, str], None] = lambda current, total, label: None
    # 只采这几个组合（自动续采 / 手动指定）；None = 全部组合
    only_slots: list[str] | None = None

    # ---------------- 组合调度：广度优先 + 轮转 + 补采 ----------------
    # 实测（账号 B）：每次采集都被 1 小时开页上限 / 当日搜索额度拦在半路。以前每个组合从第 1 页翻到
    # 第 N 页、组合顺序固定 —— 额度全花在前几个组合的深页（大多是重复）上，后面的组合永远轮不到。
    def slot_order(self, specs: list[SlotSpec], result: CollectResult | None = None) -> list[SlotSpec]:
        """这次按什么顺序采：指定了组合就只采那些；上次没采到的排最前；其余从上次停下的下一个开始。"""
        if self.only_slots is not None:
            wanted = list(dict.fromkeys(str(s) for s in self.only_slots))
            by_key = {spec.slot: spec for spec in specs}
            return [by_key[key] for key in wanted if key in by_key]
        order = list(specs)
        cursor = self.repo.get_kv(CURSOR_KEY.format(platform=self.platform)) or ""
        keys = [spec.slot for spec in order]
        if cursor in keys:
            start = keys.index(cursor) + 1
            order = order[start:] + order[:start]
        carry = read_carry(self.repo, self.platform)
        carried = [key for key in carry.get("slots", []) if key in keys]
        if carried:
            front = [spec for key in carried for spec in order if spec.slot == key]
            order = front + [spec for spec in order if spec.slot not in carried]
            if result is not None:
                result.carried = [spec.name for spec in front]
            self.log(f"[{self.platform}] 先补上次没采到的：" + "、".join(spec.name for spec in front))
        return order

    def slot_session(self, result: CollectResult, specs: list[SlotSpec]) -> SlotSession:
        """组合级账本（排序 / 轮转游标 / 补采 / 没轮到的组合），给不走 crawl_slots 的驱动（智联）用。"""
        return SlotSession(self, result, self.slot_order(specs, result))

    def crawl_slots(self, result: CollectResult, specs: list[SlotSpec],
                    fetch: Callable[[SlotSpec, SlotRun, int], tuple[int, int] | None],
                    *, max_pages: int, shallow: int | None = None) -> None:
        """共享翻页调度：第一轮每个组合翻前 ``shallow`` 页，第二轮再一个组合一个组合往深翻。

        ``fetch(spec, run, page_no)`` 开第 page_no 页（自己 ``reserve``）、处理完返回 ``(seen, new)``；
        返回 None = 这个组合到底了（没有结果）。「刚采过、跳过」/ 记水位 / 提前结束 / 深度记忆都在这里做，
        驱动只管开页面和入库。``Denied`` 原样往上抛，抛之前记下没轮到的组合（``result.missed``）、
        把轮转游标停在最后真正开过页的组合上（下次从它的下一个开始）。
        """
        shallow = SHALLOW_PAGES if shallow is None else max(1, int(shallow))
        runs: dict[str, SlotRun] = {}
        done: set[str] = set()       # 到底 / 提前结束 / 翻满
        with self.slot_session(result, specs) as session:
            session.runs, session.done = runs, done
            for depth_pass in (1, 2):
                for spec in session.order:
                    if spec.slot in done:
                        continue
                    run = runs.get(spec.slot)
                    if run is None:
                        run = runs[spec.slot] = self.slot_begin(spec.slot, spec.keyword, spec.city, max_pages)
                    cut = min(run.limit, shallow)
                    pages = range(1, cut + 1) if depth_pass == 1 else range(cut + 1, run.limit + 1)
                    finished = False
                    for page_no in pages:
                        recent = self.skip_recently_collected(spec.slot, page_no)
                        if recent is not None:
                            result.skipped += 1
                            self.log(f"[{self.platform}] {run.label} 第 {page_no} 页 {_ago(recent)}采过，跳过")
                            continue
                        self.stop.check()
                        got = fetch(spec, run, page_no)
                        session.opened(spec.slot)
                        self.note_collect(spec.slot, page_no)   # 处理完才记（没有结果也算采过）；被打断就不记
                        if got is None:
                            finished = True
                            break
                        seen, new = got
                        if self.slot_page(run, page_no, seen=seen, new=new):
                            finished = True
                            break
                    if finished or depth_pass == 2 or cut >= run.limit:
                        done.add(spec.slot)
                        self.slot_end(result, run)

    def _carry_done(self, slot: str) -> None:
        """这个组合这次真的开过页了：从「要先补的」里划掉。"""
        carry = read_carry(self.repo, self.platform)
        if slot not in carry.get("slots", []):
            return
        pairs = [(s, n) for s, n in zip(carry["slots"], carry["labels"], strict=False) if s != slot]
        write_carry(self.repo, self.platform, [s for s, _ in pairs], [n for _, n in pairs], carry.get("since"))

    def _carry_add(self, result: CollectResult) -> None:
        """停在半路：没轮到的组合并进「下次先补」（保持顺序，已有的排前面）。"""
        if not result.missed_slots:
            return
        carry = read_carry(self.repo, self.platform)
        slots, labels = list(carry.get("slots", [])), list(carry.get("labels", []))
        for slot, name in zip(result.missed_slots, result.missed, strict=False):
            if slot not in slots:
                slots.append(slot)
                labels.append(name)
        write_carry(self.repo, self.platform, slots, labels, carry.get("since"))

    # ---------------- 采集去重（别为了同样的结果重复开页面） ----------------
    def collect_seen_seconds(self, slot: str, page: int) -> float | None:
        """这一页上次采完是多久以前（秒）。**None = 没有记录**。

        注意：时间戳只精确到秒，所以"同一秒内采过"会算出 0.0 秒 ——
        用 0 当"没采过"的哨兵值会把这种情况漏掉（同一个测试当场抓到的 bug）。
        """
        raw = self.repo.get_kv(f"collect:{self.platform}:{slot}:p{int(page)}")
        if not raw:
            return None
        try:
            when = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return max(0.0, (datetime.now() - when).total_seconds())

    def skip_recently_collected(self, slot: str, page: int) -> float | None:
        """这一页该跳过吗？**None = 该采**；否则返回"多久前采过"的秒数（可能是 0.0）。

        **按页记账**而不是按词：一次采集被打断（点停止 / 崩了 / 网络断）时，
        已采完的页留着水位，下次只重做没做完的页 —— 否则中断一次就等于把整轮
        页面预算再花一遍（这个坑真踩过：白开 10 个详情页）。
        """
        hours = float(self.config.safety.recollect_after_hours or 0)
        if hours <= 0:
            return None
        seen = self.collect_seen_seconds(slot, page)
        if seen is None or seen >= hours * 3600:
            return None
        return seen

    def note_collect(self, slot: str, page: int) -> None:
        """这一页处理完了才记一笔（中途被打断就不记，下次会重做它）。"""
        self.repo.set_kv(f"collect:{self.platform}:{slot}:p{int(page)}",
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ---------------- 翻页省预算：提前结束 + 深度记忆 ----------------
    # 实测（账号 B）：前程无忧一轮看了 1200 张卡片，1130 张早就在库里；BOSS 300 张里 290 张重复。
    # 每个组合都从第 1 页翻到第 max_pages 页，大部分搜索页额度和风险都花在「全是老岗位」的页上。
    def slot_begin(self, slot: str, keyword: str, city: str, max_pages: int) -> SlotRun:
        """开始翻一个组合：按上次「有新岗位的最深一页」决定这次最多翻几页。"""
        label = f"{city}/{keyword}" if city else keyword
        name = f"{keyword} · {city}" if city else keyword
        max_pages = max(1, int(max_pages or 1))
        days = float(getattr(self.config.safety, "full_recrawl_days", 3.0) or 0)
        if days <= 0:                                   # 关掉深度记忆：每次都按 max_pages 翻（仍会提前结束）
            return SlotRun(slot, label, name, max_pages, max_pages, full=False)
        record = self._depth_record(slot)
        due = record is None
        if record is not None:
            try:
                full_at = datetime.strptime(str(record.get("full_at") or ""), "%Y-%m-%d %H:%M:%S")
                due = (datetime.now() - full_at).total_seconds() >= days * 86400
            except ValueError:
                due = True
        if due:
            return SlotRun(slot, label, name, max_pages, max_pages, full=True)
        depth = max(0, int(record.get("depth") or 0))
        limit = min(max_pages, depth + 2)
        if limit < max_pages:
            self.log(f"[{self.platform}] {label}：上次第 {depth} 页之后就没有新岗位，"
                     f"这次最多翻到第 {limit} 页（每 {days:g} 天完整翻一次）")
        return SlotRun(slot, label, name, limit, max_pages, full=False)

    def slot_page(self, run: SlotRun, page_no: int, *, seen: int, new: int) -> bool:
        """一页处理完了调用一次；返回 True = 这个组合到此为止。

        只统计真正打开的页：「刚采过、跳过」的页不调用它，所以不算进连续空页。
        """
        run.pages += 1
        if int(page_no) - run.last_page > 1:
            run.gaps = True
        run.last_page = int(page_no)
        run.seen += max(0, int(seen))
        run.new += max(0, int(new))
        if new > 0:
            run.streak = 0
            run.deepest_new = max(run.deepest_new, int(page_no))
            return False
        run.streak += 1
        need = int(getattr(self.config.safety, "stop_after_empty_pages", 2) or 0)
        if run.full or need <= 0 or run.streak < need or page_no >= run.limit:
            return False
        run.stopped_at = int(page_no)
        self.log(f"[{self.platform}] {run.label}：连续 {run.streak} 页没有新岗位，"
                 f"提前结束这个组合（省下 {run.limit - page_no} 页）")
        return True

    def slot_end(self, result: CollectResult, run: SlotRun, *, finished: bool = True) -> None:
        """组合翻完（含提前结束 / 到底）：记总数、记每组合的新岗位率、更新深度记忆。

        ``finished=False``：被额度 / 速率停在半路 —— 只记新岗位率、深度只往深记，
        不算「完整翻过一次」（否则没翻完的组合会被记成浅深度、下次少翻）。
        """
        if run.stopped_at:
            result.early_stopped += 1
            result.pages_saved += run.limit - run.stopped_at
        if finished:
            result.pages_saved += run.max_pages - run.limit      # 按上次深度封顶少翻的页
        if run.pages <= 0:                                   # 全是「刚采过」跳过的页：什么都不知道，不改记忆
            return
        result.slots.append({"slot": run.slot, "label": run.name, "seen": run.seen,
                             "new": run.new, "pages": run.pages})
        if float(getattr(self.config.safety, "full_recrawl_days", 3.0) or 0) <= 0:
            return
        record = self._depth_record(run.slot) or {}
        if run.full and finished:
            depth = run.deepest_new
            if run.gaps:                                # 有跳过的页：别把深度记浅了
                depth = max(depth, int(record.get("depth") or 0))
            record = {"depth": depth, "full_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        elif run.deepest_new > int(record.get("depth") or 0):
            record["depth"] = run.deepest_new
        else:
            return
        self.repo.set_kv(f"collect:depth:{self.platform}:{run.slot}", json.dumps(record, ensure_ascii=False))

    def _depth_record(self, slot: str) -> dict | None:
        raw = self.repo.get_kv(f"collect:depth:{self.platform}:{slot}")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def reserve(self, stage: str) -> None:
        """开页面前过闸门；被拒时抛 ``safety.Denied``。

        闸门里可能含"节流"和"速率窗口"的等待，等待时长会写进任务日志——
        否则界面会看起来像卡住了。
        """
        permit = self.policy.authorize(Action(kind=PAGE_OPEN, platform=self.platform, stage=stage))
        if permit.waited_seconds >= 1:
            self.log(f"[safety] 放慢节奏：等了 {permit.waited_seconds:.0f} 秒再开下一页")


def explain_missing(page, label: str) -> None:
    """页面上没等到预期内容时调用：先判断是不是风控 / 登录墙，是就按那个抛（会锁平台 / 提示登录），
    都不是才算「页面异常」。以前前程无忧 / 猎聘 / 智联直接抛页面异常 —— 智联整页被换成腾讯云的
    人机验证（2026-10-14）时，系统只把它当「改版或加载慢」，还会接着开页面。
    """
    from ..models import LoginRequired, PageReadError, PlatformRisk
    from ..safety.attention import tag_page
    from ..safety.risk import classify_risk

    kind = classify_risk(page.text(), page.url)
    # tag_page：验证 / 掉登录时记下是这一页，关页面时把它留给你处理（见 safety/attention.py）
    if kind == "login_required":
        raise tag_page(LoginRequired(f"{label} 需要登录"), page)
    if kind:
        raise tag_page(PlatformRisk(kind, label), page)
    raise PageReadError(f"{label} 未找到预期内容（可能改版或被拦截）")


def stop_if_risky(page, label: str) -> None:
    """详情页没等到 JD 时调用：是风控 / 登录墙就抛（停手、锁平台），不是才允许「跳过这一个、接着下一个」。

    2026-10-15：猎聘 / 前程无忧 / 智联的详情页拿不到 JD 时只记一句「已跳过」就去开下一页，
    从不看这一页是不是验证码 / 访问频繁 —— 平台已经开始拦了，我们还在一页接一页地往上撞。
    """
    from ..models import LoginRequired, PlatformRisk
    from ..safety.attention import tag_page
    from ..safety.risk import classify_risk

    try:
        kind = classify_risk(page.text(), page.url)
    except Exception:  # noqa: BLE001 - 页面读不出来就当普通失败，交给连续失败计数
        return
    if kind == "login_required":
        raise tag_page(LoginRequired(f"{label} 需要登录"), page)
    if kind:
        raise tag_page(PlatformRisk(kind, f"{label}出现风控信号"), page)


def _ago(seconds: float) -> str:
    """把"多久前"说成人话（0 秒是合法的：同一秒内又点了一次采集）。"""
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    return f"{seconds / 3600:.1f} 小时前"


@runtime_checkable
class PlatformDriver(Protocol):
    key: str
    name: str
    capabilities: frozenset[str]

    def collect(self, ctx: DriverContext) -> CollectResult: ...

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult: ...

    def monitor(self, ctx: DriverContext) -> MonitorResult: ...


def supports(driver: PlatformDriver, capability: str) -> bool:
    return capability in driver.capabilities
