"""风控信号识别：从页面 URL 与正文判断 验证码 / 限流 / 封禁 / 登录墙。

纯函数，可单测；由平台驱动在"页面没有预期内容"时调用。
"""

from __future__ import annotations

RISK_TEXT: dict[str, tuple[str, ...]] = {
    "blocked": (
        "账号存在异常", "账号异常", "账号已被限制", "账号被限制", "账号被冻结",
        "您的账号存在风险", "账号存在安全风险", "访问被拒绝",
        "访问受限", "访问被拦截", "请求被拦截", "已被封禁",
    ),
    "captcha": (
        "请完成验证", "完成安全验证", "安全验证", "拖动滑块",
        "点击完成验证", "人机验证", "滑块验证", "滑动验证",
        "验证码", "校验码",
    ),
    "rate_limit": (
        "操作过于频繁", "访问过于频繁", "请求过于频繁", "访问速度过快",
        "访问频率", "系统繁忙", "服务繁忙",
        "当前访问异常", "访问行为异常", "操作存在异常",
    ),
}


# 泛化短语：单独出现可能只是普通文案（岗位描述里就写过「如未接通请稍后再试」），
# 必须和风控语境词同现才算命中 —— 否则会拿一句 JD 把整个平台锁掉。
RISK_SOFT: dict[str, tuple[str, ...]] = {
    "rate_limit": ("请稍后再试", "请稍后重试", "稍后再试"),
}
SOFT_CONTEXT = ("访问", "请求", "操作", "系统", "服务",
                "网络", "频繁", "异常", "繁忙", "验证")
# 只匹配路径/主机特征，避免把查询参数里的 "login" 误判
RISK_URL: dict[str, tuple[str, ...]] = {
    "captcha": ("/captcha", "/verify", "security-check", "geetest", "/challenge"),
    "login_required": ("/login", "/signin", "/sign-in", "passport.", "uplogin", "/web/user/"),
}


def classify_risk(text: str = "", url: str = "") -> str | None:
    """优先级：登录墙 > 封禁 > 验证码 > 限流。命中返回类型，否则 None。

    按「严重程度」排，而不是按先匹配到谁：封禁要立刻停手，登录过期只需要停下重新登录。
    """
    low_url = (url or "").lower()
    if any(sig in low_url for sig in RISK_URL["login_required"]):
        return "login_required"
    blob = text or ""
    if any(kw in blob for kw in RISK_TEXT["blocked"]):
        return "blocked"
    if any(sig in low_url for sig in RISK_URL["captcha"]):
        return "captcha"
    if any(kw in blob for kw in RISK_TEXT["captcha"]):
        return "captcha"
    if any(kw in blob for kw in RISK_TEXT["rate_limit"]):
        return "rate_limit"
    if any(kw in blob for kw in RISK_SOFT["rate_limit"]) and any(ctx in blob for ctx in SOFT_CONTEXT):
        return "rate_limit"
    return None
