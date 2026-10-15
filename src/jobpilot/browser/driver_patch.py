"""给 patchright 自带的浏览器驱动打一个小补丁（幂等，启动时自动检查）。

为什么要改第三方代码：2026-10-15 真机排查 ——
patchright 每接管一个标签页（包括你在调试 Chrome 里自己点开的），都会**不等结果**地调一次
``setRequestInterception(true)``。如果那个标签页刚开就关掉（猎聘 / 智联点「我的简历」会先弹一个
马上跳走的中间页），这次调用就以「session closed」失败；因为没人接这个错误，Node 直接把整个驱动进程杀掉：
- 被拦着、还没放行的新标签页永远停在「正在加载…」
- 正在跑的任务报「Connection closed while reading from the driver」失败
补丁只做一件事：给这一次调用接住错误（标签页已经关了，本来也没什么要拦的）。

升级 patchright 会把补丁覆盖掉：``ensure_patched()`` 每次连浏览器前都会检查，没打就重新打上；
找不到要改的那一行（新版本改了写法）就什么都不动，只返回原因，由调用方记日志。
"""

from __future__ import annotations

from pathlib import Path

_TARGET = "        this._networkManager.setRequestInterception(true);\n"
_PATCHED = ("        this._networkManager.setRequestInterception(true).catch(() => {});"
            "  // jobpilot-patch: 标签页刚开就关时别把整个驱动杀掉\n")
_MARK = "jobpilot-patch"


def bundle_path() -> Path | None:
    try:
        import patchright
    except ImportError:
        return None
    path = Path(patchright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
    return path if path.exists() else None


def ensure_patched() -> str:
    """返回状态：patched（本次打上）/ ok（已经打过）/ 其它 = 没打上的原因。"""
    path = bundle_path()
    if path is None:
        return "找不到 patchright 驱动文件"
    text = path.read_text(encoding="utf-8")
    if _MARK in text:
        return "ok"
    count = text.count(_TARGET)
    if count != 1:
        return f"patchright 版本变了，要改的那一行出现了 {count} 次，没有打补丁"
    path.write_text(text.replace(_TARGET, _PATCHED), encoding="utf-8")
    return "patched"
