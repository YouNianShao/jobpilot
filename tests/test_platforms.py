"""三个新平台驱动的纯逻辑（选择器已实机核验，见 docs/05）。"""
from __future__ import annotations

import json

from jobpilot.platforms import build_default_registry
from jobpilot.platforms.job51 import city_code as job51_city
from jobpilot.platforms.job51 import make_job_id as job51_id
from jobpilot.platforms.job51 import parse_list as job51_list
from jobpilot.platforms.liepin import make_job_id as liepin_id
from jobpilot.platforms.liepin import parse_list as liepin_list
from jobpilot.platforms.liepin import search_url as liepin_url
from jobpilot.platforms.zhaopin import make_job_id as zhaopin_id
from jobpilot.platforms.zhaopin import parse_list as zhaopin_list
from jobpilot.platforms.zhaopin import search_url as zhaopin_url


def test_registry_has_all_four_platforms():
    registry = build_default_registry()
    assert set(registry.keys()) == {"boss", "51job", "liepin", "zhaopin"}
    for key in registry.keys():
        assert registry.supports(key, "collect") is True
        # BOSS 2026-10-10、猎聘 / 前程无忧 2026-10-14 实机核验通过；智联仍只有采集（验证码）
        expected_apply = key in ("boss", "liepin", "51job")
        assert registry.supports(key, "apply") is expected_apply


def test_job51_helpers():
    assert job51_city("上海") == "020000"
    assert job51_city("不存在") is None
    assert job51_id({"jobId": "123456"}) == "51job-123456"
    assert job51_id({"title": "t", "company": "c", "salary": "1k"}).startswith("51job-")
    rows = json.loads('[{"jobId":"1","title":"数据分析师"}]')
    assert job51_list(json.dumps(rows))[0]["title"] == "数据分析师"
    assert job51_list("坏数据") == []


def test_liepin_helpers():
    # curPage 从 0 开始
    assert liepin_url("数据分析", 1).endswith("curPage=0")
    assert liepin_url("数据分析", 3).endswith("curPage=2")
    assert liepin_id({"jobId": "1985614831"}) == "liepin-1985614831"
    assert liepin_list(None) == []


def test_zhaopin_helpers():
    assert "kw=" in zhaopin_url("数据分析")
    assert zhaopin_id({"title": "数据分析师", "company": "甲", "salary": "1万"}).startswith("zhaopin-")
    assert zhaopin_list("[]") == []

def test_only_some_platforms_are_city_restricted():
    """受限平台（用平台自己的城市码）要暴露城市表，自由文本平台不用。"""
    from jobpilot.platforms import build_default_registry

    registry = build_default_registry()
    restricted = {key: len(getattr(registry.get(key), "supported_cities", {}) or {}) for key in registry.keys()}
    assert restricted["boss"] > 300          # BOSS 用 9 位城市码
    assert restricted["51job"] > 0           # 51job 也要城市码（目前 15 个，仍是短板）
    assert restricted["liepin"] == 0         # 猎聘 / 智联按城市名直接搜
    assert restricted["zhaopin"] == 0

def test_small_city_tables_expose_all_of_them_as_quick_picks():
    """51job 只支持 15 个城市码 —— 那就把这 15 个全列出来，让边界可见。"""
    from jobpilot.platforms import build_default_registry

    registry = build_default_registry()
    driver = registry.get("51job")
    supported = set(getattr(driver, "supported_cities", {}) or {})
    assert supported, "51job 应该有城市码表"
    assert set(driver.hot_cities) == supported
    assert "上海" in supported
