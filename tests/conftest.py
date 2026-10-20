"""公共 fixture。"""
from __future__ import annotations

import pytest

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect


@pytest.fixture(autouse=True)
def _never_touch_real_data(tmp_path_factory, monkeypatch):
    """所有测试一律把「项目根 / 数据目录 / 账号登记」指到临时目录。

    2026-10-15 真实事故：保存配置的接口会把共用设置（AI / 风控）同步到「其它账号」，
    测试只把当前账号的 CONFIG_PATH 指到了临时目录，同步却按真实的账号登记去写 ——
    每跑一次测试，真实的账号 A 配置就被默认值（空 API Key）覆盖一次。
    这里兜底：哪个测试忘了隔离，写的也只是临时目录；下面再加一道绊线确认真实文件没被动。
    """
    import importlib

    import jobpilot.accounts as accounts
    import jobpilot.config as cfgmod

    root = tmp_path_factory.mktemp("jp_root")
    data = root / "data"
    data.mkdir()
    monkeypatch.setattr(cfgmod, "ROOT", root)
    monkeypatch.setattr(cfgmod, "DATA_DIR", data)
    monkeypatch.setattr(accounts, "REGISTRY", data / "accounts.json")
    targets = [cfgmod]
    for name in ("jobpilot.bootstrap", "jobpilot.web.app", "jobpilot.services.backup", "jobpilot.cli"):
        try:
            targets.append(importlib.import_module(name))
        except ImportError:
            pass
    for mod in targets:
        for attr, value in (("CONFIG_PATH", root / "config.yaml"), ("DB_PATH", data / "jobpilot.db"), ("ACCOUNT_DIR", data)):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, value)
    yield


@pytest.fixture(autouse=True)
def _no_human_dwell(request, monkeypatch):
    """拟人停留默认关掉：很多测试用真的 StopSignal，详情页每页读 4~14 秒会让整套测试慢到不能跑。

    专门测拟人的用例加 ``@pytest.mark.humanize`` 就会照配置走。
    """
    if request.node.get_closest_marker("humanize"):
        return
    import jobpilot.browser.humanize as humanize

    monkeypatch.setattr(humanize, "_cfg", lambda ctx: None)


def pytest_configure(config):
    config.addinivalue_line("markers", "humanize: 不关闭拟人停留（测 browser/humanize.py 用）")


_REAL_FILES: dict = {}


def pytest_sessionstart(session):
    """绊线：记下真实配置 / 账号登记的内容，测试跑完必须一个字节都没变。"""
    from pathlib import Path

    real_root = Path(__file__).resolve().parents[1]
    files = [real_root / "config.yaml", real_root / "data" / "accounts.json"]
    files += list((real_root / "data" / "accounts").glob("*/config.yaml"))
    _REAL_FILES.update({f: f.read_bytes() for f in files if f.exists()})


def pytest_sessionfinish(session, exitstatus):
    changed = [str(f) for f, raw in _REAL_FILES.items() if not f.exists() or f.read_bytes() != raw]
    if changed:
        session.exitstatus = 1
        print("\n!!! 测试改动了真实数据文件（必须修测试隔离）：" + "、".join(changed))


@pytest.fixture
def repo(tmp_path):
    conn = connect(tmp_path / "test.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture
def config():
    return AppConfig()


def open_time_gates(cfg):
    """把所有跟「真实时间 / 随机数」有关的闸门关掉（测试专用）。

    为什么收成一个函数：以前每个测试文件各写一行 send_windows = []，后来风控里
    加了 ctive_windows（允许开页面的时段）没人想起来同步 —— 结果就是白天全绿、
    凌晨 0-6 点集体失败。这种「时间依赖的假绿」比真 bug 难查多了。
    要专门验某个闸门的用例，在自己的用例里再把它单独打开即可。
    """
    cfg.safety.send_windows = []
    cfg.safety.active_windows = []
    cfg.safety.day_off_probability = 0.0
    return cfg
