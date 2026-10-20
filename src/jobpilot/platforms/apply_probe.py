"""投递预演（只看不点）：给还没开放自动投递的平台攒证据。

BOSS 的自动投递就是这么开起来的：先在真实岗位页上「看」按钮在不在、文字是什么、
有没有登录墙 / 风控信号，证据够了再写真正的点击。这一步**不点任何东西、不发任何东西**，
走的是和采集详情页同一条页面额度与风控闸门（开一次页面就记一次）。

不同平台的按钮写法不一样，而且会随改版变，所以这里不写死某个 CSS 类名：
在页面上找「看得见、可点击、文字像投递按钮」的元素，把找到的东西原样报告回来。
"""

from __future__ import annotations

from ..browser import humanize
from ..models import Job
from ..safety.attention import release_page, tag_page
from ..safety.risk import classify_risk
from .base import DriverContext

# 页面里所有「看得见、像按钮」的元素，按文字分三类：投递按钮 / 已投递标记 / 沟通按钮。
# 只读：不触发事件、不改 DOM。
JS_PROBE = r"""
((labels) => {
  const visible = (node) => {
    const rect = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
  };
  const textOf = (node) => (node.innerText || node.textContent || '').replace(/\s+/g, '').trim();
  const SEL = 'button, a, [role="button"], div[class*="btn"], span[class*="btn"], div[class*="apply"], a[class*="apply"]';
  const nodes = Array.from(document.querySelectorAll(SEL));
  const found = { apply: [], already: [], chat: [] };
  for (const node of nodes) {
    const text = textOf(node);
    if (!text || text.length > 12 || !visible(node)) continue;
    // 里面还套着按钮的是容器（猎聘 div.apply-box = 「投简历」+「聊一聊」，文字拼成「投简历聊一聊」），
    // 不是按钮：点它的空白处什么都不会发生（2026-10-14 真机踩坑）
    if (node.querySelector(SEL)) continue;
    const info = {
      text,
      tag: node.tagName.toLowerCase(),
      cls: (node.getAttribute('class') || '').slice(0, 80),
      disabled: !!(node.disabled || node.getAttribute('aria-disabled') === 'true' || /disable/i.test(node.getAttribute('class') || '')),
    };
    if (labels.already.some((w) => text.includes(w))) found.already.push(info);
    else if (labels.apply.some((w) => text === w || text.startsWith(w))) found.apply.push(info);
    else if (labels.chat.some((w) => text.includes(w))) found.chat.push(info);
  }
  // 登录信号分两种，不能混为一谈：
  //  - 弹出来的登录框（浮层、尺寸大）：挡住页面，确实是没登录
  //  - 页头里的「登录/注册」小链接：很多站先渲染成未登录、几百毫秒后才换成用户名
  //    （前程无忧详情页实测：0 秒有、3 秒后就没了）。只把它当「可能没登录」，由 Python 端过几秒再看一次
  const loginNodes = Array.from(document.querySelectorAll('[class*="login"], [class*="Login"], [class*="sign-in"], [id*="login"]'))
    .filter((node) => visible(node) && /登录|扫码|手机号|验证码登录/.test((node.innerText || '').slice(0, 200)))
    .slice(0, 4).map((node) => {
      const rect = node.getBoundingClientRect();
      const pos = getComputedStyle(node).position;
      return {
        cls: (node.getAttribute('class') || '').slice(0, 80), id: node.id || '', pos,
        w: Math.round(rect.width), h: Math.round(rect.height),
        text: (node.innerText || '').replace(/\s+/g, ' ').slice(0, 40),
        modal: (pos === 'fixed' || pos === 'absolute') && rect.width >= 240 && rect.height >= 160,
      };
    });
  const loginModal = loginNodes.some((n) => n.modal);
  const loginLink = loginNodes.some((n) => !n.modal);
  return {
    url: location.href,
    title: document.title,
    apply: found.apply.slice(0, 5),
    already: found.already.slice(0, 3),
    chat: found.chat.slice(0, 3),
    loginModal,
    loginLink,
    loginNodes,
    bodyHead: (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ').slice(0, 160),
  };
})
"""

LABELS = {
    # 猎聘（2026-10-14 真机更正）：「投简历聊一聊」其实是容器 div.apply-box 的拼接文字，
    # 里面是两个独立链接「投简历」(a.btn-minor) 和「聊一聊」(a.btn-main)；投递只点「投简历」
    "apply": ["申请职位", "立即申请", "申请", "投递简历", "立即投递", "投个简历", "投简历", "投递", "申请简历"],
    "already": ["已申请", "已投递", "申请成功", "投递成功", "您已投递"],
    "chat": ["立即沟通", "聊一聊", "继续沟通", "和TA聊聊", "在线沟通"],
}


