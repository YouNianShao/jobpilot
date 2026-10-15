"""命令行入口。

jobpilot status / check / collect / score / greet / confirm / apply / monitor / full
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bootstrap import build_context
from .config import CONFIG_PATH, load_config
from .platforms import build_default_registry
from .services.orchestrator import MODES, run


def _cmd_status() -> int:
    cfg = load_config()
    registry = build_default_registry()
    exists = CONFIG_PATH.exists()
    print(f"版本      : jobpilot {__version__}")
    print(f"配置文件  : {CONFIG_PATH} ({'已加载' if exists else '不存在，使用默认值'})")
    print(f"AI 服务   : {cfg.ai.model} @ {cfg.ai.base_url} (key: {'已配置' if cfg.ai.api_key else '未配置'})")
    print(f"自治等级  : {cfg.autonomy.level}   人工确认门: {'开' if cfg.confirm.required else '关'}")
    print("平台      :")
    for key, plat in cfg.platforms.items():
        driver = registry.get(key)
        caps = ",".join(sorted(driver.capabilities)) if driver else "（驱动未实现）"
        state = "启用" if plat.enabled else "停用"
        apply_capable = driver is not None and "apply" in getattr(driver, "capabilities", frozenset())
        apply_state = "开" if plat.apply else "关"
        if plat.apply and not apply_capable:
            apply_state += "（驱动未核验，暂不生效）"
        print(f"  - {key:8s} {state}  能力[{caps}]  城市={plat.cities}  投递={apply_state}")
    pages = cfg.safety.daily_pages
    print(f"页面预算  : 搜索 {pages.collect_search} / 详情 {pages.collect_detail} / 投递 {pages.apply} / 监测 {pages.monitor} / 总量 {pages.total}")
    print(f"投递节流  : 每日 {cfg.apply.daily_limit} 次，间隔 {cfg.apply.interval_min}-{cfg.apply.interval_max}s，窗口 {cfg.safety.send_windows}")
    return 0


def _cmd_check() -> int:
    """体检：和看板上的「体检」按钮走同一套检查。"""
    from .services.health import run_checks

    ctx = build_context()
    try:
        report = run_checks(ctx)
    finally:
        ctx.repo.conn.close()

    for row in report["checks"]:
        mark = "ok  " if row["ok"] else ("!!  " if row["level"] == "error" else "warn")
        print(f"[{mark}] {row['label']}：{row['detail']}")
        if not row["ok"] and row["fix"]:
            print(f"        → {row['fix']}")
    if not report["ok"]:
        print("\n有必检项没过，先按上面的提示修。")
        return 1
    if report["warned"]:
        print("\n必检项都过了；上面标 warn 的不影响跑，但值得看一眼。")
    else:
        print("\n一切正常。")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    ctx = build_context()
    ctx.log = print
    options = {}
    if getattr(args, "limit", None):
        options["limit"] = args.limit
    if args.command == "confirm":
        options["reject"] = bool(getattr(args, "reject", False))
        options["job_ids"] = getattr(args, "ids", None)
    try:
        result = run(ctx, args.command, getattr(args, "platform", None), **options)
    finally:
        ctx.repo.conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _redirect_output(path: str) -> None:
    """把 stdout / stderr 接到日志文件（追加、按行刷新）。

    为什么在进程里自己写：以前静默启动是 `cmd /c pythonw ... >> serve.log`，
    那个 cmd.exe 是控制台程序，要一直活着替 pythonw 转写日志 —— 服务跑多久，
    桌面上就挂多久一个 cmd 窗口。pythonw 自己写日志，就不需要中间这一层了。
    另外 pythonw 下 sys.stdout / sys.stderr 是 None，不接管的话报错和 traceback 会直接丢掉。
    """
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(log_path, "a", encoding="utf-8", buffering=1, errors="replace")  # noqa: SIM115 - 进程级生命周期
    sys.stdout = stream
    sys.stderr = stream


def main(argv: list[str] | None = None) -> int:
    # 先切到「上次用的那个账号」：之后读的配置、开的数据库、浏览器端口都是它的
    from . import accounts

    accounts.activate()
    parser = argparse.ArgumentParser(prog="jobpilot", description="JobPilot 多平台求职助手")
    parser.add_argument("--version", action="version", version=f"jobpilot {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="查看配置与平台状态")
    sub.add_parser("check", help="检查配置与运行环境")
    serve = sub.add_parser("serve", help="启动 Web 看板")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8700)
    serve.add_argument("--log", default="", help="把 stdout/stderr 追加写进这个文件（静默启动用）")

    labels = {
        "collect": "采集岗位", "score": "AI 评分", "greet": "生成招呼语",
        "apply": "投递已确认岗位", "monitor": "HR 监测", "full": "全流程（确认门开启时停在待确认）",
    }
    for mode, help_text in labels.items():
        child = sub.add_parser(mode, help=help_text)
        child.add_argument("--platform", default="boss")
        child.add_argument("--limit", type=int, default=None)

    follow = sub.add_parser("followup", help="为超时未回复的岗位生成跟进话术")
    follow.add_argument("--limit", type=int, default=10)

    resume = sub.add_parser("resume", help="为指定岗位生成定制简历")
    resume.add_argument("--job", required=True, help="岗位 id")

    confirm = sub.add_parser("confirm", help="确认待投递岗位")
    confirm.add_argument("--platform", default="boss")
    confirm.add_argument("--reject", action="store_true", help="改为拒绝")
    confirm.add_argument("--ids", nargs="*", default=None, help="只处理指定岗位 id")

    args = parser.parse_args(argv)
    if args.command == "status":
        return _cmd_status()
    if args.command == "check":
        return _cmd_check()
    if args.command == "serve":
        if args.log:
            _redirect_output(args.log)
        from .web.app import run_server

        run_server(args.host, args.port)
        return 0
    if args.command == "followup":
        from .services.followup import generate_follow_ups

        ctx = build_context()
        ctx.log = print
        try:
            result = generate_follow_ups(ctx, limit=args.limit)
        finally:
            ctx.repo.conn.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "resume":
        from .services.resume import generate_resume

        ctx = build_context()
        try:
            result = generate_resume(ctx, args.job)
        finally:
            ctx.repo.conn.close()
        result.pop("markdown", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("error") else 1
    if args.command in MODES:
        return _cmd_run(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
