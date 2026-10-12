"""BOSS 直聘驱动测试（纯逻辑 + 假浏览器，不需要 Chrome）。

选择器依据：docs/05-平台适配手册.md 第 5 节（2026-10-10 实机核验）。
"""
from __future__ import annotations

import json

import pytest
from conftest import open_time_gates

from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job, LoginRequired, PlatformRisk
from jobpilot.platforms.base import DriverContext, StopSignal
from jobpilot.platforms.boss import (
    JOB_URL,
    BossDriver,
    city_code,
    job_id_from_url,
    parse_detail,
    parse_list,
    search_url,
)
from jobpilot.safety.policy import Policy
from jobpilot.safety.risk import classify_risk

LIST_FIXTURE = [
    {"title": "AI 产品经理", "company": "某某科技", "location": "上海·浦东新区·陆家嘴",
     "experience": "3-5年", "education": "本科", "url": "/job_detail/abc123.html"},
]
DETAIL_FIXTURE = {
    "title": "AI 产品经理", "salary": "20-25K", "jd": "负责 AI 产品规划与落地",
    "company": "某某科技", "hr_name": "李女士", "chat_button": True,
}


def make_ctx(tmp_path, browser, *, search_limit=10, detail_limit=10, keywords=None):
    conn = connect(tmp_path / "t.db")
    repo = Repo(conn)
    cfg = AppConfig()
    boss = cfg.platforms["boss"]
    boss.enabled = True
    boss.cities = ["上海"]
    boss.keywords = keywords or ["AI"]
    boss.max_pages = 1
    cfg.safety.daily_pages.collect_search = search_limit
    cfg.safety.daily_pages.collect_detail = detail_limit
    cfg.safety.platforms = {}          # 关闭每平台覆盖，用本用例显式设的预算
    cfg.safety.page_interval_min_seconds = 0   # 测试里不等待
    cfg.safety.page_interval_max_seconds = 0
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    return DriverContext(
        config=cfg, repo=repo, browser=browser,
        policy=Policy(repo, cfg), stop=StopSignal(), platform="boss",
    )


def search_page(**kw) -> FakePage:
    kw.setdefault("evals", {"job-card-wrap": json.dumps(LIST_FIXTURE)})
    kw.setdefault("selector_text", {".job-card-wrap": ""})
    return FakePage(url=search_url("AI", "101020100", 1), **kw)


def detail_page(**kw) -> FakePage:
    kw.setdefault("evals", {"job-sec-text": json.dumps(DETAIL_FIXTURE)})
    kw.setdefault("selector_text", {".job-sec-text": ""})
    return FakePage(url=JOB_URL.format(job_id="abc123"), **kw)


# ---------------- 纯函数 ----------------
def test_city_codes_are_boss_nine_digit():
    assert city_code("上海") == "101020100"
    assert city_code("北京") == "101010100"
    assert city_code("不存在的城市") is None


def test_job_id_and_search_url():
    assert job_id_from_url("/job_detail/5df0a3.html") == "5df0a3"
    assert job_id_from_url("https://x/job_detail/abc123.html?ka=1") == "abc123"
    assert job_id_from_url("") == ""
    assert search_url("AI", "101020100", 1) == "https://www.zhipin.com/web/geek/jobs?query=AI&city=101020100"
    assert search_url("AI", "101020100", 2).endswith("&page=2")


def test_parse_helpers_are_defensive():
    assert parse_list(json.dumps(LIST_FIXTURE))[0]["title"] == "AI 产品经理"
    assert parse_list("not json") == []
    assert parse_list(None) == []
    assert parse_detail(json.dumps(DETAIL_FIXTURE))["salary"] == "20-25K"
    assert parse_detail("{bad") == {}


def test_classify_risk():
    assert classify_risk("", "https://www.zhipin.com/web/user/?ka=header-login") == "login_required"
    assert classify_risk("请完成验证后继续") == "captcha"
    assert classify_risk("您的账号存在异常") == "blocked"
    assert classify_risk("操作过于频繁，请稍后再试") == "rate_limit"
    assert classify_risk("AI 产品经理 20-25K 上海") is None


