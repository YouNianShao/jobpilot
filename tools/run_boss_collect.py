"""一次性端到端验证：用真实 Chrome 跑一遍 BOSS 采集。

用法:
    .venv\\Scripts\\python.exe tools\run_boss_collect.py [cdpUrl] [keyword]
默认 cdpUrl = http://127.0.0.1:9222（你已登录 BOSS 的那个调试 Chrome）
结果写入临时数据库，不动正式数据。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.browser import create_browser  # noqa: E402
from jobpilot.config import AppConfig  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.platforms.base import DriverContext, StopSignal  # noqa: E402
from jobpilot.platforms.boss import BossDriver  # noqa: E402
from jobpilot.safety.policy import Policy  # noqa: E402


def main() -> int:
    cdp = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "AI"

    cfg = AppConfig()
    cfg.platforms["boss"].enabled = True
    cfg.platforms["boss"].cities = ["上海"]
    cfg.platforms["boss"].keywords = [keyword]
    cfg.platforms["boss"].max_pages = 1
    cfg.safety.daily_pages.collect_search = 5
    cfg.safety.daily_pages.collect_detail = 5

    db_path = Path(tempfile.mkdtemp()) / "probe.db"
    conn = connect(db_path)
    repo = Repo(conn)

    browser = create_browser(cdp)
    print(f"CDP: {cdp}   健康检查: {browser.health()}")
    ctx = DriverContext(
        config=cfg, repo=repo, browser=browser,
        policy=Policy(repo, cfg), stop=StopSignal(), platform="boss",
        log=lambda message: print(message, flush=True),
    )

    started = time.time()
    try:
        result = BossDriver().collect(ctx)
        print(f"\n结果: new={result.new} seen={result.seen} duplicate={result.duplicate} stopped={result.stopped!r}")
        for job in repo.list_jobs(platform="boss")[:6]:
            print(f"  - {job.title} | {job.company} | {job.salary} | {job.city} | JD {len(job.jd)} 字")
    finally:
        browser.close()
        conn.close()
    print(f"耗时 {time.time() - started:.1f}s   临时库 {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
