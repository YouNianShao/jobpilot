"""猎聘驱动（2026-10-10 实机核验）。

猎聘用 CSS-module 哈希类名（如 `_40108OjE3M`），每次发版都变。
**只依赖语义类名与 data-nick 稳定属性**：
- 卡片容器 `.jobCardPcContainer`（哈希 + 语义类名，语义名稳定）
- 岗位锚点 `a[data-nick="job-detail-job-info"]`
- 公司 `[data-nick="job-detail-company-info"]`

注意：详情页 2026-08 起 302 到 wow.liepin.com，未登录只给登录墙，故**不开详情页、JD 留空**。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..models import ApplyResult, CollectResult, Job, MonitorResult, PageReadError
from ..safety.policy import Denied, is_graceful_stop
from .base import DriverContext, _ago

SEARCH_URL = "https://www.liepin.com/zhaopin/?key={kw}&curPage={page}"
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
        try:
            for keyword in cfg.keywords:
                slot = keyword
                for page_no in range(1, cfg.max_pages + 1):
                    recent = ctx.skip_recently_collected(slot, page_no)
                    if recent is not None:
                        result.skipped += 1
                        ctx.log(f"[liepin] {keyword} 第 {page_no} 页 {_ago(recent)}采过，跳过")
                        continue
                    ctx.stop.check()
                    ctx.reserve("collect_search")
                    url = search_url(keyword, page_no)
                    if page_obj is None:
                        page_obj = ctx.browser.open(url)
                    else:
                        page_obj.goto(url)
                    if not page_obj.wait_for('[data-nick="job-detail-job-info"]', timeout_ms=12000):
                        raise PageReadError(f"[liepin] 搜索页无结果容器 {keyword}")

                    items = parse_list(page_obj.eval(JS_LIST))
                    if not items:
                        break
                    result.seen += len(items)
                    ctx.log(f"[liepin] {keyword} 第 {page_no} 页 {len(items)} 条")

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
                                jd, detail_page = self._fetch_jd(ctx, detail_page, item.get("url", ""))
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
                    ctx.note_collect(slot, page_no)
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[liepin] {exc.reason}")
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
        if not page.wait_for(".job-intro-container", timeout_ms=18000):
            ctx.log(f"[liepin] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                return text, page
            ctx.stop.sleep(1.5)
        ctx.log(f"[liepin] 详情页 JD 容器为空，已跳过：{url}")
        return "", page

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        return ApplyResult(status="unsupported", detail="猎聘投递尚未实机核验")

    def monitor(self, ctx: DriverContext) -> MonitorResult:
        return MonitorResult()


DRIVER = LiepinDriver()