# 点击之后看结果：同一套按文字识别（弹窗文字各站不同，也会改版）
JS_AFTER = r"""
(() => {
  const visible = (node) => { const r = node.getBoundingClientRect(); const s = getComputedStyle(node);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  // 只看「看得见的」弹窗 / 提示：很多站页面里藏着现成的「投递成功」模板
  const popups = Array.from(document.querySelectorAll(
    '[class*="dialog"], [class*="Dialog"], [class*="modal"], [class*="Modal"], [class*="toast"], [class*="Toast"], [class*="message"], [class*="tip"], [role="dialog"], [role="alert"]'))
    .filter(visible).map((n) => (n.innerText || '').replace(/\s+/g, ' ').trim()).filter(Boolean).slice(0, 6);
  const buttons = Array.from(document.querySelectorAll('button, a, div[class*="btn"], div[class*="apply"], span[class*="btn"]'))
    .filter(visible).map((n) => (n.innerText || '').replace(/\s+/g, '').trim()).filter((t) => t && t.length <= 12).slice(0, 60);
  return { popups, buttons, url: location.href };
})()
"""

# 按文字精确找到「那一个」投递按钮并点一下（只点第一个，找不到就返回 false，不做任何猜测）
JS_CLICK = r"""
((want) => {
  const vis = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const SEL = 'button, a, [role="button"], div[class*="btn"], span[class*="btn"], div[class*="apply"], a[class*="apply"]';
  // 和预演同一条规则：跳过里面还套着按钮的容器，只点真正的按钮
  const node = Array.from(document.querySelectorAll(SEL))
    .find((n) => vis(n) && !n.querySelector(SEL) && (n.innerText || '').replace(/\s+/g, '') === want);
  if (!node) return false;
  node.click();
  return true;
})
"""

# 「选择附件简历」弹窗（猎聘 2026-10-14 真机截图：列出几份附件简历，默认选中最新那份，
# 只有一个「立即投递」）。用户选择：用平台默认选中的那份。只在下面条件全满足时点那一个按钮，
# 否则什么都不点、交还给用户：
#   - 弹窗标题像「选择简历」；恰好一份简历是选中状态；恰好一个看得见的投递按钮
JS_RESUME_CONFIRM = r"""
(() => {
  const vis = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const boxes = Array.from(document.querySelectorAll(
    '[class*="dialog"], [class*="Dialog"], [class*="modal"], [class*="Modal"], [role="dialog"]'))
    .filter(vis).filter((n) => /选择(附件)?简历/.test(n.innerText || '') && /(立即|确认|确定)?投递/.test(n.innerText || ''));
  if (!boxes.length) return { clicked: false, reason: '没找到选择简历的弹窗' };
  // 取「既有标题又有投递按钮」的最里层那个：外层遮罩带 modal 类名，标题栏（ant-c-modal-title）也带，都不是弹窗本体
  const box = boxes.find((b) => !boxes.some((o) => o !== b && b.contains(o))) || boxes[0];
  const radios = Array.from(box.querySelectorAll('input[type="radio"]'));
  let chosen = radios.filter((r) => r.checked);
  if (!radios.length) {
    chosen = Array.from(box.querySelectorAll('[class*="radio"], [class*="Radio"]'))
      .filter((n) => /(checked|selected|active)/i.test(n.getAttribute('class') || ''))
      .filter((n) => !Array.from(n.querySelectorAll('*')).some((c) => /(checked|selected|active)/i.test(c.getAttribute('class') || '')));
  }
  if (chosen.length !== 1) return { clicked: false, reason: `默认选中的简历有 ${chosen.length} 份，不确定该用哪份` };
  const item = chosen[0].closest('li, label, [class*="item"], [class*="Item"]') || chosen[0].parentElement;
  const resume = item ? (item.innerText || '').replace(/\s+/g, ' ').replace(/预览/g, '').trim().slice(0, 40) : '';
  const buttons = Array.from(box.querySelectorAll('button, a, [role="button"], [class*="btn"]'))
    .filter((n) => vis(n) && !n.querySelector('button, a, [role="button"], [class*="btn"]'))
    .filter((n) => /^(立即投递|确认投递|确定投递|投递)$/.test((n.innerText || '').replace(/\s+/g, '')));
  if (buttons.length !== 1) return { clicked: false, reason: `弹窗里的投递按钮有 ${buttons.length} 个，不确定该点哪个` };
  buttons[0].click();
  return { clicked: true, resume };
})()
"""

