"""猎聘驱动（2026-10-10 实机核验）。

猎聘用 CSS-module 哈希类名（如 `_40108OjE3M`），每次发版都变。
**只依赖语义类名与 data-nick 稳定属性**：
- 卡片容器 `.jobCardPcContainer`（哈希 + 语义类名，语义名稳定）
- 岗位锚点 `a[data-nick="job-detail-job-info"]`
- 公司 `[data-nick="job-detail-company-info"]`

详情页：2026-08 记录过 302 到 wow.liepin.com 登录墙；2026-10-10 复测没有跳转、JD 正常加载（见 docs/05）。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..browser import humanize
from ..models import ApplyResult, CollectResult, Job, MonitorResult
from ..safety.attention import release_page
from ..safety.policy import Denied, is_graceful_stop
from . import apply_probe
from .base import DriverContext, SlotSpec, explain_missing, stop_if_risky

SEARCH_URL = "https://www.liepin.com/zhaopin/?key={kw}&curPage={page}"
# 列表页拟人悬停的目标：岗位锚点（data-nick 是稳定属性）
CARD_SELECTOR = '[data-nick="job-detail-job-info"]'
JOB_URL = "https://www.liepin.com/job/{job_id}.shtml"

JS_LIST = r"""
(() => {
  const out = [];
  document.querySelectorAll('.jobCardPcContainer, [data-tlg-elem-id="c_pc_search_job_listcard"]').forEach((card) => {
    const a = card.querySelector('a[data-nick="job-detail-job-info"]');
    if (!a) return;
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || '').replace(/\s+/g, ' ').trim();
    const m = href.match(/\/job\/(\d+)\.shtml/);
    const title = (a.querySelector('.jobTitleBox .ellipsis-1[title]') || {}).getAttribute
      ? a.querySelector('.jobTitleBox .ellipsis-1[title]').getAttribute('title')
      : (text.split('【')[0] || '').trim();
    const cityM = text.match(/【([^】]+)】/);
    const salaryM = text.match(/(\d+(?:\.\d+)?-\d+(?:\.\d+)?k|面议|薪资面议)/i);
    const comp = card.querySelector('[data-nick="job-detail-company-info"]');
    out.push({
      jobId: m ? m[1] : '',
      title: (title || '').trim(),
      salary: salaryM ? salaryM[1] : '',
      city: cityM ? cityM[1] : '',
      company: comp ? (comp.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 60) : '',
      url: href
    });
  });
  return JSON.stringify(out);
})()
"""


JS_DETAIL = r"""
(() => {
  const box = document.querySelector('.job-intro-container');
  return box ? (box.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 5000) : '';
})()
"""


def search_url(keyword: str, page: int = 1) -> str:
    # curPage 从 0 开始
    return SEARCH_URL.format(kw=quote(keyword), page=max(1, page) - 1)


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
    job_id = str(raw.get("jobId") or "").strip()
    if job_id:
        return f"liepin-{job_id}"
    import hashlib

    key = f"{raw.get('title','')}|{raw.get('company','')}|{raw.get('salary','')}"
    return "liepin-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


class LiepinDriver:
    key = "liepin"
    name = "猎聘"
    # 2026-10-14 第一次真机投递跑通（「我的投递」核实）后开放 apply
    capabilities = frozenset({"collect", "apply", "monitor"})   # monitor：2026-10-16 真机只读核验「我的投递」

    def collect(self, ctx: DriverContext) -> CollectResult:
        cfg = ctx.config.platforms.get(self.key)
        result = CollectResult(platform=self.key)
        if cfg is None or not cfg.enabled:
            result.stopped = "未启用"
            return result

        wanted = {c.strip() for c in cfg.cities if c.strip()}
        pages: dict[str, object] = {"search": None, "detail": None}
        detail_exhausted = False
        specs = [SlotSpec(keyword, keyword) for keyword in cfg.keywords]   # 猎聘按关键词搜、本地按城市过滤

        def fetch(spec: SlotSpec, run, page_no: int) -> tuple[int, int] | None:
            nonlocal detail_exhausted
            keyword = spec.keyword
            ctx.reserve("collect_search")
            url = search_url(keyword, page_no)
            if pages["search"] is None:
                pages["search"] = ctx.browser.open(url)
            else:
                pages["search"].goto(url)
            page_obj = pages["search"]
            if not page_obj.wait_for('[data-nick="job-detail-job-info"]', timeout_ms=30000):
                explain_missing(page_obj, f"[liepin] 搜索页 {keyword}")

            items = parse_list(page_obj.eval(JS_LIST))
            if not items:
                return None
            result.seen += len(items)
            new_before = result.new
            ctx.log(f"[liepin] {keyword} 第 {page_no} 页 {len(items)} 条")
            # 先解析再扫一眼：滚动 / 悬停不会影响已经拿到的列表
            humanize.after_list(ctx, page_obj, CARD_SELECTOR)

            for item in items:
                ctx.stop.check()
                city = (item.get("city") or "").split("-")[0].strip()
                if wanted and city and not any(w in city or city in w for w in wanted):
                    result.filtered += 1
                    continue
                job_id = make_job_id(item)
                if ctx.repo.get_job(job_id):
                    result.duplicate += 1
                    continue
                jd = ""
                if cfg.fetch_detail and not detail_exhausted:
                    try:
                        jd, pages["detail"] = self._fetch_jd(ctx, pages["detail"], item.get("url", ""))
                    except Denied as exc:
                        if exc.kind in {"budget", "rate"}:
                            detail_exhausted = True
                            ctx.log("[liepin] 详情页预算用尽，后续只采列表字段（JD 留空）")
                        else:
                            raise
                inserted, _ = ctx.repo.upsert_job(Job(
                    id=job_id, platform=self.key,
                    external_id=str(item.get("jobId") or ""),
                    title=item.get("title", ""), company=item.get("company", ""),
                    salary=item.get("salary", ""), city=city,
                    url=item.get("url", ""), jd=jd,
                ))
                if inserted:
                    result.new += 1
                    ctx.repo.add_event(job_id, "collected", keyword)
                else:
                    result.duplicate += 1
            return len(items), result.new - new_before

        try:
            # 广度优先：每个关键词先翻前 2 页，再一个一个往深翻（见 DriverContext.crawl_slots）
            ctx.crawl_slots(result, specs, fetch, max_pages=cfg.max_pages)
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[liepin] {exc.reason}")
            else:
                raise
        finally:
            for page in pages.values():
                release_page(page)   # 验证 / 掉登录的那一页留给你处理
        return result

    def _fetch_jd(self, ctx: DriverContext, page, url: str) -> tuple[str, object]:
        """打开详情页读 JD；预算不足时抛 Denied（由调用方决定是否继续）。"""
        if not url:
            return "", page
        ctx.reserve("collect_detail")
        ctx.log(f"[liepin] 抓 JD 详情页：{url}")
        if page is None:
            page = ctx.browser.open(url)
        else:
            page.goto(url)
        if not page.wait_for(".job-intro-container", timeout_ms=30000):
            stop_if_risky(page, "[liepin] 详情页")      # 是验证码 / 访问频繁就停手，不是才跳过这一个
            ctx.log(f"[liepin] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                # 读到 JD 后像人一样读一会儿再走：秒开秒关、从不滚动的详情页访问是很明显的机器特征
                humanize.after_detail(ctx, page, text)
                return text, page
            ctx.stop.sleep(1.5)
        stop_if_risky(page, "[liepin] 详情页")
        ctx.log(f"[liepin] 详情页 JD 容器为空，已跳过：{url}")
        return "", page

    def probe_apply(self, ctx: DriverContext, job: Job) -> dict:
        """投递预演：只打开岗位页看「投个简历 / 立即投递」在不在，不点、不投。"""
        return apply_probe.probe(ctx, job, platform=self.key, ready_selector=".job-intro-container, .job-apply-operate, body", label="猎聘")

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        """一键投递：点岗位页上的「投简历」(a.btn-minor)。

        2026-10-14 第一次真机投递：以前把容器「投简历聊一聊」当按钮点，点在空白处什么都没发生
        （「我的投递」里没有这条）。现在只点真正的按钮；看不出结果时去「我的投递」核对，不再点任何东西。
        """
        # 点「投简历」会弹「选择附件简历」：用户选的是用猎聘默认选中的那份（2026-10-14）
        status, detail = apply_probe.click_apply(ctx, job, platform=self.key,
                                                 ready_selector=".job-intro-container, .job-apply-operate, body", label="猎聘",
                                                 confirm_default_resume=True)
        if status == "failed" and self._in_my_applications(ctx, job):
            return ApplyResult(status="applied", detail="点了「投简历」，已在「我的投递」里核实")
        return ApplyResult(status=status, detail=detail)

    MY_APPLY_URL = "https://c.liepin.com/job/record/apply"

    def _in_my_applications(self, ctx: DriverContext, job: Job) -> bool:
        """打开「我的投递」，看这个岗位（标题 + 公司挨在一起）在不在。只读。"""
        from .job51 import in_apply_list

        ctx.reserve("apply")                      # 核对也是开页面：记在投递额度上、过同一道闸门
        page = ctx.browser.open(self.MY_APPLY_URL)
        try:
            page.wait_for("body", timeout_ms=30000)
            for _ in range(3):                    # 列表是异步加载的，等一会儿再读
                ctx.stop.sleep(2)
                if in_apply_list(page.text(), job.title, job.company):
                    return True
            return False
        finally:
            release_page(page)   # 验证 / 掉登录的那一页留给你处理

    def monitor(self, ctx: DriverContext) -> MonitorResult:
        """HR 进展：只读「我的投递」列表（已查看 / 邀请应聘 / 不合适…），不点、不进会话。"""
        from .record_monitor import monitor_records

        return monitor_records(ctx, platform=self.key, url=self.MY_APPLY_URL, ready="body", label="猎聘「我的投递」")


DRIVER = LiepinDriver()
