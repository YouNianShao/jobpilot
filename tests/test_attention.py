"""遇到验证 / 掉登录：页面留着、切到前台、看板提醒、「我已处理，继续」只解验证码锁（降速保留）。"""
from __future__ import annotations

import pytest
from conftest import open_time_gates
from fastapi.testclient import TestClient

import jobpilot.safety.attention as attention
from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.platforms.liepin import LiepinDriver
from jobpilot.platforms.registry import Registry
from jobpilot.safety.policy import GLOBAL_LOCK_KEY, Policy
from jobpilot.services.applier import apply_jobs
from jobpilot.services.collector import collect
from jobpilot.services.context import ServiceContext

CAPTCHA_TEXT = "请完成安全验证 拖动滑块"


class OnePageBrowser(FakeBrowser):
    """不管开什么网址都落到同一个页面（模拟整页被换成验证页）。带 cdp_url：像真的调试浏览器那样会去切标签页。"""

    cdp_url = "http://127.0.0.1:1"      # 不会真的连：activate_tab 在用例里被替换掉

    def __init__(self, page: FakePage) -> None:
        super().__init__([page])
        self.page = page

    def open(self, url: str) -> FakePage:
        self.page.goto(url)
        return self.page

    def close(self) -> None:            # 真驱动的 close() 只断开连接，不关标签页
        pass


@pytest.fixture
def rig(tmp_path, monkeypatch):
    cfg = open_time_gates(AppConfig())
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.delivery_cooldown_min_minutes = 0
    cfg.safety.delivery_cooldown_max_minutes = 0
    cfg.platforms["liepin"].keywords = ["AI"]
    cfg.platforms["liepin"].max_pages = 1
    repo = Repo(connect(tmp_path / "attn.db"))
    activated: list[tuple[str, str]] = []
    # 绝不碰真实网络 / 真实 Chrome：记下调用就行
    monkeypatch.setattr(attention, "activate_tab", lambda cdp, url, timeout=3.0: activated.append((cdp, url)) or True)

    def make(page: FakePage) -> ServiceContext:
        browser = OnePageBrowser(page)
        return ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg),
                              stop=StopSignal(), browser_factory=lambda: browser)

    yield cfg, repo, make, activated
    repo.conn.close()


def _registry() -> Registry:
    registry = Registry()
    registry.register(LiepinDriver())
    return registry


def test_captcha_in_collect_keeps_page_open_activates_it_and_flags_attention(rig):
    cfg, repo, make, activated = rig
    page = FakePage(url="", text=CAPTCHA_TEXT)
    ctx = make(page)
    out = collect(ctx, "liepin", registry=_registry())

    assert out["blocked"] == "captcha"
    assert page.closed is False                      # 那一页留给你处理
    item = attention.get(repo, "liepin")
    assert item["kind"] == "captcha" and item["url"] and "liepin" in item["url"]
    assert activated == [(OnePageBrowser.cdp_url, item["url"])]
    assert ctx.policy.locks.active("liepin")["kind"] == "captcha"


def test_rate_limit_still_closes_page_and_raises_no_attention(rig):
    cfg, repo, make, activated = rig
    page = FakePage(url="", text="访问过于频繁，请稍后再试")
    out = collect(make(page), "liepin", registry=_registry())
    assert out["blocked"] == "rate_limit"
    assert page.closed is True                       # 限流时页面上没什么可做的，照常关
    assert attention.get(repo, "liepin") is None and activated == []


def test_login_wall_in_collect_flags_login_attention(rig):
    cfg, repo, make, activated = rig
    page = FakePage(url="", text="")
    page.goto = lambda url: setattr(page, "_url", "https://passport.liepin.com/login?from=x")
    out = collect(make(page), "liepin", registry=_registry())
    assert out["login_required"] is True
    assert page.closed is False
    assert attention.get(repo, "liepin")["kind"] == "login"
    assert len(activated) == 1


def test_resume_clears_captcha_lock_but_keeps_penalty(rig):
    cfg, repo, make, _ = rig
    ctx = make(FakePage(text=CAPTCHA_TEXT))
    collect(ctx, "liepin", registry=_registry())
    assert ctx.policy.penalty_state("liepin") is not None

    ok, message = attention.resume(repo, ctx.policy, "liepin")
    assert ok and "已解除" in message
    assert ctx.policy.locks.active("liepin") is None
    assert attention.get(repo, "liepin") is None
    assert ctx.policy.penalty_factor("liepin") > 1.0     # 降速保留
    kinds = [row["kind"] for row in repo.conn.execute("SELECT kind FROM events")]
    assert "risk_resume" in kinds


def test_resume_refuses_rate_limit_lock(rig):
    cfg, repo, make, _ = rig
    ctx = make(FakePage())
    ctx.policy.record_risk("liepin", "rate_limit", "访问过于频繁")
    ok, message = attention.resume(repo, ctx.policy, "liepin")
    assert not ok and "限流" in message and "分钟" in message
    assert ctx.policy.locks.active("liepin") is not None


