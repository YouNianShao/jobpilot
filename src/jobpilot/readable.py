"""把系统内部的写法翻成人话：任务结果 / 停止原因 / 风控事件。

库里存的是原始记录（便于排查、不丢信息），**展示时**再翻译 —— 所以早就写进库里的
旧记录（``boss new=0 seen=0``、``boss/page_failures · …``）也能显示成中文，不用改数据。
"""

from __future__ import annotations

import re

PLATFORM_NAMES = {"boss": "BOSS直聘", "51job": "前程无忧", "liepin": "猎聘", "zhaopin": "智联招聘"}

# 风控类型（locks.kind / PlatformRisk.kind / 页面失败熔断）
RISK_KIND_NAMES = {
    "captcha": "出现验证码", "rate_limit": "平台提示访问太频繁", "blocked": "平台限制了账号访问",
    "page_failures": "页面连续读不到内容", "login_required": "登录已失效", "multi_platform": "多个平台同时命中风控",
}

# 配置项路径 → 配置页上的说法（错误信息里偶尔会带出来）
_CONFIG_HINTS = {
    "safety.active_windows": "设置 → 风控 → 允许开页面的时段",
    "safety.send_windows": "设置 → 风控 → 投递时间窗",
}

_PLATFORM_TAG = re.compile(r"\[(boss|51job|liepin|zhaopin)\]\s*")
_PLATFORM_WORD = re.compile(r"(?<![A-Za-z0-9_])(boss|51job|liepin|zhaopin)(?![A-Za-z0-9_])")


def platform_name(key: str) -> str:
    return PLATFORM_NAMES.get(key, key)


def clean(text: object) -> str:
    """一句原始文案 → 人话：平台代号换中文名、配置路径换成配置页上的位置、去掉重复的平台前缀。"""
    out = str(text or "").strip()
    if not out:
        return ""
    # 浏览器没开：patchright 的原文是一长串英文调用日志，用户只需要知道「去开浏览器」
    if "connect_over_cdp" in out or "ECONNREFUSED" in out:
        return "连不上浏览器：带调试端口的 Chrome 没有开着。点左下角「启动浏览器」，登录招聘网站后再试"
    out = _PLATFORM_TAG.sub(lambda m: platform_name(m.group(1)) + "：", out)
    for path, hint in _CONFIG_HINTS.items():
        out = out.replace(f"（{path}）", f"（{hint}）").replace(path, hint)
    # policy 里这句是写给开发看的长句：换成一句结论 + 去哪改
    out = re.sub(r"当前不在允许开页面的时段（[^）]*）[；;].*$", "现在不在允许开页面的时段（可以在 设置 → 风控 里调整）", out)
    out = re.sub(r"要改去设置\s*→\s*风控", "可以在设置 → 风控里改", out)
    out = _PLATFORM_WORD.sub(lambda m: platform_name(m.group(1)), out)
    out = re.sub(r"(BOSS直聘|前程无忧|猎聘|智联招聘) 处于风险冷却", r"\1 正在冷却", out)
    for kind, name in RISK_KIND_NAMES.items():
        out = out.replace(f"/{kind}", f"：{name}")
    # 「BOSS直聘 第 3 次：BOSS直聘：搜索页…」这种平台名重复
    out = re.sub(r"(BOSS直聘|前程无忧|猎聘|智联招聘)(.{0,12}?)：\1：", r"\1\2：", out)
    return out


# ---------------- 任务结果 ----------------

_COUNTS = (
    ("new", "新增 {} 个岗位"), ("duplicate", "{} 个已在库里"), ("filtered", "{} 个被列表预筛拦下"),
    ("scored", "评分 {} 个"), ("prefiltered", "{} 个预筛淘汰"), ("failed", "{} 个失败"),
    ("greeted", "写了 {} 条招呼语"), ("confirmed", "确认 {} 个"), ("rejected", "拒绝 {} 个"),
    ("applied", "投出 {} 个"), ("already", "{} 个之前投过"), ("checked", "看了 {} 个会话"),
    ("awaiting_confirmation", "{} 个等你确认"),
)

_STEP_NAMES = {"collect": "采集", "score": "评分", "greet": "招呼语", "apply": "投递"}


def _counts(result: dict) -> list[str]:
    parts = []
    for key, template in _COUNTS:
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value:
            parts.append(template.format(value))
    return parts


def _problem(result: dict) -> str:
    if result.get("error"):
        text = clean(result["error"])
        if result.get("locked_until"):
            text += f"；平台暂停到 {str(result['locked_until'])[5:16]}"
        elif result.get("login_required"):
            text += "；请在调试 Chrome 里重新登录"
        return text
    if result.get("stopped"):
        return clean(result["stopped"])
    if isinstance(result.get("skipped"), str) and result["skipped"]:
        return clean(result["skipped"])
    return ""


