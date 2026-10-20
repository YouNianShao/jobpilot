"""服务层运行上下文：配置 + 存储 + 推理 + 安全内核 + 停止信号。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ai.llm import LlmClient
from ..config import ROOT, AppConfig
from ..db import Repo
from ..platforms.base import DriverContext, StopSignal
from ..resume_io import read_resume_file
from ..safety.policy import Policy


def _noop_log(_: str) -> None:
    pass


def _noop_progress(current: int, total: int, label: str) -> None:
    pass


def _noop_stage(_: str) -> None:
    pass


def _noop_lane(_platform: str, _info: dict) -> None:
    pass


@dataclass
class ServiceContext:
    config: AppConfig
    repo: Repo
    llm: LlmClient
    policy: Policy
    stop: StopSignal = field(default_factory=StopSignal)
    log: Callable[[str], None] = _noop_log
    progress: Callable[[int, int, str], None] = _noop_progress
    # 全流程走到哪一步了（collect / score / greet / apply），任务监控台的步骤条用
    stage: Callable[[str], None] = _noop_stage
    # 所有平台轮流投递时，每个平台各自的进度（任务监控台拆成四条小进度条用）
    lane: Callable[[str, dict], None] = _noop_lane
    # 测试注入假浏览器用；None 表示用真实的 patchright
    browser_factory: Callable[[], Any] | None = None

    def resume_text(self) -> tuple[str, str]:
        """读简历正文，返回 (文本, 问题)。所有服务共用这一个入口。

        为什么必须统一：以前四个服务各读各的，还各有各的坑——
        评分在文件读不到时会静默降级成「（未提供简历）」照样打分（分数全是假的），
        招呼语同样；而相对路径只有定制简历会按项目根解析，别的服务按当前工作目录。
        """
        raw = (self.config.profile.resume_path or "").strip()
        if not raw:
            return "", "还没有配置简历（设置 → 简历与话术）"
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            return "", f"简历文件不存在：{path}（设置 → 简历与话术）"
        # .md / .txt / .docx / .pdf 都走同一个解析入口（带缓存，PDF 不会每次请求都重新解析）
        text, problem = read_resume_file(path)
        if problem:
            return "", f"{problem}：{path}"
        return text, ""

    def create_browser(self):
        if self.browser_factory is not None:
            browser = self.browser_factory()
        else:
            from ..browser import create_browser

            browser = create_browser(self.config.browser.cdp_url)
        # 记下这个任务开过的浏览器连接：「停止」时如果任务卡在一句不返回的页面调用里，靠它强制断开
        self.__dict__.setdefault("_browsers", []).append(browser)
        # 任务等待（间隔 / 拟人停留 / 冷却）时让这个连接继续处理浏览器消息，见 StopSignal.sleep
        pump = getattr(browser, "pump", None)
        if pump is not None and hasattr(self.stop, "add_pump"):
            self.stop.add_pump(pump)
        return browser

    def kill_browsers(self) -> int:
        """强制断开这个任务的所有浏览器驱动（不碰 Chrome）；返回断开了几个。"""
        killed = 0
        for browser in self.__dict__.get("_browsers", []):
            kill = getattr(browser, "kill_driver", None)
            if kill is not None:
                kill()
                killed += 1
        return killed

    def driver_context(self, browser, platform: str) -> DriverContext:
        return DriverContext(
            config=self.config, repo=self.repo, browser=browser,
            policy=self.policy, stop=self.stop, platform=platform,
            log=self.log, progress=self.progress,
        )
