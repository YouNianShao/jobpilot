"""调试：智联 JS_LIST 在真实页面上到底能给多少条匹配到岗位号。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.browser import create_browser  # noqa: E402
from jobpilot.platforms.zhaopin import JS_LIST, search_url  # noqa: E402

browser = create_browser("http://127.0.0.1:9222")
page = browser.open(search_url("数据分析"))
print("wait .job-card:", page.wait_for(".job-card", timeout_ms=15000))
raw = page.eval(JS_LIST)
items = json.loads(raw) if isinstance(raw, str) else (raw or [])
print("卡片数:", len(items))
with_number = [item for item in items if item.get("number")]
print("带岗位号的:", len(with_number))
print("前 4 条:")
for item in items[:4]:
    print(f"   number={item.get('number') or '(空)':<26} title={item.get('title')}")

# 页面里 positionName / positionNumber 的实际数量与配对情况
probe = page.eval("""(() => {
  const html = document.documentElement.innerHTML;
  return JSON.stringify({
    names: (html.match(/"positionName":"[^"]{1,80}"/g) || []).length,
    numbers: (html.match(/"positionNumber":"[A-Za-z0-9]{6,}"/g) || []).length,
    pairs: (html.match(/"positionName":"[^"]{1,80}"[^}]{0,300}?"positionNumber":"[A-Za-z0-9]{6,}"/g) || []).length,
    firstPair: ((html.match(/"positionName":"([^"]{1,80})"[^}]{0,300}?"positionNumber":"([A-Za-z0-9]{6,})"/) || []).slice(1)
  });
})()""")
print("页面内嵌 JSON:", probe)
page.close()
browser.close()
