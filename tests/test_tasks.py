"""后台任务运行器：互斥、停止、完成。"""
from __future__ import annotations

import time

import pytest

from jobpilot.ai.llm import FakeLlm
from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.tasks import TaskAlreadyRunning, TaskRunner


def ctx_factory(tmp_path):
    def factory():
        cfg = AppConfig()
        repo = Repo(connect(tmp_path / "tasks.db"))
        return ServiceContext(config=cfg, repo=repo, llm=FakeLlm(), policy=Policy(repo, cfg),
                              stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))
    return factory


def wait(runner, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not runner.running:
            return runner.snapshot()
        time.sleep(0.02)
    raise AssertionError("任务未在超时内结束")


def test_confirm_task_completes(tmp_path):
    runner = TaskRunner(ctx_factory(tmp_path))
    runner.start("confirm", "boss")
    snap = wait(runner)
    assert snap["status"] == "completed"
    assert snap["result"]["confirmed"] == 0


def test_single_task_mutex(tmp_path, monkeypatch):
    # 现在只有占用同一资源（同一平台 / AI 批处理）才互斥；confirm 不占任何平台，可以并行（见 test_parallel_tasks）
    import jobpilot.tasks as tasks_module

    monkeypatch.setattr(tasks_module, "run_flow", lambda *a, **k: (time.sleep(0.3), {"ok": True})[1])
    runner = TaskRunner(ctx_factory(tmp_path))
    runner.start("score", "")
    with pytest.raises(TaskAlreadyRunning):
        runner.start("score", "")
    assert wait(runner)["status"] == "completed"


def test_stop_marks_task_stopped(tmp_path, monkeypatch):
    import jobpilot.tasks as tasks_module

    def slow(ctx, mode, platform=None, **options):
        for _ in range(400):
            ctx.stop.check()
            time.sleep(0.01)
        return {}

    monkeypatch.setattr(tasks_module, "run_flow", slow)
    runner = TaskRunner(ctx_factory(tmp_path))
    runner.start("confirm", "boss")
    time.sleep(0.05)
    runner.stop()
    assert wait(runner)["status"] == "stopped"


def test_run_options_reach_the_flow(tmp_path, monkeypatch):
    """「运行前配置」的参数必须真的传到流程里 —— 不能只是界面上好看。"""
    import jobpilot.tasks as tasks_module

    captured = {}

    def fake_flow(ctx, mode, platform=None, **options):
        captured["mode"] = mode
        captured["platform"] = platform
        captured.update(options)
        return {"ok": True}

    monkeypatch.setattr(tasks_module, "run_flow", fake_flow)
    runner = TaskRunner(ctx_factory(tmp_path))
    runner.start("collect", "boss", keywords=["数据分析"],
                 cities=["上海"], max_pages=2, limit=5)
    wait(runner)

    assert captured == {
        "mode": "collect", "platform": "boss",
        "keywords": ["数据分析"], "cities": ["上海"],
        "max_pages": 2, "limit": 5,
    }