# ---------------- 采集流程 ----------------
def test_collect_inserts_job_with_detail_fields(tmp_path):
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    result = BossDriver().collect(ctx)

    assert result.new == 1
    assert result.seen == 1
    job = ctx.repo.get_job("boss-abc123")
    assert job is not None
    assert job.title == "AI 产品经理"
    assert job.salary == "20-25K"          # 来自详情页（列表页薪资不可用）
    assert job.jd.startswith("负责 AI 产品")
    assert job.city == "上海"               # 从 location 按 · 取首段
    assert job.url.endswith("/job_detail/abc123.html")


def test_collect_dedups_on_second_run(tmp_path):
    """岗位级去重：允许重复采同一个词时，靠 job id 兜底，不会重复入库。"""
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    ctx.config.safety.recollect_after_hours = 0        # 关掉词级去重，单测岗位去重
    driver = BossDriver()
    driver.collect(ctx)
    again = driver.collect(ctx)
    assert again.new == 0
    assert again.duplicate == 1
    assert len(ctx.repo.list_jobs(platform="boss")) == 1


def test_recently_collected_word_is_skipped(tmp_path):
    """同一个 城市+关键词 在窗口内不再重复开搜索页 —— 省下的是真的页面预算。"""
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    driver = BossDriver()

    first = driver.collect(ctx)
    assert first.new == 1 and first.skipped == 0
    used = ctx.policy.budget.used("boss", "collect_search")

    second = driver.collect(ctx)
    assert second.skipped == 1                 # 明确计数，不是悄悄不动
    assert second.new == 0
    assert ctx.policy.budget.used("boss", "collect_search") == used    # 一个页面都没多开


def test_recollect_window_zero_means_always_collect(tmp_path):
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    ctx.config.safety.recollect_after_hours = 0
    driver = BossDriver()
    driver.collect(ctx)
    again = driver.collect(ctx)
    assert again.skipped == 0
    assert ctx.policy.budget.used("boss", "collect_search") == 2       # 允许重复开


def test_collect_stops_gracefully_when_search_budget_runs_out(tmp_path):
    # 两个关键词但搜索预算只有 1 → 第二个关键词被预算拦下，优雅停止
    ctx = make_ctx(
        tmp_path,
        FakeBrowser([search_page(), detail_page()]),
        search_limit=1,
        keywords=["AI", "数据分析"],
    )
    result = BossDriver().collect(ctx)
    assert result.new == 1
    assert result.stopped                      # 明确记录停止原因


def test_collect_uses_detail_budget(tmp_path):
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]), detail_limit=1)
    BossDriver().collect(ctx)
    assert ctx.policy.budget.used("boss", "collect_detail") == 1


def test_collect_fails_closed_on_captcha(tmp_path):
    page = FakePage(url=search_url("AI", "101020100", 1), text="请完成验证后继续")
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(PlatformRisk) as exc:
        BossDriver().collect(ctx)
    assert exc.value.kind == "captcha"


def test_collect_fails_closed_on_login_wall(tmp_path):
    # 按搜索 URL 打开，但被重定向到登录墙 → page.url 是登录页
    page = FakePage(
        url="https://www.zhipin.com/web/user/?ka=header-login",
        match_url=search_url("AI", "101020100", 1),
    )
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(LoginRequired):
        BossDriver().collect(ctx)


def test_rate_window_stops_collect_gracefully(tmp_path):
    """真机路径上的速率闸门：短时间开页面太多、又等不起时，采集优雅停手。

    注意这里要的是"按设计停手"，不是抛异常——一条吓人的失败记录会让用户
    以为程序坏了，而实际是安全内核在保护账号。
    """
    ctx = make_ctx(
        tmp_path,
        FakeBrowser([search_page(), detail_page(), search_page()]),
        keywords=["AI", "数据分析", "Python"],
    )
    ctx.config.safety.max_pages_per_10min = 2      # 只允许开 2 页（搜索页 + 详情页）
    ctx.config.safety.max_pages_per_hour = 0       # 只测 10 分钟窗
    ctx.config.safety.rate_wait_max_seconds = 0    # 不允许等待
    result = BossDriver().collect(ctx)

    assert result.new == 1                          # 已经采到的岗位保留
    assert "上限" in result.stopped                 # 明确告诉用户是被速率窗口停下的
    assert ctx.policy.budget.used("boss", "collect_search") == 1   # 被拦下的那次没白吃额度


