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


def test_liepin_reads_the_jd_page_before_leaving(monkeypatch):
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

        def reserve(self, stage):
            pass

        def log(self, msg):
            pass

    text, _ = LiepinDriver()._fetch_jd(Ctx(), Page(), "https://www.liepin.com/job/1.shtml")
    assert text.startswith("岗位职责") and any("scrollBy" in c for c in calls)
    assert slept and 2.0 <= slept[-1] <= 4.5
