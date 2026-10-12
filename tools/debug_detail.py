"""调试：patchright 打开详情页后，到底能不能等到 JD 容器。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.browser import create_browser  # noqa: E402

TARGETS = [
    ("51job", "https://jobs.51job.com/all/173851192.html", ".bmsg"),
    ("zhaopin", "https://www.zhaopin.com/jobdetail/CCL1493228500J40928543912.htm",
     ".describtion-card__content-wrap, .seo-card__content"),
]

browser = create_browser("http://127.0.0.1:9222")
for name, url, selector in TARGETS:
    print("=" * 60)
    print(name, url)
    started = time.time()
    page = browser.open(url)
    print(f"  打开耗时 {time.time() - started:.1f}s  url={page.url}")
    try:
        print(f"  wait_for({selector}) ->", page.wait_for(selector, timeout_ms=15000))
    except Exception as exc:  # noqa: BLE001
        print("  wait_for 抛错:", type(exc).__name__, exc)
    for probe in (".bmsg", ".describtion-card__content-wrap", ".seo-card__content",
                  ".job-detail", "body"):
        try:
            print(f"   exists({probe:<40}) = {page.exists(probe)}")
        except Exception as exc:  # noqa: BLE001
            print(f"   exists({probe}) 抛错: {exc}")
    try:
        print("  文本前 160 字:", page.text()[:160].replace("\n", " "))
    except Exception as exc:  # noqa: BLE001
        print("  text 抛错:", exc)
    page.close()
browser.close()