def test_capabilities_only_declare_verified_actions(tmp_path):
    """capabilities 就是「实机跑通过什么」的清单，多写一个都算撒谎。
    采集 / 投递 2026-10-10 核验；聊天监测 2026-10-11 核验（只读列表）。"""
    driver = BossDriver()
    assert driver.capabilities == frozenset({"collect", "apply"})
    ctx = make_ctx(tmp_path, FakeBrowser([]))
    assert driver.apply(ctx, None).status == "unsupported"

def test_city_table_covers_the_whole_country():
    """城市表以前只有 20 个，别的城市会被静默跳过 —— 现在应该覆盖全国。"""
    from jobpilot.platforms.boss import CITY_CODES, HOT_CITIES

    assert len(CITY_CODES) > 300
    assert CITY_CODES["宁波"] == "101210400"          # 老表里没有的城市
    assert CITY_CODES["三亚"] == "101310200"
    assert len(HOT_CITIES) >= 20 and all(name in CITY_CODES for name in HOT_CITIES)
    # 界面靠这两个属性判断"城市是否受限"
    assert BossDriver.supported_cities is CITY_CODES
    assert len(BossDriver.hot_cities) >= 20


def test_city_code_accepts_raw_code_or_name():
    assert city_code("宁波") == "101210400"
    assert city_code("  上海 ") == "101020100"
    assert city_code("101020100") == "101020100"      # 直接粘城市码也认
    assert city_code("火星") is None
    assert city_code("") is None

def test_collect_reports_when_no_city_is_supported(tmp_path):
    """城市全填错时不能静默"完成"，要说清是城市不在内置表里。"""
    ctx = make_ctx(tmp_path, FakeBrowser([search_page()]), keywords=["AI"])
    ctx.config.platforms["boss"].cities = ["火星", "月球"]
    result = BossDriver().collect(ctx)
    assert result.new == 0
    assert "内置表" in result.stopped
    assert ctx.policy.budget.used("boss", "collect_search") == 0     # 一个页面都没开

def test_fetch_detail_switch_is_honored(tmp_path):
    """「打开详情页抓 JD」以前对 BOSS 是硬开的，现在必须真的能关。"""
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    ctx.config.platforms["boss"].fetch_detail = False
    result = BossDriver().collect(ctx)

    assert result.new == 1
    assert ctx.policy.budget.used("boss", "collect_detail") == 0     # 一个详情页都没开
    assert ctx.repo.get_job("boss-abc123").salary == ""             # 详情页才有的字段
    assert ctx.repo.get_job("boss-abc123").title == "AI 产品经理"    # 列表页字段仍然有

def test_probe_apply_reports_button_without_sending(tmp_path):
    """投递预演：找到按钮就报"结构支持"，且明确没有任何东西被发出去。"""
    url = JOB_URL.format(job_id="abc123")
    page = FakePage(
        url=url, text="AI 产品经理 20-25K 上海",
        selector_text={".job-sec-text": "", ".btn-startchat": ""},
        evals={"btn-startchat": {
            "url": url, "title": "AI 产品经理 - BOSS直聘",
            "chatButton": {"selector": ".btn-startchat", "text": "立即沟通", "visible": True},
            "chatInput": None, "needLogin": False,
        }},
    )
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    job = Job(id="boss-abc123", platform="boss", external_id="abc123",
              title="AI 产品经理", company="某某科技", url=url)

    report = BossDriver().probe_apply(ctx, job)

    assert report["ok"] is True
    assert report["sent_anything"] is False          # 预演绝不发消息
    assert "结构支持" in report["verdict"]
    assert ctx.policy.budget.used("boss", "collect_detail") == 1    # 和采集详情页同一条额度


def test_probe_apply_reports_missing_button(tmp_path):
    page = FakePage(url=JOB_URL.format(job_id="abc123"), selector_text={".job-sec-text": ""},
                    evals={"btn-startchat": {"url": "x", "title": "t", "chatButton": None,
                                             "chatInput": None, "needLogin": False}})
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    job = Job(id="boss-abc123", platform="boss", external_id="abc123",
              title="AI 产品经理", company="某某科技", url=JOB_URL.format(job_id="abc123"))

    report = BossDriver().probe_apply(ctx, job)
    assert report["ok"] is False
    assert "没找到" in report["verdict"]

