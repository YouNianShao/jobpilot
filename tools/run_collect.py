"""一次性验证：对指定平台跑一遍真实采集（临时库，不动正式数据）。

用法: python tools\run_collect.py --platform 51job --keyword 数据分析
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.ai.llm import FakeLlm  # noqa: E402
from jobpilot.browser import create_browser  # noqa: E402
from jobpilot.config import AppConfig  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.platforms.base import StopSignal  # noqa: E402
from jobpilot.safety.policy import Policy  # noqa: E402
from jobpilot.services.collector import collect  # noqa: E402
from jobpilot.services.context import ServiceContext  # noqa: E402


def _arg(name: str, default: str) -> str:
    argv = sys.argv[1:]
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def main() -> int:
    platform = _arg("--platform", "51job")
    keyword = _arg("--keyword", "数据分析")
    cdp = _arg("--cdp", "http://127.0.0.1:9222")

    cfg = AppConfig()
    plat = cfg.platforms[platform]
    plat.enabled = True
    plat.cities = ["上海"]
    plat.keywords = [keyword]
    pages = _arg("--pages", "1")
    plat.max_pages = int(pages) if str(pages).isdigit() else 1
    if "--detail" in sys.argv:
        plat.fetch_detail = True
    cfg.safety.daily_pages.collect_search = 3
    cfg.safety.daily_pages.collect_detail = 3

    db_path = Path(tempfile.mkdtemp()) / "collect.db"
    repo = Repo(connect(db_path))
    ctx = ServiceContext(
        config=cfg, repo=repo, llm=FakeLlm(), policy=Policy(repo, cfg), stop=StopSignal(),
        log=lambda message: print("   " + message, flush=True),
        browser_factory=lambda: create_browser(cdp),
    )
    print(f"  fetch_detail = {plat.fetch_detail}")
    result = collect(ctx, platform)
    print(f"\n== {platform} ==")
    print(result)
    jobs = repo.list_jobs(platform=platform)
    with_jd = [job for job in jobs if job.jd]
    with_url = [job for job in jobs if job.url]
    print(f"  有 JD 的岗位：{len(with_jd)} / {len(jobs)}    JD 字数：{[len(j.jd) for j in with_jd]}")
    print(f"  有链接的岗位：{len(with_url)} / {len(jobs)}")
    for job in jobs[:5]:
        print(f"  - {job.title} | {job.company} | {job.salary} | {job.city} | url={'有' if job.url else '无'} | jd={len(job.jd)}字")
    repo.conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
