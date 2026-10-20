"""patchright 实现的真实浏览器驱动：连接用户**已在运行**的 Chrome（CDP）。

原则：
- 只连接，不启动、不关闭用户的浏览器（close() 只断开 Playwright 驱动）
- 复用用户的登录态（connect_over_cdp 拿到的就是真实 profile）
- 不做指纹伪装；风控交给 safety 层
"""

from __future__ import annotations

from typing import Any

from .driver import Page

NAV_TIMEOUT_MS = 45000          # 只等到服务器开始回页面；国内招聘站慢的时候 20 秒不够（见 goto）
CLICK_TIMEOUT_MS = 8000
# 连浏览器的超时：默认 3 分钟，浏览器状态异常时任务就干等 3 分钟才报错（2026-10-15 真踩过）
CONNECT_TIMEOUT_MS = 20000

# 驱动进程挂了 / 连接断了时 patchright 抛出的原文：碰到这些就重连一次再试
_DEAD = ("Connection closed", "Target page, context or browser has been closed", "Browser has been closed",
         "browser has disconnected", "Target closed")
_PATCH_CHECKED = False


def is_dead_connection(exc: BaseException) -> bool:
    return any(word in str(exc) for word in _DEAD)


# 单次页面操作（读正文 / 执行脚本 / 等元素…）最长等多久。2026-10-16 真机：智联的页面整页卡死
# （连 CDP 直接执行 1+1 都不回），而 patchright 的 evaluate 没有超时 —— 任务线程永远卡在那一句，
# 点「停止」也停不下来；驱动一直挂在浏览器上，你在同一个浏览器里点登录也一直转圈。
CALL_TIMEOUT_S = 35.0


class PageHung(RuntimeError):
    """页面在 CALL_TIMEOUT_S 秒内没有任何响应：驱动已被强制断开，浏览器本身不受影响。"""


_PROBE_JS = r"""
const [base, ms] = [process.argv[2], Number(process.argv[3])];
const list = await (await fetch(base + '/json/list')).json();
const dead = [];
for (const t of list.filter((x) => x.type === 'page' && x.webSocketDebuggerUrl)) {
  const alive = await new Promise((done) => {
    let ws; const timer = setTimeout(() => { try { ws.close(); } catch (e) {} done(false); }, ms);
    try { ws = new WebSocket(t.webSocketDebuggerUrl); } catch (e) { clearTimeout(timer); done(false); return; }
    ws.onopen = () => ws.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression: '1+1' } }));
    ws.onmessage = (e) => { const m = JSON.parse(e.data); if (m.id === 1) { clearTimeout(timer); ws.close(); done(true); } };
    ws.onerror = () => { clearTimeout(timer); done(false); };
  });
  if (!alive) dead.push({ id: t.id, url: t.url });
}
console.log(JSON.stringify(dead));
"""


