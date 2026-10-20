"""像人一样浏览：详情页读一会儿、列表页扫一眼。

秒开秒关、从不滚动、鼠标从不动的页面访问，是招聘平台最容易识别的机器特征。
这里只做三类动作：**停留**（全部经过 ``stop.sleep``，「停止」按钮随时能打断）、
**滚轮滚动**、**鼠标移动 / 悬停**。

为什么故意不做「随机点击」：招聘站上几乎没有能证明「点了一定没事」的位置 ——
卡片、按钮、甚至看起来是空白的边距，都可能挂着「投递 / 立即沟通 / 收藏 / 关注」的事件，
一次误点就是一次真实的投递或打招呼，发出去撤不回来。鼠标移动和悬停最多弹出一个预览浮层，
不会改变任何东西，所以拟人只做到这一步。

页面方法都是**可选的**（用 getattr 取）：``mouse_move(x, y, steps)``、``mouse_wheel(dx, dy)``、
``hover(selector)``、``viewport() -> (w, h)``。缺了就退回 ``eval("window.scrollBy(...)")`` 或直接跳过，
这样测试用的 FakePage 和老驱动都能照常工作。随机数一律从传入的 ``rng`` 取，种子固定时结果可复现。
"""

from __future__ import annotations

import json
import math
import random

# 取视口里看得见的卡片位置（只读，不触发事件）
JS_CARD_RECTS = r"""
((sel) => {
  const w = window.innerWidth, h = window.innerHeight;
  return Array.from(document.querySelectorAll(sel))
    .map((n) => n.getBoundingClientRect())
    .filter((r) => r.width > 20 && r.height > 20 && r.top >= 0 && r.left >= 0 && r.bottom <= h && r.right <= w)
    .slice(0, 30)
    .map((r) => [r.left, r.top, r.width, r.height]);
})
"""

DEFAULT_VIEWPORT = (1280, 800)


def _call(page, name: str, *args) -> bool:
    """调页面上的可选方法；没有这个方法或调用出错都返回 False（拟人动作失败不影响正事）。"""
    fn = getattr(page, name, None)
    if not callable(fn):
        return False
    try:
        fn(*args)
        return True
    except Exception:  # noqa: BLE001 - 滚不动 / 移不了鼠标都不该打断采集
        return False


def _viewport(page) -> tuple[int, int]:
    fn = getattr(page, "viewport", None)
    if callable(fn):
        try:
            w, h = fn()
            if w and h:
                return int(w), int(h)
        except Exception:  # noqa: BLE001
            pass
    return DEFAULT_VIEWPORT


def scroll(page, dy: int) -> None:
    """滚轮滚一下；驱动没有滚轮方法就退回脚本滚动。"""
    if not _call(page, "mouse_wheel", 0, int(dy)):
        try:
            page.eval(f"window.scrollBy(0, {int(dy)})")
        except Exception:  # noqa: BLE001
            pass


def move_mouse(page, x: float, y: float, rng=random) -> None:
    """鼠标沿一条略弯的折线移过去：先到偏离直线的中间点，再到目标点（人手不会走直线）。"""
    if not callable(getattr(page, "mouse_move", None)):
        return
    w, h = _viewport(page)
    # 中间点：目标附近、垂直方向偏一点
    mid_x = min(max(1.0, x + rng.uniform(-0.12, 0.12) * w), w - 1.0)
    mid_y = min(max(1.0, y + rng.uniform(-0.12, 0.12) * h), h - 1.0)
    _call(page, "mouse_move", round(mid_x), round(mid_y), rng.randint(6, 18))
    _call(page, "mouse_move", round(x), round(y), rng.randint(8, 24))


def _idle_move(page, rng) -> None:
    """读的时候鼠标随手挪一下：落点在视口中间区域，避开边缘的导航栏和浮层。"""
    w, h = _viewport(page)
    move_mouse(page, rng.uniform(0.15, 0.85) * w, rng.uniform(0.2, 0.8) * h, rng)


def read_duration(min_s: float, max_s: float, text_len: int | None = None, rng=random) -> float:
    """读一页要多久：对数正态（多数读得快、偶尔读很久），JD 越长越久，夹在 [min_s, max_s]。"""
    lo, hi = max(0.0, float(min_s)), max(0.0, float(max_s))
    if hi < lo:
        lo, hi = hi, lo
    # 中位数落在区间 30% 处；偏态让少数几页读得明显更久
    frac = rng.lognormvariate(math.log(0.3), 0.5)
    if text_len:
        # 长 JD 最多把停留拉长 80%（3000 字封顶），避免一页读上好几分钟
        frac *= 1.0 + 0.8 * min(1.0, max(0, int(text_len)) / 3000)
    return lo + (hi - lo) * min(1.0, frac)


def _split(total: float, parts: int, rng) -> list[float]:
    """把总时长切成长短不一的几段（停顿不能是等长的节拍）。"""
    weights = [rng.uniform(0.4, 1.6) for _ in range(max(1, parts))]
    scale = total / sum(weights)
    return [w * scale for w in weights]


