"""调试：驱动里的 JS_DETAIL 到底返回了什么。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.browser import create_browser  # noqa: E402
from jobpilot.platforms.job51 import JOB_URL, JS_DETAIL  # noqa: E402

print("JS_DETAIL 前 80 字:", repr(JS_DETAIL[:80]))
browser = create_browser("http://127.0.0.1:9333")
page = browser.open(JOB_URL.format(job_id="173851192"))
print("wait_for(.bmsg):", page.wait_for(".bmsg", timeout_ms=15000))
result = page.eval(JS_DETAIL)
print("eval 类型:", type(result).__name__)
print("eval 长度:", len(result) if result else 0)
print("eval 前 120 字:", repr((result or "")[:120]))

# 对照：直接用原生表达式
raw = page.eval("document.querySelector('.bmsg') ? document.querySelector('.bmsg').innerText.slice(0,80) : 'NONE'")
print("原生表达式:", repr(raw))
page.close()
browser.close()
