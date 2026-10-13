"""前程无忧 51job 驱动（2026-10-10 实机核验）。

关键发现：列表节点的 `sensorsdata` 属性里**直接带完整结构化数据**
（jobId / jobTitle / jobSalary / jobArea / jobYear / jobDegree），
因此**不需要逐条打开详情页**——采集快且对平台压力小。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..models import ApplyResult, CollectResult, Job, MonitorResult, PageReadError
from ..safety.policy import Denied, is_graceful_stop
from . import apply_probe
from .base import DriverContext, _ago

SEARCH_URL = "https://we.51job.com/pc/search?jobArea={city}&keyword={kw}&searchType=2&keywordType=&page={page}"
# 2026-10-10 实机验证：列表里的公司链接路径段是 /all/，岗位详情同一段可用
JOB_URL = "https://jobs.51job.com/all/{job_id}.html"

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
    capabilities = frozenset({"collect"})

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

        page_obj = None
        detail_page = None
        detail_exhausted = False
        skipped_cities: list[str] = []
        try:
            for city in cfg.cities:
                code = city_code(city)
                if not code:
                    skipped_cities.append(city)
                    ctx.log(f"[51job] 城市不在内置表里，跳过：{city}")
                    continue
                for keyword in cfg.keywords:
                    slot = f"{city}|{keyword}"
                    for page_no in range(1, cfg.max_pages + 1):
                        recent = ctx.skip_recently_collected(slot, page_no)
                        if recent is not None:
                            result.skipped += 1
                            ctx.log(f"[51job] {city}/{keyword} 第 {page_no} 页 {_ago(recent)}采过，跳过")
                            continue
                        ctx.stop.check()
                        ctx.reserve("collect_search")
                        url = search_url(keyword, code, page_no)
                        if page_obj is None:
                            page_obj = ctx.browser.open(url)
                        else:
                            page_obj.goto(url)
                        if not page_obj.wait_for(".joblist-item", timeout_ms=12000):
                            raise PageReadError(f"[51job] 搜索页无结果容器 {city}/{keyword}")

                        items = parse_list(page_obj.eval(JS_LIST))
                        if not items:
                            break
                        result.seen += len(items)
                        ctx.log(f"[51job] {city}/{keyword} 第 {page_no} 页 {len(items)} 条")

                        for item in items:
                            ctx.stop.check()
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
                        ctx.note_collect(slot, page_no)
        except Denied as exc:
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[51job] {exc.reason}")
            else:
                raise
        finally:
            for page in (page_obj, detail_page):
                if page is not None:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
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
        if not page.wait_for(".bmsg", timeout_ms=18000):
            ctx.log(f"[51job] 详情页没等到 JD 容器，已跳过：{url}")
            return "", page
        # 容器先出现、正文后渲染的情况很常见：轮询直到拿到非空文本，避免白花一次开页额度
        for _ in range(6):
            text = str(page.eval(JS_DETAIL) or "").strip()
            if text:
                return text, page
            ctx.stop.sleep(1.5)
        ctx.log(f"[51job] 详情页 JD 容器为空，已跳过：{url}")
        return "", page

    def probe_apply(self, ctx: DriverContext, job: Job) -> dict:
        """投递预演：只打开岗位页看「申请」按钮在不在，不点、不投。证据够了才写真正的投递。"""
        return apply_probe.probe(ctx, job, platform=self.key, ready_selector=".bmsg, .tHeader, body", label="前程无忧")

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        # 能力集里没有 apply：任务层会先拒绝，这里只是兜底。先用 probe_apply 在真机上把按钮核验清楚
        return ApplyResult(status="unsupported", detail="51job 投递尚未实机核验")

    def monitor(self, ctx: DriverContext) -> MonitorResult:
        return MonitorResult()


DRIVER = Job51Driver()
