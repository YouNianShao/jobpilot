"""平台驱动注册表。

加一个平台 = 在 platforms/ 下新增一个模块并暴露 DRIVER，然后登记到 PLATFORM_MODULES。
服务层不认识任何平台细节。
"""

from __future__ import annotations

from importlib import import_module

from .base import PlatformDriver, supports

PLATFORM_MODULES = ("boss", "job51", "liepin", "zhaopin")


class Registry:
    def __init__(self) -> None:
        self._drivers: dict[str, PlatformDriver] = {}

    def register(self, driver: PlatformDriver) -> None:
        self._drivers[driver.key] = driver

    def get(self, key: str) -> PlatformDriver | None:
        return self._drivers.get(key)

    def require(self, key: str) -> PlatformDriver:
        driver = self.get(key)
        if driver is None:
            raise KeyError(f"未注册的平台驱动: {key}")
        return driver

    def keys(self) -> tuple[str, ...]:
        return tuple(self._drivers)

    def enabled(self, config) -> tuple[str, ...]:
        return tuple(k for k in self._drivers if config.platforms.get(k) and config.platforms[k].enabled)

    def supports(self, key: str, capability: str) -> bool:
        driver = self.get(key)
        return bool(driver and supports(driver, capability))


def build_default_registry() -> Registry:
    """加载已实现的平台驱动（分阶段开发：尚未实现的模块会被跳过）。"""
    registry = Registry()
    for name in PLATFORM_MODULES:
        try:
            module = import_module(f"jobpilot.platforms.{name}")
        except ModuleNotFoundError:
            continue
        driver = getattr(module, "DRIVER", None)
        if driver is not None:
            registry.register(driver)
    return registry
