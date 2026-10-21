"""采集组合调度：广度优先 / 轮转 / 补采 / 速率停手后自动续采（假浏览器，不开真网页）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import unquote

import pytest
from conftest import open_time_gates
from fastapi.testclient import TestClient

from jobpilot import readable
from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.platforms.base import CARRY_KEY, CURSOR_KEY, StopSignal, read_carry, write_carry
from jobpilot.safety.policy import Policy
from jobpilot.services import auto_resume, collector
from jobpilot.services.context import ServiceContext
from jobpilot.services.orchestrator import TaskNotRunnable
from jobpilot.tasks import TaskAlreadyRunning

KEYWORDS = ["A", "B", "C"]
SLOTS = [f"上海|{k}" for k in KEYWORDS]


class Search(FakePage):
    """51job 搜索页：每个 关键词×页 都给一批全新的岗位（不会提前结束）；记下访问顺序。"""

    def __init__(self, empty: set[str] = frozenset()):
        super().__init__(url="about:blank", selector_text={".joblist-item": ""})
        self.visited: list[str] = []
        self.empty = set(empty)

    def goto(self, url: str) -> None:
        super().goto(url)
        kw = unquote(re.search(r"keyword=([^&]*)", url).group(1))
        page = int(re.search(r"page=(\d+)", url).group(1))
        self.visited.append(f"{kw}{page}")

    def eval(self, js: str):
        tag = self.visited[-1]
        if tag[0] in self.empty:
            return "[]"
        return json.dumps([{"jobId": f"{tag}-{i}", "title": f"岗位{i}", "company": "甲公司", "salary": "1万",
                            "area": "上海·徐汇", "url": f"https://jobs.51job.com/all/{tag}-{i}.html"}
                           for i in range(3)])


class OneTab(FakeBrowser):
    def open(self, url: str) -> FakePage:
        self.pages[0].goto(url)
        return self.pages[0]


def make(tmp_path, *, max_pages=3, search_budget=100, per_hour=0, keywords=KEYWORDS):
    cfg = AppConfig()
    c = cfg.platforms["51job"]
    c.enabled, c.cities, c.keywords, c.max_pages, c.fetch_detail = True, ["上海"], list(keywords), max_pages, False
    cfg.safety.platforms = {}
    cfg.safety.daily_pages.collect_search = search_budget
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    cfg.safety.recollect_after_hours = 0
    cfg.safety.stop_after_empty_pages = 0
    cfg.safety.full_recrawl_days = 0
    cfg.safety.max_pages_per_10min = 0
    cfg.safety.max_pages_per_hour = per_hour
    cfg.safety.rate_wait_max_seconds = 0
    open_time_gates(cfg)
    db = tmp_path / "t.db"
    page = Search()
    state = {"page": page}

    def ctx():
        repo = Repo(connect(db))
        return ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg, sleep=lambda _s: None),
                              stop=StopSignal(), browser_factory=lambda: OneTab([state["page"]]))
    return ctx, state


def fresh_page(state) -> Search:
    state["page"] = Search()
    return state["page"]


# ---------------- 广度优先 ----------------
def test_breadth_first_shallow_pages_of_every_slot_first(tmp_path):
    ctx, state = make(tmp_path, max_pages=3)
    out = collector.collect(ctx(), "51job")
    assert state["page"].visited == ["A1", "A2", "B1", "B2", "C1", "C2", "A3", "B3", "C3"]
    assert out["new"] == 27 and out["missed"] == [] and out["resume_at"] == ""
    assert [s["slot"] for s in out["slots"]] == SLOTS


def test_breadth_first_empty_slot_ends_in_first_pass(tmp_path):
    ctx, state = make(tmp_path, max_pages=3)
    state["page"].empty = {"B"}
    collector.collect(ctx(), "51job")
    assert state["page"].visited == ["A1", "A2", "B1", "C1", "C2", "A3", "C3"]


# ---------------- 轮转 + 额度用完 ----------------
def test_budget_stop_records_missed_and_carry_no_resume(tmp_path):
    ctx, state = make(tmp_path, max_pages=2, search_budget=3)
    c = ctx()
    out = collector.collect(c, "51job")
    assert state["page"].visited == ["A1", "A2", "B1"]
    assert out["missed"] == ["C · 上海"] and out["resume_at"] == ""
    assert "额度" in out["stopped"] or "上限" in out["stopped"]
    assert auto_resume.read_resume(c.repo, "51job") is None              # 当日额度：不自动续采
    carry = read_carry(c.repo, "51job")
    assert carry["slots"] == ["上海|C"] and carry["labels"] == ["C · 上海"]
    assert c.repo.get_kv(CURSOR_KEY.format(platform="51job")) == "上海|B"
    text = readable.run_summary("collect", out)
    assert "额度用完，没采到：C · 上海" in text


def test_rotation_starts_after_cursor(tmp_path):
    ctx, state = make(tmp_path, max_pages=1)
    c = ctx()
    c.repo.set_kv(CURSOR_KEY.format(platform="51job"), "上海|A")
    collector.collect(c, "51job")
    assert state["page"].visited == ["B1", "C1", "A1"]
    assert c.repo.get_kv(CURSOR_KEY.format(platform="51job")) == "上海|A"


def test_rotation_across_budget_stops_covers_all_slots(tmp_path):
    """每次只够开 1 页：三次跑下来三个组合都轮到了（以前永远只采 A）。"""
    ctx, state = make(tmp_path, max_pages=1, search_budget=1)
    seen = []
    for _day in range(3):
        c = ctx()
        c.repo.conn.execute("DELETE FROM page_access")    # 模拟第二天额度重置
        c.repo.conn.commit()
        page = fresh_page(state)
        collector.collect(c, "51job")
        seen += page.visited
    assert seen == ["A1", "B1", "C1"]


def test_carry_goes_first_next_run_and_is_consumed(tmp_path):
    ctx, state = make(tmp_path, max_pages=1, search_budget=1)
    c = ctx()
    out = collector.collect(c, "51job")
    assert out["missed"] == ["B · 上海", "C · 上海"]
    # 第二天：额度够了；没采到的 B、C 排最前（再从游标之后接上 A）
    c.repo.conn.execute("DELETE FROM page_access")
    c.repo.conn.commit()
    c.config.safety.daily_pages.collect_search = 100
    page = fresh_page(state)
    out2 = collector.collect(ctx(), "51job")
    assert page.visited == ["B1", "C1", "A1"]
    assert out2["carried"] == ["B · 上海", "C · 上海"]
    assert read_carry(c.repo, "51job") == {}
    assert "先补了上次没采到的：B · 上海、C · 上海" in readable.run_summary("collect", out2)


def test_carry_removed_slot_by_slot_when_stopped_again(tmp_path):
    ctx, state = make(tmp_path, max_pages=1, search_budget=1)
    c = ctx()
    collector.collect(c, "51job")                          # A 采了；B、C 没采到
    c.repo.conn.execute("DELETE FROM page_access")
    c.repo.conn.commit()
    fresh_page(state)
    collector.collect(ctx(), "51job")                      # 只够 1 页：补了 B，C 还欠着
    carry = read_carry(c.repo, "51job")
    assert carry["slots"] == ["上海|C", "上海|A"]          # B 划掉了；C 还欠着，这次没轮到的 A 接在后面


def test_carry_expires_after_three_days(tmp_path):
    ctx, state = make(tmp_path, max_pages=1)
    c = ctx()
    old = (datetime.now() - timedelta(days=3, hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    write_carry(c.repo, "51job", ["上海|C"], ["C · 上海"], old)
    assert read_carry(c.repo, "51job") == {}
    collector.collect(c, "51job")
    assert state["page"].visited == ["A1", "B1", "C1"]     # 过期的不再排前面
    assert auto_resume.carry_status(c.repo, ["51job"]) == {}


def test_carry_status_shape(tmp_path):
    ctx, _ = make(tmp_path)
    c = ctx()
    write_carry(c.repo, "51job", ["上海|C"], ["C · 上海"], "2099-01-01 00:00:00")
    write_carry(c.repo, "51job", ["上海|C"], ["C · 上海"])
    status = auto_resume.carry_status(c.repo, ["51job", "boss"])
    assert list(status) == ["51job"] and status["51job"]["labels"] == ["C · 上海"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", status["51job"]["since"])
    assert c.repo.get_kv(CARRY_KEY.format(platform="51job"))


# ---------------- 速率窗口停手 → 自动续采 ----------------
def test_rate_stop_writes_resume_with_pending_slots(tmp_path):
    ctx, state = make(tmp_path, max_pages=2, per_hour=3)
    c = ctx()
    before = datetime.now()
    out = collector.collect(c, "51job", keywords=["A", "B", "C"])
    assert state["page"].visited == ["A1", "A2", "B1"]
    record = auto_resume.read_resume(c.repo, "51job")
    assert record["mode"] == "collect"
    assert record["slots"] == ["上海|B", "上海|C"]           # 没翻完的 B + 没轮到的 C
    assert record["labels"] == ["B · 上海", "C · 上海"]
    assert record["options"] == {"keywords": ["A", "B", "C"]}
    # 窗口重新打开 ≈ 最早那页滑出 1 小时窗口
    assert before.timestamp() + 3500 < record["not_before"] <= datetime.now().timestamp() + 3602
    assert out["missed"] == ["C · 上海"] and out["resume_at"] and out["resume_slots"] == 2
    assert read_carry(c.repo, "51job")["slots"] == ["上海|C"]   # 没轮到的照样进「下次先补」
    text = readable.run_summary("collect", out)
    assert re.search(r"1 小时开页上限到了，约 \d+ 分钟后自动接着采 2 个组合", text)
    assert "额度用完" not in text


def test_full_rate_stop_records_full_mode_without_apply(tmp_path):
    from jobpilot.services import orchestrator

    ctx, _ = make(tmp_path, max_pages=2, per_hour=3)
    c = ctx()
    orchestrator.run_full_multi(c, [{"platform": "51job", "cities": ["上海"]}], apply_after=True)
    record = auto_resume.read_resume(c.repo, "51job")
    assert record["mode"] == "full" and record["options"] == {"cities": ["上海"], "apply_after": False}
    mode, platform, options = auto_resume.start_args("51job", record)
    assert (mode, platform) == ("full", "")
    assert options == {"plans": [{"platform": "51job", "cities": ["上海"], "slots": ["上海|B", "上海|C"]}],
                       "apply_after": False}


def test_resume_run_only_crawls_pending_slots_and_keeps_cursor(tmp_path):
    ctx, state = make(tmp_path, max_pages=2, per_hour=3)
    c = ctx()
    collector.collect(c, "51job")
    c.repo.conn.execute("DELETE FROM page_access")
    c.repo.conn.commit()
    cursor = c.repo.get_kv(CURSOR_KEY.format(platform="51job"))
    page = fresh_page(state)
    c.config.safety.max_pages_per_hour = 0
    out = collector.collect(ctx(), "51job", slots=["上海|B", "上海|C"])
    assert page.visited == ["B1", "B2", "C1", "C2"]
    assert out["missed"] == [] and read_carry(c.repo, "51job") == {}
    assert c.repo.get_kv(CURSOR_KEY.format(platform="51job")) == cursor   # 续采不动轮转游标


def test_normal_complete_run_clears_stale_resume(tmp_path):
    ctx, _ = make(tmp_path, max_pages=1)
    c = ctx()
    auto_resume.record_resume(c.repo, c.policy, "51job", mode="collect", slots=["上海|C"], labels=["C · 上海"])
    collector.collect(c, "51job")
    assert auto_resume.read_resume(c.repo, "51job") is None


def test_risk_lock_stop_neither_missed_nor_resume(tmp_path):
    ctx, state = make(tmp_path, max_pages=1)
    c = ctx()
    c.policy.locks.lock("51job", "测试", "captcha", 30)
    out = collector.collect(c, "51job")
    assert state["page"].visited == [] and out["missed"] == []
    assert auto_resume.read_resume(c.repo, "51job") is None and read_carry(c.repo, "51job") == {}


# ---------------- 后台续采调度 ----------------
class FakeRunner:
    def __init__(self, busy=(), refuse=None):
        self.busy, self.refuse, self.started = set(busy), refuse, []

    def running_platforms(self):
        return set(self.busy)

    def start(self, mode, platform="", **options):
        if self.refuse:
            raise self.refuse
        self.started.append((mode, platform, options))
        return type("T", (), {"id": "t1"})()


NOW = datetime(2026, 10, 21, 10, 0, 0)


def pending(c, *, mode="collect", due_in=0, age_hours=0.5):
    record = {"mode": mode, "slots": ["上海|B", "上海|C"], "labels": ["B · 上海", "C · 上海"],
              "options": {"keywords": ["A", "B", "C"]} if mode == "collect" else {"apply_after": False},
              "window": "1 小时", "not_before": int((NOW + timedelta(seconds=due_in)).timestamp()),
              "created_at": int((NOW - timedelta(hours=age_hours)).timestamp())}
    c.repo.set_kv("collect:resume:51job", json.dumps(record, ensure_ascii=False))


def scheduler(ctx, runner):
    return auto_resume.AutoResume(ctx, runner, clock=lambda: NOW, registry=_Registry())


class _Registry:
    def keys(self):
        return ["51job"]


def test_scheduler_not_yet_due_waits(tmp_path):
    ctx, _ = make(tmp_path)
    pending(ctx(), due_in=600)
    runner = FakeRunner()
    out = scheduler(ctx, runner).tick()
    assert out["51job"]["action"] == "wait" and "10:10 后自动接着采" in out["51job"]["reason"]
    assert runner.started == [] and auto_resume.read_resume(ctx().repo, "51job")


def test_scheduler_due_starts_collect_with_slots_and_clears(tmp_path):
    ctx, _ = make(tmp_path)
    pending(ctx())
    runner = FakeRunner()
    out = scheduler(ctx, runner).tick()
    assert out["51job"]["started"] and out["51job"]["task_id"] == "t1"
    assert runner.started == [("collect", "51job", {"keywords": ["A", "B", "C"], "slots": ["上海|B", "上海|C"]})]
    assert auto_resume.read_resume(ctx().repo, "51job") is None


def test_scheduler_due_full_starts_plan_without_apply(tmp_path):
    ctx, _ = make(tmp_path)
    pending(ctx(), mode="full")
    runner = FakeRunner()
    scheduler(ctx, runner).tick()
    assert runner.started == [("full", "", {"plans": [{"platform": "51job", "slots": ["上海|B", "上海|C"]}],
                                            "apply_after": False})]


def test_scheduler_busy_platform_waits(tmp_path):
    ctx, _ = make(tmp_path)
    pending(ctx())
    runner = FakeRunner(busy={"51job"})
    out = scheduler(ctx, runner).tick()
    assert out["51job"]["action"] == "wait" and runner.started == []
    assert auto_resume.read_resume(ctx().repo, "51job")


@pytest.mark.parametrize("exc", [TaskAlreadyRunning("busy"), TaskNotRunnable("no")])
def test_scheduler_refused_keeps_record(tmp_path, exc):
    ctx, _ = make(tmp_path)
    pending(ctx())
    auto = scheduler(ctx, FakeRunner(refuse=exc))
    assert auto.tick()["51job"]["started"] is False
    assert auto_resume.read_resume(ctx().repo, "51job")["slots"] == ["上海|B", "上海|C"]
    assert "没续上" in auto.waiting["51job"]


def test_scheduler_outside_active_window_waits(tmp_path):
    ctx, _ = make(tmp_path)
    c = ctx()
    c.config.safety.active_windows = ["12:00-13:00"]
    pending(c)
    out = scheduler(ctx, FakeRunner()).tick()
    assert out["51job"]["action"] == "wait" and "时段" in out["51job"]["reason"]


def test_scheduler_lock_waits(tmp_path):
    ctx, _ = make(tmp_path)
    c = ctx()
    until = (NOW + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    c.repo.set_lock("51job", "验证码", "captcha", until)
    pending(c)
    out = scheduler(ctx, FakeRunner()).tick()
    assert out["51job"]["action"] == "wait" and "冷却" in out["51job"]["reason"]


def test_scheduler_expired_drops_but_carry_stays(tmp_path):
    ctx, _ = make(tmp_path)
    c = ctx()
    write_carry(c.repo, "51job", ["上海|C"], ["C · 上海"])
    pending(c, age_hours=13)
    runner = FakeRunner()
    out = scheduler(ctx, runner).tick()
    assert out["51job"]["action"] == "drop" and "12 小时" in out["51job"]["reason"]
    assert runner.started == [] and auto_resume.read_resume(c.repo, "51job") is None
    assert read_carry(c.repo, "51job")["slots"] == ["上海|C"]                  # 明天照样先补
    events = c.repo.conn.execute("SELECT detail FROM events WHERE kind='collect_resume'").fetchall()
    assert any("不再自动续采" in row[0] for row in events)


def test_scheduler_budget_exhausted_drops(tmp_path):
    ctx, _ = make(tmp_path, search_budget=1)
    c = ctx()
    c.repo.record_page("51job", "collect_search")
    pending(c)
    out = scheduler(ctx, FakeRunner()).tick()
    assert out["51job"]["action"] == "drop" and "额度" in out["51job"]["reason"]


# ---------------- /api/status + 取消 ----------------
@pytest.fixture
def http(tmp_path):
    from jobpilot.web.app import create_app

    ctx, _ = make(tmp_path)
    from jobpilot.tasks import TaskRunner

    return TestClient(create_app(ctx, runner=TaskRunner(ctx)), base_url="http://127.0.0.1:8700"), ctx


def test_status_collect_resume_and_carry_fields(http):
    client, ctx = http
    body = client.get("/api/status").json()
    assert body["collect_resume"] == {} and body["collect_carry"] == {}
    c = ctx()
    auto_resume.record_resume(c.repo, c.policy, "51job", mode="collect", slots=["上海|C"], labels=["C · 上海"])
    write_carry(c.repo, "51job", ["上海|C"], ["C · 上海"])
    body = client.get("/api/status").json()
    item = body["collect_resume"]["51job"]
    assert set(item) == {"at", "in_seconds", "labels", "mode", "reason"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", item["at"])
    assert isinstance(item["in_seconds"], int) and item["in_seconds"] >= 0
    assert item["labels"] == ["C · 上海"] and item["mode"] == "collect" and "自动接着采" in item["reason"]
    assert body["collect_carry"]["51job"]["labels"] == ["C · 上海"]


def test_cancel_resume_endpoint(http):
    client, ctx = http
    c = ctx()
    auto_resume.record_resume(c.repo, c.policy, "51job", mode="collect", slots=["上海|C"], labels=["C · 上海"])
    assert client.post("/api/collect/resume/cancel", json={"platform": "51job"}).json() == {"ok": True}
    assert auto_resume.read_resume(c.repo, "51job") is None
    assert client.get("/api/status").json()["collect_resume"] == {}


def test_create_app_never_starts_resume_thread(tmp_path):
    from jobpilot.web.app import create_app

    ctx, _ = make(tmp_path)
    app = create_app(ctx, runner=FakeRunner())
    assert app.state.auto_resume._thread is None
    assert create_app(ctx, runner=FakeRunner(), auto_monitor=False).state.auto_resume is None


# ---------------- 摘要 ----------------
def test_summary_missed_caps_labels():
    labels = [f"词{i} · 上海" for i in range(6)]
    text = readable.run_summary("collect", {"platform": "boss", "new": 1, "missed": labels})
    assert "额度用完，没采到：词0 · 上海、词1 · 上海、词2 · 上海、词3 · 上海 等 6 个（下次先补）" in text
    assert "新增 1 个岗位" in text


def test_summary_full_multi_prefixes_platform():
    result = {"steps": {"collect": {"new": 0, "per_platform": {
        "boss": {"platform": "boss", "new": 0, "missed": ["AI · 上海"]},
        "51job": {"platform": "51job", "new": 0, "resume_at": "2026-10-21 10:18:00", "resume_slots": 3,
                  "resume_window": "1 小时", "resume_in_minutes": 18},
    }}}}
    text = readable.run_summary("full", result)
    assert "BOSS直聘 额度用完，没采到：AI · 上海" in text
    assert "前程无忧 1 小时开页上限到了，约 18 分钟后自动接着采 3 个组合" in text
