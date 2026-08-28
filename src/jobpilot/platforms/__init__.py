from __future__ import annotations

from jobpilot.platforms.base import BaseAdapter
from jobpilot.platforms.job51 import Job51Adapter
from jobpilot.platforms.liepin import LiepinAdapter
from jobpilot.platforms.zhaopin import ZhaopinAdapter

ADAPTERS: dict[str, BaseAdapter] = {
    Job51Adapter.key: Job51Adapter(),
    LiepinAdapter.key: LiepinAdapter(),
    ZhaopinAdapter.key: ZhaopinAdapter(),
}


def get_adapter(key: str) -> BaseAdapter | None:
    return ADAPTERS.get(key)
