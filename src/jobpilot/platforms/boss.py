"""BOSS直聘平台驱动。

采集路径已于 2026-10-10 在真实登录环境核验（见 docs/05-平台适配手册.md 第 5 节）：
- 搜索页 /web/geek/jobs，列表选择器全部命中
- 列表页薪资被剥离（数字不可读），**薪资改从详情页 `.info-primary .salary` 取**
- 详情页标签已改版，经验/学历改从列表取

投递（打招呼）已于 2026-10-10 实机核验（点击后平台会先发默认招呼语，我们在聊天面板里补发个性化招呼语）；
聊天监测 2026-10-16 真机只读核验了聊天列表（只读列表层，绝不点进会话），capabilities = collect + apply + monitor。
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..browser import humanize
from ..models import (
    ApplyResult,
    CollectResult,
    Job,
    LoginRequired,
    MonitorResult,
    PageReadError,
    PlatformRisk,
    Status,
    normalize,
)
from ..prefilter import prefilter
from ..safety.attention import release_page, tag_page
from ..safety.policy import SEND_MESSAGE, Action, Denied, is_graceful_stop
from ..safety.risk import classify_risk

# 城市表在 boss_cities.py 里（373 个，来自 BOSS 公开接口，用 tools/refresh_boss_cities.py 刷新）。
# 以前只内置 20 个，别的城市会被静默跳过 —— 那是"跑了但什么都没采到"的典型。
from .base import DriverContext, SlotSpec
from .boss_cities import CITY_CODES, HOT_CITIES  # noqa: F401  （HOT_CITIES 给界面做快捷选择）
from .record_monitor import write_observation

SEARCH_URL = "https://www.zhipin.com/web/geek/jobs?query={kw}&city={city}"
JOB_URL = "https://www.zhipin.com/job_detail/{job_id}.html"
# 列表页拟人悬停的目标：和 JS_LIST 用同一个卡片选择器
CARD_SELECTOR = ".job-card-wrap"

# 聊天列表页：监测**只读这一页**，绝不点进任何一个会话（点进去会把未读标成已读）
CHAT_URL = "https://www.zhipin.com/web/geek/chat"

# 城市表见 boss_cities.py（373 个，来源与刷新方式写在那文件头）

JS_LIST = r"""
(() => {
  const out = [];
  document.querySelectorAll('.job-card-wrap').forEach((wrap) => {
    const box = wrap.querySelector('.job-card-box') || wrap;
    const nameEl = box.querySelector('.job-name');
    const href = nameEl ? nameEl.getAttribute('href') : '';
    if (!nameEl || !href) return;
    const tags = box.querySelectorAll('.tag-list li');
    const companyEl = box.querySelector('.boss-name') || box.querySelector('.company-name');
    const locEl = box.querySelector('.company-location');
    out.push({
      title: nameEl.textContent.trim(),
      company: companyEl ? companyEl.textContent.trim() : '',
      location: locEl ? locEl.textContent.trim() : '',
      experience: tags[0] ? tags[0].textContent.trim() : '',
      education: tags[1] ? tags[1].textContent.trim() : '',
      url: href
    });
  });
  return JSON.stringify(out);
})()
"""

JS_DETAIL = r"""
(() => {
  const pick = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : ''; };
  const title = pick('.info-primary .name h1') || (document.title.split('_')[0] || '').replace(/^「|」$/g, '').trim();
  const company = (document.title.match(/_(.+?)招聘/) || [])[1] || '';
  const hrRaw = pick('.boss-info-attr .name') || pick('.job-boss-info .name');
  return JSON.stringify({
    title: title,
    salary: pick('.info-primary .salary') || pick('.salary'),
    jd: pick('.job-sec-text'),
    company: company,
    hr_name: hrRaw.replace(/\s*(刚刚活跃|今日活跃|本周活跃|在线|随时到岗)$/, '').trim(),
    chat_button: Boolean(document.querySelector('.btn-startchat, .op-btn-chat, [ka*="chat"]'))
  });
})()
"""


JS_CHAT_LIST = r"""
(() => {
  const out = [];
  document.querySelectorAll('li[role=listitem]').forEach((li) => {
    const pick = (sel) => { const n = li.querySelector(sel); return n ? (n.textContent || '').trim() : ''; };
    const box = [...li.querySelectorAll('.name-box span')].map((s) => (s.textContent || '').trim());
    const unread = li.querySelector('.unread-count') || li.querySelector('.badge-count');
    out.push({
      time: pick('.time'),
      name: box[0] || '',
      company: box[1] || '',
      hr_title: box[2] || '',
      last: pick('.last-msg-text'),
      status: pick('.message-status'),
      unread: unread ? (unread.textContent || '').trim() : ''
    });
  });
  return JSON.stringify(out);
})()
"""

# ---- 聊天列表 -> 进展状态（2026-10-11 实机核验，40 条真实会话） ----
# 列表里的状态徽标只出现在**我自己发的**最后一条消息上：
#   [已读] = 对方看过了但没回；[送达] = 对方还没看。
# 没有徽标 = 最后一条不是我发的（HR 回话了，或者是系统提示 / 我发的附件）。
CHAT_ROW = "li[role=listitem]"
# 系统提示：机器发的，不是人在说话 —— 这类行不算进展
CHAT_SYSTEM_HINTS = (
    "您的附件简历", "附件简历", "已发送给",
    "对方已同意", "您正在与", "简历已发送", "已投递",
)
# 我自己发出去的附件：列表里只显示文件名、而且没有徽标，别误当成 HR 回话
CHAT_FILE_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".ppt", ".pptx", ".zip", ".rar", ".png", ".jpg", ".jpeg",
)
# 关键词 -> 进展状态，按顺序匹配（先具体后笼统）
CHAT_STATE_RULES = (
    ("offer", ("offer", "录用", "入职通知")),
    ("interview", ("面试", "面谈", "笔试", "到面", "约个时间")),
    ("rejected", ("不合适", "不匹配", "不完全一致", "已招满", "岗位关闭", "祝您")),
)
# 状态只能往前走，不能往回退：HR 回过话之后我又发了消息，徽标会变回 [已读]，
# 那不是「退回已查看」。
STATE_RANK = {
    "viewed": 1, "replied": 2, "rejected": 3,
    "interview": 4, "interviewed": 5, "offer": 6,
}


def classify_chat_row(row: dict) -> tuple[str, str] | None:
    """一条会话行 -> （进展状态, 展示文本）；None = 这一行不产生进展。

    判断依据全部来自 2026-10-11 的实机样本（40 条真实会话）：
    - 带 [已读] 徽标 -> 最后一条是我发的、对方看过 -> viewed
    - 带 [送达] 徽标 -> 最后一条是我发的、对方还没看 -> 没有新进展，跳过
    - 不带徽标     -> 最后一条是对方的；去掉系统提示和我的附件之后，
                      再按关键词判 rejected / interview / offer，剩下的算 replied
    """
    text = str(row.get("last") or "").strip()
    if not text:
        return None
    status = str(row.get("status") or "").strip()
    if status:
        return ("viewed", text) if "已读" in status else None
    if any(hint in text for hint in CHAT_SYSTEM_HINTS):
        return None
    if text.lower().endswith(CHAT_FILE_EXTENSIONS):
        return None
    low = text.lower()
    for state, words in CHAT_STATE_RULES:
        if any(word in low for word in words):
            return state, text
    return "replied", text


# 监测时拿来对会话的岗位状态：投出去了的（含「早就聊过」和已经记过回复 / 面试的）
MONITOR_JOB_STATUSES = (
    Status.APPLIED.value, Status.ALREADY.value, Status.REPLIED.value, Status.INTERVIEW.value,
)


def boss_jobs_by_company(repo) -> dict[str, list[str]]:
    """归一化公司名 -> BOSS 上已打过招呼的岗位 id（只看 BOSS、只看投出去了的）。"""
    out: dict[str, list[str]] = {}
    for status in MONITOR_JOB_STATUSES:
        for job in repo.list_jobs(platform="boss", status=status, limit=5000):
            out.setdefault(normalize(job.company), []).append(job.id)
    return out


def unread_of(row: dict) -> bool:
    """列表上的未读角标：有数字（或任何非 0 文字）= 有没读的消息。"""
    badge = str(row.get("unread") or "").strip()
    return bool(badge) and badge not in ("0",)


def city_code(city: str) -> str | None:
    """城市名 -> 9 位城市码；直接给 9 位码也认（方便从浏览器地址栏复制）。"""
    text = (city or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 6:
        return text
    return CITY_CODES.get(text)


def search_url(keyword: str, city: str, page: int = 1) -> str:
    url = SEARCH_URL.format(kw=quote(keyword), city=city)
    return url if page <= 1 else f"{url}&page={page}"


def job_id_from_url(url: str) -> str:
    """从 /job_detail/<id>.html 提取岗位 id。"""
    match = re.search(r"/job_detail/([^.?#/]+)", str(url or ""))
    return match.group(1) if match else ""


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


def parse_detail(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _looks_logged_out(text: str) -> bool:
    """BOSS 掉登录时不会去登录页：搜索页直接跳回首页，右上角显示「登录/注册」。

    网址里没有 /login 之类的特征，通用的 classify_risk 认不出来，以前就落成了「页面异常」，
    连续 3 次还被当成风控锁了一小时（2026-10-12 实测：调试 Chrome 里的登录过期了）。
    """
    # 只认「登录/注册」入口：光是跳回首页不够（已登录时岗位下线也可能跳首页，那不是掉登录）。
    # 只看页头一段，不拿 JD 正文去匹配。
    return "登录/注册" in (text or "")[:3000]


def _guard(ctx: DriverContext, page, expected_selector: str, label: str) -> None:
    """等待预期内容出现；超时后再判定是风控、掉登录还是改版，fail-closed。"""
    if page.wait_for(expected_selector, timeout_ms=30000):
        return
    text, url = page.text(), page.url
    kind = classify_risk(text, url)
    if kind == "login_required" or (kind is None and _looks_logged_out(text)):
        raise tag_page(LoginRequired(f"{label} 需要登录：BOSS 的登录已失效，请在调试 Chrome 里重新登录后再试"), page)
    if kind:
        raise tag_page(PlatformRisk(kind, label), page)
    raise PageReadError(f"{label} 未找到预期内容（可能改版或被拦截）")


class BossDriver:
    key = "boss"
    name = "BOSS直聘"
# collect / apply 已实机核验；monitor 只读聊天列表（2026-10-16 核验）—— 打开会话会把消息标已读，所以从不点进去
    capabilities = frozenset({"collect", "apply", "monitor"})   # monitor：2026-10-16 真机只读核验聊天列表

    # 界面用这两个属性判断「城市是否受限」：受限的平台填了表外的城市会被跳过
    supported_cities = CITY_CODES
    hot_cities = HOT_CITIES

    # ---------------- 采集 ----------------
    def collect(self, ctx: DriverContext) -> CollectResult:
        cfg = ctx.config.platforms.get(self.key)
        result = CollectResult(platform=self.key)
        if cfg is None or not cfg.enabled:
            result.stopped = "未启用"
            return result

        pages: dict[str, object] = {"search": None, "detail": None}
        skipped_cities: list[str] = []
        specs: list[SlotSpec] = []
        for city in cfg.cities:
            code = city_code(city)
            if not code:
                skipped_cities.append(city)
                ctx.log(f"[boss] 城市不在内置表里，跳过：{city}")
                continue
            specs += [SlotSpec(f"{city}|{keyword}", keyword, city, code) for keyword in cfg.keywords]

        def fetch(spec: SlotSpec, run, page_no: int) -> tuple[int, int] | None:
            city, keyword = spec.city, spec.keyword
            ctx.reserve("collect_search")
            url = search_url(keyword, spec.data, page_no)
            if pages["search"] is None:
                pages["search"] = ctx.browser.open(url)
            else:
                pages["search"].goto(url)
            search_page = pages["search"]
            _guard(ctx, search_page, ".job-card-wrap", f"[boss] 搜索页 {city}/{keyword}")

            items = parse_list(search_page.eval(JS_LIST))
            if not items:
                return None                       # 这页确实没有结果，也算采过
            result.seen += len(items)
            new_before = result.new
            ctx.log(f"[boss] {city}/{keyword} 第 {page_no} 页 {len(items)} 条")
            # 先解析再扫一眼：滚动 / 悬停不会影响已经拿到的列表
            humanize.after_list(ctx, search_page, CARD_SELECTOR)

            for item in items:
                ctx.stop.check()
                ext_id = job_id_from_url(item.get("url", ""))
                if not ext_id:
                    continue
                job_id = f"boss-{ext_id}"
                if ctx.repo.get_job(job_id):
                    result.duplicate += 1
                    continue
                job_city = item.get("location", "").split("·")[0] if item.get("location") else ""

                # 只看列表就能判掉的（实习 / 排除词 / 岗位性质）不再开详情页：
                # 每个详情页都占预算、还要等一次 5~14 秒的节流，开了也是被预筛拒掉。
                listed = Job(
                    id=job_id, platform=self.key, external_id=ext_id,
                    title=item.get("title", ""), company=item.get("company", ""),
                    city=job_city, url=JOB_URL.format(job_id=ext_id),
                    status=Status.REJECTED.value,
                )
                rejected, reason = prefilter(listed, ctx.config)
                if rejected:
                    inserted, _ = ctx.repo.upsert_job(listed)
                    if inserted:
                        ctx.repo.add_event(job_id, "collected", f"{city}/{keyword}")
                        ctx.repo.add_event(job_id, "prefilter_rejected", f"{reason}（列表页预筛，未开详情页）")
                    result.filtered += 1
                    continue

                # 尊重"打开详情页抓 JD"这个开关（以前 BOSS 是硬开的，
                # 开关点了也不会变——和另外三个平台行为不一致）。
                # BOSS 的列表页没有薪资也没有 JD，所以它在默认配置里是 True。
                if cfg.fetch_detail:
                    detail = self._fetch_detail(ctx, ext_id, pages["detail"])
                    if pages["detail"] is None:
                        pages["detail"] = detail.pop("_page", None)
                    else:
                        detail.pop("_page", None)
                else:
                    detail = {}

                inserted, _ = ctx.repo.upsert_job(Job(
                    id=job_id,
                    platform=self.key,
                    external_id=ext_id,
                    title=detail.get("title") or item.get("title", ""),
                    company=detail.get("company") or item.get("company", ""),
                    salary=detail.get("salary", ""),
                    city=job_city,
                    url=JOB_URL.format(job_id=ext_id),
                    jd=detail.get("jd", ""),
                    status="new",
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
            # 预算耗尽 / 平台被锁：停止该平台，已入库的岗位保留
            if is_graceful_stop(exc):
                result.stopped = exc.reason
                ctx.log(f"[boss] {exc.reason}")
            else:
                raise
        finally:
            for page in pages.values():
                release_page(page)   # 验证 / 掉登录的那一页留给你处理
        if skipped_cities and len(skipped_cities) == len(cfg.cities):
            # 一个城市都没跑成：直接说清楚，别让任务显示"完成"却一条都没采到
            result.stopped = ("配置的城市都不在内置表里：" + "、".join(skipped_cities)
                              + "（设置 → 平台 → 城市，界面会当场标红）")
        return result

    def _fetch_detail(self, ctx: DriverContext, ext_id: str, page) -> dict:
        ctx.reserve("collect_detail")
        url = JOB_URL.format(job_id=ext_id)
        if page is None:
            page = ctx.browser.open(url)
        else:
            page.goto(url)
        _guard(ctx, page, ".job-sec-text", f"[boss] 详情页 {ext_id}")
        detail = parse_detail(page.eval(JS_DETAIL))
        if detail.get("jd"):
            # 读到 JD 后像人一样读一会儿再走：秒开秒关、从不滚动的详情页访问是很明显的机器特征
            humanize.after_detail(ctx, page, detail["jd"])
        return {**detail, "_page": page}

        # ---------------- 投递 / 监测（待实机核验） ----------------
    # 预演用的候选选择器：只"看"不"点"，所以可以先在真机上跑，把证据攒齐再决定要不要开放自动投递
    JS_APPLY_PROBE = r"""
