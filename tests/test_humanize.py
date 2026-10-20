"""拟人浏览（browser/humanize.py）：停留时长、滚动、悬停、可打断，以及**从不点击**。"""
from __future__ import annotations

import random
import time

import pytest

from jobpilot.browser import humanize
from jobpilot.browser.fake import FakePage
from jobpilot.config import AppConfig, config_problems
from jobpilot.platforms.base import StopRequested, StopSignal

pytestmark = pytest.mark.humanize


class RecordingStop(StopSignal):
    """真的 StopSignal，只是不真的等：记下每次要等多久；第 stop_after 次等待时模拟用户点了「停止」。"""

    def __init__(self, stop_after: int | None = None) -> None:
        super().__init__()
        self.slept: list[float] = []
        self.stop_after = stop_after

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        if self.stop_after is not None and len(self.slept) >= self.stop_after:
            self.request()
        self.check()


class HumanPage(FakePage):
    """带上可选拟人方法的假页面（签名和 PatchrightPage 将来加的一致）。"""

    def __init__(self, rects=None, **kw) -> None:
        super().__init__(**kw)
        self.moves: list[tuple[int, int, int]] = []
        self.wheels: list[tuple[int, int]] = []
        self.hovers: list[str] = []
        self.rects = rects

    def mouse_move(self, x, y, steps):
        self.moves.append((x, y, steps))

    def mouse_wheel(self, dx, dy):
        self.wheels.append((dx, dy))

    def hover(self, selector):
        self.hovers.append(selector)

    def viewport(self):
        return (1440, 900)

    def eval(self, js):
        if "getBoundingClientRect" in js and self.rects is not None:
            return self.rects
        return super().eval(js)


def _read(page, stop, seed=1, **kw):
    kw.setdefault("min_s", 4.0)
    kw.setdefault("max_s", 14.0)
    return humanize.read_page(page, stop, rng=random.Random(seed), **kw)


def test_read_page_dwells_within_bounds_and_scrolls():
    for seed in range(30):
        page, stop = HumanPage(), RecordingStop()
        planned = _read(page, stop, seed=seed)
        assert 4.0 <= planned <= 14.0
        assert sum(stop.slept) == pytest.approx(planned)
        assert len(page.wheels) >= 2
        assert len(page.moves) <= 4                 # 0~2 次挪动，每次两段折线
        assert page.clicks == [] and page.fills == {}


def test_read_page_is_deterministic_under_seed():
    a, b = HumanPage(), HumanPage()
    sa, sb = RecordingStop(), RecordingStop()
    _read(a, sa, seed=7)
    _read(b, sb, seed=7)
    assert a.wheels == b.wheels and a.moves == b.moves and sa.slept == sb.slept


def test_longer_jd_reads_longer_on_average():
    def mean(text_len):
        return sum(humanize.read_duration(4, 30, text_len, random.Random(s)) for s in range(200)) / 200

    assert mean(3000) > mean(100) * 1.3
    # 同一个种子：长 JD 永远不会比短 JD 读得少
    for s in range(50):
        assert humanize.read_duration(4, 14, 3000, random.Random(s)) >= humanize.read_duration(4, 14, 0, random.Random(s))
    assert humanize.read_duration(4, 14, 10 ** 6, random.Random(3)) <= 14


def test_stop_mid_dwell_raises_promptly():
    page, stop = HumanPage(), RecordingStop(stop_after=2)
    with pytest.raises(StopRequested):
        _read(page, stop)
    assert len(stop.slept) == 2                     # 第二次等待就停了，没把剩下的读完


def test_real_stop_signal_sleep_is_interrupted():
    stop = StopSignal()
    stop.request()
    started = time.time()
    with pytest.raises(StopRequested):
        humanize.read_page(HumanPage(), stop, min_s=10, max_s=20, rng=random.Random(0))
    assert time.time() - started < 1.0


def test_missing_optional_methods_fall_back_to_eval_scroll():
    """老驱动 / FakePage 没有鼠标和滚轮方法：退回 window.scrollBy，不报错、不点击。"""
    evals: list[str] = []

    class Plain(FakePage):
        def eval(self, js):
            evals.append(js)
            return None

    page = Plain()
    _read(page, RecordingStop())
    assert sum("window.scrollBy(0," in js for js in evals) >= 2
    assert page.clicks == []
    assert humanize.glance_list(page, RecordingStop(), ".job-card", max_hovers=2, rng=random.Random(0)) == 0
    assert page.clicks == []


