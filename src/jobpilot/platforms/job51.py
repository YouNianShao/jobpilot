"""前程无忧 51job 驱动（2026-10-10 实机核验）。

关键发现：列表节点的 `sensorsdata` 属性里**直接带完整结构化数据**
（jobId / jobTitle / jobSalary / jobArea / jobYear / jobDegree），
因此**不需要逐条打开详情页**——采集快且对平台压力小。
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

SEARCH_URL = "https://we.51job.com/pc/search?jobArea={city}&keyword={kw}&searchType=2&keywordType=&page={page}"
# 2026-10-10 实机验证：列表里的公司链接路径段是 /all/，岗位详情同一段可用
JOB_URL = "https://jobs.51job.com/all/{job_id}.html"
# 列表页拟人悬停的目标：每条岗位一个 .joblist-item
CARD_SELECTOR = ".joblist-item"

CITY_CODES: dict[str, str] = {
    "上海": "020000", "北京": "010000", "广州": "030200", "深圳": "040000",
    "杭州": "080200", "成都": "090200", "南京": "070200", "武汉": "180200",
    "西安": "200200", "苏州": "070300", "重庆": "060000", "天津": "050000",
    "长沙": "190200", "郑州": "170200", "青岛": "120200",
}

JS_LIST = r"""
(() => {
  const out = [];
  document.querySelectorAll('.joblist-item').forEach((item) => {
    const node = item.querySelector('.joblist-item-job') || item;
    let data = {};
    try { data = JSON.parse(node.getAttribute('sensorsdata') || '{}'); } catch (e) {}
    const nameEl = item.querySelector('.jname');
    const compEl = item.querySelector('a.comp .cname') || item.querySelector('a.comp');
    const links = Array.from(item.querySelectorAll('a[href]')).map(a => a.getAttribute('href') || '');
    const jobLink = links.find(h => /\/\d+\.html|\/jobs\//.test(h)) || '';
    const jobId = data.jobId || '';
    out.push({
      jobId: jobId,
      jobUrl: jobId ? ('https://jobs.51job.com/all/' + jobId + '.html') : jobLink,
      title: data.jobTitle || (nameEl ? (nameEl.getAttribute('title') || nameEl.textContent || '').trim() : ''),
      salary: data.jobSalary || '',
      area: data.jobArea || '',
      experience: data.jobYear || '',
      education: data.jobDegree || '',
      company: compEl ? (compEl.textContent || '').replace(/\s+/g, ' ').trim() : '',
      url: jobId ? ('https://jobs.51job.com/all/' + jobId + '.html') : jobLink
    });
  });
  return JSON.stringify(out);
})()
"""


JS_DETAIL = r"""
(() => {
  const el = document.querySelector('.bmsg');
  return el ? (el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 5000) : '';
})()
"""


def city_code(city: str) -> str | None:
    return CITY_CODES.get((city or "").strip())


def search_url(keyword: str, city: str, page: int = 1) -> str:
    return SEARCH_URL.format(city=city, kw=quote(keyword), page=max(1, page))


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


def in_apply_list(text: str, title: str, company: str) -> bool:
    """「我的申请」页正文里，标题和公司挨在一起出现（同一条记录）才算投过。"""
    title, company = (title or "").strip(), (company or "").strip()
    if not title or not company or not text:
        return False
    start = text.find(title)
    while start >= 0:
        if company in text[start: start + len(title) + 200]:
            return True
        start = text.find(title, start + 1)
    return False


def make_job_id(raw: dict) -> str:
    job_id = str(raw.get("jobId") or "").strip()
    if job_id:
        return f"51job-{job_id}"
    import hashlib

    key = f"{raw.get('title','')}|{raw.get('company','')}|{raw.get('salary','')}"
    return "51job-" + hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


class Job51Driver:
    key = "51job"
    name = "前程无忧"
    # 2026-10-14 两次真机投递都跑通（第二次自动在「我的申请」核实成功）后开放 apply
    capabilities = frozenset({"collect", "apply", "monitor"})   # monitor：2026-10-16 真机只读核验「我的投递」

    # 界面用这两个属性判断「城市是否受限」：受限的平台填了表外的城市会被跳过
    supported_cities = CITY_CODES
# 51job 用的是自家 6 位地区码，公开静态资源里取不到完整表（见 docs/05），
    # 所以这里把"我们确定支持的那 15 个"直接当快捷选择显示出来 ——
    # 让用户看得见边界，而不是填了别的城市以后被静默跳过。
    hot_cities: tuple[str, ...] = tuple(CITY_CODES)

    def collect(self, ctx: DriverContext) -> CollectResult:
        cfg = ctx.config.platforms.get(self.key)
        result = CollectResult(platform=self.key)
        if cfg is None or not cfg.enabled:
            result.stopped = "未启用"
            return result

        pages: dict[str, object] = {"search": None, "detail": None}
        detail_exhausted = False
        skipped_cities: list[str] = []
        specs: list[SlotSpec] = []
        for city in cfg.cities:
            code = city_code(city)
            if not code:
                skipped_cities.append(city)
                ctx.log(f"[51job] 城市不在内置表里，跳过：{city}")
                continue
            specs += [SlotSpec(f"{city}|{keyword}", keyword, city, code) for keyword in cfg.keywords]

        def fetch(spec: SlotSpec, run, page_no: int) -> tuple[int, int] | None:
            nonlocal detail_exhausted
            city, keyword = spec.city, spec.keyword
            ctx.reserve("collect_search")
            url = search_url(keyword, spec.data, page_no)
            if pages["search"] is None:
                pages["search"] = ctx.browser.open(url)
            else:
                pages["search"].goto(url)
            page_obj = pages["search"]
            if not page_obj.wait_for(".joblist-item", timeout_ms=30000):
                explain_missing(page_obj, f"[51job] 搜索页 {city}/{keyword}")

            items = parse_list(page_obj.eval(JS_LIST))
            if not items:
                return None
            result.seen += len(items)
            new_before = result.new
            ctx.log(f"[51job] {city}/{keyword} 第 {page_no} 页 {len(items)} 条")
            # 先解析再扫一眼：滚动 / 悬停不会影响已经拿到的列表
            humanize.after_list(ctx, page_obj, CARD_SELECTOR)

            for item in items:
                ctx.stop.check()
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
                            ctx.log("[51job] 详情页预算用尽，后续只采列表字段（JD 留空）")
                        else:
                            raise
                inserted, _ = ctx.repo.upsert_job(Job(
                    id=job_id, platform=self.key,
                    external_id=str(item.get("jobId") or ""),
                    title=item.get("title", ""), company=item.get("company", ""),
                    salary=item.get("salary", ""),
                    city=(item.get("area", "") or "").split("·")[0],
                    url=item.get("url", ""), jd=jd,
                ))
                if inserted:
                    result.new += 1
                    ctx.repo.add_event(job_id, "collected", f"{city}/{keyword}")
                else:
                    result.duplicate += 1
            return len(items), result.new - new_before

        try:
            # 广度优先：每个组合先翻前 2 页，再一个组合一个组合往深翻（见 DriverContext.crawl_slots）
            ctx.crawl_slots(result, specs, fetch, max_pages=cfg.max_pages)
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[51job] {exc.reason}")
            else:
                raise
        finally:
            for page in pages.values():
                release_page(page)   # 验证 / 掉登录的那一页留给你处理
        if skipped_cities and len(skipped_cities) == len(cfg.cities):
            result.stopped = ("配置的城市都不在内置表里：" + "、".join(skipped_cities)
                              + "（51job 目前只内置 15 个城市）")
        return result

    def _fetch_jd(self, ctx: DriverContext, page, url: str) -> tuple[str, object]:
        """打开详情页读 JD；返回 (jd, page)。预算不足时抛 Denied，由调用方决定是否继续。"""
        if not url:
            return "", page
        ctx.reserve("collect_detail")
        ctx.log(f"[51job] 抓 JD 详情页：{url}")
        if page is None:
            page = ctx.browser.open(url)
        else:
            page.goto(url)
        if not page.wait_for(".bmsg", timeout_ms=30000):
            stop_if_risky(page, "[51job] 详情页")       # 是验证码 / 访问频繁就停手，不是才跳过这一个
            ctx.log(f"[51job] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                # 读到 JD 后像人一样读一会儿再走：秒开秒关、从不滚动的详情页访问是很明显的机器特征
                humanize.after_detail(ctx, page, text)
                return text, page
            ctx.stop.sleep(1.5)
        stop_if_risky(page, "[51job] 详情页")
        ctx.log(f"[51job] 详情页 JD 容器为空，已跳过：{url}")
        return "", page

    def probe_apply(self, ctx: DriverContext, job: Job) -> dict:
        """投递预演：只打开岗位页看「申请」按钮在不在，不点、不投。证据够了才写真正的投递。"""
        return apply_probe.probe(ctx, job, platform=self.key, ready_selector=".bmsg, .tHeader, body", label="前程无忧")

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        """一键投递：岗位页上的「立即投递」（2026-10-13 预演核验：两个按钮，文字都是「立即投递」）。

        """
        status, detail = apply_probe.click_apply(ctx, job, platform=self.key,
                                                 ready_selector=".bmsg, .tHeader, body", label="前程无忧")
        if status == "failed":
            # 2026-10-14 实测：点完「立即投递」页面上没有可识别的成功提示，但其实投成功了
            # （「我的申请」里有这条）。所以看不出结果时去「我的申请」核对一次，**不再点任何按钮**。
            if self._in_my_applications(ctx, job):
                return ApplyResult(status="applied", detail="点了「立即投递」，已在「我的申请」里核实")
        return ApplyResult(status=status, detail=detail)

    MY_APPLY_URL = "https://i.51job.com/userset/my_apply.php?lang=c"

    def _in_my_applications(self, ctx: DriverContext, job: Job) -> bool:
        """打开「我的申请」，看这个岗位（标题 + 公司挨在一起）在不在。只读。"""
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
        """HR 进展：只读「我的申请」列表（感兴趣 / 邀面试 / 已查阅…），不点、不进会话。"""
        from .record_monitor import monitor_records

        # 前程无忧每一行不写进展，进展 = 所在的筛选栏（已查阅 / 感兴趣 / 邀面试），依次切过去看
        return monitor_records(ctx, platform=self.key, url=self.MY_APPLY_URL, ready="body", label="前程无忧「我的申请」",
                               tabs=("已查阅", "感兴趣", "邀面试"))


DRIVER = Job51Driver()
