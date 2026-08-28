from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from jobpilot.browser import Browser
from jobpilot.control import yield_point
from jobpilot.db import get_conn, insert_job, job_exists
from jobpilot.platforms.base import BaseAdapter

# 51job 新版搜索页（SPA）。jobArea 为城市码，keyword 为关键词。
SEARCH_URL = "https://we.51job.com/pc/search?jobArea={city}&keyword={kw}&searchType=2&keywordType=&page={page}"

CITY_CODES = {
    "上海": "020000",
    "北京": "010000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    "成都": "090200",
    "南京": "070200",
    "武汉": "180200",
    "西安": "200200",
    "苏州": "070300",
    "重庆": "060000",
    "天津": "050000",
    "长沙": "190200",
    "郑州": "170200",
    "青岛": "120200",
}

# 从搜索结果页提取岗位卡片（已按真实 DOM 实现）
JS_LIST = r"""
(() => {
  const items = Array.from(document.querySelectorAll('.joblist-item'));
  return items.map(it => {
    const jobEl = it.querySelector('.joblist-item-job');
    let jobId = '';
    if (jobEl) {
      try { jobId = (JSON.parse(jobEl.getAttribute('sensorsdata') || '{}').jobId) || ''; } catch (e) {}
    }
    const nameEl = it.querySelector('.joblist-item-jobname .jname') || it.querySelector('.jname');
    const compEl = it.querySelector('a.comp');
    const compName = (compEl && compEl.querySelector('.cname')) || compEl;
    const salEl = it.querySelector('.joblist-item-jobinfo .sal') || it.querySelector('.sal');
    const areaEl = it.querySelector('.joblist-item-jobinfo .area');
    const tags = Array.from(it.querySelectorAll('.joblist-item-tags .tag'))
      .map(t => (t.getAttribute('title') || t.textContent || '').trim())
      .filter(Boolean);
    return {
      jobId: jobId,
      title: nameEl ? (nameEl.getAttribute('title') || nameEl.textContent || '').trim() : '',
      company: compName ? compName.textContent.replace(/\s+/g, ' ').trim() : '',
      salary: salEl ? salEl.textContent.trim() : '',
      city: areaEl ? areaEl.textContent.replace(/\s+/g, ' ').trim().split(/\s+/)[0] : '',
      tags: tags,
      url: compEl ? compEl.getAttribute('href') : ''
    };
  });
})()
"""

# 在搜索结果中定位某岗位（按标题+公司匹配），返回其序号（0-based）
JS_FIND = r"""
((title, company) => {
  const items = Array.from(document.querySelectorAll('.joblist-item'));
  for (let i = 0; i < items.length; i++) {
    const t = (items[i].querySelector('.jname') || {}).textContent || '';
    const c = ((items[i].querySelector('a.comp .cname') || {}).textContent || '').replace(/\s+/g, ' ').trim();
    if (t.indexOf(title) >= 0 && (company === '' || c.indexOf(company) >= 0)) return i;
  }
  return -1;
})
"""

# 在搜索结果中按唯一 jobId 定位岗位，返回其序号（0-based）
JS_FIND_BY_ID = r"""
((jobId) => {
  const items = Array.from(document.querySelectorAll('.joblist-item'));
  for (let i = 0; i < items.length; i++) {
    const jobEl = items[i].querySelector('.joblist-item-job');
    let id = '';
    if (jobEl) {
      try { id = (JSON.parse(jobEl.getAttribute('sensorsdata') || '{}').jobId) || ''; } catch (e) {}
    }
    if (id === jobId) return i;
  }
  return -1;
})
"""

# 检测投递后的结果弹窗状态
JS_APPLY_RESULT = r"""
(() => {
  const ws = Array.from(document.querySelectorAll('.el-dialog__wrapper'))
    .filter(w => getComputedStyle(w).display !== 'none');
  const body = document.body.innerText.replace(/\s+/g, ' ');
  if (ws.length) {
    const t = ws.map(w => w.innerText.replace(/\s+/g, ' ')).join(' || ');
    if (/投递成功/.test(t)) return { state: 'applied', text: t.slice(0, 50) };
    if (/请选择需要投递的简历/.test(t)) return { state: 'need_resume', text: t.slice(0, 50) };
    if (/选择城市/.test(t)) return { state: 'city_dialog', text: t.slice(0, 60) };
  }
  if (/(已申请|申请成功)/.test(body)) return { state: 'already', text: '' };
  return { state: 'unknown', text: body.slice(0, 60) };
})()
"""

