"""一次性 L1 全链路验证：真实 Chrome 采集 + 真实 AI 评分/招呼语 + 人工确认。

用法:
    python tools\run_boss_chain.py --cdp http://127.0.0.1:9333 --ai-config E:\\JobPilot\\config.yaml
结果写临时库；--ai-config 只读取其中的 ai 段（api_key 不会被打印）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from jobpilot.ai.llm import OpenAiCompatClient  # noqa: E402
from jobpilot.browser import create_browser  # noqa: E402
from jobpilot.config import AiCfg, ProfileCfg, load_config  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.platforms.base import StopSignal  # noqa: E402
from jobpilot.safety.policy import Policy  # noqa: E402
from jobpilot.services.context import ServiceContext  # noqa: E402
from jobpilot.services.orchestrator import run  # noqa: E402


def _arg(name: str, default: str | None = None) -> str | None:
    argv = sys.argv[1:]
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


def main() -> int:
    cdp = _arg("--cdp", "http://127.0.0.1:9333") or ""
    ai_config = _arg("--ai-config")

    cfg = load_config()
    if ai_config and Path(ai_config).exists():
        data = yaml.safe_load(Path(ai_config).read_text(encoding="utf-8")) or {}
        merged = {**cfg.ai.model_dump(), **(data.get("ai") or {})}
        cfg.ai = AiCfg(**{k: v for k, v in merged.items() if k in AiCfg.model_fields})
        prof = data.get("profile") or {}
        merged_profile = {**cfg.profile.model_dump(), **{k: v for k, v in prof.items() if k in ProfileCfg.model_fields}}
        cfg.profile = ProfileCfg(**merged_profile)
    cfg.ai.max_attempts = 1

    cfg.platforms["boss"].enabled = True
    cfg.platforms["boss"].cities = ["上海"]
    cfg.platforms["boss"].keywords = [_arg("--keyword", "AI") or "AI"]
    cfg.platforms["boss"].max_pages = 1
    cfg.safety.daily_pages.collect_search = 3
    cfg.safety.daily_pages.collect_detail = 3
    cfg.confirm.required = True

    db_path = Path(tempfile.mkdtemp()) / "chain.db"
    repo = Repo(connect(db_path))
    llm = OpenAiCompatClient(cfg.ai)
    ctx = ServiceContext(
        config=cfg, repo=repo, llm=llm, policy=Policy(repo, cfg), stop=StopSignal(),
        log=lambda message: print("   " + message, flush=True),
        browser_factory=lambda: create_browser(cdp),
    )

    def show(step: str, result: dict) -> None:
        print(f"\n== {step} ==")
        print(json.dumps(result, ensure_ascii=False, indent=1))

    show("collect", run(ctx, "collect", "boss"))
    show("score", run(ctx, "score", "boss", limit=3))
    show("greet", run(ctx, "greet", "boss", limit=3))
    show("confirm", run(ctx, "confirm", "boss"))

    print("\n== 岗位明细 ==")
    for job in ctx.repo.list_jobs(platform="boss"):
        print(f"[{job.status}] {job.score} 分 | {job.company} | {job.title}")
        if job.greeting:
            print(f"    招呼语: {job.greeting}")
        if job.score_reason:
            print(f"    理由: {job.score_reason}")

    print(f"\nAI 用量: {llm.usage}")
    repo.conn.close()
    print(f"临时库: {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