# 一键投递之后的结果文字（2026-10-13 起按真机结果补充）
# 「已向对方发送简历」：智联点「立即投递」后的提示（2026-10-16 实测）
AFTER_SUCCESS = ("投递成功", "申请成功", "已投递", "已申请", "简历已投递", "投递简历成功", "已向对方发送简历")
AFTER_NEED_RESUME = ("请选择", "选择简历", "完善简历", "上传简历", "创建简历", "附件简历", "选择城市", "求职意向")
AFTER_LIMIT = ("今日投递", "次数已达", "上限", "频繁", "稍后再试")


def click_apply(ctx: DriverContext, job: Job, *, platform: str, ready_selector: str, label: str,
                confirm_default_resume: bool = False) -> tuple[str, str]:
    """真正的一键投递：打开岗位页 → 先确认没投过 → **只点一次**投递按钮 → 按页面文字判断结果。

    返回 ``(status, detail)``。安全约束：
    - 只点「文字像投递按钮、看得见、不是灰的」那一个元素；找不到就不点（返回 unsupported）
    - 点完看不出结果也**不再点第二次**：返回 failed，交给熔断计数和人工核对，绝不重试
    - 登录墙 / 风控信号直接抛出，由投递服务按规则停手、上锁
    - ``confirm_default_resume``：弹出「选择附件简历」时，用平台默认选中的那份、点弹窗里唯一的
      投递按钮（各一次）；弹窗不是预期的样子就不点，交还给用户
    """
    import json

    from ..models import LoginRequired, PlatformRisk

    if job is None or not job.url:
        return "unsupported", "这个岗位没有原链接"
    ctx.reserve("apply")                              # 投递有自己的页面额度，和采集分开计
    page = ctx.browser.open(job.url)
    try:
        page.wait_for(ready_selector, timeout_ms=30000)
        script = "(" + JS_PROBE + ")(" + json.dumps(LABELS, ensure_ascii=False) + ")"
        data = page.eval(script) or {}
        if data.get("loginLink") and not data.get("loginModal"):
            ctx.stop.sleep(4)                         # 页头登录态晚渲染（见 probe 里的说明）
            data = page.eval(script) or data
        risk = classify_risk(page.text(), page.url)
        if risk == "login_required" or data.get("loginModal") or data.get("loginLink"):
            raise tag_page(LoginRequired(f"{label}需要登录：请在调试 Chrome 里重新登录{label}"), page)
        if risk:
            raise tag_page(PlatformRisk(risk, f"{label}岗位页出现风控信号"), page)
        if data.get("already"):
            return "already", f"页面显示「{data['already'][0]['text']}」"
        buttons = [b for b in (data.get("apply") or []) if not b.get("disabled")]
        if not buttons:
            return "unsupported", "页面上没有可点的投递按钮（岗位可能已下线，或只能聊天）"
        target = buttons[0]["text"]
        # 人会先读一眼岗位再点投递：只停留 / 滚动 / 挪鼠标，不加任何点击
        humanize.before_apply(ctx, page)
        if not page.eval("(" + JS_CLICK + ")(" + json.dumps(target, ensure_ascii=False) + ")"):
            return "unsupported", f"找到了「{target}」但点不到（页面可能刚变化）"
        ctx.log(f"[{platform}] 已点「{target}」，等结果…")
        after = {}
        resume_used = ""                              # 非空 = 已经在简历弹窗里点过一次投递
        for _ in range(8):                            # 最多等约 8 秒
            ctx.stop.sleep(1.0)
            after = page.eval(JS_AFTER) or {}
            text = " ".join(after.get("popups") or [])
            buttons_now = after.get("buttons") or []
            if any(w in text for w in AFTER_SUCCESS) or any(w in b for b in buttons_now for w in ("已投递", "已申请")):
                return "applied", f"点了「{target}」，页面显示投递成功" + (f"（附件简历：{resume_used}）" if resume_used else "")
            if any(w in text for w in AFTER_NEED_RESUME) and not resume_used:
                if confirm_default_resume:
                    picked = page.eval(JS_RESUME_CONFIRM) or {}
                    if picked.get("clicked"):
                        resume_used = picked.get("resume") or "平台默认选中的那份"
                        ctx.log(f"[{platform}] 选择简历弹窗：用默认选中的「{resume_used}」，已点投递，等结果…")
                        continue
                    return "need_resume", f"弹出了选择简历的窗口，但{picked.get('reason') or '样子和预期不一样'}：请到平台上手动完成这一次"
                return "need_resume", "需要选择或完善简历：请到平台上手动完成这一次（" + text[:40] + "）"
            if any(w in text for w in AFTER_LIMIT):
                raise PlatformRisk("rate_limit", f"{label}提示投递受限：{text[:40]}")
            later_risk = classify_risk(page.text(), page.url)
            if later_risk and later_risk != "login_required":
                raise tag_page(PlatformRisk(later_risk, f"{label}点投递后出现风控信号"), page)
        popup = " / ".join(after.get("popups") or [])[:60]
        what = f"「{target}」" + (f"和简历弹窗里的投递（{resume_used}）" if resume_used else "")
        return "failed", (f"点了{what}但看不出结果，没有再点第二次，请到平台上核对"
                          + (f"（页面提示：{popup}）" if popup else ""))
    finally:
        release_page(page)   # 验证 / 掉登录的那一页留给你处理


