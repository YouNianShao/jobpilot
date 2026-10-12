"""组装 ServiceContext：把配置 / 存储 / 推理 / 安全内核 串起来。"""

from __future__ import annotations

from .ai.llm import LlmClient, build_llm
from .config import DB_PATH, AppConfig, load_config
from .db import Repo, connect
from .platforms.base import StopSignal
from .safety.policy import Policy
from .services.context import ServiceContext


def build_context(
    config: AppConfig | None = None,
    *,
    repo: Repo | None = None,
    llm: LlmClient | None = None,
) -> ServiceContext:
    cfg = config or load_config()
    store = repo or Repo(connect(DB_PATH))
    stop = StopSignal()
    return ServiceContext(
        config=cfg,
        repo=store,
        llm=llm or build_llm(cfg.ai),
        # 节流等待用 stop.sleep：暂停/停止能立刻打断等待，而不是傻等
        policy=Policy(store, cfg, sleep=stop.sleep),
        stop=stop,
    )
