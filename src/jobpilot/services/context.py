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


@dataclass
class ServiceContext:
    config: AppConfig
    repo: Repo
    llm: LlmClient
    policy: Policy
    stop: StopSignal = field(default_factory=StopSignal)
    log: Callable[[str], None] = _noop_log
    progress: Callable[[int, int, str], None] = _noop_progress
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
            return self.browser_factory()
        from ..browser import create_browser

        return create_browser(self.config.browser.cdp_url)

    def driver_context(self, browser, platform: str) -> DriverContext:
        return DriverContext(
            config=self.config, repo=self.repo, browser=browser,
            policy=self.policy, stop=self.stop, platform=platform,
            log=self.log, progress=self.progress,
        )
