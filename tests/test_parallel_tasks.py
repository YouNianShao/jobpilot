"""方案二：不同平台的任务可以同时跑，同一平台 / AI 批处理互斥；全流程只评自己平台的岗位。"""
from __future__ import annotations

import threading
import time

import pytest
from conftest import open_time_gates
from test_chain import ScriptedLlm

from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig, PlatformCfg
from jobpilot.db import Repo, connect
from jobpilot.models import CollectResult, Job, Status
from jobpilot.platforms.base import StopSignal
from jobpilot.platforms.registry import Registry
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.tasks import TaskAlreadyRunning, TaskRunner

KEYS = ("51job", "liepin")


class HoldDriver:
    """采集时可以被"卡住"：用 Event 把任务停在半路，好让另一个任务在它跑着时启动。"""

    capabilities = frozenset({"collect", "apply"})

    def __init__(self, key: str) -> None:
        self.key, self.name = key, key
        self.started = threading.Event()
        self.release = threading.Event()
        self.hold = False

    def collect(self, ctx) -> CollectResult:
        self.started.set()
        while self.hold and not self.release.is_set():
            ctx.stop.check()
            time.sleep(0.01)
        ctx.repo.upsert_job(Job(id=f"{self.key}-c1", platform=self.key, external_id="c1",
                                title=f"{self.key} 产品经理", company="某公司", jd="AI 产品"))
        return CollectResult(platform=self.key, new=1, seen=1)

    def apply(self, ctx, job):  # pragma: no cover - 这里的测试不投递
        raise AssertionError("不该投递")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    import jobpilot.tasks as tasks_module

    reg = Registry()
    drivers = {key: HoldDriver(key) for key in KEYS}
    for driver in drivers.values():
        reg.register(driver)
    resume = tmp_path / "r.md"
    resume.write_text("# 简历\n做过 AI 产品。\n", encoding="utf-8")
    db = tmp_path / "p.db"

    def make_config():
        cfg = open_time_gates(AppConfig())
        cfg.profile.resume_path = str(resume)
        cfg.safety.page_interval_min_seconds = cfg.safety.page_interval_max_seconds = 0
        cfg.safety.delivery_cooldown_min_minutes = cfg.safety.delivery_cooldown_max_minutes = 0
        for key in KEYS:
            cfg.platforms[key] = PlatformCfg(enabled=True, apply=True, keywords=["产品"], cities=["上海"])
        return cfg

    def factory():
        cfg = make_config()
        repo = Repo(connect(db))
        stop = StopSignal()
        return ServiceContext(config=cfg, repo=repo, llm=ScriptedLlm(), policy=Policy(repo, cfg), stop=stop,
                              browser_factory=lambda: FakeBrowser([]))

    real_run = tasks_module.run_flow
    monkeypatch.setattr(tasks_module, "build_default_registry", lambda: reg)
    monkeypatch.setattr(tasks_module, "run_flow", lambda ctx, mode, platform=None, **o: real_run(
        ctx, mode, platform, registry=reg, **o))
    seed = Repo(connect(db))
    yield TaskRunner(factory), drivers, seed, factory
    seed.conn.close()


