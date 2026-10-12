"""系统体检：能跑 / 不能跑 / 半能跑，都要说清楚。"""
from __future__ import annotations

from jobpilot.browser.fake import FakeBrowser
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.health import run_checks


class DownBrowser:
    def health(self) -> bool:
        return False

    def close(self) -> None:
        pass


def make_ctx(tmp_path, *, browser=None, key="sk-test", resume=True):
    cfg = AppConfig()
    cfg.ai.api_key = key
    cfg.safety.active_windows = []          # 体检不该被时段影响
    if resume:
        path = tmp_path / "resume.md"
        path.write_text("6 年数据分析经验。", encoding="utf-8")
        cfg.profile.resume_path = str(path)
    else:
        cfg.profile.resume_path = ""
    repo = Repo(connect(tmp_path / "h.db"))
    factory = (lambda: browser) if browser is not None else (lambda: FakeBrowser([]))
    return ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg),
                          stop=StopSignal(), browser_factory=factory)


def _by_key(report):
    return {row["key"]: row for row in report["checks"]}


def test_checks_pass_in_a_healthy_setup(tmp_path):
    report = run_checks(make_ctx(tmp_path))

    rows = _by_key(report)
    assert rows["browser"]["ok"] is True
    assert rows["resume"]["ok"] is True and rows["resume"]["detail"].endswith("字")
    assert rows["db"]["ok"] is True
    assert rows["ai_key"]["ok"] is True
    assert "browser" not in report["failed"]


def test_checks_flag_browser_down_as_hard_failure(tmp_path):
    report = run_checks(make_ctx(tmp_path, browser=DownBrowser()))

    assert report["ok"] is False
    assert "browser" in report["failed"]
    rows = _by_key(report)
    assert rows["browser"]["level"] == "error"
    assert "remote-debugging-port" in rows["browser"]["fix"]


def test_checks_treat_missing_key_or_resume_as_hard_failures(tmp_path):
    """缺 key / 缺简历 = 评分、招呼语、面试准备、跟进全线不可用 —— 这是必检项，不是"注意"。"""
    report = run_checks(make_ctx(tmp_path, key="", resume=False))

    assert report["ok"] is False
    assert {"ai_key", "resume"} <= set(report["failed"])
    rows = _by_key(report)
    assert rows["ai_key"]["level"] == "error" and rows["ai_key"]["fix"]
    assert rows["resume"]["level"] == "error"


def test_checks_flag_unsupported_apply_switch(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.config.platforms["51job"].apply = True     # 开关开着，但驱动没声明 apply

    report = run_checks(ctx)

    rows = _by_key(report)
    assert rows["apply_switch"]["ok"] is False
    assert "51job" in rows["apply_switch"]["detail"]