def probe(ctx: DriverContext, job: Job, *, platform: str, ready_selector: str, label: str) -> dict:
    """打开岗位页看一眼就关掉。返回给界面的报告里 ``sent_anything`` 永远是 False。"""
    base = {"platform": platform, "job_id": job.id if job else "", "sent_anything": False}
    if job is None or not job.url:
        return {**base, "ok": False, "verdict": "这个岗位没有原链接，没法预演"}
    ctx.log(f"[{platform}] 投递预演（只看不点）：{job.title}")
    ctx.reserve("collect_detail")                 # 和采集详情页同一条额度、同一道风控闸门
    page = ctx.browser.open(job.url)
    try:
        page.wait_for(ready_selector, timeout_ms=30000)   # 等不到也继续：没内容本身就是要报告的结果
        import json

        script = "(" + JS_PROBE + ")(" + json.dumps(LABELS, ensure_ascii=False) + ")"
        data = page.eval(script) or {}
        if data.get("loginLink") and not data.get("loginModal"):
            # 只有页头的「登录/注册」：多半是登录状态还没渲染出来，等一下再看（不额外开页面）
            ctx.stop.sleep(4)
            data = page.eval(script) or data
        risk = classify_risk(page.text(), page.url)
    finally:
        release_page(page)   # 验证 / 掉登录的那一页留给你处理

    apply_btns, already, chat = data.get("apply") or [], data.get("already") or [], data.get("chat") or []
    # 没登录时页面上会有「验证码登录 / 获取验证码」：这是登录表单，不是平台弹出来的人机验证。
    # 智联 10-13 实测就是这样被误判成 captcha 的 —— 有登录信号时，先当「需要登录」处理。
    if risk == "captcha" and (data.get("loginModal") or data.get("loginLink")):
        risk = "login_required"
    need_login = risk == "login_required" or bool(data.get("loginModal")) or bool(data.get("loginLink"))
    if need_login:
        verdict, ok = f"需要登录：请先在打开的 Chrome 里登录{label}，再预演", False
    elif risk:
        verdict, ok = f"页面出现风控信号（{risk}）：已停手，不要再连续打开这个平台", False
    elif already:
        verdict, ok = f"这个岗位显示「{already[0]['text']}」：之前已经投过，页面结构可识别", True
    elif apply_btns:
        first = apply_btns[0]
        state = "（按钮是灰的，暂时点不了）" if first.get("disabled") else ""
        verdict, ok = f"找到投递按钮「{first['text']}」{state}：页面结构支持自动投递", not first.get("disabled")
    elif chat:
        verdict, ok = f"没有投递按钮，只有「{chat[0]['text']}」：这个岗位走聊天，不是一键投递", False
    else:
        verdict, ok = "没找到投递按钮：可能岗位已下线、需要登录，或页面改版了", False
    return {
        **base, "job_url": job.url, "ok": ok, "verdict": verdict, "risk": risk,
        "button": apply_btns[0] if apply_btns else None,
        "candidates": {"apply": apply_btns, "already": already, "chat": chat},
        "need_login": need_login,
        "page_title": data.get("title", ""), "page_url": data.get("url", ""),
        "login_nodes": data.get("loginNodes") or [],
    }
