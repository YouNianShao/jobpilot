from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import webview

from jobpilot.browser import Browser
from jobpilot.web import run_server

ROOT = Path(__file__).resolve().parents[2]
CHROME_BIN = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
NODE_BIN = r"C:\Users\Admin\.workbuddy\binaries\node\versions\22.22.2\node.exe"
PROXY_PORT = 3457
CHROME_PORT = 9223
CHROME_PROFILE = ROOT / "chrome-profile"
DATA_DIR = ROOT / "data"
APP_LOG = DATA_DIR / "jobpilot.log"
PROXY_LOG = DATA_DIR / "proxy.log"

# 0x08000000：以无控制台窗口方式启动子进程（node 代理不再弹出黑窗）
CREATE_NO_WINDOW = 0x08000000


def _log(message: str) -> None:
    """无控制台运行时（pythonw）把关键信息写入 data/jobpilot.log，便于排查。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(APP_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _resolve_node() -> str | None:
    if Path(NODE_BIN).exists():
        return NODE_BIN
    return shutil.which("node")


def _resolve_chrome() -> str | None:
    candidates = [
        CHROME_BIN,
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    return None


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def ensure_infra() -> None:
    """确保 Chrome(9223) 与浏览器代理(3457) 常驻；缺失则静默拉起。

    关键点：node 代理用 CREATE_NO_WINDOW 启动（不再弹第二个控制台窗口），
    输出重定向到 data/proxy.log。
    """
    if not _port_listening(CHROME_PORT):
        chrome = _resolve_chrome()
        if chrome:
            CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
            _log("启动 Chrome 调试实例 (9223)")
            subprocess.Popen(
                [chrome, f"--user-data-dir={CHROME_PROFILE}",
                 f"--remote-debugging-port={CHROME_PORT}", "https://www.51job.com/"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                close_fds=True,
            )
        else:
            _log("未找到 Chrome，跳过启动（请手动开启远程调试 9223）")

    if not _port_listening(PROXY_PORT):
        node = _resolve_node()
        if node:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _log("启动浏览器代理 (3457)，日志写入 data/proxy.log")
            env = {**os.environ,
                   "BOSSHUNTER_BROWSER_PROXY_PORT": str(PROXY_PORT),
                   "BOSSHUNTER_CHROME_PORTS": str(CHROME_PORT),
                   "BOSSHUNTER_ENABLE_PORT_GUARD": "true"}
            with open(PROXY_LOG, "a", encoding="utf-8", buffering=1) as log:
                subprocess.Popen(
                    [node, str(ROOT / "src/jobpilot/browser/runtime/cdp-proxy.mjs")],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
        else:
            _log("未找到 node，无法启动浏览器代理")

    deadline = time.time() + 20
    while time.time() < deadline:
        if Browser(f"http://127.0.0.1:{PROXY_PORT}").health():
            _log("运行环境就绪")
            return
        time.sleep(1)
    _log("等待浏览器代理就绪超时（继续启动看板，稍后可重试）")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开看板（不创建原生窗口）")
    args = ap.parse_args()

    _log("---- JobPilot 启动 ----")
    print("[JobPilot] 正在启动运行环境...")
    ensure_infra()
    print("[JobPilot] 运行环境就绪。")

    srv = run_server("127.0.0.1", 8699)
    port = 8699
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[JobPilot] 看板地址: {url}")
    _log(f"看板地址: {url}")

    if args.browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            _log("无法自动打开浏览器: " + traceback.format_exc())
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    try:
        webview.create_window(
            "JobPilot 求职领航",
            url,
            width=1100,
            height=820,
            text_select=True,
        )
        webview.start()
    except Exception as e:  # noqa: BLE001 - 原生窗口创建失败（如 WebView2 缺失/损坏）
        _log(f"原生窗口启动失败({e})，改用默认浏览器")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            _log("无法打开浏览器: " + traceback.format_exc())
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - pythonw 无 stderr，异常必须落盘
        _log("启动失败: " + traceback.format_exc())
        sys.exit(1)