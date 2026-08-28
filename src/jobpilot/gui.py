from __future__ import annotations

import os
import socket
import subprocess
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


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def ensure_infra() -> None:
    """确保 Chrome(9223) 与浏览器代理(3457) 常驻。缺失则拉起。"""
    if not _port_listening(CHROME_PORT):
        CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(
            [CHROME_BIN, f"--user-data-dir={CHROME_PROFILE}",
             f"--remote-debugging-port={CHROME_PORT}", "https://www.51job.com/"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    if not _port_listening(PROXY_PORT):
        env = {**os.environ,
               "BOSSHUNTER_BROWSER_PROXY_PORT": str(PROXY_PORT),
               "BOSSHUNTER_CHROME_PORTS": str(CHROME_PORT),
               "BOSSHUNTER_ENABLE_PORT_GUARD": "true"}
        subprocess.Popen(
            [NODE_BIN, str(ROOT / "src/jobpilot/browser/runtime/cdp-proxy.mjs")],
            env=env, creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    # 等待代理连上 Chrome
    deadline = time.time() + 20
    while time.time() < deadline:
        if Browser(f"http://127.0.0.1:{PROXY_PORT}").health():
            return
        time.sleep(1)


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开看板（不创建原生窗口）")
    args = ap.parse_args()

    print("[JobPilot] 正在启动运行环境...")
    ensure_infra()
    print("[JobPilot] 运行环境就绪。")

    srv = run_server("127.0.0.1", 8699)
    port = 8699
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[JobPilot] 看板地址: {url}")

    if args.browser:
        try:
            webbrowser.open(url)
        except Exception:
            print(f"[JobPilot] 无法自动打开浏览器，请手动访问: {url}")
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
    except Exception as e:  # 原生窗口创建失败（如 WebView2 缺失/损坏）
        traceback.print_exc()
        print(f"[JobPilot] 原生窗口启动失败（{e}），改用默认浏览器打开看板。")
        try:
            webbrowser.open(url)
        except Exception:
            print(f"[JobPilot] 请手动访问看板: {url}")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
