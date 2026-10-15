"""帮用户把「带调试端口的 Chrome」开起来。

JobPilot 不自己登录招聘网站：它连的是用户自己开着、已经登录好的 Chrome（CDP）。
以前这个 Chrome 是旧版 JobPilot 的启动器顺手拉起来的，旧版一删，就没人开它了 ——
点「采集」只会得到一句 ``connect ECONNREFUSED 127.0.0.1:9222``。

这里只做一件事：没有在跑，就用**独立的** profile 目录（``chrome-profile/``）开一个；
登录态存在这个目录里，下次再开还在。已经在跑就什么都不做（不重复开、不碰用户的其它 Chrome）。
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)


def debug_port(cdp_url: str) -> tuple[str, int]:
    parts = urlsplit(cdp_url or "http://127.0.0.1:9333")
    return parts.hostname or "127.0.0.1", parts.port or 9333


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_headless(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口上是不是一个**无头** Chrome（看不见窗口、也没有用户的登录态）。

    真实故障：别的自动化脚本（DrissionPage 之类）用临时目录在 9222 起了个无头 Chrome，
    JobPilot 以为「已经开着」什么都不做，点「启动浏览器」就像没反应。
    """
    import json
    from urllib.request import urlopen

    try:
        with urlopen(f"http://{host}:{port}/json/version", timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return False
    return "headless" in str(info.get("User-Agent", "")).lower()


def occupied_message(port: int) -> str:
    return (f"调试端口 {port} 被别的程序占着：上面是一个看不见窗口的无头 Chrome（没有你的登录），"
            "通常是另一个自动化工具（比如 BossHunter 的 Start.py）开的。"
            "请先关掉那个程序（或在任务管理器里结束它的 Chrome），再点「启动浏览器」。")


def find_chrome() -> str | None:
    for raw in CANDIDATES:
        path = os.path.expandvars(raw)
        if Path(path).is_file():
            return path
    return None


def launch(cdp_url: str, profile_dir: Path, *, start_url: str = "https://www.zhipin.com/",
           wait_seconds: float = 10.0) -> dict:
    """返回 ``{ok, already, error, profile}``；``error`` 是给用户看的一句中文。"""
    host, port = debug_port(cdp_url)
    if host not in ("127.0.0.1", "localhost"):
        return {"ok": False, "error": f"配置里的浏览器地址是 {host}，不是本机，没法替你打开"}
    if port_open(host, port):
        if is_headless(host, port):
            return {"ok": False, "occupied": True, "error": occupied_message(port)}
        return {"ok": True, "already": True, "profile": str(profile_dir)}
    chrome = find_chrome()
    if not chrome:
        return {"ok": False, "error": "这台电脑上没找到 Chrome 或 Edge，请先安装 Chrome"}
    profile_dir.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
         "--no-first-run", "--no-default-browser-check", start_url],
        creationflags=flags, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if port_open(host, port):
            return {"ok": True, "already": False, "profile": str(profile_dir)}
        time.sleep(0.3)
    return {"ok": False, "error": f"Chrome 开了，但 {wait_seconds:.0f} 秒内调试端口 {port} 没有响应。"
                                  "如果你已经开着别的 Chrome 窗口，请把它们全部关掉再试"}
