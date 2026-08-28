from __future__ import annotations

import argparse
from pathlib import Path

from jobpilot.config import load_config
from jobpilot.pipeline import run_pipeline


def main() -> None:
    ap = argparse.ArgumentParser(prog="jobpilot", description="JobPilot 多平台自动岗位投递")
    ap.add_argument("--platform", default="51job", help="平台标识，如 51job")
    ap.add_argument("--mode", choices=["collect", "score", "apply", "full", "monitor"], default="collect")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    res = run_pipeline(cfg, args.platform, args.mode)
    print(res)


if __name__ == "__main__":
    main()
