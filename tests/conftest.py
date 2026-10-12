"""公共 fixture。"""
from __future__ import annotations

import pytest

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect


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
