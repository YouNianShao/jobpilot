"""JD 抓取开关：开了才开详情页，且受详情页预算约束。"""
from __future__ import annotations

import json

from conftest import open_time_gates

from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.platforms.base import DriverContext, StopSignal
from jobpilot.platforms.job51 import Job51Driver
from jobpilot.platforms.job51 import search_url as j51_search
from jobpilot.platforms.liepin import LiepinDriver
from jobpilot.platforms.zhaopin import ZhaopinDriver
from jobpilot.platforms.zhaopin import search_url as zp_search
from jobpilot.safety.policy import Policy

JD_TEXT = "工作职责：负责经营分析与数据建模，输出可视化分析报告与决策建议。"

LIST_51 = [{"jobId": "173851192", "title": "数据分析师", "company": "甲公司",
            "salary": "1.5-2.5万", "area": "上海·徐汇区", "url": "https://jobs.51job.com/all/173851192.html"}]
LIST_ZP = [{"number": "CCL1493228500J40928543912", "title": "数据分析师", "company": "甲公司",
            "salary": "9000-11000元", "location": "上海 徐汇", "tags": ["本科", "Sql"]}]


def make_ctx(tmp_path, browser, platform, *, fetch_detail, detail_limit=5):
    cfg = AppConfig()
    cfg.platforms[platform].enabled = True
    cfg.platforms[platform].cities = ["上海"]
    cfg.platforms[platform].keywords = ["数据分析"]
    cfg.platforms[platform].max_pages = 1
    cfg.platforms[platform].fetch_detail = fetch_detail
    cfg.safety.platforms = {}          # 用显式设的预算
    cfg.safety.daily_pages.collect_search = 5
    cfg.safety.daily_pages.collect_detail = detail_limit
    cfg.safety.page_interval_min_seconds = 0
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    cfg.safety.page_interval_max_seconds = 0
    repo = Repo(connect(tmp_path / f"{platform}.db"))
    return DriverContext(config=cfg, repo=repo, browser=browser,
                         policy=Policy(repo, cfg, sleep=lambda _s: None),
                         stop=StopSignal(), platform=platform)


def test_51job_fetches_jd_when_enabled(tmp_path):
    search = FakePage(url=j51_search("数据分析", "020000", 1),
                      evals={".joblist-item": json.dumps(LIST_51)},
                      selector_text={".joblist-item": ""})
    detail = FakePage(url=LIST_51[0]["url"],
                      evals={".bmsg": JD_TEXT}, selector_text={".bmsg": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([search, detail]), "51job", fetch_detail=True)

    Job51Driver().collect(ctx)
    assert ctx.repo.get_job("51job-173851192").jd == JD_TEXT


def test_51job_skips_jd_when_disabled(tmp_path):
    search = FakePage(url=j51_search("数据分析", "020000", 1),
                      evals={".joblist-item": json.dumps(LIST_51)},
                      selector_text={".joblist-item": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([search]), "51job", fetch_detail=False)

    Job51Driver().collect(ctx)
    assert ctx.repo.get_job("51job-173851192").jd == ""
    assert ctx.policy.budget.used("51job", "collect_detail") == 0


def test_51job_stops_fetching_when_detail_budget_runs_out(tmp_path):
    two = LIST_51 + [{"jobId": "2", "title": "另一个岗位", "company": "乙公司",
                      "salary": "1万", "area": "上海·浦东", "url": "https://jobs.51job.com/all/2.html"}]
    search = FakePage(url=j51_search("数据分析", "020000", 1),
                      evals={".joblist-item": json.dumps(two)},
                      selector_text={".joblist-item": ""})
    detail = FakePage(url=LIST_51[0]["url"], evals={".bmsg": JD_TEXT}, selector_text={".bmsg": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([search, detail]), "51job", fetch_detail=True, detail_limit=1)

    result = Job51Driver().collect(ctx)
    assert result.new == 2                              # 列表数据照常入库
    assert ctx.repo.get_job("51job-173851192").jd == JD_TEXT
    assert ctx.repo.get_job("51job-2").jd == ""          # 预算用尽后不再抓 JD


LIST_LP = [{"jobId": "1985614831", "title": "产品支持岗", "company": "中国平安",
            "salary": "8-12k", "city": "上海", "url": "https://www.liepin.com/job/1985614831.shtml"}]


def test_liepin_fetches_jd_when_enabled(tmp_path):
    search = FakePage(url="https://www.liepin.com/zhaopin/?key=%E6%95%B0%E6%8D%AE%E5%88%86%E6%9E%90&curPage=0",
                      evals={".jobCardPcContainer": json.dumps(LIST_LP)},
                      selector_text={"[data-nick=" + '"job-detail-job-info"' + "]": ""})
    detail = FakePage(url=LIST_LP[0]["url"], evals={".job-intro-container": JD_TEXT},
                      selector_text={".job-intro-container": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([search, detail]), "liepin", fetch_detail=True)

    LiepinDriver().collect(ctx)
    saved = ctx.repo.list_jobs(platform="liepin")
    assert saved and saved[0].jd == JD_TEXT


def test_zhaopin_fetches_jd_when_enabled(tmp_path):
    search = FakePage(url=zp_search("数据分析", "上海"),      # 有城市码的城市带 jl= 搜（默认配置是上海）
                      evals={"job-card": json.dumps(LIST_ZP)},
                      selector_text={".job-card": ""})
    detail = FakePage(url="https://www.zhaopin.com/jobdetail/CCL1493228500J40928543912.htm",
                      evals={".describtion-card__content-wrap": JD_TEXT},
                      selector_text={".describtion-card__content-wrap": ""})
    ctx = make_ctx(tmp_path, FakeBrowser([search, detail]), "zhaopin", fetch_detail=True)

    ZhaopinDriver().collect(ctx)
    saved = ctx.repo.list_jobs(platform="zhaopin")
    assert saved and saved[0].jd == JD_TEXT
    assert saved[0].url.endswith("CCL1493228500J40928543912.htm")
