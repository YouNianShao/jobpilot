r"""前端自检：**真的把页面跑起来**，抓 JS 异常并检查关键 DOM 是否被填充。

为什么必须用它：`node --check` 对一个少了右括号的 app.js 会返回 0（漏报），
只有真的加载页面才能发现——这次就是这么抓到一个"整页空白"的语法错误的。

用法: python tools\check_ui.py [url]
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CDP = ROOT / "tools" / "cdp.mjs"
PROBE = ROOT / "tools" / "page_probe.js"
NODE_CANDIDATES = [
    r"C:\Users\Admin\.workbuddy\binaries\node\versions\22.22.2\node.exe",
    shutil.which("node") or "",
]


def node_bin() -> str:
    for candidate in NODE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 node")


def cdp(*args: str) -> str:
    result = subprocess.run([node_bin(), str(CDP), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    return (result.stdout or "").strip()


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8700/"
    # 加时间戳：刚改完 index.html 时浏览器可能还拿着旧缓存，会误报"页面抛异常"
    sep = "&" if "?" in url else "?"
    target = cdp("open", f"{url}{sep}v={int(time.time())}")
    if not target:
        print("打不开页面：", url)
        return 1
    time.sleep(3)
    watch = cdp("watch", target, "8")
    probe = cdp("eval", target, str(PROBE))
    cdp("close", target)

    problems: list[str] = []
    if "EXCEPTION" in watch:
        problems.append("页面抛异常：\n" + watch)
    try:
        data = json.loads(probe)
    except ValueError:
        problems.append("自检脚本没返回 JSON：" + probe[:200])
        data = {}
    for key, label in (("platformOptions", "平台下拉"),
                       ("kpiCards", "KPI 卡片"),
                       ("riskChildren", "风控面板")):
        if not data.get(key):
            problems.append(f"{label}没有渲染（{key} = {data.get(key)}）")
    if data.get("browserText") == "检测中…":
        problems.append('前端没有完成首次刷新（浏览器状态仍是「检测中…」）')
    for name, info in (data.get("views") or {}).items():
        if info.get("missing"):
            problems.append(f"{name} 视图不存在（导航里找不到）")
        elif not info.get("active"):
            problems.append(f"{name} 视图点了没切换过去")
        elif info.get("nodes", 0) < 12 or info.get("text", 0) < 8:
            problems.append(
                f"{name} 视图几乎是空白的（nodes={info.get('nodes')} text={info.get('text')}）"
            )

    if problems:
        print("前端自检失败：")
        for item in problems:
            print("  -", item)
        return 1
    views = " · ".join(f"{k}:{v.get('nodes')}" for k, v in (data.get("views") or {}).items())
    print("前端自检通过：平台", data.get("platformOptions"), "项 · KPI", data.get("kpiCards"),
          "张 · 风控", data.get("riskChildren"), "块 ·", data.get("browserText"), "· 视图", views)
    return 0


if __name__ == "__main__":
    sys.exit(main())
