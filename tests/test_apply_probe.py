"""前程无忧 / 猎聘的投递预演：只看不点。

这些用例验证的是「怎么解读页面」和「绝对不点、不投」；页面上的按钮到底叫什么，
要在真实浏览器里用你的账号预演出来，这里用假页面模拟几种典型情况。
"""
from __future__ import annotations

import pytest
from conftest import open_time_gates

from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import DriverContext, StopSignal
from jobpilot.platforms.job51 import JOB_URL as JOB51_URL
from jobpilot.platforms.job51 import Job51Driver
from jobpilot.platforms.liepin import JOB_URL as LIEPIN_URL
from jobpilot.platforms.liepin import LiepinDriver
from jobpilot.safety.policy import Policy


def ctx_for(tmp_path, platform, page):
    cfg = open_time_gates(AppConfig())
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    repo = Repo(connect(tmp_path / "t.db"))
    browser = FakeBrowser([page])
    return DriverContext(config=cfg, repo=repo, browser=browser, policy=Policy(repo, cfg),
                         stop=StopSignal(), platform=platform), browser


def page_with(url, report, text="AI 产品经理 上海"):
    # 预演脚本里一定有 "labels" 这个词：FakePage 按 eval 内容里的关键字返回预置结果
    return FakePage(url=url, text=text, selector_text={"body": ""}, evals={"labels": report})


DRIVERS = [
    (Job51Driver, "51job", JOB51_URL.format(job_id="123456")),
    (LiepinDriver, "liepin", LIEPIN_URL.format(job_id="654321")),
]


@pytest.mark.parametrize(("driver_cls", "platform", "url"), DRIVERS)
def test_finds_apply_button_and_never_clicks(tmp_path, driver_cls, platform, url):
    page = page_with(url, {"url": url, "title": "AI 产品经理", "loginOpen": False,
                           "apply": [{"text": "申请职位", "tag": "button", "cls": "btn apply", "disabled": False}],
                           "already": [], "chat": []})
    ctx, _ = ctx_for(tmp_path, platform, page)
    job = Job(id=f"{platform}-1", platform=platform, external_id="1", title="AI 产品经理", company="某公司", url=url)

    report = driver_cls().probe_apply(ctx, job)

    assert report["ok"] is True and "申请职位" in report["verdict"]
    assert report["sent_anything"] is False
    assert page.clicks == [] and page.fills == {}          # 预演绝不点、绝不填
    assert ctx.policy.budget.used(platform, "collect_detail") == 1   # 和采集详情页同一条额度
    assert page.closed


@pytest.mark.parametrize(("driver_cls", "platform", "url"), DRIVERS)
def test_login_wall_and_risk_are_reported_not_ignored(tmp_path, driver_cls, platform, url):
    job = Job(id=f"{platform}-1", platform=platform, external_id="1", title="t", company="c", url=url)
    login = page_with(url, {"url": url, "title": "登录", "loginOpen": True, "apply": [], "already": [], "chat": []})
    ctx, _ = ctx_for(tmp_path, platform, login)
    report = driver_cls().probe_apply(ctx, job)
    assert report["ok"] is False and report["need_login"] is True and "登录" in report["verdict"]

    risky = page_with(url, {"url": url, "title": "x", "loginOpen": False, "apply": [], "already": [], "chat": []},
                      text="请完成安全验证后继续访问")
    ctx, _ = ctx_for(tmp_path / "b", platform, risky)
    report = driver_cls().probe_apply(ctx, job)
    assert report["ok"] is False and report["risk"] == "captcha" and "停手" in report["verdict"]


def test_already_applied_and_chat_only_are_distinguished(tmp_path):
    url = JOB51_URL.format(job_id="9")
    job = Job(id="51job-9", platform="51job", external_id="9", title="t", company="c", url=url)
    done = page_with(url, {"url": url, "title": "t", "loginOpen": False, "apply": [],
                           "already": [{"text": "已申请", "tag": "span", "cls": "", "disabled": True}], "chat": []})
    ctx, _ = ctx_for(tmp_path, "51job", done)
    assert "已申请" in Job51Driver().probe_apply(ctx, job)["verdict"]

    chat = page_with(url, {"url": url, "title": "t", "loginOpen": False, "apply": [], "already": [],
                           "chat": [{"text": "立即沟通", "tag": "a", "cls": "", "disabled": False}]})
    ctx, _ = ctx_for(tmp_path / "b", "51job", chat)
    report = Job51Driver().probe_apply(ctx, job)
    assert report["ok"] is False and "聊天" in report["verdict"]


def test_probe_does_not_open_auto_apply():
    """预演只是攒证据：能力集里仍然没有 apply，任务层会继续拒绝自动投递。"""
    assert "apply" not in Job51Driver.capabilities
    assert "apply" not in LiepinDriver.capabilities
