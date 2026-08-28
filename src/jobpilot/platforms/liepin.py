from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import quote

from jobpilot.browser import Browser
from jobpilot.control import yield_point
from jobpilot.db import get_conn, insert_job, job_exists
from jobpilot.platforms.base import BaseAdapter

# 猎聘搜索页（无需登录即可访问）。city 用区号式（实测 020→广州）；
# 若过滤不准，请在 config.yaml 的 platforms.liepin.cities 直接用正确码。
SEARCH_URL = "https://www.liepin.com/zhaopin/?key={kw}{city}&curPage={page}"

CITY_CODES = {
    "北京": "010", "上海": "021", "广州": "020", "深圳": "0755",
    "杭州": "0571", "成都": "028", "武汉": "027", "南京": "025",
    "西安": "029", "苏州": "0512", "重庆": "023", "天津": "022",
    "长沙": "0731", "郑州": "0371", "青岛": "0532", "厦门": "0592",
}

# 从搜索结果卡片提取岗位（猎聘卡片用 CSS-module 哈希类，故按稳定属性定位）
JS_LIST = r"""
(() => {
  const cards = Array.from(document.querySelectorAll('.job-card-pc-container'));
  const out = [];
  for (const card of cards) {
    const a = card.querySelector('a[data-nick="job-detail-job-info"]');
    if (!a) continue;
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/job\/(\d+)\.shtml/);
    const jobId = m ? m[1] : '';
    if (!jobId) continue;
    const txt = (a.textContent || '').replace(/\s+/g, ' ').trim();
    const title = (txt.split('【')[0] || '').trim();
    const cm = txt.match(/【(.+?)】/);
    const city = cm ? cm[1].trim() : '';
    const sm = txt.match(/(\d+(?:\.\d+)?-\d+(?:\.\d+)?k|面议|薪资面议)/i);
    const salary = sm ? sm[1] : '';
    const compEl = card.querySelector('div[data-nick^="job-detail-comp"]');
    const company = compEl ? compEl.textContent.replace(/\s+/g, ' ').trim() : '';
    out.push({ jobId, title, company, salary, city, url: href });
  }
  return out;
})()
"""

# 在详情页 .job-apply-operate 内找"投递"类按钮并点击，返回按钮文案（找不到返回 null）
JS_CLICK_APPLY = r"""
(() => {
  const box = document.querySelector('.job-apply-operate');
  if (!box) return null;
  const el = Array.from(box.querySelectorAll('button, a, div, span')).find(e =>
    /投递|申请简历|立即投递|投个简历|简历投递/.test(e.textContent || ''));
  if (!el) return null;
  const label = (el.textContent || '').trim();
  el.click();
  return label;
})()
"""

# 点击后检测投递结果（文本匹配，兼容弹窗/成功/已投/需登录）
JS_APPLY_RESULT = r"""
(() => {
  const body = document.body.innerText.replace(/\s+/g, ' ');
  if (/投递成功|投递完成|已成功投递|简历已投递/.test(body)) return {state:'applied'};
  if (/您已投递|已经投递|已投递过|已经申请/.test(body)) return {state:'already'};
  if (/请登录|请先登录|登录后投递|登录后即可|扫码登录/.test(body)) return {state:'need_login'};
  const dlg = Array.from(document.querySelectorAll('[class*=dialog],[class*=modal],[class*=layer],[class*=pop]'))
    .find(d => d.offsetParent !== null);
  if (dlg) {
    const t = dlg.innerText.replace(/\s+/g, ' ');
    if (/选择简历|投递到以下|请选择/.test(t)) {
      const ok = Array.from(dlg.querySelectorAll('button')).find(b => /确定|确认|投递|提交/.test(b.textContent||''));
      if (ok) { ok.click(); return {state:'need_resume', clicked:true}; }
      return {state:'need_resume'};
    }
    if (/登录/.test(t)) return {state:'need_login'};
    return {state:'dialog', text: t.slice(0,40)};
  }
  return {state:'unknown', text: body.slice(0,50)};
})()
"""


class LiepinAdapter(BaseAdapter):
    key = "liepin"
    name = "猎聘"

    # ---------- 采集 ----------
    def collect(self, cfg: dict, browser: Browser) -> int:
        conn = get_conn()
        pcfg = cfg.get("platforms", {}).get("liepin", {})
        cities = pcfg.get("cities", []) or []
        kws = pcfg.get("keywords", []) or []
        max_pages = min(int(pcfg.get("max_pages", 3) or 3), 10)
        new_count = 0
        tid = None
        try:
            for city in cities:
                code = CITY_CODES.get(city, city) if city else ""
                city_q = f"&city={quote(code)}" if code else ""
                for kw in kws:
                    for page in range(1, max_pages + 1):
                        yield_point()
                        url = SEARCH_URL.format(kw=quote(kw), city=city_q, page=page - 1)
                        if tid is None:
                            tid = browser.new_tab(url)
                            if not tid:
                                break
                        else:
                            browser.navigate(tid, url)
                        time.sleep(4)
                        browser.wait_for_load(tid, 8)
                        browser.scroll(tid, y=1200)
                        time.sleep(1)
                        if self._needs_login(browser, tid):
                            print("[liepin] 搜索页跳登录，请确认浏览器登录态。")
                            return new_count
                        raw = browser.evaluate(tid, JS_LIST)
                        jobs = self._parse_json(raw) or []
                        if not jobs:
                            break  # 本关键词已无更多结果
                        for j in jobs:
                            jid = "liepin-" + j.get("jobId", "")
                            if not jid or jid == "liepin-":
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
        url = job.get("url") or ""
        if "/job/" not in url:
            return "unsupported"
        tid = None
        try:
            tid = browser.new_tab(url)
            if not tid:
                return "failed"
            time.sleep(4)
            browser.wait_for_load(tid, 8)
            if self._needs_login(browser, tid):
                return "need_login"  # 需在驱动浏览器内登录猎聘
            browser.scroll(tid, y=600)
            time.sleep(1)
            clicked = browser.evaluate(tid, JS_CLICK_APPLY)
            if not clicked:
                return "unsupported"  # 未找到投递按钮（可能需登录/反爬拦截）
            time.sleep(3)
            res = browser.evaluate(tid, JS_APPLY_RESULT) or {}
            state = res.get("state", "unknown")
            if state == "applied":
                return "applied"
            if state == "already":
                return "already"
            if state == "need_resume":
                # 已尝试点确认；再等一会确认结果
                time.sleep(3)
                res2 = browser.evaluate(tid, JS_APPLY_RESULT) or {}
                if res2.get("state") == "applied":
                    return "applied"
                return "need_resume"
            if state == "need_login":
                return "need_login"
            # unknown / dialog：保守标记，便于后续重试
            return "unsupported"
        finally:
            if tid:
                browser.close_tab(tid)

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
