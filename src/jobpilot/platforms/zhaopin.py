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

from ..models import ApplyResult, CollectResult, Job, MonitorResult, PageReadError
from ..safety.policy import Denied, is_graceful_stop
from . import apply_probe
from .base import DriverContext, _ago

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
    const positionNumber = byName.get(norm(title)) || '';
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


class ZhaopinDriver:
    key = "zhaopin"
    name = "智联招聘"
    capabilities = frozenset({"collect"})

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
        try:
            for keyword, city in ((k, c) for k in cfg.keywords for c in targets):
                slot = f"{keyword}@{city}" if city else keyword
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
                if not page_obj.wait_for(".job-card", timeout_ms=12000):
                    raise PageReadError(f"[zhaopin] 搜索页无结果容器 {keyword}")

                processed: set[str] = set()
                cards = 0
                for batch in range(1, max(1, cfg.max_pages) + 1):
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
                    ctx.log(f"[zhaopin] {keyword} 第 {batch} 批：累计 {cards} 张卡片，本批新增 {len(fresh)} 条")

                    for item in fresh:
                        ctx.stop.check()
                        job_id = make_job_id(item)
                        processed.add(job_id)
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
                                jd, detail_page = self._fetch_jd(ctx, detail_page, job_url)
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
                result.seen += len(processed)
                ctx.note_collect(slot, batch)      # 滚动的每一批各记一笔
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[zhaopin] {exc.reason}")
            else:
                raise
        finally:
            for page in (page_obj, detail_page):
                if page is not None:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
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

    def _fetch_jd(self, ctx: DriverContext, page, url: str) -> tuple[str, object]:
        """打开详情页读 JD（选择器含智联自己的拼写 describtion）；预算不足时抛 Denied。"""
        if not url:
            return "", page
        ctx.reserve("collect_detail")
        ctx.log(f"[zhaopin] 抓 JD 详情页：{url}")
        if page is None:
            page = ctx.browser.open(url)
        else:
            page.goto(url)
        if not page.wait_for(".describtion-card__content-wrap, .seo-card__content", timeout_ms=18000):
            ctx.log(f"[zhaopin] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                return text, page
            ctx.stop.sleep(1.5)
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
        return ApplyResult(status="unsupported", detail="智联仅支持采集，请在平台内手动投递")

    def monitor(self, ctx: DriverContext) -> MonitorResult:
        return MonitorResult()


DRIVER = ZhaopinDriver()
