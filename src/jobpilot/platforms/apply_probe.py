"""投递预演（只看不点）：给还没开放自动投递的平台攒证据。

BOSS 的自动投递就是这么开起来的：先在真实岗位页上「看」按钮在不在、文字是什么、
有没有登录墙 / 风控信号，证据够了再写真正的点击。这一步**不点任何东西、不发任何东西**，
走的是和采集详情页同一条页面额度与风控闸门（开一次页面就记一次）。

不同平台的按钮写法不一样，而且会随改版变，所以这里不写死某个 CSS 类名：
在页面上找「看得见、可点击、文字像投递按钮」的元素，把找到的东西原样报告回来。
"""

from __future__ import annotations

from ..models import Job
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
  const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], div[class*="btn"], span[class*="btn"], div[class*="apply"], a[class*="apply"]'));
  const found = { apply: [], already: [], chat: [] };
  for (const node of nodes) {
    const text = textOf(node);
    if (!text || text.length > 12 || !visible(node)) continue;
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
  // 只算「真的弹出来」的登录框：很多站每个页面都埋着隐藏的登录弹窗（BOSS 上踩过这个误报）
  const loginOpen = Array.from(document.querySelectorAll('[class*="login"], [class*="Login"], [class*="sign-in"], [id*="login"]'))
    .some((node) => visible(node) && /登录|扫码|手机号|验证码登录/.test((node.innerText || '').slice(0, 200)));
  return {
    url: location.href,
    title: document.title,
    apply: found.apply.slice(0, 5),
    already: found.already.slice(0, 3),
    chat: found.chat.slice(0, 3),
    loginOpen,
    bodyHead: (document.body ? (document.body.innerText || '') : '').replace(/\s+/g, ' ').slice(0, 160),
  };
})
"""

LABELS = {
    "apply": ["申请职位", "立即申请", "申请", "投递简历", "立即投递", "投个简历", "投递", "申请简历"],
    "already": ["已申请", "已投递", "申请成功", "投递成功", "您已投递"],
    "chat": ["立即沟通", "聊一聊", "继续沟通", "和TA聊聊", "在线沟通"],
}


def probe(ctx: DriverContext, job: Job, *, platform: str, ready_selector: str, label: str) -> dict:
    """打开岗位页看一眼就关掉。返回给界面的报告里 ``sent_anything`` 永远是 False。"""
    base = {"platform": platform, "job_id": job.id if job else "", "sent_anything": False}
    if job is None or not job.url:
        return {**base, "ok": False, "verdict": "这个岗位没有原链接，没法预演"}
    ctx.log(f"[{platform}] 投递预演（只看不点）：{job.title}")
    ctx.reserve("collect_detail")                 # 和采集详情页同一条额度、同一道风控闸门
    page = ctx.browser.open(job.url)
    try:
        page.wait_for(ready_selector, timeout_ms=15000)   # 等不到也继续：没内容本身就是要报告的结果
        import json

        data = page.eval("(" + JS_PROBE + ")(" + json.dumps(LABELS, ensure_ascii=False) + ")") or {}
        risk = classify_risk(page.text(), page.url)
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    apply_btns, already, chat = data.get("apply") or [], data.get("already") or [], data.get("chat") or []
    if risk == "login_required" or data.get("loginOpen"):
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
        "need_login": bool(risk == "login_required" or data.get("loginOpen")),
        "page_title": data.get("title", ""), "page_url": data.get("url", ""),
    }
