"""智联招聘驱动（2026-10-10 实机核验）。

智联已改为 SPA，旧选择器（`.joblist-box__item` 等）**全部失效**。新结构：
- 卡片 `.job-card`（语义类名，稳定）
- 标题 `.job-card__title-main [aria-label]`
- 薪资 `.job-card__salary`
- 公司 `.job-card__company-name`
- 地点 `.job-card__location span[title]`
- 标签 `.job-card__skill-tag`（含学历/经验/技能）

链接：列表卡片本身没有链接，但**页面内嵌 JSON 带 `positionNumber`**，用它拼
`jobdetail/{number}.htm`（该格式已在详情页核验过）。
⚠️ 已实测的局限：内嵌 JSON **只覆盖第 1 批**（实测第 1 批 20/20 全部拿到岗位号），
而滚动追加的卡片里没有任何岗位号可用（无 data-* 属性、无岗位锚点、Vue 实例未暴露），
所以**第 2 批起 url 留空**。宁可没有链接，也不给一个错的。

翻页：**智联是无限滚动**（滚到底追加约 20 条），不是页码。因此 `max_pages`
在这里的含义是"要加载几批"，每多加载一批占一次搜索页额度。
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import quote

from ..browser import humanize
from ..models import ApplyResult, CollectResult, Job, MonitorResult
from ..safety.attention import release_page
from ..safety.policy import Denied, is_graceful_stop
from . import apply_probe
from .base import DriverContext, SlotSpec, _ago, explain_missing, stop_if_risky

SEARCH_URL = "https://www.zhaopin.com/jobs?kw={kw}"
# 2026-10-13 实测：不带城市时智联按 IP 猜城市，返回的不是你要的城市 → 本地按城市过滤会把整页都丢掉
# （那天下午「看到 20 个、20 个都被拦下」就是这个）。带上 jl=城市码，返回的就是这个城市的岗位。
SEARCH_URL_CITY = "https://www.zhaopin.com/jobs?jl={city}&kw={kw}"

# 智联城市码。上海 538 是 2026-10-13 实机核验的（20/20 都是上海、20/20 有岗位号）；
# 其余来自智联公开的城市页链接，没有逐个实测 —— 表里没有的城市仍按旧办法（不带城市 + 本地过滤）。
CITY_CODES: dict[str, str] = {
    "上海": "538", "北京": "530", "广州": "763", "深圳": "765", "杭州": "653", "成都": "801",
    "南京": "635", "武汉": "736", "西安": "854", "苏州": "639", "重庆": "551", "天津": "531",
}
JOB_URL = "https://www.zhaopin.com/jobdetail/{number}.htm"
# 列表页拟人悬停的目标：和 JS_LIST 用同一个卡片选择器
CARD_SELECTOR = ".job-card"

# 无限滚动：滚到底会追加卡片，不是翻页
JS_SCROLL = ("(() => { window.scrollTo(0, document.body.scrollHeight); "
             "return document.querySelectorAll('.job-card').length; })()")
JS_CARD_COUNT = "document.querySelectorAll('.job-card').length"

JS_LIST = r"""
(() => {
  const html = document.documentElement.innerHTML;
  const norm = (value) => String(value || '').replace(/\s+/g, '');
  // 严格配对：positionName 与 positionNumber 在同一对象里相邻出现。
  // 早期版本按"两个数组下标对齐"配对，很脆弱（条数不等就整张表失效）。
  const byName = new Map();
  for (const pair of html.matchAll(/"positionName":"([^"]{1,80})"[^}]{0,300}?"positionNumber":"([A-Za-z0-9]{6,})"/g)) {
    byName.set(norm(pair[1]), pair[2]);
  }
  const out = [];
  document.querySelectorAll('.job-card').forEach((card) => {
    const titleEl = card.querySelector('.job-card__title-main [aria-label]')
      || card.querySelector('.job-card__title-clamp .vue-clamp__text')
      || card.querySelector('.job-card__title-main');
    const title = titleEl ? (titleEl.getAttribute('aria-label') || titleEl.textContent || '').trim() : '';
    if (!title) return;
    const salaryEl = card.querySelector('.job-card__salary');
    const compEl = card.querySelector('.job-card__company-name');
    const locEl = card.querySelector('.job-card__location span[title]') || card.querySelector('.job-card__location');
    const tags = Array.from(card.querySelectorAll('.job-card__skill-tag'))
      .map(t => (t.textContent || '').replace(/\s+/g, ' ').trim());
    // 岗位编号优先从卡片自己的数据里拿（每张卡片的 Vue 组件都带 job.number）。
    // 2026-10-16：只靠页面 HTML 里内嵌的 JSON 时，只有第一屏 20 张卡片有编号 —— 往下滚动
    // 加载的批次是接口返回的，不进 HTML，于是八成岗位没有编号、没有链接，还会用哈希做 id 导致重复入库。
    let positionNumber = '';
    try {
      const job = card.__vue__ && card.__vue__.$props && card.__vue__.$props.job;
      if (job && /^[A-Za-z0-9]{6,}$/.test(String(job.number || ''))) positionNumber = String(job.number);
    } catch (e) { /* 拿不到就用下面的老办法 */ }
    // 第二来源：卡片里指向详情页的链接（有的加载方式不带 Vue 数据、但卡片里有 jobdetail/<编号>.htm）
    if (!positionNumber) {
      const a = card.querySelector('a[href*="jobdetail/"]');
      const m = a && (a.getAttribute('href') || '').match(/jobdetail\/([A-Za-z0-9]{6,})/);
      if (m) positionNumber = m[1];
    }
    if (!positionNumber) positionNumber = byName.get(norm(title)) || '';
    out.push({
      number: positionNumber,
      title: title,
      salary: salaryEl ? (salaryEl.textContent || '').trim() : '',
      company: compEl ? (compEl.textContent || '').replace(/\s+/g, ' ').trim() : '',
      location: locEl ? (locEl.getAttribute('title') || locEl.textContent || '').replace(/\s+/g, ' ').trim() : '',
      tags: tags
    });
  });
  return JSON.stringify(out);
})()
"""

JS_DETAIL = r"""
(() => {
  const el = document.querySelector('.describtion-card__content-wrap')
    || document.querySelector('.describtion-card__detail-content')
    || document.querySelector('.seo-card__content');
  return el ? (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 5000) : '';
})()
"""


def search_url(keyword: str, city: str = "") -> str:
    code = CITY_CODES.get((city or "").strip())
    if code:
        return SEARCH_URL_CITY.format(city=code, kw=quote(keyword))
    return SEARCH_URL.format(kw=quote(keyword))


def parse_list(raw) -> list[dict]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def make_job_id(raw: dict) -> str:
    number = str(raw.get("number") or "").strip()
    if number:
        return f"zhaopin-{number}"
    key = f"{raw.get('title','')}|{raw.get('company','')}|{raw.get('salary','')}"
    return "zhaopin-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def _job_label(item: dict) -> str:
    """日志里写「岗位名@公司」，任务监控台一眼看出在抓哪个岗位（以前写整串链接，只能截成一串编号）。"""
    title = str(item.get("title") or "").strip()
    company = str(item.get("company") or "").strip()
    return f"{title}@{company}" if title and company else title or company


class ZhaopinDriver:
    key = "zhaopin"
    name = "智联招聘"
    # 2026-10-16 第一次真机投递跑通（点「立即投递」→ 提示「已向对方发送简历」→「我的投递 · 投递成功」核实）后开放 apply
    capabilities = frozenset({"collect", "apply", "monitor"})   # monitor：2026-10-16 真机只读核验「我的投递」

    def collect(self, ctx: DriverContext) -> CollectResult:
        cfg = ctx.config.platforms.get(self.key)
        result = CollectResult(platform=self.key)
        if cfg is None or not cfg.enabled:
            result.stopped = "未启用"
            return result

        wanted = {c.strip() for c in cfg.cities if c.strip()}
        page_obj = None
        detail_page = None
        detail_exhausted = False
        # 有城市码的城市逐个搜（带 jl=）；没有城市码的，合成一次不带城市的搜索、再本地过滤（旧办法）
        coded = [c for c in wanted if c in CITY_CODES]
        targets = coded + ([""] if len(coded) < len(wanted) or not wanted else [])
        specs = [SlotSpec(f"{k}@{c}" if c else k, k, c) for k in cfg.keywords for c in targets]
        # 智联是「一个搜索页往下滚、一批一批加载」：同一个组合的批次必须连着在同一页里滚，
        # 所以这里不做广度优先（换组合 = 换页面，回不到原来滚到的位置）；轮转 / 补采 / 没轮到的组合照常记。
        try:
            with ctx.slot_session(result, specs) as session:
                for spec in session.order:
                    keyword, city, slot = spec.keyword, spec.city, spec.slot
                    recent = ctx.skip_recently_collected(slot, 1)
                    if recent is not None:
                        result.skipped += 1
                        ctx.log(f"[zhaopin] {city + '/' if city else ''}{keyword} {_ago(recent)}采过，跳过")
                        continue
                    ctx.stop.check()
                    ctx.reserve("collect_search")
                    url = search_url(keyword, city)
                    if page_obj is None:
                        page_obj = ctx.browser.open(url)
                    else:
                        page_obj.goto(url)
                    session.opened(slot)
                    if not page_obj.wait_for(".job-card", timeout_ms=30000):
                        explain_missing(page_obj, f"[zhaopin] 搜索页 {keyword}")

                    processed: set[str] = set()
                    cards = 0
                    run = ctx.slot_begin(slot, keyword, city, cfg.max_pages)   # 一批 = 一页
                    for batch in range(1, run.limit + 1):
                        if batch > 1:
                            ctx.stop.check()
                            ctx.reserve("collect_search")      # 每多加载一批 = 一次搜索页额度
                            grown = self._load_more(ctx, page_obj, cards)
                            if grown <= cards:
                                ctx.log(f"[zhaopin] {keyword} 第 {batch} 批没有新增卡片，说明已到底")
                                break
                        items = parse_list(page_obj.eval(JS_LIST))
                        cards = max(cards, len(items))
                        fresh = [item for item in items if make_job_id(item) not in processed]
                        new_before = result.new
                        ctx.log(f"[zhaopin] {keyword} 第 {batch} 批：累计 {cards} 张卡片，本批新增 {len(fresh)} 条")
                        # 先解析再扫一眼；下一批的「滚到底加载」按卡片数判断，不受这里的滚动影响
                        humanize.after_list(ctx, page_obj, CARD_SELECTOR)

                        for item in fresh:
                            ctx.stop.check()
                            job_id = make_job_id(item)
                            processed.add(job_id)
                            # 拿不到岗位编号 = 没有链接：不入库（岗位池里的岗位都要能点开、能投递）
                            if not str(item.get("number") or "").strip():
                                result.failed += 1
                                continue
                            location = item.get("location", "")
                            if wanted and location and not any(w in location for w in wanted):
                                result.filtered += 1
                                continue
                            if ctx.repo.get_job(job_id):
                                result.duplicate += 1
                                continue
                            tags = item.get("tags") or []
                            number = str(item.get("number") or "").strip()
                            job_url = JOB_URL.format(number=number) if number else ""
                            jd = " ".join(str(t) for t in tags)
                            if cfg.fetch_detail and job_url and not detail_exhausted:
                                try:
                                    jd, detail_page = self._fetch_jd(ctx, detail_page, job_url, _job_label(item))
                                except Denied as exc:
                                    if exc.kind in {"budget", "rate"}:
                                        detail_exhausted = True
                                        ctx.log("[zhaopin] 详情页预算用尽，后续只采列表字段（JD 留标签）")
                                    else:
                                        raise
                            inserted, _ = ctx.repo.upsert_job(Job(
                                id=job_id, platform=self.key,
                                external_id=number or job_id.split("-", 1)[1],
                                title=item.get("title", ""), company=item.get("company", ""),
                                salary=item.get("salary", ""),
                                city=(location.split(" ")[0] if location else ""),
                                url=job_url,
                                jd=jd,
                            ))
                            if inserted:
                                result.new += 1
                                ctx.repo.add_event(job_id, "collected", f"{keyword}（第 {batch} 批）")
                            else:
                                result.duplicate += 1
                        if ctx.slot_page(run, batch, seen=len(fresh), new=result.new - new_before):
                            break
                    result.seen += len(processed)
                    ctx.note_collect(slot, batch)      # 滚动的每一批各记一笔
                    ctx.slot_end(result, run)
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[zhaopin] {exc.reason}")
            else:
                raise
        finally:
            for page in (page_obj, detail_page):
                release_page(page)   # 验证 / 掉登录的那一页留给你处理
        return result

    def _load_more(self, ctx: DriverContext, page, previous: int, timeout_s: float = 9.0) -> int:
        """滚到底并**等待卡片数增长**；返回当前卡片数（等不到就返回原值 = 已到底）。

        用"等数量变化"而不是固定 sleep：慢网络下固定 sleep 会漏数据，快网络下又白等。
        """
        page.eval(JS_SCROLL)
        deadline = time.time() + timeout_s
        current = previous
        while time.time() < deadline:
            ctx.stop.sleep(0.6)
            try:
                current = int(page.eval(JS_CARD_COUNT))
            except (TypeError, ValueError):
                continue
            if current > previous:
                ctx.stop.sleep(0.6)      # 再多等一拍，让最后一张卡片字段渲染完
                return current
        return current

    def _fetch_jd(self, ctx: DriverContext, page, url: str, label: str = "") -> tuple[str, object]:
        """打开详情页读 JD（选择器含智联自己的拼写 describtion）；预算不足时抛 Denied。"""
        if not url:
            return "", page
        ctx.reserve("collect_detail")
        ctx.log(f"[zhaopin] 抓 JD 详情页：{label or url}")
        if page is None:
            page = ctx.browser.open(url)
        else:
            page.goto(url)
        if not page.wait_for(".describtion-card__content-wrap, .seo-card__content", timeout_ms=30000):
            stop_if_risky(page, "[zhaopin] 详情页")     # 是验证码 / 访问频繁就停手，不是才跳过这一个
            ctx.log(f"[zhaopin] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                # 读到 JD 后像人一样读一会儿再走：秒开秒关、从不滚动的详情页访问是很明显的机器特征
                humanize.after_detail(ctx, page, text)
                return text, page
            ctx.stop.sleep(1.5)
        stop_if_risky(page, "[zhaopin] 详情页")
        ctx.log(f"[zhaopin] 详情页 JD 容器为空，已跳过：{url}")
        return "", page

    def probe_apply(self, ctx: DriverContext, job: Job) -> dict:
        """投递预演：只打开岗位页看「申请职位」在不在、有没有风控，不点、不投。

        旧项目留下的结论是「智联详情页有会话级风控，投递被封」（从没在新版上核验过），
        所以智联要不要做投递，完全取决于这一步在真实页面上看到什么。
        """
        return apply_probe.probe(ctx, job, platform=self.key,
                                 ready_selector=".describtion-card__content-wrap, .summary-plane, body", label="智联招聘")

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        """一键投递：点岗位页上的「立即投递」（2026-10-16 预演核验：button.a-button，账号已登录、没有验证码）。

        和猎聘 / 前程无忧同一套：只点一次；弹出选简历的窗口时用平台默认选中的那份；
        看不出结果就去「我的投递」核对，绝不点第二次。
        """
        status, detail = apply_probe.click_apply(ctx, job, platform=self.key,
                                                 ready_selector=".describtion-card__content-wrap, .summary-plane, body",
                                                 label="智联招聘", confirm_default_resume=True)
        if status == "failed" and self._in_my_applications(ctx, job):
            return ApplyResult(status="applied", detail="点了「立即投递」，已在「我的投递」里核实")
        return ApplyResult(status=status, detail=detail)

    MY_APPLY_URL = "https://i.zhaopin.com/schedule?status=intersted"

    def _in_my_applications(self, ctx: DriverContext, job: Job) -> bool:
        """打开「我的投递」，看这个岗位（标题 + 公司挨在一起）在不在。只读。"""
        from .job51 import in_apply_list

        ctx.reserve("apply")                      # 核对也是开页面：记在投递额度上、过同一道闸门
        page = ctx.browser.open(self.MY_APPLY_URL)
        try:
            page.wait_for("body", timeout_ms=30000)
            # 这个网址默认打开的是「有意向」那一栏；刚投的岗位在「投递成功」栏（2026-10-16 实测）。
            # 切栏只是筛选列表，不会发出任何东西
            try:
                page.click("text=投递成功", timeout_ms=5000)
            except Exception:  # noqa: BLE001 - 切不过去就看当前这一栏
                pass
            for _ in range(3):                    # 列表是异步加载的，等一会儿再读
                ctx.stop.sleep(2)
                if in_apply_list(page.text(), job.title, job.company):
                    return True
            return False
        finally:
            page.close()

    def monitor(self, ctx: DriverContext) -> MonitorResult:
        """HR 进展：只读「我的投递」列表（切几个筛选栏，只是筛选）+「消息」聊天列表；都不点进任何岗位或会话。"""
        from ..browser.patchright_driver import PageHung
        from ..models import LoginRequired, PlatformRisk
        from .base import StopRequested
        from .record_monitor import monitor_records, monitor_zhaopin_chat

        result = monitor_records(ctx, platform=self.key, url=self.MY_APPLY_URL, ready="body", label="智联「我的投递」",
                                 tabs=("投递成功", "被查看", "邀面试", "不合适"))
        # 再读一遍「消息」聊天列表（2026-10-20 核对过）：HR 发的话、未读都在这里；读不到不影响上面的结果
        try:
            monitor_zhaopin_chat(ctx, result)
        except (Denied, StopRequested, PlatformRisk, LoginRequired, PageHung):
            raise                                  # 闸门 / 停止 / 风控 / 掉登录照常往上报
        except Exception as exc:  # noqa: BLE001 - 消息页改版 / 加载失败：只记一笔
            ctx.log(f"[zhaopin] 消息列表没读到：{exc}")
        return result


DRIVER = ZhaopinDriver()
