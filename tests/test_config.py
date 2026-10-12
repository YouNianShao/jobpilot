"""配置校验与读写。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobpilot.config import AppConfig, load_config, save_config


def test_defaults_are_safe():
    cfg = AppConfig()
    assert cfg.confirm.required is True          # 默认开启人工确认
    assert cfg.greeting.enabled is True
    assert cfg.scoring.threshold == 60
    assert cfg.platforms["51job"].enabled is True
    assert cfg.safety.daily_pages.total == 500


def test_yaml_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    cfg = AppConfig()
    cfg.ai.api_key = "test-key"
    cfg.scoring.threshold = 77
    save_config(cfg, path)

    loaded = load_config(path)
    assert loaded.scoring.threshold == 77
    assert loaded.ai.api_key == "test-key"


def test_invalid_value_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("scoring:\n  threshold: 999\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config(path)


def test_missing_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.confirm.required is True

def test_platform_section_inherits_platform_defaults(tmp_path):
    """平台段只写想改的键，其余继承该平台的默认值。"""
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\n  boss:\n    cities: [北京]\n", encoding="utf-8")
    cfg = load_config(path)

    assert cfg.platforms["boss"].cities == ["北京"]
    assert cfg.platforms["boss"].enabled is True
    assert cfg.platforms["boss"].fetch_detail is True    # BOSS 列表页没有薪资/JD，默认开详情页
    assert cfg.platforms["boss"].max_pages == 3


def test_explicit_platform_value_wins(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("platforms:\n  boss:\n    fetch_detail: false\n    max_pages: 7\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.platforms["boss"].fetch_detail is False
    assert cfg.platforms["boss"].max_pages == 7
