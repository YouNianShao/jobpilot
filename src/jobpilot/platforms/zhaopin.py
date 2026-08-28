from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote

from jobpilot.browser import Browser
from jobpilot.control import yield_point
from jobpilot.db import get_conn, insert_job, job_exists
from jobpilot.platforms.base import BaseAdapter

# 智联搜索页（无需登录即可访问）。city 参数（cityCode / 路径 jl 码）实测无效，
# 搜索返回全国混合结果，因此城市过滤在 Python 侧按提取到的 city 字段做。
# 分页用路径格式 /sou/jl489/kw{code}/p{n}（?kw= 仅重定向到第 1 页）。
SEARCH_URL = "https://www.zhaopin.com/sou?kw={kw}"

# 从搜索结果卡片提取岗位（已用真实 Chrome 验证：卡片 .joblist-box__item，
# 标题 .jobinfo__name，公司 .companyinfo__name，薪资 .jobinfo__salary，
# 城市取首个 .jobinfo__other-info-item 文本按「·」切分）。
JS_LIST = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('.joblist-box__item'));
  const out = [];
  for (const card of cards) {
    const a = card.querySelector('a.jobinfo__name');
    if (!a) continue;
    const href = a.getAttribute('href') || '';
    const m = href.match(/jobdetail\/([^.?]+)/);
    const jobId = m ? m[1] : '';
    if (!jobId) continue;
    const title = (a.textContent || '').replace(/\s+/g, ' ').trim();
    const compEl = card.querySelector('.companyinfo__name');
    const company = compEl ? compEl.textContent.replace(/\s+/g, ' ').trim() : '';
    const salEl = card.querySelector('.jobinfo__salary');
    const salary = salEl ? salEl.textContent.replace(/\s+/g, ' ').trim() : '';
    const locItem = card.querySelector('.jobinfo__other-info-item');
    const city = locItem ? locItem.textContent.replace(/\s+/g, ' ').trim().split('·')[0] : '';
    out.push({ jobId, title, company, salary, city, url: href });
  }
  return out;
})()
"""


class ZhaopinAdapter(BaseAdapter):
    key = "zhaopin"
    name = "智联招聘"
    collect_only = True  # 仅采集；投递被平台反爬封锁，需用户手动

    # ---------- 采集 ----------
    def collect(self, cfg: dict, browser: Browser) -> int:
        conn = get_conn()
        pcfg = cfg.get("platforms", {}).get("zhaopin", {})
        cities = pcfg.get("cities", []) or []
        kws = pcfg.get("keywords", []) or []
        max_pages = min(int(pcfg.get("max_pages", 3) or 3), 10)
        new_count = 0
        tid = None
        try:
            for kw in kws:
                page_url = SEARCH_URL.format(kw=quote(kw))
                for page in range(1, max_pages + 1):
                    yield_point()
                    if tid is None:
                        tid = browser.new_tab(page_url)
                        if not tid:
                            break
                    elif page == 1:
                        browser.navigate(tid, page_url)
                    else:
                        # 用上一页最终 URL 换页码（路径 /p{n}）
                        info = browser.info(tid) or {}
                        base = info.get("url", "") or page_url
                        nxt = re.sub(r"/p\d+$", f"/p{page}", base)
                        if nxt == base:
                            break  # 无法翻页，停止本关键词
                        browser.navigate(tid, nxt)
                    time.sleep(4)
                    browser.wait_for_load(tid, 8)
                    browser.scroll(tid, y=1500)
                    time.sleep(1)
                    if self._needs_login(browser, tid):
                        print("[zhaopin] 搜索页跳登录，请确认浏览器登录态。")
                        return new_count
                    raw = browser.evaluate(tid, JS_LIST)
                    jobs = self._parse_json(raw) or []
                    if not jobs:
                        break  # 本关键词已无更多结果
                    for j in jobs:
                        jid = "zhaopin-" + j.get("jobId", "")
                        if not jid or jid == "zhaopin-":
                            continue
                        # 城市过滤（配置为空则不过滤）
                        if cities and j.get("city") not in cities:
                            continue
                        if job_exists(conn, jid):
                            continue
                        rec = {
                            "id": jid,
                            "platform": self.key,
                            "title": j.get("title", ""),
                            "company": j.get("company", ""),
                            "salary": j.get("salary", ""),
                            "city": j.get("city", ""),
                            "experience": "",
                            "jd": "",
                            "hr_name": "",
                            "url": j.get("url", ""),
                            "search_url": page_url,
                        }
                        insert_job(conn, rec)
                        new_count += 1
        finally:
            if tid:
                browser.close_tab(tid)
            conn.close()
        return new_count

    # ---------- 投递（采集-only，不会被 pipeline 调用） ----------
    def apply(self, cfg: dict, browser: Browser, job: dict) -> str:
        # 智联投递被平台反爬封锁，投递请用户在「岗位列表」点直达链接手动完成。
        return "unsupported"

    # ---------- 内部工具 ----------
    @staticmethod
    def _parse_json(raw: Any) -> list:
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                return []
        return []

    @staticmethod
    def _needs_login(browser: Browser, tid: str) -> bool:
        info = browser.info(tid) or {}
        url = (info.get("url") or "").lower()
        return "login" in url or "passport" in url or "uplogin" in url
