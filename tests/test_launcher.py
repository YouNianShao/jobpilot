"""启动链的编码契约。

这两个断言是拿一次真实故障换来的：我在 start.bat 里写了中文注释，
cmd 按 OEM 代码页解码 .bat，中文被误解码成命令，**在快捷方式的隐藏窗口里
把后面的行吃掉了** —— 表现就是"双击没反应"，而直接跑 bat 却是好的。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_start_bat_is_ascii_only():
    raw = (ROOT / "start.bat").read_bytes()
    offenders = [index for index, byte in enumerate(raw) if byte > 127]
    assert not offenders, (
        f"start.bat 里出现非 ASCII 字节（位置 {offenders[:5]}）："
        "cmd 按 OEM 代码页解码 .bat，中文注释会被当成命令执行"
    )
    assert b"%~dp0" in raw, "start.bat 应该用 %~dp0 定位自己，别硬编码盘符"


def test_launch_ps1_has_utf8_bom():
    """Windows PowerShell 5.1 读没有 BOM 的 .ps1 会按系统代码页解码，中文全乱（连报告文件名都会写错）。"""
    raw = (ROOT / "tools" / "launch.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "launch.ps1 必须带 UTF-8 BOM"


def test_launch_starts_pythonw_directly_without_cmd():
    """服务要直接用 pythonw 起，日志由进程自己写（serve --log）。

    以前中间垫了一层 `cmd.exe /c "pythonw ... >> serve.log"`：cmd 是控制台程序，
    WMI 起它时会弹黑框，而且它要一直活着替 pythonw 转写日志 —— 服务跑多久，桌面上就挂多久。
    """
    text = (ROOT / "tools" / "launch.ps1").read_text(encoding="utf-8-sig")
    body = text.split("#>", 1)[-1]          # 先去掉文件头的 <# … #> 说明块，再去掉 # 行注释
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "cmd.exe" not in code.lower(), "启动链里不该再出现 cmd.exe（会在桌面上挂一个控制台窗口）"
    assert "pythonw.exe" in code and "--log" in code


def test_serve_log_option_redirects_output(tmp_path):
    import sys

    from jobpilot.cli import _redirect_output

    log = tmp_path / "logs" / "serve.log"
    saved = sys.stdout, sys.stderr
    try:
        _redirect_output(str(log))
        print("hello")
        sys.stderr.write("oops\n")
        stream = sys.stdout
    finally:
        sys.stdout, sys.stderr = saved
    stream.close()
    assert log.read_text(encoding="utf-8") == "hello\noops\n"


def test_failure_report_points_at_serve_log_only():
    """服务自己写日志之后 serve.err.log 不会再有内容；启动失败报告不能再让人去看那个空文件。"""
    text = (ROOT / "tools" / "launch.ps1").read_text(encoding="utf-8-sig")
    body = text.split("#>", 1)[-1]
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "serve.err.log" not in code
    assert "$launchMark" in code and "Write-Report" in code   # 报告只附「这一次启动之后」的日志