def test_recollect_window_is_per_page(tmp_path):
    """按页记账：第 1 页已采过就只跳过第 1 页，第 2 页照常采。

    这条是拿真实浪费换来的：采集被中途停止时，如果只按"整个关键词"记账，
    下次会把整轮页面预算再花一遍（当时白开了 10 个详情页）。
    """
    # FakeBrowser.open 是按 URL 匹配的，所以要准备"第 2 页"那个 URL 的假页面
    page_two = FakePage(url=search_url("AI", "101020100", 2),
                        evals={"job-card-wrap": json.dumps(LIST_FIXTURE)},
                        selector_text={".job-card-wrap": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([page_two, detail_page()]), keywords=["AI"])
    ctx.config.platforms["boss"].max_pages = 2
    ctx.note_collect("上海|AI", 1)          # 假装第 1 页已经采完

    result = BossDriver().collect(ctx)

    assert result.skipped == 1                                      # 只跳过第 1 页
    assert ctx.policy.budget.used("boss", "collect_search") == 1     # 只开了第 2 页
    assert result.new == 1

def apply_page(button_text="立即沟通", panel_text="谢女士 已发送 看到贵司的这个职位"):
    """假岗位页：把 apply 流程要用的选择器都摆上。"""
    return FakePage(
        url=JOB_URL.format(job_id="abc123"),
        selector_text={".job-sec-text": "", ".btn-startchat": button_text,
                       ".dialog-wrap.startchat-dialog": panel_text,
                       "textarea.input-area": "", "div.send-message": "发送"},
        evals={"job-sec-text": json.dumps(DETAIL_FIXTURE)},
    )


def make_job(greeting="你好，我做过 6 年数据分析，想聊聊这个岗位。"):
    return Job(id="boss-abc123", platform="boss", external_id="abc123",
               title="AI 产品经理", company="某某科技",
               url=JOB_URL.format(job_id="abc123"), greeting=greeting)


def test_apply_clicks_then_sends_greeting(tmp_path):
    page = apply_page()
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    result = BossDriver().apply(ctx, make_job())

    assert result.status == "applied"
    assert page.clicks[0] == ".btn-startchat"          # 先点沟通
    assert ".btn-startchat" in page.clicks
    assert "div.send-message" in page.clicks           # 再点发送
    assert page.fills.get("textarea.input-area", "").startswith("你好")
    assert ctx.policy.budget.used("boss", "apply") == 1


def test_apply_skips_when_already_in_touch(tmp_path):
    """按钮已经是「继续沟通」= 早就聊过 —— 绝不重复发。"""
    page = apply_page(button_text="继续沟通")
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    result = BossDriver().apply(ctx, make_job())

    assert result.status == "already"
    assert page.clicks == []                           # 一个点击都没有


def test_apply_without_greeting_still_applies(tmp_path):
    """没有个性化招呼语时，"点一下立即沟通"本身就是完整投递。"""
    page = apply_page()
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    result = BossDriver().apply(ctx, make_job(greeting=""))

    assert result.status == "applied"
    assert page.clicks == [".btn-startchat"]           # 只点沟通，不点发送


def test_apply_gate_denial_after_default_greeting_counts_as_applied(tmp_path):
    """点「立即沟通」时平台已经替你发了默认招呼语 —— 这时候个性化那条被风控闸门拦下，
    只能记 applied。记 failed 会谎报「投递失败」，还会把熔断计数推上去。"""
    from datetime import datetime

    page = apply_page()
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    start = (datetime.now().hour + 1) % 24
    ctx.config.safety.send_windows = [f"{start:02d}:00-{(start + 1) % 24:02d}:00"]

    result = BossDriver().apply(ctx, make_job())

    assert result.status == "applied"
    assert ".btn-startchat" in page.clicks            # 默认招呼语那一下确实点了
    assert "div.send-message" not in page.clicks      # 个性化那条没发出去


# ---------------- 列表页预筛：判得掉的岗位不开详情页 ----------------
INTERN_ITEM = {"title": "AI 产品实习生", "company": "某某科技", "location": "上海·徐汇区",
               "experience": "在校/应届", "education": "本科", "url": "/job_detail/intern1.html"}


def test_list_prefilter_skips_detail_page_for_intern_title(tmp_path):
    """实习岗只看标题就能判掉：不开详情页、不占详情页预算，但入库留痕（下次也不再开）。"""
    page = search_page(evals={"job-card-wrap": json.dumps([INTERN_ITEM, *LIST_FIXTURE])})
    ctx = make_ctx(tmp_path, FakeBrowser([page, detail_page()]))
    result = BossDriver().collect(ctx)

    assert result.filtered == 1
    assert result.new == 1                                      # 正常岗位照常开详情页入库
    assert ctx.policy.budget.used("boss", "collect_detail") == 1
    intern = ctx.repo.get_job("boss-intern1")
    assert intern is not None and intern.status == "rejected" and intern.jd == ""
    kinds = [event["kind"] for event in ctx.repo.list_events("boss-intern1")]
    assert "prefilter_rejected" in kinds


def test_list_prefilter_honors_deal_breakers(tmp_path):
    ctx = make_ctx(tmp_path, FakeBrowser([search_page(), detail_page()]))
    ctx.config.profile.deal_breakers = ["产品经理"]
    result = BossDriver().collect(ctx)

    assert result.filtered == 1 and result.new == 0
    assert ctx.policy.budget.used("boss", "collect_detail") == 0


def test_list_prefiltered_job_is_not_reopened_next_run(tmp_path):
    page = search_page(evals={"job-card-wrap": json.dumps([INTERN_ITEM])})
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    ctx.config.safety.recollect_after_hours = 0
    driver = BossDriver()
    driver.collect(ctx)
    again = driver.collect(ctx)

    assert again.duplicate == 1 and again.filtered == 0
    assert ctx.policy.budget.used("boss", "collect_detail") == 0


# ---------------- 掉登录：被跳回首页（2026-10-12 实测） ----------------
def test_redirect_to_homepage_means_logged_out_not_page_error(tmp_path):
    """BOSS 掉登录时搜索页跳回 zhipin.com/，网址里没有 /login —— 以前被当成「页面异常」，连续 3 次锁一小时。"""
    page = FakePage(url="https://www.zhipin.com/", text="上海[切换] 首页 职位 公司 我要招聘 我要找工作 登录/注册",
                    match_url=search_url("AI", "101020100", 1))
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(LoginRequired, match="重新登录"):
        BossDriver().collect(ctx)


def test_login_register_header_means_logged_out(tmp_path):
    page = search_page(selector_text={}, text="首页 职位 公司 登录/注册 热门职位 Java Python")
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(LoginRequired):
        BossDriver().collect(ctx)


def test_collect_service_does_not_lock_on_logged_out(tmp_path):
    """经过采集服务：掉登录只停下并提示，不计页面异常、不上锁、不降速。"""
    from jobpilot.services.collector import collect

    page = FakePage(url="https://www.zhipin.com/", text="登录/注册", match_url=search_url("AI", "101020100", 1))
    ctx = make_ctx(tmp_path, FakeBrowser([page]))

    class Svc:
        config, repo, policy = ctx.config, ctx.repo, ctx.policy

        def log(self, message):
            pass

        def create_browser(self):
            return FakeBrowser([page])

        def driver_context(self, browser, platform):
            return ctx

    result = collect(Svc(), "boss", registry={"boss": BossDriver()})
    assert result["login_required"] is True
    assert ctx.policy.page_failures("boss") == 0
    assert ctx.policy.locks.active("boss") is None
    assert not ctx.repo.get_kv("penalty:boss")


def test_homepage_redirect_while_logged_in_is_not_called_logout(tmp_path):
    """已登录（页头没有「登录/注册」）却跳回首页：不能硬说成掉登录，仍按页面异常。"""
    from jobpilot.models import PageReadError

    page = FakePage(url="https://www.zhipin.com/", text="首页 职位 公司 消息 简历",
                    match_url=search_url("AI", "101020100", 1))
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(PageReadError):
        BossDriver().collect(ctx)


def test_logged_in_page_without_cards_is_still_a_page_error(tmp_path):
    """登录正常、只是没等到卡片（改版 / 加载慢）：仍按页面异常处理，别把所有失败都说成掉登录。"""
    from jobpilot.models import PageReadError

    page = search_page(selector_text={}, text="首页 职位 公司 消息 简历 张先生")
    ctx = make_ctx(tmp_path, FakeBrowser([page]))
    with pytest.raises(PageReadError):
        BossDriver().collect(ctx)
