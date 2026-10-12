"""重新抓 BOSS 的城市表（来源是 BOSS 自己的公开接口，不抄任何第三方仓库）。

用法: python tools\refresh_boss_cities.py
产出: src/jobpilot/platforms/boss_cities.py（覆盖写入；改完跑一遍 pytest）
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://www.zhipin.com/wapi/zpCommon/data/cityGroup.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "jobpilot" / "platforms" / "boss_cities.py"

# 常用城市：界面上的快捷选择，属于产品选择，不是抓来的数据
HOT = ["北京", "上海", "深圳", "广州", "杭州", "成都", "武汉", "南京", "西安", "苏州",
       "天津", "重庆", "郑州", "长沙", "东莞", "佛山", "合肥", "厦门", "青岛", "大连",
       "宁波", "无锡", "济南", "福州", "昆明", "南昌", "沈阳", "哈尔滨", "石家庄", "太原"]

HEAD = [
    '"""BOSS 直聘的城市编码表（9 位数字，来自 BOSS 公开接口）。',
    "",
    "来源: {url}",
    "抓取: {today}（共 {count} 个城市）",
    "",
    "为什么单独放一个模块：以前只内置 20 个城市，用户填了别的城市会被**静默跳过**，",
    "采集看起来「跑了但什么都没采到」。城市表要么全，要么让界面明确说清哪些不支持。",
    "",
    "刷新用 tools/refresh_boss_cities.py，不要手改这个文件。",
    '"""',
    "",
    "from __future__ import annotations",
    "",
]


def fetch() -> dict[str, str]:
    request = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    codes: dict[str, str] = {}
    for group in payload["zpData"]["cityGroup"]:
        for city in group.get("cityList") or []:
            name = str(city.get("name") or "").strip()
            code = str(city.get("code") or "").strip()
            if name and code.isdigit():
                codes.setdefault(name, code)
    if len(codes) < 100:
        raise SystemExit(f"只抓到 {len(codes)} 个城市，接口可能变了，先别覆盖文件")
    return codes


def main() -> int:
    codes = fetch()
    hot = [name for name in HOT if name in codes]
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [line.format(url=URL, today=today, count=len(codes)) for line in HEAD]
    lines.append(f"HOT_CITIES: tuple[str, ...] = {tuple(hot)!r}")
    lines.append("")
    lines.append("CITY_CODES: dict[str, str] = {")
    lines += [f'    "{name}": "{code}",' for name, code in sorted(codes.items(), key=lambda kv: kv[1])]
    lines += ["}", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"写入 {OUT}（{len(codes)} 个城市，常用 {len(hot)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
