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
    stop = StopSignal()
    stop.sleep = lambda seconds: None        # 「等结果」的等待在测试里不真的等
    return DriverContext(config=cfg, repo=repo, browser=browser, policy=Policy(repo, cfg),
                         stop=stop, platform=platform), browser


def page_with(url, report, text="AI 产品经理 上海"):
    # 预演脚本里一定有 "labels" 这个词：FakePage 按 eval 内容里的关键字返回预置结果
    return FakePage(url=url, text=text, selector_text={"body": ""}, evals={"labels": report})


DRIVERS = [
    (Job51Driver, "51job", JOB51_URL.format(job_id="123456")),
    (LiepinDriver, "liepin", LIEPIN_URL.format(job_id="654321")),
]


@pytest.mark.parametrize(("driver_cls", "platform", "url"), DRIVERS)
def test_finds_apply_button_and_never_clicks(tmp_path, driver_cls, platform, url):
    page = page_with(url, {"url": url, "title": "AI 产品经理", "loginModal": False, "loginLink": False,
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
    login = page_with(url, {"url": url, "title": "登录", "loginModal": True, "loginLink": False, "apply": [], "already": [], "chat": []})
    ctx, _ = ctx_for(tmp_path, platform, login)
    report = driver_cls().probe_apply(ctx, job)
    assert report["ok"] is False and report["need_login"] is True and "登录" in report["verdict"]

    risky = page_with(url, {"url": url, "title": "x", "loginModal": False, "loginLink": False, "apply": [], "already": [], "chat": []},
                      text="请完成安全验证后继续访问")
    ctx, _ = ctx_for(tmp_path / "b", platform, risky)
    report = driver_cls().probe_apply(ctx, job)
    assert report["ok"] is False and report["risk"] == "captcha" and "停手" in report["verdict"]


def test_already_applied_and_chat_only_are_distinguished(tmp_path):
    url = JOB51_URL.format(job_id="9")
    job = Job(id="51job-9", platform="51job", external_id="9", title="t", company="c", url=url)
    done = page_with(url, {"url": url, "title": "t", "loginModal": False, "loginLink": False, "apply": [],
                           "already": [{"text": "已申请", "tag": "span", "cls": "", "disabled": True}], "chat": []})
    ctx, _ = ctx_for(tmp_path, "51job", done)
    assert "已申请" in Job51Driver().probe_apply(ctx, job)["verdict"]

    chat = page_with(url, {"url": url, "title": "t", "loginModal": False, "loginLink": False, "apply": [], "already": [],
                           "chat": [{"text": "立即沟通", "tag": "a", "cls": "", "disabled": False}]})
    ctx, _ = ctx_for(tmp_path / "b", "51job", chat)
    report = Job51Driver().probe_apply(ctx, job)
    assert report["ok"] is False and "聊天" in report["verdict"]


def test_probe_does_not_open_auto_apply():
    """预演只是攒证据：没跑通真机投递的平台，能力集里没有 apply，任务层会拒绝自动投递。"""
    assert "apply" in Job51Driver.capabilities           # 2026-10-14 真机投递跑通后开放
    assert "apply" in LiepinDriver.capabilities


def test_header_login_link_that_disappears_is_not_called_logged_out(tmp_path):
    """前程无忧实测：详情页先渲染成未登录（页头有「登录/注册」），几秒后才换成用户名。
    第一次看到的是页头链接、不是弹窗时要再看一次，不能直接报「需要登录」。"""
    url = JOB51_URL.format(job_id="7")
    first = {"url": url, "title": "t", "loginModal": False, "loginLink": True, "apply": [], "already": [], "chat": []}
    later = {**first, "loginLink": False,
             "apply": [{"text": "立即投递", "tag": "div", "cls": "apply-btn-new big", "disabled": False}]}

    class LatePage(FakePage):
        def __init__(self):
            super().__init__(url=url, text="AI 产品经理", selector_text={"body": ""})
            self.calls = 0

        def eval(self, js):
            self.calls += 1
            return first if self.calls == 1 else later

    page = LatePage()
    ctx, _ = ctx_for(tmp_path, "51job", page)
    job = Job(id="51job-7", platform="51job", external_id="7", title="t", company="c", url=url)
    report = Job51Driver().probe_apply(ctx, job)
    assert page.calls == 2
    assert report["ok"] is True and report["need_login"] is False and "立即投递" in report["verdict"]
    assert ctx.policy.budget.used("51job", "collect_detail") == 1      # 再看一次不额外开页面


def test_liepin_resume_plus_chat_button_counts_as_apply():
    """猎聘的按钮是「投简历聊一聊」：投简历 + 开会话，判成投递按钮（顺序：已投 → 投递 → 聊天）。"""
    from jobpilot.platforms.apply_probe import LABELS

    text = "投简历聊一聊"
    assert not any(w in text for w in LABELS["already"])
    assert any(text == w or text.startswith(w) for w in LABELS["apply"])


# ---------------- 真正的一键投递（能力集还没放开；这里验证「只点一次」和结果判断） ----------------

class ScriptedPage(FakePage):
    """按顺序回放：第 1 次 eval = 投递前的页面扫描，之后的 eval 若含 node.click 记一次点击，其余 = 点完后的页面。"""

    def __init__(self, url, before, afters, text="AI 产品经理 上海"):
        super().__init__(url=url, text=text, selector_text={"body": ""})
        self.before, self.afters, self.click_count, self.after_calls = before, list(afters), 0, 0

    resume_result = None          # 「选择简历」弹窗脚本的返回值（None = 页面上没有这种弹窗）
    resume_calls = 0

    def eval(self, js):
        if "选择(附件)?简历" in js:
            self.resume_calls += 1
            return self.resume_result or {"clicked": False, "reason": "没找到选择简历的弹窗"}
        if "node.click()" in js:
            self.click_count += 1
            return True
        if "popups" in js:
            self.after_calls += 1
            return self.afters.pop(0) if len(self.afters) > 1 else self.afters[0]
        return self.before


BEFORE = {"loginModal": False, "loginLink": False, "already": [], "chat": [],
          "apply": [{"text": "立即投递", "tag": "div", "cls": "apply-btn-new big", "disabled": False}]}


def run_apply(tmp_path, page, platform="51job"):
    ctx, _ = ctx_for(tmp_path, platform, page)
    job = Job(id=f"{platform}-1", platform=platform, external_id="1", title="t", company="c", url=page.url)
    driver = Job51Driver() if platform == "51job" else LiepinDriver()
    return driver.apply(ctx, job), ctx


def test_apply_clicks_once_and_reads_success(tmp_path):
    page = ScriptedPage(JOB51_URL.format(job_id="1"), BEFORE, [{"popups": ["投递成功！HR 会尽快查看"], "buttons": []}])
    result, ctx = run_apply(tmp_path, page)
    assert (result.status, page.click_count) == ("applied", 1)
    assert ctx.policy.budget.used("51job", "apply") == 1               # 投递走自己的页面额度


def test_apply_never_clicks_twice_when_result_is_unclear(tmp_path):
    page = ScriptedPage(JOB51_URL.format(job_id="1"), BEFORE, [{"popups": [], "buttons": ["立即投递"]}])
    result, _ = run_apply(tmp_path, page)
    assert result.status == "failed" and "没有再点第二次" in result.detail
    assert page.click_count == 1 and page.after_calls == 8


def test_apply_skips_already_applied_and_missing_button_without_clicking(tmp_path):
    done = ScriptedPage(JOB51_URL.format(job_id="1"), {**BEFORE, "already": [{"text": "已申请"}]}, [{}])
    result, _ = run_apply(tmp_path, done)
    assert result.status == "already" and done.click_count == 0
    chat_only = ScriptedPage(JOB51_URL.format(job_id="1"), {**BEFORE, "apply": []}, [{}])
    result, _ = run_apply(tmp_path / "b", chat_only)
    assert result.status == "unsupported" and chat_only.click_count == 0


def test_apply_resume_popup_is_handed_back_to_the_user(tmp_path):
    # 前程无忧没开「用默认简历」：弹简历窗口就交还给用户，弹窗里什么都不点
    page = ScriptedPage(JOB51_URL.format(job_id="1"), BEFORE, [{"popups": ["请选择要投递的附件简历"], "buttons": []}])
    result, _ = run_apply(tmp_path, page)
    assert result.status == "need_resume" and page.click_count == 1 and page.resume_calls == 0


def test_liepin_uses_the_default_selected_resume_then_reads_success(tmp_path):
    page = ScriptedPage(LIEPIN_URL.format(job_id="1"), {**BEFORE, "apply": [{"text": "投简历", "disabled": False}]},
                        [{"popups": ["选择附件简历 招聘方将同时收到您的默认在线简历和附件简历"], "buttons": []},
                         {"popups": ["投递成功"], "buttons": []}])
    page.resume_result = {"clicked": True, "resume": "某某-简历"}
    result, _ = run_apply(tmp_path, page, "liepin")
    assert result.status == "applied" and "某某-简历" in result.detail
    assert page.click_count == 1 and page.resume_calls == 1          # 投简历一次、弹窗里投递一次


def test_liepin_odd_resume_dialog_is_handed_back_without_clicking(tmp_path):
    page = ScriptedPage(LIEPIN_URL.format(job_id="1"), {**BEFORE, "apply": [{"text": "投简历", "disabled": False}]},
                        [{"popups": ["选择附件简历"], "buttons": []}])
    page.resume_result = {"clicked": False, "reason": "默认选中的简历有 0 份，不确定该用哪份"}
    result, _ = run_apply(tmp_path, page, "liepin")
    assert result.status == "need_resume" and "0 份" in result.detail and page.resume_calls == 1


def test_apply_login_and_rate_limit_stop_the_platform(tmp_path):
    from jobpilot.models import LoginRequired, PlatformRisk

    login = ScriptedPage(JOB51_URL.format(job_id="1"), {**BEFORE, "loginModal": True}, [{}])
    with pytest.raises(LoginRequired):
        run_apply(tmp_path, login)
    assert login.click_count == 0
    limited = ScriptedPage(JOB51_URL.format(job_id="1"), BEFORE, [{"popups": ["今日投递次数已达上限"], "buttons": []}])
    with pytest.raises(PlatformRisk):
        run_apply(tmp_path / "b", limited)


def test_login_form_captcha_words_mean_login_not_risk(tmp_path):
    """没登录时页面里有「验证码登录」：应报「需要登录」，不能当成风控（风控会锁平台）。"""
    from jobpilot.platforms.zhaopin import ZhaopinDriver

    url = "https://www.zhaopin.com/jobdetail/X1.htm"
    page = page_with(url, {"url": url, "title": "t", "loginModal": False, "loginLink": True, "already": [], "chat": [],
                           "apply": [{"text": "立即投递", "tag": "button", "cls": "a-button", "disabled": False}]},
                     text="登录查看完整内容 手机号 验证码登录")
    ctx, _ = ctx_for(tmp_path, "zhaopin", page)
    job = Job(id="zhaopin-1", platform="zhaopin", external_id="1", title="t", company="c", url=url)
    report = ZhaopinDriver().probe_apply(ctx, job)
    assert report["need_login"] is True and report["risk"] == "login_required" and "登录" in report["verdict"]


def test_51job_unclear_click_is_verified_on_my_apply_page(tmp_path):
    """2026-10-14 真机：点完没有可识别的成功提示，但「我的申请」里有 —— 核对后算投递成功，且只点了一次。"""
    from jobpilot.platforms.job51 import in_apply_list

    url = JOB51_URL.format(job_id="173756213")
    job_page = ScriptedPage(url, BEFORE, [{"popups": [], "buttons": ["立即投递"]}])
    my_apply = FakePage(url=Job51Driver.MY_APPLY_URL, selector_text={"body": ""},
                        text="全部 / 数据专员(J0001) / 8千-1.2万 / 立即沟通 / 上海某医药销售有限公司 / 上海-静安区 / 申请于2026-10-14")
    ctx, _ = ctx_for(tmp_path, "51job", job_page)
    ctx.browser.pages.append(my_apply)
    job = Job(id="51job-173756213", platform="51job", external_id="173756213", title="数据专员(J0001)",
              company="上海某医药销售有限公司", url=url)
    result = Job51Driver().apply(ctx, job)
    assert result.status == "applied" and "核实" in result.detail
    assert job_page.click_count == 1                                   # 核对不会再点
    assert not in_apply_list(my_apply.text(), "数据专员(J0001)", "别的公司")


def test_51job_unclear_click_not_in_my_apply_stays_failed(tmp_path):
    url = JOB51_URL.format(job_id="1")
    job_page = ScriptedPage(url, BEFORE, [{"popups": [], "buttons": ["立即投递"]}])
    my_apply = FakePage(url=Job51Driver.MY_APPLY_URL, selector_text={"body": ""}, text="全部 / 别的岗位 / 别的公司")
    ctx, _ = ctx_for(tmp_path, "51job", job_page)
    ctx.browser.pages.append(my_apply)
    job = Job(id="51job-1", platform="51job", external_id="1", title="数据专员", company="某公司", url=url)
    assert Job51Driver().apply(ctx, job).status == "failed"


def test_liepin_unclear_click_is_verified_on_my_apply_page(tmp_path):
    url = LIEPIN_URL.format(job_id="1984127783")
    job_page = ScriptedPage(url, {**BEFORE, "apply": [{"text": "投简历", "disabled": False}]}, [{"popups": [], "buttons": ["投简历"]}])
    my_apply = FakePage(url=LiepinDriver.MY_APPLY_URL, selector_text={"body": ""},
                        text="我的投递 / 示例岗位 / 【北京】 / 10-20k / 某科技公司 / 已投递 / 2026-10-14 21:30")
    ctx, _ = ctx_for(tmp_path, "liepin", job_page)
    ctx.browser.pages.append(my_apply)
    job = Job(id="liepin-1984127783", platform="liepin", external_id="1984127783", title="示例岗位",
              company="某科技公司", url=url)
    result = LiepinDriver().apply(ctx, job)
    assert result.status == "applied" and "我的投递" in result.detail
    assert job_page.click_count == 1


LIEPIN_DOM = """<html><body><div class="job-apply-operate"><div class="apply-box">
  <a class="btn-minor" href="javascript:void(0)">投简历</a>
  <a class="btn-main" href="javascript:void(0)">聊一聊</a>
</div></div></body></html>"""


def test_container_with_buttons_inside_is_never_the_click_target():
    """2026-10-14 真机：猎聘 div.apply-box 的拼接文字「投简历聊一聊」被当成按钮，点在空白处没投出去。

    真浏览器里跑预演 / 点击脚本：只能认出、点中里面的「投简历」，绝不会点到「聊一聊」。
    """
    import json

    from jobpilot.platforms.apply_probe import JS_CLICK, JS_PROBE, LABELS

    sync_playwright = pytest.importorskip("patchright.sync_api").sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.set_content(LIEPIN_DOM)
            # 点到哪个链接就在 body 上记下它的 class（不用内联 onclick：页面脚本隔离下不一定执行）
            page.evaluate("document.querySelectorAll('a').forEach(a => a.addEventListener('click', "
                          "() => document.body.dataset.hit = a.className))")
            found = page.evaluate("(" + JS_PROBE + ")(" + json.dumps(LABELS, ensure_ascii=False) + ")")
            assert [b["text"] for b in found["apply"]] == ["投简历"]
            assert page.evaluate("(" + JS_CLICK + ")('投简历聊一聊')") is False   # 容器文字点不到任何东西
            assert page.evaluate("(" + JS_CLICK + ")('投简历')") is True
            assert page.evaluate("document.body.dataset.hit") == "btn-minor"
            browser.close()
    except Exception as exc:  # noqa: BLE001
        if "Executable doesn't exist" in str(exc) or "channel" in str(exc).lower() and "not found" in str(exc).lower():
            pytest.skip(f"这台机器没有 Chrome：{exc}")
        raise


RESUME_DIALOG = """<html><body><div class="ant-c-modal-wrap"><div class="ant-c-modal" role="dialog">
<div class="ant-c-modal-content">
  <div class="ant-c-modal-header"><div class="ant-c-modal-title"><span>选择附件简历</span>
    <span>招聘方将同时收到您的默认在线简历和附件简历</span></div></div>
  <div class="ant-c-modal-body"><div class="ant-c-radio-group">
    <label class="ant-c-radio-wrapper"><span class="ant-c-radio"><input class="ant-c-radio-input" type="radio" name="r" {c1}></span>
      <span><p>某某-简历</p><p>2026-10-09 13:36上传</p><div>预览</div></span></label>
    <label class="ant-c-radio-wrapper"><span class="ant-c-radio"><input class="ant-c-radio-input" type="radio" name="r"></span>
      <span><p>某某-简历-AI金融数据v3</p><div>预览</div></span></label>
    <label class="ant-c-radio-wrapper"><span class="ant-c-radio"><input class="ant-c-radio-input" type="radio" name="r"></span>
      <span><p>某某-简历-AI数据分析</p><div>预览</div></span></label>
  </div></div>
  <div class="ant-c-modal-footer"><button type="button" class="btn-apply ant-c-btn ant-c-btn-primary"><span>立即投递</span></button>{extra}</div>
</div></div></div></body></html>"""


@pytest.mark.parametrize(("c1", "extra", "clicked"), [
    ("checked", "", True),                                   # 2026-10-14 真机弹窗的结构（antd）：默认选中第一份、一个「立即投递」
    ("", "", False),                                         # 没有默认选中：不猜
    ("checked", '<button class="btn-x">投递</button>', False),  # 两个投递按钮：不猜
])
def test_resume_dialog_script_in_a_real_browser(c1, extra, clicked):
    from jobpilot.platforms.apply_probe import JS_RESUME_CONFIRM

    sync_playwright = pytest.importorskip("patchright.sync_api").sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content(RESUME_DIALOG.format(c1=c1, extra=extra))
        page.evaluate("document.querySelectorAll('button').forEach(b => b.addEventListener('click', "
                      "() => document.body.dataset.hit = b.className))")
        out = page.evaluate(JS_RESUME_CONFIRM)
        hit = page.evaluate("document.body.dataset.hit || ''")
        browser.close()
    assert out["clicked"] is clicked
    if clicked:
        assert hit.startswith("btn-apply") and out["resume"].startswith("某某-简历") and "AI" not in out["resume"]
    else:
        assert hit == ""