def read_page(page, stop, *, min_s: float, max_s: float, text_len: int | None = None, rng=random) -> float:
    """像人读页面一样停留：几次大小不一的滚动（偶尔往回滚一点）、零到两次鼠标挪动，停顿不规则。

    返回计划停留的秒数。所有等待都走 ``stop.sleep``：点「停止」会在下一次等待时抛 StopRequested。
    """
    duration = read_duration(min_s, max_s, text_len, rng)
    if duration <= 0:
        return 0.0
    actions = ["scroll"] * rng.randint(2, 5) + ["move"] * rng.randint(0, 2)
    rng.shuffle(actions)
    pauses = _split(duration, len(actions) + 1, rng)
    scrolled = 0
    for pause, action in zip(pauses[:-1], actions, strict=True):
        stop.sleep(pause)
        if action == "move":
            _idle_move(page, rng)
            continue
        # 往回滚一点只在已经往下滚过之后：人会回头再看一眼上面那段
        if scrolled > 0 and rng.random() < 0.15:
            dy = -rng.randint(60, 200)
        elif rng.random() < 0.3:
            dy = rng.randint(80, 180)       # 小幅挪一挪
        else:
            dy = rng.randint(200, 520)
        scroll(page, dy)
        scrolled += dy
    stop.sleep(pauses[-1])
    return duration


def _card_rects(page, card_selector: str) -> list[tuple[float, float, float, float]]:
    try:
        raw = page.eval("(" + JS_CARD_RECTS + ")(" + json.dumps(card_selector, ensure_ascii=False) + ")")
    except Exception:  # noqa: BLE001
        return []
    rects: list[tuple[float, float, float, float]] = []
    for item in raw if isinstance(raw, list) else []:
        # 只认 [x, y, w, h] 四个数：假页面 / 别的脚本返回的东西直接忽略
        if isinstance(item, (list, tuple)) and len(item) == 4 and all(isinstance(v, (int, float)) for v in item):
            rects.append(tuple(float(v) for v in item))
    return rects


def _hover_card(page, card_selector: str, rects: list, index: int, rng) -> bool:
    """把鼠标移到第 index 张卡片里的随机一点；没有鼠标方法时退回 hover(选择器)。只悬停，不点。"""
    if callable(getattr(page, "mouse_move", None)) and index < len(rects):
        x, y, w, h = rects[index]
        move_mouse(page, x + w * rng.uniform(0.25, 0.75), y + h * rng.uniform(0.3, 0.7), rng)
        return True
    return _call(page, "hover", f"{card_selector} >> nth={index}")


def glance_list(page, stop, card_selector: str, *, max_hovers: int = 2, rng=random) -> int:
    """在搜索结果页扫一眼：不均匀地往下滚几下，鼠标在 1~max_hovers 张卡片上停一停（最多 3 张）。

    只滚动和悬停，**绝不点击**。返回悬停了几张卡片。
    """
    steps = rng.randint(2, 4)
    hover_after = rng.randint(1, steps)          # 滚到第几下时停下来看卡片
    hovered = 0
    for step in range(1, steps + 1):
        scroll(page, rng.randint(250, 700))
        stop.sleep(rng.uniform(0.4, 1.6))
        if step != hover_after or max_hovers <= 0:
            continue
        rects = _card_rects(page, card_selector)
        if not rects and not callable(getattr(page, "hover", None)):
            continue
        pool = len(rects) if rects else 5
        want = rng.randint(1, max(1, min(3, int(max_hovers))))
        for index in sorted(rng.sample(range(pool), min(want, pool))):
            if _hover_card(page, card_selector, rects, index, rng):
                hovered += 1
                stop.sleep(rng.uniform(0.6, 2.0))
    return hovered


# ---------------- 给平台驱动用的入口（读配置、吞掉拟人动作自身的异常） ----------------
def _cfg(ctx):
    try:
        cfg = ctx.config.safety.humanize
    except AttributeError:
        return None
    return cfg if cfg.enabled else None


def after_detail(ctx, page, text: str = "") -> None:
    """详情页读到 JD 之后：按配置停留读一会儿再走。关掉拟人时什么都不做。"""
    cfg = _cfg(ctx)
    if cfg is None or page is None:
        return
    read_page(page, ctx.stop, min_s=cfg.detail_read_min_seconds, max_s=cfg.detail_read_max_seconds,
              text_len=len(text or ""))


def after_list(ctx, page, card_selector: str) -> None:
    """搜索结果页解析完之后：往下扫一眼、在一两张卡片上悬停。"""
    cfg = _cfg(ctx)
    if cfg is None or page is None or not cfg.list_glance:
        return
    glance_list(page, ctx.stop, card_selector, max_hovers=cfg.max_hovers)


# 点投递按钮前的停留：人会先把岗位看一眼再点，但不必像采集那样读满
BEFORE_APPLY_SECONDS = (2.0, 6.0)


def before_apply(ctx, page) -> None:
    """点投递 / 立即沟通之前停一会儿（只滚动和挪鼠标，不加任何点击）。"""
    if _cfg(ctx) is None or page is None:
        return
    read_page(page, ctx.stop, min_s=BEFORE_APPLY_SECONDS[0], max_s=BEFORE_APPLY_SECONDS[1])