def test_resume_refuses_while_global_lock_active(rig):
    cfg, repo, make, _ = rig
    ctx = make(FakePage())
    ctx.policy.record_risk("liepin", "captcha", "验证")
    ctx.policy.locks.lock(GLOBAL_LOCK_KEY, "两个平台都被拦", "multi_platform", minutes=60)
    ok, message = attention.resume(repo, ctx.policy, "liepin")
    assert not ok and "整体冷却" in message
    assert ctx.policy.locks.active("liepin") is not None


def test_login_attention_cleared_by_resume_without_lock(rig):
    cfg, repo, make, _ = rig
    ctx = make(FakePage())
    from jobpilot.models import LoginRequired

    attention.flag(repo, "liepin", LoginRequired("猎聘需要登录"))
    ok, _ = attention.resume(repo, ctx.policy, "liepin")
    assert ok and attention.get(repo, "liepin") is None
    # 没有待处理时再点：说清楚，不假装成功
    ok, message = attention.resume(repo, ctx.policy, "liepin")
    assert not ok and "没有" in message


def test_captcha_attention_disappears_when_lock_expires(rig):
    cfg, repo, make, _ = rig
    ctx = make(FakePage(text=CAPTCHA_TEXT))
    collect(ctx, "liepin", registry=_registry())
    assert attention.active(repo, ctx.policy, ["liepin"])
    repo.clear_lock("liepin")                          # 等同于锁到期
    assert attention.active(repo, ctx.policy, ["liepin"]) == []
    assert attention.get(repo, "liepin") is None


def test_captcha_on_apply_page_keeps_it_open(rig):
    cfg, repo, make, activated = rig
    cfg.platforms["liepin"].apply = True
    repo.upsert_job(Job(id="liepin-1", platform="liepin", external_id="1", title="AI 产品", company="甲",
                        url="https://www.liepin.com/job/1.shtml"))
    repo.set_status("liepin-1", Status.APPROVED.value)
    page = FakePage(text=CAPTCHA_TEXT)
    out = apply_jobs(make(page), "liepin", registry=_registry())
    assert "风控" in out["stopped"]
    assert page.closed is False
    assert attention.get(repo, "liepin")["url"] == "https://www.liepin.com/job/1.shtml"
    assert activated and activated[0][1] == "https://www.liepin.com/job/1.shtml"


def test_status_and_resume_endpoint(tmp_path):
    from jobpilot.bootstrap import build_context
    from jobpilot.web.app import create_app

    db = tmp_path / "web.db"
    repo = Repo(connect(db))
    policy = Policy(repo, AppConfig())
    policy.record_risk("liepin", "captcha", "猎聘搜索页")
    from jobpilot.models import PlatformRisk

    attention.flag(repo, "liepin", PlatformRisk("captcha", "猎聘搜索页"))
    # 直接上锁：再走一次 record_risk 就是「一天两个平台」，会触发整体锁（那是另一条用例）
    policy.locks.lock("zhaopin", "访问过于频繁", "rate_limit", minutes=30)
    repo.conn.close()

    http = TestClient(create_app(lambda: build_context(AppConfig(), repo=Repo(connect(db)))),
                      base_url="http://127.0.0.1:8700")
    items = http.get("/api/status").json()["attention"]
    assert [(i["platform"], i["kind"]) for i in items] == [("liepin", "captcha")]

    bad = http.post("/api/risk/resume", json={"platform": "zhaopin"})
    assert bad.status_code == 409 and "等锁到期" in bad.json()["error"]
    assert http.post("/api/risk/resume", json={"platform": "nope"}).status_code == 404

    good = http.post("/api/risk/resume", json={"platform": "liepin"})
    assert good.status_code == 200 and good.json()["ok"] is True
    status = http.get("/api/status").json()
    assert status["attention"] == [] and "liepin" not in status["locks"]
    assert status["platforms"]["liepin"]["penalty"] is not None


def test_activate_tab_picks_matching_target(monkeypatch):
    """/json/list 里找网址对得上的那一页，再调 /json/activate/<id>（urlopen 被替换，不走网络）。"""
    import io
    import json as _json

    calls = []
    targets = [
        {"id": "A", "type": "page", "url": "https://www.zhipin.com/"},
        {"id": "B", "type": "page", "url": "https://www.liepin.com/zhaopin/?key=AI"},
        {"id": "C", "type": "service_worker", "url": "https://www.liepin.com/sw.js"},
    ]

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=0):
        calls.append(url)
        return Resp(_json.dumps(targets).encode() if url.endswith("/json/list") else b"ok")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert attention.activate_tab("http://127.0.0.1:9999", "https://www.liepin.com/zhaopin/?key=AI")
    assert calls[-1] == "http://127.0.0.1:9999/json/activate/B"
    # 网址又跳过一次：按同一个网站兜底
    assert attention.activate_tab("http://127.0.0.1:9999", "https://www.liepin.com/other")
    assert calls[-1] == "http://127.0.0.1:9999/json/activate/B"
    assert not attention.activate_tab("http://127.0.0.1:9999", "https://www.51job.com/x")
