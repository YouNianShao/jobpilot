r"""一次性核验：用真实 Chrome 跑一遍 BOSS 聊天列表监测（只读列表，不进会话）。

用法:
    .venv\Scripts\python.exe tools\run_boss_monitor.py [cdpUrl]
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
    cdp = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9333"

    cfg = AppConfig()
    cfg.platforms["boss"].enabled = True
    cfg.safety.daily_pages.monitor = 40
    # 核验脚本会在非允许时段跑：这是为了拿证据，正式任务仍然照常被闸门挡住
    cfg.safety.active_windows = []
    cfg.safety.send_windows = []
    cfg.safety.day_off_probability = 0.0

    db_path = Path(tempfile.mkdtemp()) / "monitor.db"
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
        result = BossDriver().monitor(ctx)
        print(f"\n结果: checked={result.checked} events={result.events}")
        STATE, HR, COMPANY, TEXT = "state", "hr_name", "company", "last_text"
        for row in repo.list_interactions()[:20]:
            state, hr, company = row[STATE], row[HR], row[COMPANY]
            print(f"  - {state!s:<9} {hr!s:<8} {company!s:<18} {row[TEXT][:36]}")
    except Exception as exc:  # noqa: BLE001 - 核验脚本：连不上/要登录都要说清楚
        print(f"监测中断: {type(exc).__name__} {exc}")
    finally:
        browser.close()
        conn.close()
    print(f"耗时 {time.time() - started:.1f}s   临时库 {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
