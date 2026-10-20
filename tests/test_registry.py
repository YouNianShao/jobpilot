"""平台驱动注册表与协议。"""
from __future__ import annotations

import pytest

from jobpilot.models import ApplyResult, CollectResult, Job, MonitorResult
from jobpilot.platforms.base import StopRequested, StopSignal, supports
from jobpilot.platforms.registry import Registry, build_default_registry


class FakeDriver:
    key = "fake"
    name = "假平台"
    capabilities = frozenset({"collect"})

    def collect(self, ctx) -> CollectResult:
        return CollectResult(platform=self.key, new=1)

    def apply(self, ctx, job: Job) -> ApplyResult:
        return ApplyResult(status="unsupported")

    def monitor(self, ctx) -> MonitorResult:
        return MonitorResult()


def test_register_and_lookup():
    registry = Registry()
    registry.register(FakeDriver())
    assert registry.keys() == ("fake",)
    assert registry.get("fake").name == "假平台"
    assert registry.supports("fake", "collect") is True
    assert registry.supports("fake", "apply") is False
    with pytest.raises(KeyError):
        registry.require("nope")


def test_supports_helper():
    assert supports(FakeDriver(), "collect") is True
    assert supports(FakeDriver(), "monitor") is False


def test_stop_signal_check_raises():
    signal = StopSignal()
    signal.check()            # 未停止时不抛
    signal.request()
    with pytest.raises(StopRequested):
        signal.check()


def test_default_registry_builds():
    registry = build_default_registry()
    assert isinstance(registry, Registry)
    # BOSS 采集 + 投递已实机核验（2026-10-10）；聊天监测仍未核验
    assert "boss" in registry.keys()
    assert registry.supports("boss", "collect") is True
    assert registry.supports("boss", "apply") is True
    assert registry.supports("boss", "monitor") is True

# ---------------- 能力门：跑不了就直说，别让用户白等一场 ----------------
def gate_config(enabled: bool = True, apply: bool = True):
    from jobpilot.config import AppConfig, PlatformCfg

    cfg = AppConfig()
    cfg.platforms = {"fake": PlatformCfg(enabled=enabled, apply=apply)}
    return cfg


def test_check_mode_blocks_unverified_capability():
    from jobpilot.services.orchestrator import check_mode

    registry = Registry()
    registry.register(FakeDriver())          # 只声明 collect
    assert check_mode(gate_config(), "collect", "fake", registry=registry) is None
    apply_reason = check_mode(gate_config(), "apply", "fake", registry=registry)
    assert apply_reason and "自动投递" in apply_reason
    monitor_reason = check_mode(gate_config(), "monitor", "fake", registry=registry)
    assert monitor_reason and "HR 监测" in monitor_reason


def test_check_mode_respects_platform_switch_and_enabled():
    from jobpilot.services.orchestrator import check_mode

    class Applier(FakeDriver):
        capabilities = frozenset({"collect", "apply"})

    registry = Registry()
    registry.register(Applier())
    # 驱动支持投递，但设置里关掉了「允许对该平台投递」
    reason = check_mode(gate_config(apply=False), "apply", "fake", registry=registry)
    assert reason and "允许对该平台投递" in reason
    assert check_mode(gate_config(apply=True), "apply", "fake", registry=registry) is None
    # 平台整体停用
    disabled = check_mode(gate_config(enabled=False), "collect", "fake", registry=registry)
    assert disabled and "停用" in disabled


def test_check_mode_needs_platform_for_page_modes():
    from jobpilot.services.orchestrator import check_mode

    registry = Registry()
    registry.register(FakeDriver())
    assert "需要指定平台" in (check_mode(gate_config(), "collect", "", registry=registry) or "")
    # confirm / followup 是纯数据库 / 纯文本，不需要平台
    assert check_mode(gate_config(), "confirm", None, registry=registry) is None
    assert check_mode(gate_config(), "followup", None, registry=registry) is None


def test_task_runner_refuses_unrunnable_mode(tmp_path):
    from jobpilot.services.orchestrator import TaskNotRunnable
    from jobpilot.tasks import TaskRunner

    cfg = gate_config()
    runner = TaskRunner(lambda: type("Ctx", (), {"config": cfg, "repo": type("R", (), {"conn": None})()})())
    with pytest.raises(TaskNotRunnable) as exc:
        runner.start("apply", "fake")     # 默认注册表里没有 fake，且 fake 能力也不支持
    assert "fake" in str(exc.value)