def wait_all(runner, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.running:
            return
        time.sleep(0.02)
    raise AssertionError("任务未在超时内结束")


def hold(runner, drivers, key, mode="collect", **options):
    """启动一个任务，并等它卡在 key 平台的采集里。"""
    drivers[key].hold = True
    task = runner.start(mode, key if "plans" not in options else "", **options)
    assert drivers[key].started.wait(3), "任务没有走到采集"
    return task


def test_two_platforms_run_at_the_same_time(rig):
    runner, drivers, _, _ = rig
    first = hold(runner, drivers, "51job", "full")
    second = hold(runner, drivers, "liepin")          # 第二个任务真的开跑了，而不是排队
    assert runner.get(first.id).status == "running" and runner.get(second.id).status == "running"
    assert runner.running_platforms() == {"51job", "liepin"}
    assert [s["id"] for s in runner.snapshots()] == [second.id, first.id]   # 新的在前
    drivers["51job"].release.set()
    drivers["liepin"].release.set()
    wait_all(runner)
    assert runner.get(first.id).status == "completed"
    assert runner.get(second.id).status == "completed"


def test_same_platform_is_refused_with_its_name(rig):
    runner, drivers, _, _ = rig
    hold(runner, drivers, "51job", "full")
    with pytest.raises(TaskAlreadyRunning) as exc:
        runner.start("collect", "51job")
    assert "前程无忧" in str(exc.value) and "全流程" in str(exc.value)
    # 多平台全流程里带上了被占用的平台，也要拒绝
    with pytest.raises(TaskAlreadyRunning):
        runner.start("full", "", plans=[{"platform": "liepin"}, {"platform": "51job"}])
    runner.stop()
    wait_all(runner)


def test_apply_all_claims_every_apply_capable_platform(rig, monkeypatch):
    import jobpilot.tasks as tasks_module

    runner, _, _, _ = rig
    gate, entered = threading.Event(), threading.Event()

    def slow_flow(ctx, mode, platform=None, **options):
        entered.set()
        while not gate.is_set():
            ctx.stop.check()
            time.sleep(0.01)
        return {}

    monkeypatch.setattr(tasks_module, "run_flow", slow_flow)
    runner_seed = rig[2]
    for key in KEYS:                                  # 每个平台都有已确认、等着投的岗位
        runner_seed.upsert_job(Job(id=f"{key}-a1", platform=key, external_id="a1", title=f"{key} 岗", company=f"{key} 公司"))
        runner_seed.set_status(f"{key}-a1", Status.APPROVED.value)
    task = runner.start("apply", "")
    assert entered.wait(3)
    assert runner.running_platforms() == set(KEYS)
    assert runner.snapshot()["claims"] == sorted(KEYS)
    with pytest.raises(TaskAlreadyRunning) as exc:
        runner.start("collect", "liepin")
    assert "猎聘" in str(exc.value)
    gate.set()
    wait_all(runner)
    assert runner.get(task.id).status == "completed"


def test_ai_batches_conflict_but_confirm_always_runs(rig, monkeypatch):
    import jobpilot.tasks as tasks_module

    runner, _, _, _ = rig
    gate = threading.Event()

    def slow_flow(ctx, mode, platform=None, **options):
        if mode == "score":
            while not gate.is_set():
                ctx.stop.check()
                time.sleep(0.01)
        return {}

    monkeypatch.setattr(tasks_module, "run_flow", slow_flow)
    runner.start("score", "")
    with pytest.raises(TaskAlreadyRunning) as exc:
        runner.start("score", "")
    assert "AI" in str(exc.value)
    with pytest.raises(TaskAlreadyRunning):
        runner.start("greet", "51job")        # 「评所有平台」占着每个平台的评分位
    confirm = runner.start("confirm", "")
    runner.start("collect", "liepin")         # AI 批处理不占平台：采集照样能开
    deadline = time.time() + 3
    while runner.get(confirm.id).status == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert runner.get(confirm.id).status == "completed"
    assert runner.running_platforms() <= {"liepin"}      # "ai" 不算平台
    gate.set()
    wait_all(runner)


def test_stop_by_id_only_stops_that_task_and_stop_all_stops_everything(rig):
    runner, drivers, _, _ = rig
    a = hold(runner, drivers, "51job")
    b = hold(runner, drivers, "liepin")
    runner.stop(a.id)
    deadline = time.time() + 3
    while runner.get(a.id).status == "running" and time.time() < deadline:
        time.sleep(0.02)
    assert runner.get(a.id).status == "stopped"
    assert runner.get(b.id).status == "running"
    assert runner.running_platforms() == {"liepin"}

    runner.stop()
    wait_all(runner)
    assert runner.get(b.id).status == "stopped"
    with pytest.raises(KeyError):
        runner.stop("nope")
    # 列表：都跑完了，最新的在前
    assert [s["id"] for s in runner.snapshots()][:2] == [b.id, a.id]


def test_pause_by_id_only_pauses_that_task(rig):
    runner, drivers, _, _ = rig
    a = hold(runner, drivers, "51job")
    b = hold(runner, drivers, "liepin")
    runner.pause(a.id)
    snaps = {s["id"]: s for s in runner.snapshots()}
    assert snaps[a.id]["paused"] and not snaps[b.id]["paused"]
    assert runner.paused
    runner.stop()                              # 暂停中的任务也要能停下来
    wait_all(runner)
    assert runner.get(a.id).status == "stopped"


def test_full_flow_scores_only_its_own_platform(rig):
    runner, _, seed, _ = rig
    # 猎聘上有一个还没评分的岗位：前程无忧的全流程不能顺手评它（猎聘可能正同时跑着自己的流程）
    seed.upsert_job(Job(id="liepin-old", platform="liepin", external_id="old", title="数据分析", company="X",
                        jd="AI", status=Status.NEW.value))
    task = runner.start("full", "51job")
    wait_all(runner)
    assert runner.get(task.id).status == "completed"
    assert seed.get_job("51job-c1").status != Status.NEW.value      # 自己平台的评了
    other = seed.get_job("liepin-old")
    assert other.status == Status.NEW.value and other.score is None  # 别的平台的没动


def test_multi_full_scores_only_chosen_platforms(rig):
    runner, _, seed, _ = rig
    seed.upsert_job(Job(id="boss-old", platform="boss", external_id="old", title="数据分析", company="X",
                        jd="AI", status=Status.NEW.value))
    runner.start("full", "", plans=[{"platform": "51job"}, {"platform": "liepin"}], apply_after=False)
    wait_all(runner)
    snap = runner.snapshot()
    assert snap["status"] == "completed", snap
    assert snap["result"]["steps"]["score"]["scored"] == 2
    assert seed.get_job("boss-old").score is None


def test_scorer_skips_jobs_scored_by_another_task_meanwhile(rig):
    from jobpilot.services.scorer import score_jobs

    _, _, seed, factory = rig
    for index in range(3):
        seed.upsert_job(Job(id=f"liepin-{index}", platform="liepin", external_id=str(index),
                            title="数据分析", company="X", jd="AI", status=Status.NEW.value))
    ctx = factory()

    class OtherTaskWins(ScriptedLlm):
        """第一次调模型时，模拟另一个任务把剩下的岗位都评完了。"""

        def complete(self, **kw):
            if self.calls == 0:
                for index in (1, 2):
                    seed.set_score(f"liepin-{index}", 70, "别的任务评的", Status.PENDING_CONFIRM.value)
            return super().complete(**kw)

    ctx.llm = OtherTaskWins()
    out = score_jobs(ctx, "liepin")
    ctx.repo.conn.close()
    assert ctx.llm.calls == 1 and out["scored"] == 1 and out["already_scored"] == 2
    assert seed.get_job("liepin-1").score == 70                       # 没被覆盖


def test_status_lists_tasks_and_busy_platforms_and_account_switch_refuses(rig):
    from fastapi.testclient import TestClient

    from jobpilot.web.app import create_app

    runner, drivers, _, factory = rig
    task = hold(runner, drivers, "51job")
    client = TestClient(create_app(factory, runner=runner), base_url="http://127.0.0.1:8700")
    body = client.get("/api/status").json()
    assert body["running"] is True
    assert body["busy_platforms"] == ["51job"]
    assert body["tasks"][0]["id"] == task.id and body["tasks"][0]["claims"] == ["51job"]
    assert body["task"]["id"] == task.id

    res = client.post("/api/accounts/switch", json={"id": "B"})
    assert res.status_code == 409 and "任务" in res.json()["error"]

    conflict = client.post("/api/tasks", json={"mode": "collect", "platform": "51job"})
    assert conflict.status_code == 409 and "前程无忧" in conflict.json()["error"]

    assert client.post("/api/tasks/stop", json={"id": "nope"}).status_code == 404
    assert client.post("/api/tasks/pause", params={"id": task.id}).json()["paused"] is True
    client.post("/api/tasks/resume", json={"id": task.id})
    client.post("/api/tasks/stop", json={"id": task.id})
    wait_all(runner)
    assert runner.get(task.id).status == "stopped"


def test_stop_during_a_hanging_step_reads_as_stopped_not_failed():
    """点了停止之后才冒出来的错（例如正卡在连浏览器、20 秒后超时）：任务应显示「已停止」，不是「出错」。"""
    import threading
    import time

    from jobpilot.platforms.base import StopSignal
    from jobpilot.tasks import TaskRunner

    class Ctx:
        def __init__(self):
            self.stop = StopSignal()
            self.config = None

            class R:
                class conn:
                    @staticmethod
                    def close():
                        pass

                def upsert_task_run(self, row):
                    pass

            self.repo = R()

    gate = threading.Event()

    def flow(ctx, mode, platform, **o):
        gate.wait(5)
        raise RuntimeError("连不上浏览器：20 秒内没有响应")

    import jobpilot.tasks as tasks_mod

    runner = TaskRunner(lambda: Ctx())
    orig_run, orig_check = tasks_mod.run_flow, tasks_mod.check_mode
    tasks_mod.run_flow, tasks_mod.check_mode = flow, (lambda *a, **k: None)
    try:
        task = runner.start("confirm", "")
        runner.stop(task.id)
        gate.set()
        for _ in range(50):
            if task.status != "running":
                break
            time.sleep(0.05)
        assert task.status == "stopped" and task.finished_at
    finally:
        tasks_mod.run_flow, tasks_mod.check_mode = orig_run, orig_check


def test_scoring_one_platform_runs_alongside_collect_and_apply_and_other_scores(rig, monkeypatch):
    """用户要求（2026-10-16）：BOSS 在采集时前程无忧可以评分；前程无忧评分时智联可以投递；
    不同平台的评分也能同时跑。只有同一平台的评分不能叠两个。"""
    import jobpilot.tasks as tasks_module

    runner, drivers, _, _ = rig
    gate = threading.Event()

    def slow_flow(ctx, mode, platform, **o):
        while not gate.is_set():
            ctx.stop.check()
            time.sleep(0.01)
        return {}

    monkeypatch.setattr(tasks_module, "run_flow", slow_flow)
    monkeypatch.setattr(tasks_module, "check_mode", lambda *a, **k: None)
    started = [runner.start("collect", "boss"), runner.start("score", "51job"),
               runner.start("apply", "zhaopin"), runner.start("score", "liepin")]
    assert all(t.status == "running" for t in started)
    with pytest.raises(TaskAlreadyRunning) as exc:
        runner.start("score", "51job")                                  # 同一平台的评分不能叠
    assert "前程无忧" in str(exc.value)
    with pytest.raises(TaskAlreadyRunning):
        runner.start("score", "")                                       # 评所有平台和单平台评分冲突
    assert runner.running_platforms() == {"boss", "zhaopin"}           # 评分位不算「平台忙」
    gate.set()
    wait_all(runner)
    assert all(runner.get(t.id).status == "completed" for t in started)



def test_apply_all_skips_platform_busy_with_another_task(rig, monkeypatch):
    """2026-10-21：智联在采集时点「开始投递」被整个拒绝（要投的岗位并不在智联）。
    现在只占有已确认岗位、此刻空闲的平台；被占着的跳过并写进日志；都被占着才拒绝"""
    import jobpilot.tasks as tasks_module

    runner, drivers, seed, _ = rig
    busy, other = KEYS[0], KEYS[1]
    for key in (busy, other):
        seed.upsert_job(Job(id=f"{key}-a1", platform=key, external_id="a1", title=f"{key} 岗", company=f"{key} 公司"))
        seed.set_status(f"{key}-a1", Status.APPROVED.value)
    hold(runner, drivers, busy)                       # 这个平台在采集
    seen = {}
    real = tasks_module.run_flow

    def spy(ctx, mode, platform=None, **options):
        if mode == "apply":
            seen["platforms"] = options.get("platforms")
            return {}
        return real(ctx, mode, platform, **options)

    monkeypatch.setattr(tasks_module, "run_flow", spy)
    task = runner.start("apply", "")
    assert set(task.options["platforms"]) == {other}
    assert busy not in runner._claims[task.id]
    assert any("先不投" in line for line in task.logs)
    # 只有被占着的平台有已确认岗位 → 拒绝，并说清是哪个平台在忙
    seed.set_status(f"{other}-a1", Status.APPLIED.value)
    while runner.get(task.id).status == "running":
        time.sleep(0.02)
    with pytest.raises(TaskAlreadyRunning):
        runner.start("apply", "")
    drivers[busy].release.set()
    wait_all(runner)
    assert seen["platforms"] == [other]


def test_score_all_skips_platform_already_scoring(rig, monkeypatch):
    """一键评分（2026-10-21）：只占有待评分、此刻没在评的平台；正在评的跳过；全在评才拒绝"""
    import jobpilot.tasks as tasks_module

    runner, _, seed, _ = rig
    a, b = KEYS[0], KEYS[1]
    for key in (a, b):
        seed.upsert_job(Job(id=f"{key}-n1", platform=key, external_id="n1", title=f"{key} 新岗", company=f"{key} 新公司"))
    gate, entered = threading.Event(), threading.Event()
    seen = {}

    def flow(ctx, mode, platform=None, **options):
        if platform == a:                               # a 平台的评分卡住，模拟「正在评」
            entered.set()
            while not gate.is_set():
                ctx.stop.check()
                time.sleep(0.01)
            return {}
        seen["platforms"] = options.get("platforms")
        return {}

    monkeypatch.setattr(tasks_module, "run_flow", flow)
    runner.start("score", a)
    assert entered.wait(3)
    task = runner.start("score", "")
    assert task.options["platforms"] == [b]
    assert any("先不评" in line for line in task.logs)
    while runner.get(task.id).status == "running":
        time.sleep(0.02)
    assert seen["platforms"] == [b]
    seed.conn.execute("UPDATE jobs SET status='scored' WHERE id=?", (f"{b}-n1",))
    seed.conn.commit()
    with pytest.raises(TaskAlreadyRunning):              # 只剩 a 有待评分、而它正在评
        runner.start("score", "")
    gate.set()
    wait_all(runner)