def test_scroll_errors_never_escape():
    class Broken(HumanPage):
        def mouse_wheel(self, dx, dy):
            raise RuntimeError("detached")

        def eval(self, js):
            raise RuntimeError("detached")

    _read(Broken(), RecordingStop())                # 拟人动作失败不能打断采集


def test_glance_list_hovers_at_most_max_and_never_clicks():
    rects = [[100, 80 + i * 120, 600, 100] for i in range(6)]
    for seed in range(30):
        for max_hovers in (0, 1, 2, 3):
            page = HumanPage(rects=rects)
            hovered = humanize.glance_list(page, RecordingStop(), ".job-card", max_hovers=max_hovers,
                                           rng=random.Random(seed))
            assert hovered <= max_hovers
            assert len(page.wheels) >= 2
            assert page.clicks == [] and page.fills == {}
            for x, y, _steps in page.moves:          # 落点都在视口里
                assert 0 <= x <= 1440 and 0 <= y <= 900


def test_glance_list_falls_back_to_hover_selector_without_mouse():
    class HoverOnly(FakePage):
        def __init__(self):
            super().__init__()
            self.hovers = []

        def hover(self, selector):
            self.hovers.append(selector)

    page = HoverOnly()
    hovered = humanize.glance_list(page, RecordingStop(), ".job-card", max_hovers=2, rng=random.Random(4))
    assert 1 <= hovered <= 2 and all(h.startswith(".job-card") for h in page.hovers)
    assert page.clicks == []


def test_config_defaults_and_cross_check():
    cfg = AppConfig()
    h = cfg.safety.humanize
    assert (h.enabled, h.detail_read_min_seconds, h.detail_read_max_seconds, h.list_glance, h.max_hovers) == (
        True, 4.0, 14.0, True, 2)
    assert "safety.humanize.detail_read_max_seconds" not in config_problems(cfg)
    cfg.safety.humanize.detail_read_min_seconds = 20
    assert "safety.humanize.detail_read_max_seconds" in config_problems(cfg)
    with pytest.raises(ValueError):
        AppConfig.model_validate({"safety": {"humanize": {"max_hovers": -1}}})


class _Ctx:
    def __init__(self, cfg):
        self.config = cfg
        self.stop = RecordingStop()

    def reserve(self, stage):
        pass

    def log(self, msg):
        pass


def _liepin_page():
    return HumanPage(evals={".job-intro-container": "岗位职责：负责数据分析"},
                     selector_text={".job-intro-container": "x"})


def test_liepin_fetch_jd_reads_like_a_person_when_enabled():
    from jobpilot.platforms.liepin import LiepinDriver

    ctx, page = _Ctx(AppConfig()), _liepin_page()
    text, _ = LiepinDriver()._fetch_jd(ctx, page, "https://www.liepin.com/job/1.shtml")
    assert text.startswith("岗位职责")
    assert len(page.wheels) >= 2 and sum(ctx.stop.slept) >= 4.0
    assert page.clicks == []


def test_disabled_config_skips_humanize_in_driver(monkeypatch):
    from jobpilot.platforms.liepin import LiepinDriver

    called = []
    monkeypatch.setattr(humanize, "read_page", lambda *a, **k: called.append(a))
    cfg = AppConfig()
    cfg.safety.humanize.enabled = False
    ctx, page = _Ctx(cfg), _liepin_page()
    text, _ = LiepinDriver()._fetch_jd(ctx, page, "https://www.liepin.com/job/1.shtml")
    assert text.startswith("岗位职责")
    assert called == [] and page.wheels == [] and ctx.stop.slept == []


def test_list_glance_switch_is_respected():
    cfg = AppConfig()
    cfg.safety.humanize.list_glance = False
    page = HumanPage(rects=[[0, 0, 300, 100]])
    humanize.after_list(_Ctx(cfg), page, ".job-card")
    assert page.wheels == [] and page.moves == []
