"""采集翻页省预算：连续空页提前结束 + 深度记忆 + 每组合新岗位率（假浏览器，不开真网页）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from conftest import open_time_gates

from jobpilot import readable
from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import CollectResult
from jobpilot.platforms.base import DriverContext, StopSignal
from jobpilot.platforms.job51 import Job51Driver
from jobpilot.platforms.job51 import search_url as j51_search
from jobpilot.safety.policy import Policy

SLOT = "上海|数据分析"
DEPTH_KEY = f"collect:depth:51job:{SLOT}"


class PagedSearch(FakePage):
    """按 URL 里的 page= 返回那一页的卡片；``fresh`` 里的页给新岗位，其余页全是同一批老岗位。"""

    def __init__(self, fresh: set[int]):
        super().__init__(url=j51_search("数据分析", "020000", 1), selector_text={".joblist-item": ""})
        self.fresh = fresh
        self.visited: list[int] = []

    def _page(self) -> int:
        return int(re.search(r"page=(\d+)", self.url).group(1))

    def eval(self, js: str):
        page = self._page()
        if not self.visited or self.visited[-1] != page:
            self.visited.append(page)
        prefix = f"p{page}-" if page in self.fresh else "old-"
        return json.dumps([{"jobId": f"{prefix}{i}", "title": f"岗位{i}", "company": "甲公司",
                            "salary": "1万", "area": "上海·徐汇", "url": f"https://jobs.51job.com/all/{prefix}{i}.html"}
                           for i in range(20)])


class OneTab(FakeBrowser):
    """不管打开哪个 URL 都落在同一个标签页上（真实驱动也是复用一个搜索页）。"""

    def open(self, url: str) -> FakePage:
        self.pages[0].goto(url)
        return self.pages[0]


def make_ctx(tmp_path, *, max_pages=10, empty_pages=2, recrawl_days=3.0, recollect_hours=0.0):
    cfg = AppConfig()
    c = cfg.platforms["51job"]
    c.enabled, c.cities, c.keywords, c.max_pages, c.fetch_detail = True, ["上海"], ["数据分析"], max_pages, False
    cfg.safety.platforms = {}
    cfg.safety.daily_pages.collect_search = 100
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.recollect_after_hours = recollect_hours
    cfg.safety.stop_after_empty_pages = empty_pages
    cfg.safety.full_recrawl_days = recrawl_days
    open_time_gates(cfg)
    repo = Repo(connect(tmp_path / "t.db"))
    logs: list[str] = []
    ctx = DriverContext(config=cfg, repo=repo, browser=None, policy=Policy(repo, cfg, sleep=lambda _s: None),
                        stop=StopSignal(), platform="51job", log=logs.append)
    return ctx, logs


def seed_old_jobs(ctx):
    """把「老岗位」先采进库：之后每页都是重复。"""
    page = PagedSearch(fresh=set())
    ctx.browser = OneTab([page])
    ctx.config.platforms["51job"].max_pages = 1
    Job51Driver().collect(ctx)


def run(ctx, fresh: set[int], max_pages: int):
    page = PagedSearch(fresh)
    ctx.browser = OneTab([page])
    ctx.config.platforms["51job"].max_pages = max_pages
    return Job51Driver().collect(ctx), page


def set_depth(ctx, depth: int, days_ago: float = 0.0):
    when = (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    ctx.repo.set_kv(DEPTH_KEY, json.dumps({"depth": depth, "full_at": when}))


# ---------------- 连续空页提前结束 ----------------
def test_stops_after_two_empty_pages(tmp_path):
    ctx, logs = make_ctx(tmp_path, recrawl_days=0)       # 关掉深度记忆，只看提前结束
    seed_old_jobs(ctx)
    result, page = run(ctx, fresh={1}, max_pages=10)
    assert page.visited == [1, 2, 3]                    # 第 1 页有新，第 2、3 页全重复 → 停
    assert result.early_stopped == 1 and result.pages_saved == 7
    assert any("连续 2 页没有新岗位，提前结束这个组合（省下 7 页）" in line for line in logs)
    assert result.slots == [{"slot": SLOT, "label": "数据分析 · 上海", "seen": 60, "new": 20, "pages": 3}]


def test_streak_resets_on_new_jobs(tmp_path):
    ctx, _ = make_ctx(tmp_path, recrawl_days=0)
    seed_old_jobs(ctx)
    result, page = run(ctx, fresh={1, 3, 5}, max_pages=10)
    assert page.visited == [1, 2, 3, 4, 5, 6, 7]         # 2、4 空一页就被 3、5 的新岗位重置
    assert result.early_stopped == 1 and result.pages_saved == 3


def test_zero_disables_early_stop(tmp_path):
    ctx, _ = make_ctx(tmp_path, empty_pages=0, recrawl_days=0)
    seed_old_jobs(ctx)
    result, page = run(ctx, fresh=set(), max_pages=6)
    assert page.visited == [1, 2, 3, 4, 5, 6]
    assert result.early_stopped == 0 and result.pages_saved == 0


def test_recently_collected_pages_do_not_count(tmp_path):
    ctx, _ = make_ctx(tmp_path, recrawl_days=0, recollect_hours=6)
    seed_old_jobs(ctx)                                  # 第 1 页刚采过
    ctx.note_collect(SLOT, 2)                           # 第 2 页也刚采过
    result, page = run(ctx, fresh={3}, max_pages=10)
    assert result.skipped == 2
    assert page.visited == [3, 4, 5]                    # 跳过的 1、2 不算空页：3 有新，4、5 空 → 停
    assert result.early_stopped == 1


# ---------------- 深度记忆 ----------------
def test_full_recrawl_when_no_record_goes_deep_and_records_depth(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    seed_old_jobs(ctx)                                   # 只有 1 页、第 1 页无新 → 记 depth 0
    ctx.repo.set_kv(DEPTH_KEY, "")                       # 当作没有记录
    result, page = run(ctx, fresh={2}, max_pages=8)
    assert page.visited == list(range(1, 9))             # 完整翻：不提前结束
    assert result.early_stopped == 0
    record = json.loads(ctx.repo.get_kv(DEPTH_KEY))
    assert record["depth"] == 2 and record["full_at"]


def test_depth_caps_pages_until_full_recrawl_due(tmp_path):
    ctx, logs = make_ctx(tmp_path, empty_pages=0)
    seed_old_jobs(ctx)
    set_depth(ctx, 3, days_ago=1)
    result, page = run(ctx, fresh=set(), max_pages=20)
    assert page.visited == [1, 2, 3, 4, 5]               # min(20, 3 + 2)
    assert result.pages_saved == 15
    assert any("上次第 3 页之后就没有新岗位，这次最多翻到第 5 页（每 3 天完整翻一次）" in line for line in logs)

    set_depth(ctx, 3, days_ago=4)                         # 超过 3 天：完整翻一次
    _, page = run(ctx, fresh={7}, max_pages=10)
    assert page.visited == list(range(1, 11))
    assert json.loads(ctx.repo.get_kv(DEPTH_KEY))["depth"] == 7


def test_capped_run_deepens_record_when_new_jobs_found(tmp_path):
    ctx, _ = make_ctx(tmp_path, empty_pages=0)
    seed_old_jobs(ctx)
    set_depth(ctx, 2, days_ago=1)
    _, page = run(ctx, fresh={4}, max_pages=20)
    assert page.visited == [1, 2, 3, 4]                  # 封顶到第 4 页，第 4 页有新
    assert json.loads(ctx.repo.get_kv(DEPTH_KEY))["depth"] == 4


def test_recrawl_days_zero_disables_depth_memory(tmp_path):
    ctx, _ = make_ctx(tmp_path, empty_pages=0, recrawl_days=0)
    seed_old_jobs(ctx)
    set_depth(ctx, 1, days_ago=0)
    _, page = run(ctx, fresh=set(), max_pages=6)
    assert page.visited == list(range(1, 7))


# ---------------- 新岗位率提示 ----------------
def test_slot_end_without_opened_pages_records_nothing(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    result = CollectResult(platform="51job")
    ctx.slot_end(result, ctx.slot_begin("上海|AI", "AI", "上海", 5))
    assert result.slots == [] and ctx.repo.get_kv("collect:depth:51job:上海|AI") is None


def test_readable_mentions_low_yield_slots():
    result = {"platform": "boss", "new": 3, "duplicate": 97, "slots": [
        {"slot": "上海|AI", "label": "AI · 上海", "seen": 100, "new": 3, "pages": 5},
        {"slot": "上海|产品", "label": "产品 · 上海", "seen": 30, "new": 0, "pages": 2},     # 样本太少
        {"slot": "北京|AI", "label": "AI · 北京", "seen": 100, "new": 20, "pages": 5},     # 产出正常
    ]}
    text = readable.run_summary("collect", result)
    assert "新增 3 个岗位" in text
    assert "AI · 上海 新岗位只有 3%，可以换个关键词或加城市" in text
    assert "产品 · 上海" not in text and "AI · 北京" not in text


def test_readable_full_flow_hints_capped_at_three():
    slots = [{"slot": f"s{i}", "label": f"词{i} · 上海", "seen": 50, "new": 0, "pages": 3} for i in range(5)]
    result = {"steps": {"collect": {"new": 0, "per_platform": {
        "boss": {"platform": "boss", "new": 0, "slots": slots[:3]},
        "51job": {"platform": "51job", "new": 0, "slots": slots[3:]},
    }}}}
    text = readable.run_summary("full", result)
    assert text.count("新岗位只有") == 3
    assert "BOSS直聘 词0 · 上海 新岗位只有 0%" in text