(() => {
  const pick = (selectors) => {
    for (const sel of selectors) {
      const node = document.querySelector(sel);
      if (node) {
        const text = (node.innerText || node.textContent || '').trim().slice(0, 20);
        const rect = node.getBoundingClientRect();
        return { selector: sel, text, visible: rect.width > 0 && rect.height > 0 };
      }
    }
    return null;
  };
  return {
    url: location.href,
    title: document.title,
    chatButton: pick(['.btn-startchat', 'a.btn-startchat', '.op-btn-chat', '[class*="startchat"]', '[class*="btn-chat"]']),
    chatInput: pick(['.chat-input', '#chat-input', '.input-area textarea', 'textarea[class*="input"]']),
    // 只看"真的显示出来"的登录框：BOSS 每个页面都埋着隐藏的登录弹窗，
    // 直接 querySelector 会永远为真（第一次真机预演就踩了这个误报）
    needLogin: (() => {
      for (const sel of ['.sign-form', '.login-dialog', '[class*="login-dialog"]']) {
        const node = document.querySelector(sel);
        if (!node) continue;
        const rect = node.getBoundingClientRect();
        const style = getComputedStyle(node);
        if (rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden') {
          return true;
        }
      }
      return false;
    })(),
    bodyHead: (document.body ? (document.body.innerText || '') : '').slice(0, 120),
  };
})()
"""
    def probe_apply(self, ctx: DriverContext, job: Job) -> dict:
        """投递预演：只打开岗位页看"按钮在不在"，**不点、不发**。

        为什么值得单独做：自动投递迟迟没开放，是因为"点了会真的发消息、会把消息标已读"，
        得由用户决定。但"按钮选择器对不对"这件事不需要发消息就能验 —— 打开岗位详情页
        本来就是采集在做的事（同样的页面预算、同样的风控闸门）。
        """
        if job is None or not job.url:
            return {"platform": self.key, "job_id": job.id, "ok": False, "reason": "这个岗位没有原链接"}
        ctx.log(f"[boss] 投递预演（只看不发）：{job.title}")
        ctx.reserve("collect_detail")          # 和采集详情页走同一条额度
        page = ctx.browser.open(job.url)
        data = {}
        risk = None
        try:
            _guard(ctx, page, ".job-sec-text, .btn-startchat, body", f"[boss] 投递预演 {job.id}")
            data = page.eval(self.JS_APPLY_PROBE) or {}
            risk = classify_risk(page.text(), page.url)
        finally:
            release_page(page)   # 验证 / 掉登录的那一页留给你处理

        button = data.get("chatButton")
        if risk:
            verdict, ok = f"页面命中风控/登录信号：{risk}", False
        elif button and button.get("visible"):
            verdict, ok = f"找到沟通按钮（{button.get('text') or button.get('selector')}），结构支持自动投递", True
        else:
            verdict, ok = "没找到可见的沟通按钮：选择器需要更新", False
        return {
            "platform": self.key, "job_id": job.id, "job_url": job.url, "ok": ok,
            "verdict": verdict, "risk": risk, "button": button,
            "chat_input": data.get("chatInput"), "need_login": bool(data.get("needLogin")),
            "page_title": data.get("title", ""), "sent_anything": False,
        }

    # 投递（打招呼）—— 流程与选择器都是 2026-10-10 在真实岗位页核验的
    APPLY_CHAT_BUTTON = ".btn-startchat"
    APPLY_PANEL = ".dialog-wrap.startchat-dialog"
    APPLY_INPUT = "textarea.input-area"
    APPLY_SEND = "div.send-message"

    def apply(self, ctx: DriverContext, job: Job) -> ApplyResult:
        """BOSS 的"投递"就是打招呼。

        实机流程：
        1. 打开岗位详情页（占一次投递页额度）
        2. 点「立即沟通」→ **平台会立刻用你账号里的默认招呼语发出去**（这一步本身就是投递）
        3. 右下角出现聊天面板 `.dialog-wrap.startchat-dialog`
        4. 把 AI 生成的个性化招呼语填进 `textarea.input-area`，点 `div.send-message` 发出
        5. 面板里出现「已发送」即视为成功

        按钮已经是「继续沟通」= 早就聊过 → 直接返回 already，**绝不重复发**。
        """
        if job is None or not job.url:
            return ApplyResult(status="unsupported", detail="岗位没有原链接")

        ctx.reserve("apply")
        page = ctx.browser.open(job.url)
        try:
            _guard(ctx, page, self.APPLY_CHAT_BUTTON, f"[boss] 投递 {job.title}")
            label = (page.inner_text(self.APPLY_CHAT_BUTTON) or "").strip()
            if "继续沟通" in label:
                return ApplyResult(status=Status.ALREADY.value, detail="已经和这个岗位打过招呼")

            # 人会先看一眼岗位再点沟通：只停留 / 滚动 / 挪鼠标，不加任何点击
            humanize.before_apply(ctx, page)
            page.click(self.APPLY_CHAT_BUTTON)
            ctx.log("[boss] 已点「立即沟通」——平台先发出了账号里的默认招呼语")

            greeting = (job.greeting or "").strip()
            if not greeting:
                # 没有个性化招呼语时，点这一下就是完整投递
                return ApplyResult(status=Status.APPLIED.value, detail="已打招呼（该岗位没有生成招呼语）")

            # 发消息也要过闸门（时间窗 / 随机休息日 / 采集→投递冷却）。
            # 关键：走到这里，第一步的默认招呼语已经发出去了，所以闸门拒绝不能算失败，
            # 也不能让异常冒出去把整个任务判成 error —— 那会谎报「投递失败」，还会推高熔断。
            try:
                ctx.policy.authorize(Action(kind=SEND_MESSAGE, platform=self.key))
            except Denied as exc:
                ctx.log(f"[boss] 个性化招呼语被闸门拦下：{exc.reason}")
                return ApplyResult(
                    status=Status.APPLIED.value,
                    detail=f"已打招呼；个性化招呼语被闸门拦下（{exc.reason}）",
                )

            if not page.wait_for(self.APPLY_PANEL, timeout_ms=8000):
                return ApplyResult(status="unsupported",
                                   detail="点了沟通但没等到聊天面板（可能改版）")
            page.fill(self.APPLY_INPUT, greeting)
            if not page.wait_for(self.APPLY_SEND, timeout_ms=5000):
                return ApplyResult(status="unsupported",
                                   detail="填了招呼语但没有出现发送按钮（可能改版）")
            page.click(self.APPLY_SEND)
            ctx.stop.sleep(1.5)          # 等消息上屏；可被停止打断

            panel = page.inner_text(self.APPLY_PANEL) or ""
            if "已发送" in panel:
                return ApplyResult(status=Status.APPLIED.value, detail="已打招呼 + 已发出个性化招呼语")
            return ApplyResult(status=Status.FAILED.value, detail="点了发送，但面板里没看到「已发送」")
        finally:
            release_page(page)   # 验证 / 掉登录的那一页留给你处理

    # ---------------- 监测（只读列表，绝不进会话） ----------------
    def monitor(self, ctx: DriverContext) -> MonitorResult:
        """读聊天列表，不进任何一个会话。

        为什么不进会话：点进去会把未读消息标成已读，而且**撤不回来** —— HR 那边会看到
        「已读不回」。所以这里只读列表层，拿得到「谁回了我 / 最后一句说了什么 /
        HR 读没读我的消息」，拿不到完整对话记录。这个取舍是故意的。
        """
        result = MonitorResult()
        ctx.reserve("monitor")
        page = ctx.browser.open(CHAT_URL)
        try:
            _guard(ctx, page, CHAT_ROW, "[boss] HR 监测")
            rows = json.loads(page.eval(JS_CHAT_LIST) or "[]")
            if not isinstance(rows, list):
                rows = []

            # 会话列表里没有岗位标题，只能靠公司名把会话对回岗位池。只比 BOSS 上真的打过招呼的岗位
            # （以前拿全部平台、全部状态的岗位比，同名公司一多就全是「对不上」）；同名公司还有
            # 多个岗位时**不猜** —— 宁可不匹配，也不要张冠李戴。
            by_company = boss_jobs_by_company(ctx.repo)
            known = ctx.repo.interactions_by_key(self.key)
            events: dict[str, int] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                parsed = classify_chat_row(row)
                if parsed is None:
                    continue
                state, text = parsed
                hr = str(row.get("name") or "").strip()
                company = str(row.get("company") or "").strip()
                hits = by_company.get(normalize(company), [])
                job_id = hits[0] if len(hits) == 1 else None
                key = f"auto:{job_id}" if job_id else f"auto:{self.key}:{hr}|{company}"
                outcome = write_observation(
                    ctx, key=key, platform=self.key, job_id=job_id, hr_name=hr, state=state,
                    text=text, deeplink=CHAT_URL, company=company,
                    unread=unread_of(row), msg_time=str(row.get("time") or "").strip(), known=known,
                )
                if outcome == "advanced":
                    events[state] = events.get(state, 0) + 1
            result.checked = len(rows)
            result.events = events
        finally:
            release_page(page)   # 验证 / 掉登录的那一页留给你处理
        ctx.log(f"[boss] 监测 {result.checked} 条会话（只读列表，没打开任何会话）")
        return result


DRIVER = BossDriver()