# 在"选择城市"对话框里勾选第一个城市并点确认（默认简历已设好时）
JS_CITY_CONFIRM = r"""
(() => {
  const dlg = document.querySelector('.jbs_resume_cascader_d') || document.querySelector('.el-dialog__wrapper');
  if (!dlg) return false;
  const opt = dlg.querySelector('.el-checkbox,.el-radio,[class*=city-item],[class*=option]');
  if (opt) opt.click();
  const btn = Array.from(dlg.querySelectorAll('button')).find(b => /确定|确认|提交/.test(b.textContent || ''));
  if (btn) { btn.click(); return true; }
  return false;
})()
"""

JS_CLOSE_DIALOG = r"""
(() => {
  const ws = Array.from(document.querySelectorAll('.el-dialog__wrapper'))
    .filter(w => getComputedStyle(w).display !== 'none');
  for (const w of ws) {
    const b = w.querySelector('.el-dialog__headerbtn');
    if (b) b.click();
  }
  return ws.length;
})()
"""


class Job51Adapter(BaseAdapter):
    key = "51job"
    name = "前程无忧"

    # ---------- 采集 ----------
    def collect(self, cfg: dict, browser: Browser) -> int:
        conn = get_conn()
        pcfg = cfg.get("platforms", {}).get("51job", {})
        cities = pcfg.get("cities", []) or []
        kws = pcfg.get("keywords", []) or []
        max_pages = min(int(pcfg.get("max_pages", 3) or 3), 10)
        new_count = 0
        tid = None
        try:
            for city in cities:
                code = CITY_CODES.get(city, "020000")
                for kw in kws:
                    for page in range(1, max_pages + 1):
                        yield_point()
                        url = SEARCH_URL.format(city=code, kw=quote(kw), page=page)
                        if tid is None:
                            tid = browser.new_tab(url)
                            if not tid:
                                break
                        else:
                            browser.navigate(tid, url)
                        time.sleep(4)
                        browser.wait_for_load(tid, 8)
                        browser.scroll(tid, y=1500)
                        time.sleep(1)
                        browser.scroll(tid, y=3000)
                        time.sleep(1)
                        if self._needs_login(browser, tid):
                            print("[51job] 登录态失效，请重新登录后重试。")
                            return new_count
                        raw = browser.evaluate(tid, JS_LIST)
                        jobs = self._parse_json(raw) or []
                        if not jobs:
                            break
                        for j in jobs:
                            jurl = j.get("url", "")
                            if not jurl:
                                continue
                            jid = self._job_id(j.get("jobId", ""), j.get("title", ""), j.get("company", ""))
                            if job_exists(conn, jid):
                                # 回填来源搜索页（旧库无 search_url 时），便于后续投递定位
                                conn.execute(
                                    "UPDATE jobs SET search_url=? WHERE id=? AND (search_url IS NULL OR search_url='')",
                                    (url, jid),
                                )
                                conn.commit()
                                continue
                            rec = {
                                "id": jid,
                                "platform": self.key,
                                "title": j.get("title", ""),
                                "company": j.get("company", ""),
                                "salary": j.get("salary", ""),
                                "city": j.get("city", ""),
                                "experience": "",
                                "jd": " ".join(j.get("tags", [])),
                                "hr_name": "",
                                "url": jurl,
                                "search_url": url,
                            }
                            insert_job(conn, rec)
                            new_count += 1
        finally:
            if tid:
                browser.close_tab(tid)
            conn.close()
        return new_count

    # ---------- 投递 ----------
    def apply(self, cfg: dict, browser: Browser, job: dict) -> str:
        # 搜索结果页投递（登录态有效）；用 岗位ID/标题 在搜索结果中定位
        surl = job.get("search_url") or ""
        if not surl:
            return "unsupported"
        q = parse_qs(urlparse(surl).query)
        city = q.get("jobArea", ["020000"])[0]
        orig_kw = q.get("keyword", [""])[0]
        orig_page = int(q.get("page", ["1"])[0] or 1)
        raw_id = job["id"][6:] if job["id"].startswith("51job-") else job["id"]
        title = job.get("title", "")
        company = job.get("company", "")

        # 定位策略：岗位标题窄关键词(1..2页) 优先 -> 原关键词(1..2页) 兜底
        title_kw = title.strip().split("（")[0].split("(")[0][:10].strip()
        strategies = []
        if title_kw:
            strategies.append((title_kw, [1, 2]))
        if orig_kw and orig_kw != title_kw:
            strategies.append((orig_kw, sorted({orig_page, 1, 2})))

        tid = None
        try:
            for kw, pages in strategies:
                if not kw:
                    continue
                base = SEARCH_URL.format(city=city, kw=quote(kw), page=1)
                if tid is None:
                    tid = browser.new_tab(base)
                    if not tid:
                        return "failed"
                else:
                    browser.navigate(tid, base)
                time.sleep(3)
                browser.wait_for_load(tid, 8)
                if self._needs_login(browser, tid):
                    return "unsupported"  # 登录态失效
                browser.scroll(tid, 1500); time.sleep(0.8)
                browser.scroll(tid, 3000); time.sleep(0.8)
                idx = None
                for page in pages:
                    if page != 1:
                        browser.navigate(tid, SEARCH_URL.format(city=city, kw=quote(kw), page=page))
                        time.sleep(2.5)
                        browser.wait_for_load(tid, 8)
                        browser.scroll(tid, 1500); time.sleep(0.8)
                        browser.scroll(tid, 3000); time.sleep(0.8)
                    i = self._find_by_id(browser, tid, raw_id)
                    if i is None and title:
                        r = browser.evaluate(tid, JS_FIND + f"({json.dumps(title)}, {json.dumps(company)})")
                        if isinstance(r, int) and r >= 0:
                            i = r
                    if i is not None:
                        idx = i
                        break
                if idx is None:
                    continue
                browser.evaluate(
                    tid,
                    f"document.querySelectorAll('.joblist-item')[{idx}].scrollIntoView({{block:'center'}})",
                )
                time.sleep(1)
                if not browser.click(tid, f".joblist-item:nth-of-type({idx + 1}) .btn.apply"):
                    continue
                time.sleep(3)
                return self._handle_apply_modal(browser, tid)
            return "unsupported"
        finally:
            if tid:
                browser.close_tab(tid)

    # ---------- 内部工具 ----------
    @staticmethod
    def _job_id(job_id: str, title: str, company: str) -> str:
        if job_id:
            return f"51job-{job_id}"
        base = (title + "|" + company).encode("utf-8")
        return "51job-" + hashlib.md5(base).hexdigest()[:16]

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
        return "login" in url or "passport" in url

    @staticmethod
    def _find_by_id(browser: Browser, tid: str, job_id: str) -> int | None:
        raw = browser.evaluate(
            tid,
            JS_FIND_BY_ID + f"({json.dumps(job_id)})",
        )
        if isinstance(raw, int) and raw >= 0:
            return raw
        return None

    @staticmethod
    def _handle_apply_modal(browser: Browser, tid: str) -> str:
        res = browser.evaluate(tid, JS_APPLY_RESULT)
        state = (res or {}).get("state", "unknown")
        if state == "applied":
            browser.evaluate(tid, JS_CLOSE_DIALOG)
            return "applied"
        if state == "already":
            return "already"
        if state == "need_resume":
            browser.evaluate(tid, JS_CLOSE_DIALOG)
            return "need_resume"
        if state == "city_dialog":
            # 默认简历+城市已设好时，尝试勾选并确认
            browser.evaluate(tid, JS_CITY_CONFIRM)
            time.sleep(3)
            res2 = browser.evaluate(tid, JS_APPLY_RESULT)
            if (res2 or {}).get("state") == "applied":
                browser.evaluate(tid, JS_CLOSE_DIALOG)
                return "applied"
            browser.evaluate(tid, JS_CLOSE_DIALOG)
            return "need_setup"
        # unknown：未识别到结果，保守标记以便重试（重试时按钮多为"已申请"→already）
        browser.evaluate(tid, JS_CLOSE_DIALOG)
        return "unsupported"
