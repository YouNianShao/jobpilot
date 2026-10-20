"""浏览器驱动补丁 + 断线重连一次 + 在调试 Chrome 里打开岗位链接。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import jobpilot.browser.driver_patch as dp
import jobpilot.browser.patchright_driver as drv


def test_patch_is_idempotent_and_refuses_unknown_versions(tmp_path, monkeypatch):
    bundle = tmp_path / "coreBundle.js"
    bundle.write_text("a\n" + dp._TARGET + "b\n", encoding="utf-8")
    monkeypatch.setattr(dp, "bundle_path", lambda: bundle)
    assert dp.ensure_patched() == "patched"
    text = bundle.read_text(encoding="utf-8")
    assert ".catch(() => {})" in text and dp._TARGET not in text
    assert dp.ensure_patched() == "ok" and bundle.read_text(encoding="utf-8") == text   # 打过就不再改

    bundle.write_text("新版本写法变了\n", encoding="utf-8")
    assert "没有打补丁" in dp.ensure_patched()
    assert bundle.read_text(encoding="utf-8") == "新版本写法变了\n"                    # 认不出就一个字都不动


def test_real_installed_driver_is_patched():
    path = dp.bundle_path()
    if path is None:
        pytest.skip("没装 patchright")
    assert dp._MARK in path.read_text(encoding="utf-8")


class _Ctx:
    def __init__(self, fail_times):
        self.fail_times = fail_times

    def new_page(self):
        if self.fail_times:
            self.fail_times -= 1
            raise Exception("BrowserContext.new_page: Connection closed while reading from the driver")
        return _Page()


class _Page:
    url = ""

    def goto(self, *a, **k):
        pass


def test_open_reconnects_once_when_the_driver_died(monkeypatch):
    browser = drv.PatchrightBrowser("http://127.0.0.1:9")
    contexts = [_Ctx(1), _Ctx(0)]
    connects = []

    def fake_ensure():
        if browser._context is None:
            connects.append(1)
            browser._context = contexts[len(connects) - 1]
        return browser._context

    monkeypatch.setattr(browser, "_ensure", fake_ensure)
    page = browser.open("https://www.liepin.com/")
    assert page is not None and len(connects) == 2                     # 第一次断了，重连后成功


def test_open_does_not_retry_ordinary_errors(monkeypatch):
    browser = drv.PatchrightBrowser("http://127.0.0.1:9")

    class Boom:
        def new_page(self):
            raise ValueError("别的错误")

    monkeypatch.setattr(browser, "_ensure", lambda: Boom())
    with pytest.raises(ValueError):
        browser.open("https://x")


def test_driver_death_reads_as_plain_chinese():
    from jobpilot.readable import clean

    assert "连接中途断了" in clean("Exception: Page.goto: Connection closed while reading from the driver")


@pytest.fixture
def client(monkeypatch):
    import jobpilot.web.app as web
    from jobpilot.config import AppConfig

    monkeypatch.setattr(web, "load_config", lambda: AppConfig())

    class Idle:
        running = False

    return TestClient(web.create_app(lambda: None, runner=Idle()), base_url="http://127.0.0.1:8700")


def test_open_link_only_allows_job_sites(client):
    for url in ("https://evil.example.com/", "javascript:alert(1)", "file:///C:/x", "https://zhipin.com.evil.cn/"):
        res = client.post("/api/browser/open", json={"url": url})
        assert res.status_code == 400 and res.json()["ok"] is False


def test_open_link_tells_you_when_the_browser_is_not_running(client, monkeypatch):
    import jobpilot.browser.launcher as launcher

    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: False)
    res = client.post("/api/browser/open", json={"url": "https://www.liepin.com/job/1.shtml"}).json()
    assert res["ok"] is False and res["not_running"] is True and "启动浏览器" in res["error"]


def test_open_link_opens_a_tab_through_cdp_http(client, monkeypatch):
    import io
    import json

    import jobpilot.browser.launcher as launcher

    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "is_headless", lambda *a, **k: False)
    calls = []

    def fake_urlopen(req, timeout=0):
        url = req.full_url if hasattr(req, "full_url") else req
        calls.append((getattr(req, "method", "GET"), url))
        return io.BytesIO(json.dumps({"id": "T1"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    res = client.post("/api/browser/open", json={"url": "https://jobs.51job.com/all/168708377.html"}).json()
    assert res == {"ok": True}
    assert calls[0][0] == "PUT" and "/json/new?https://jobs.51job.com/all/168708377.html" in calls[0][1]
    assert calls[1][1].endswith("/json/activate/T1")


@pytest.mark.parametrize("driver_path", ["jobpilot.platforms.liepin:LiepinDriver", "jobpilot.platforms.job51:Job51Driver",
                                         "jobpilot.platforms.zhaopin:ZhaopinDriver"])
def test_detail_page_that_is_a_captcha_stops_instead_of_skipping(driver_path):
    """2026-10-15：详情页拿不到 JD 时以前只记「已跳过」接着开下一页，从不看是不是验证码 / 访问频繁。"""
    import importlib

    from jobpilot.models import PlatformRisk

    mod, cls = driver_path.split(":")
    driver = getattr(importlib.import_module(mod), cls)()

    class Page:
        url = "https://example.com/job/1"

        def goto(self, url):
            pass

        def wait_for(self, *a, **k):
            return False

        def text(self):
            return "访问过于频繁，请完成安全验证"

    class Ctx:
        def reserve(self, stage):
            pass

        def log(self, msg):
            pass

    with pytest.raises(PlatformRisk):
        driver._fetch_jd(Ctx(), Page(), "https://example.com/job/1")

    class Plain(Page):
        def text(self):
            return "岗位已下线"

    assert driver._fetch_jd(Ctx(), Plain(), "https://example.com/job/1")[0] == ""      # 普通失败：照旧跳过


def test_liepin_is_paced_like_bosshunter_even_for_old_saved_configs():
    """猎聘详情页会拦（2026-10-15）：默认放慢到 ~7.5–21 秒一页；老配置里没写猎聘的也要补上。"""
    from jobpilot.config import AppConfig

    old = AppConfig.model_validate({"safety": {"platforms": {"boss": {"delay_multiplier": 2.0}}}})
    s = old.safety
    assert s.multiplier_for("liepin") == 3.0 and s.per_10min_for("liepin") == 6
    assert s.pages_for("liepin").collect_detail == 90
    assert s.multiplier_for("boss") == 2.0                                   # 用户写了的照用户的来
    custom = AppConfig.model_validate({"safety": {"platforms": {"liepin": {"delay_multiplier": 4.0}}}})
    assert custom.safety.multiplier_for("liepin") == 4.0
    assert AppConfig().platforms["liepin"].max_pages == 1


@pytest.mark.humanize
def test_liepin_reads_the_jd_page_before_leaving(monkeypatch):
    """读到 JD 后走共用的拟人停留（browser/humanize.py）：滚动几下、停留时长在配置区间内。"""
    from jobpilot.config import AppConfig
    from jobpilot.platforms.liepin import LiepinDriver

    calls, slept = [], []

    class Page:
        url = "https://www.liepin.com/job/1.shtml"

        def goto(self, url):
            pass

        def wait_for(self, *a, **k):
            return True

        def eval(self, js):
            calls.append(js)
            return "岗位职责：负责数据分析" if "scrollBy" not in js else None

    class Stop:
        def sleep(self, s):
            slept.append(s)

    class Ctx:
        stop = Stop()
        config = AppConfig()

        def reserve(self, stage):
            pass

        def log(self, msg):
            pass

    text, _ = LiepinDriver()._fetch_jd(Ctx(), Page(), "https://www.liepin.com/job/1.shtml")
    # 这个假页面没有滚轮方法：退回脚本滚动，至少滚两下
    assert text.startswith("岗位职责") and sum("scrollBy" in c for c in calls) >= 2
    cfg = Ctx.config.safety.humanize
    assert cfg.detail_read_min_seconds - 1e-6 <= sum(slept) <= cfg.detail_read_max_seconds + 1e-6


def test_watchdog_turns_a_hung_call_into_pagehung(monkeypatch):
    """页面整页卡死时 evaluate 永远不返回：看门狗到点强制断开驱动，这一句以 PageHung 返回。"""
    import threading

    monkeypatch.setattr(drv, "CALL_TIMEOUT_S", 0.3)
    browser = drv.PatchrightBrowser("http://127.0.0.1:9")
    released = threading.Event()
    monkeypatch.setattr(browser, "kill_driver", lambda: (setattr(browser, "_killed", True), released.set()))

    def hangs():
        released.wait(5)
        raise Exception("Connection closed")

    with pytest.raises(drv.PageHung):
        browser.guarded(hangs)
    assert released.is_set()


def test_fast_calls_are_untouched(monkeypatch):
    monkeypatch.setattr(drv, "CALL_TIMEOUT_S", 5)
    browser = drv.PatchrightBrowser("http://127.0.0.1:9")
    assert browser.guarded(lambda: 42) == 42
    with pytest.raises(ValueError):
        browser.guarded(lambda: (_ for _ in ()).throw(ValueError("普通错误照常抛")))


def test_stop_force_kills_a_task_stuck_in_a_page_call(monkeypatch):
    """点了停止 8 秒后任务还在跑：强制断开它的浏览器驱动。"""
    import threading
    import time

    import jobpilot.tasks as tasks_mod
    from jobpilot.platforms.base import StopSignal

    killed = threading.Event()

    class Ctx:
        def __init__(self):
            from jobpilot.config import AppConfig

            self.stop = StopSignal()
            self.config = AppConfig()

            class R:
                class conn:
                    @staticmethod
                    def close():
                        pass

                def upsert_task_run(self, row):
                    pass

            self.repo = R()

        def kill_browsers(self):
            killed.set()
            return 1

    def flow(ctx, mode, platform, **o):
        killed.wait(5)                                  # 模拟卡在一句不返回的页面调用里
        raise RuntimeError("Connection closed")

    monkeypatch.setattr(tasks_mod, "run_flow", flow)
    monkeypatch.setattr(tasks_mod, "check_mode", lambda *a, **k: None)
    monkeypatch.setattr(tasks_mod.TaskRunner, "STOP_GRACE_S", 0.2)
    runner = tasks_mod.TaskRunner(lambda: Ctx())
    task = runner.start("confirm", "")
    runner.stop(task.id)
    for _ in range(60):
        if task.status != "running":
            break
        time.sleep(0.05)
    assert killed.is_set() and task.status == "stopped"
    assert any("强制断开" in line for line in task.logs)


def test_zhaopin_list_reads_the_job_number_from_each_card():
    """滚动加载的批次不在页面 HTML 里：编号要从卡片自己的数据（Vue 组件的 job.number）拿。"""
    from jobpilot.platforms.zhaopin import JS_LIST

    assert "__vue__" in JS_LIST and "job.number" in JS_LIST


def test_stop_sleep_keeps_the_browser_connection_pumping():
    """任务等待时要让浏览器连接继续收发消息（否则新开的标签页一直转圈，2026-10-16 真机）。"""
    import time

    from jobpilot.platforms.base import StopSignal

    pumped = []

    def pump(seconds):
        pumped.append(seconds)
        time.sleep(seconds)
        return True

    stop = StopSignal()
    stop.add_pump(pump)
    t0 = time.time()
    stop.sleep(1.2)
    assert pumped and abs(sum(pumped) - 1.2) < 0.3 and time.time() - t0 < 2.0


def test_dead_pump_falls_back_to_plain_sleep():
    import time

    from jobpilot.platforms.base import StopSignal

    def broken(seconds):
        raise RuntimeError("Connection closed")

    stop = StopSignal()
    stop.add_pump(broken)
    t0 = time.time()
    stop.sleep(0.6)
    assert 0.5 < time.time() - t0 < 1.5 and not stop.__dict__["_pumps"]


def test_create_browser_registers_its_pump():
    from jobpilot.config import AppConfig
    from jobpilot.platforms.base import StopSignal
    from jobpilot.services.context import ServiceContext

    class B:
        def pump(self, s):
            return True

    ctx = ServiceContext(config=AppConfig(), repo=None, llm=None, policy=None, stop=StopSignal(), browser_factory=B)
    ctx.create_browser()
    assert len(ctx.stop.__dict__["_pumps"]) == 1


def test_paused_task_keeps_pumping_and_stop_still_works():
    import threading
    import time

    from jobpilot.platforms.base import StopRequested, StopSignal

    pumped = []
    stop = StopSignal()
    stop.add_pump(lambda s: (pumped.append(s), time.sleep(s), True)[-1])
    stop.pause()
    threading.Timer(1.2, stop.request).start()
    t0 = time.time()
    try:
        stop.check()
        raise AssertionError("应该被停止")
    except StopRequested:
        pass
    assert len(pumped) >= 2 and time.time() - t0 < 2.5