def _nothing(mode: str, result: dict) -> str:
    """一切正常、但什么都没做：说清楚是「没有可处理的」，而不是一根横线。"""
    if mode == "score" or "scored" in result:
        return "没有待评分的岗位"
    if mode == "greet" or "greeted" in result:
        return "没有需要写招呼语的岗位"
    if mode == "apply" or "applied" in result:
        return "没有已确认、待投递的岗位"
    if mode == "collect" or "new" in result:
        return "没有采到新岗位"
    if mode == "confirm" or "confirmed" in result:
        return "没有待确认的岗位"
    return "完成"


def run_summary(mode: str, result: object, error: str = "") -> str:
    """任务历史「结果 / 原因」一列。"""
    if error:
        return clean(error)
    if not isinstance(result, dict):
        return clean(result) if result else ("任务被停止" if mode else "—")
    steps = result.get("steps")
    if isinstance(steps, dict):                      # 全流程：逐步说
        parts = []
        for key, sub in steps.items():
            if not isinstance(sub, dict):
                continue
            problem = _problem(sub)
            counts = _counts(sub)
            if problem:
                parts.append(f"{_STEP_NAMES.get(key, key)}：{problem}")
                break                                # 前一步没做成，后面的步骤也就没东西了
            if counts:
                parts.append(f"{_STEP_NAMES.get(key, key)}：" + "，".join(counts))
        if result.get("awaiting_confirmation"):
            parts.append(f"{result['awaiting_confirmation']} 个等你确认")
        if not parts and result.get("stopped") is not None:
            return "任务被停止"
        return "；".join(parts) or "没有可处理的岗位"
    problem = _problem(result)
    counts = _counts(result)
    if problem and counts:
        return "，".join(counts) + "；" + problem
    return problem or "，".join(counts) or _nothing(mode, result)


def run_failed(result: object, error: str = "") -> bool:
    """跑完了但没做成（被冷却 / 时段 / 登录挡住）：界面上不能显示成绿色的「完成」。"""
    if error:
        return True
    if not isinstance(result, dict):
        return False
    if result.get("error"):
        return True
    if result.get("stopped") and not _counts(result):
        return True
    return any(run_failed(sub) for sub in (result.get("steps") or {}).values() if isinstance(sub, dict))


# ---------------- 风控 / 系统事件 ----------------

EVENT_NAMES = {
    "risk_lock": "暂停平台", "rate_stop": "速率停手", "risk": "风控信号",
    "page_error": "页面异常", "collect": "采集", "monitor": "监测",
}

_KV = re.compile(r"(\w+)=(\d+)")


def event_text(kind: str, detail: str) -> str:
    """风控事件一行的说明文字。"""
    detail = str(detail or "")
    if kind in ("collect", "monitor"):
        head, _, rest = detail.partition(" ")
        name = platform_name(head)
        values = {k: int(v) for k, v in _KV.findall(rest)}
        if kind == "monitor":
            return f"{name} 看了 {values.get('checked', 0)} 个会话"
        new, seen = values.get("new", 0), values.get("seen", 0)
        if not seen:
            # 这一条事件分不出是「最近采过、跳过」还是「被冷却 / 时段挡住」：只陈述结果，原因看任务历史
            return f"{name} 这次没有打开新的搜索页，没采到岗位（原因见任务历史）"
        return f"{name} 看到 {seen} 个岗位，新增 {new} 个" + ("" if new else "（都已在库里）")
    if kind == "risk_lock":
        # 新格式「boss/page_failures · 连续 3 次页面异常 · 62 分钟」；整体锁「〔整体〕… · 5 小时」
        parts = [p.strip() for p in detail.split("·")]
        if parts and parts[0].startswith("〔整体〕"):
            return "所有平台暂停 " + (parts[-1] if len(parts) > 1 else "") + "：" + clean(parts[0][4:])
        head = parts[0] if parts else ""
        platform, _, why = head.partition("/")
        reason = RISK_KIND_NAMES.get(why, clean(why)) if why else ""
        length = parts[-1] if len(parts) >= 3 else ""
        text = f"{platform_name(platform)} 暂停 {length}".strip()
        if reason:
            text += f"：{reason}"
        if len(parts) >= 3 and parts[1] and parts[1] != reason:
            text += f"（{clean(parts[1])}）"
        return text
    if kind == "risk":
        platform, _, why = detail.partition(" ")
        return f"{platform_name(platform)}：{RISK_KIND_NAMES.get(why.strip(), clean(why))}"
    if kind == "page_error":
        # 「boss 第 3 次：[boss] 搜索页 上海/AI 未找到预期内容（可能改版或被拦截）」
        m = re.match(r"(\S+) 第 (\d+) 次：(.*)", detail)
        if m:
            platform, count, rest = m.groups()
            rest = _PLATFORM_TAG.sub("", rest)
            return f"{platform_name(platform)} 第 {count} 次页面异常：{clean(rest)}"
    return clean(detail)