def close_frozen_tabs(cdp_url: str, probe_s: float = 4.0) -> list[str]:
    """关掉「整页卡死」的标签页：对每个页面执行一次 1+1，probe_s 秒不回的就关掉。

    卡死的页面会让 connect_over_cdp 一直等它（接管时它不回话，2026-10-16 实测），
    之后所有任务都连不上浏览器。正常的页（包括你正在用的）都会立刻回 2，不会被关。
    探测用 patchright 自带的 node（它有 WebSocket），不额外装依赖。返回关掉的网址。
    """
    import json
    import subprocess
    import tempfile
    import urllib.request
    from pathlib import Path as _Path

    try:
        import patchright
    except ImportError:
        return []
    node = _Path(patchright.__file__).parent / "driver" / ("node.exe" if _Path("C:/").exists() else "node")
    if not node.exists():
        return []
    base = cdp_url.rstrip("/")
    with tempfile.TemporaryDirectory() as tmp:
        script = _Path(tmp) / "probe.mjs"
        script.write_text(_PROBE_JS, encoding="utf-8")
        try:
            run = subprocess.run([str(node), str(script), base, str(int(probe_s * 1000))], capture_output=True,
                                 text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            dead = json.loads((run.stdout or "[]").strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return []
    closed = []
    for target in dead:
        try:
            urllib.request.urlopen(base + "/json/close/" + target["id"], timeout=3).close()
            closed.append(target.get("url", ""))
        except OSError:
            pass
    return closed


class PatchrightPage:
    def __init__(self, page: Any, owner: PatchrightBrowser | None = None) -> None:
        self._page = page
        self._owner = owner

    def _guard(self, fn, *args, **kwargs):
        """给没有超时的调用加一道看门狗：超时就强制断开驱动，让这一句以异常返回，而不是永远卡住。"""
        if self._owner is None:
            return fn(*args, **kwargs)
        return self._owner.guarded(fn, *args, **kwargs)

    @property
    def url(self) -> str:
        return self._page.url or ""

    @property
    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:  # noqa: BLE001 - 页面可能已关闭
            return ""

    def goto(self, url: str) -> None:
        """打开网址。只等到服务器开始回页面（commit），之后的「等内容出来」交给调用方的 wait_for。

        2026-10-16 实测：前程无忧 / 智联的搜索页在一个没装任何自动化的全新浏览器里也要 30 秒
        才算加载完（页面里挂着一直不结束的统计 / 广告脚本），以前死等 domcontentloaded 20 秒 ——
        页面早就能读了，任务却以「Timeout 20000ms」整个失败。超时了但已经跳到目标网址，也照常往下走。
        """
        try:
            self._guard(self._page.goto, url, wait_until="commit", timeout=NAV_TIMEOUT_MS)
        except PageHung:
            raise
        except Exception as exc:  # noqa: BLE001
            if "Timeout" not in str(exc):
                raise
            current = self._page.url or ""
            if not current or current == "about:blank" or current.split("#")[0] == "chrome-error://chromewebdata/":
                raise
            # 网址已经变过去了：服务器回了页面，只是没「加载完」—— 让后面的 wait_for 去等具体内容

    def text(self) -> str:
        return self._guard(self._page.evaluate,
                           "(document.body && document.body.innerText || '').slice(0, 4000)") or ""

    def html(self) -> str:
        return self._guard(self._page.content)

    def eval(self, js: str) -> Any:
        return self._guard(self._page.evaluate, js)

    def click(self, selector: str, timeout_ms: int = CLICK_TIMEOUT_MS) -> None:
        self._page.locator(selector).first.click(timeout=timeout_ms)

    def fill(self, selector: str, value: str) -> None:
        self._page.locator(selector).first.fill(value)

    def inner_text(self, selector: str) -> str:
        try:
            return self._page.locator(selector).first.inner_text(timeout=3000)
        except Exception:  # noqa: BLE001 - 元素不存在时返回空
            return ""

    def exists(self, selector: str) -> bool:
        return bool(self._guard(self._page.evaluate, "s => Boolean(document.querySelector(s))", selector))

    def wait_for(self, selector: str, timeout_ms: int = 8000) -> bool:
        """等待元素出现（SPA 必须靠等待，不能靠 sleep）。"""
        try:
            self._guard(self._page.wait_for_selector, selector, timeout=timeout_ms, state="attached")
            return True
        except PageHung:
            raise                                    # 整页卡死不是「元素没出现」：要让任务知道
        except Exception:  # noqa: BLE001 - 超时即视为未出现
            return False

    # ---- 拟人动作（browser/humanize.py 用）：只移动、滚动、悬停，**从不点击** ----
    def viewport(self) -> tuple[int, int]:
        size = self._page.viewport_size or {}
        if size.get("width"):
            return int(size["width"]), int(size["height"])
        dims = self._guard(self._page.evaluate, "[window.innerWidth, window.innerHeight]") or [1280, 800]
        return int(dims[0]), int(dims[1])

    def mouse_move(self, x: float, y: float, steps: int = 12) -> None:
        self._guard(self._page.mouse.move, x, y, steps=max(1, int(steps)))

    def mouse_wheel(self, dx: float, dy: float) -> None:
        self._guard(self._page.mouse.wheel, dx, dy)

    def hover(self, selector: str) -> None:
        self._guard(self._page.locator(selector).first.hover, timeout=3000)

    def screenshot(self, path: str) -> None:
        self._page.screenshot(path=path, full_page=False)

    def close(self) -> None:
        try:
            self._page.close()
        except Exception:  # noqa: BLE001
            pass


class PatchrightBrowser:
    """连接一个已开启远程调试的 Chrome；不会启动或关闭它。"""

    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        self._pw = None
        self._browser = None
        self._context = None

    def _ensure(self):
        global _PATCH_CHECKED
        if self._context is not None:
            return self._context
        from patchright.sync_api import sync_playwright

        if not _PATCH_CHECKED:
            # 每个进程第一次连浏览器前检查一次驱动补丁（见 driver_patch.py：标签页刚开就关会把驱动杀掉）
            from .driver_patch import ensure_patched

            _PATCH_CHECKED = True
            try:
                ensure_patched()
            except OSError:
                pass
        manager = sync_playwright()
        self._pw = manager.start()
        # 记下驱动子进程：卡死时看门狗 / 「停止」靠结束它来让卡住的那一句返回
        try:
            self._driver_pid = manager._connection._transport._proc.pid
        except AttributeError:
            self._driver_pid = None
        try:
            try:
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=CONNECT_TIMEOUT_MS)
            except Exception as first:
                if "Timeout" not in str(first):
                    raise
                # 连不上多半是有标签页整页卡死（接管时它永远不回话，2026-10-16 实测）：
                # 关掉没有响应的标签页再连一次。只关「连执行 1+1 都不回」的页，正常的页一个不碰
                closed = close_frozen_tabs(self.cdp_url)
                if not closed:
                    raise
                self.close()
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url, timeout=CONNECT_TIMEOUT_MS)
        except Exception as exc:
            self.close()
            if "Timeout" in str(exc):
                raise RuntimeError(
                    f"连不上浏览器：{CONNECT_TIMEOUT_MS // 1000} 秒内没有响应。调试 Chrome 可能卡住了"
                    "（比如有标签页一直在加载）：把它整个关掉，再点左下角「启动浏览器」") from exc
            raise
        if not self._browser.contexts:
            raise RuntimeError(f"已连上 CDP 但没有浏览器上下文（请确认 Chrome 已登录）: {self.cdp_url}")
        self._context = self._browser.contexts[0]
        return self._context

    def health(self) -> bool:
        """CDP 端口活着吗？

        这里**故意不去连 Playwright**：connect_over_cdp 在浏览器状态异常时会卡住几十秒
        到几分钟，而 health() 会被 /api/health 和「系统体检」调用 —— 结果是「浏览器连不上」
        这条信息本身表现成「接口卡死」，连启动自检都被拖垮（真踩过）。
        打一下 /json/version 就行，2 秒超时，永远给得出答案。
        """
        import urllib.request

        base = self.cdp_url.rstrip("/")
        try:
            with urllib.request.urlopen(base + "/json/version", timeout=2) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001 - 连不上就是没连上
            return False

    def open(self, url: str) -> Page:
        try:
            page = self._ensure().new_page()
        except Exception as exc:  # noqa: BLE001
            if not is_dead_connection(exc):
                raise
            # 驱动进程挂了（浏览器还开着）：断开、重连一次。任务不必因为这一下就整个失败
            self.close()
            page = self._ensure().new_page()
        wrapped = PatchrightPage(page, self)
        wrapped.goto(url)
        return wrapped

    def find(self, url_contains: str) -> Page | None:
        for page in self._ensure().pages:
            if url_contains in (page.url or ""):
                return PatchrightPage(page, self)
        return None

    def pump(self, seconds: float) -> bool:
        """让驱动在 seconds 秒里继续收发浏览器消息（任务等待时调用）；没连着就返回 False。

        sync API 只有在我们调用它的时候才处理驱动发来的消息；线程 time.sleep 时消息堆着，
        浏览器那边新开的标签页就一直等（实测：挂一个闲着的连接，新标签页 60 秒都加载不完；
        连接在 wait_for_timeout 里泵着时 28 秒正常加载完）。
        """
        if self._context is None or self._killed:
            return False
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            return False
        pages[0].wait_for_timeout(max(1, int(seconds * 1000)))
        return True

    def guarded(self, fn, *args, **kwargs):
        """执行一次页面调用；CALL_TIMEOUT_S 秒还没返回（页面卡死）就强制断开驱动，抛 PageHung。"""
        import threading

        fired = threading.Event()

        def bite():
            fired.set()
            self.kill_driver()

        dog = threading.Timer(CALL_TIMEOUT_S, bite)
        dog.daemon = True
        dog.start()
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if fired.is_set() or self._killed:
                raise PageHung(f"页面 {CALL_TIMEOUT_S:.0f} 秒没有任何响应（整页卡死），已断开自动化连接。"
                               "这个标签页可以关掉；浏览器和登录状态都不受影响") from exc
            raise
        finally:
            dog.cancel()

    def kill_driver(self) -> None:
        """强制结束驱动子进程（不碰 Chrome）。可以从别的线程调：「停止」按钮用它打断卡住的调用。

        结束后所有挂在这个连接上的调用立刻以「Connection closed」返回，Chrome 收到断开后
        会放开驱动拦着的请求 —— 你自己开的标签页（例如去登录）就能正常加载了。
        """
        import os
        import signal

        self._killed = True
        pid = getattr(self, "_driver_pid", None)
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    _killed = False
    _driver_pid = None

    def close(self) -> None:
        """只断开 Playwright 驱动，不动用户的 Chrome。"""
        # 驱动被强制结束过也要走 stop()：它负责收掉这条线程上的事件循环，
        # 不收的话同一线程下次再连会报「在 asyncio 循环里用 Sync API」（实测）。进程已死，stop() 很快返回
        self._killed = False
        self._driver_pid = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._browser = None
        self._pw = None
