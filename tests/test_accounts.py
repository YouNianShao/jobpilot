"""多账号：两个账号的岗位池 / 投递计数 / 浏览器完全分开；AI 和风控规则共用。

全部在临时目录里跑：把 ROOT / DATA_DIR / 账号登记文件都指到 tmp_path，不碰真实数据。
"""
from __future__ import annotations

import pytest

import jobpilot.accounts as accounts
import jobpilot.config as cfgmod
from jobpilot.config import AppConfig, load_config, save_config


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(cfgmod, "ROOT", tmp_path)
    monkeypatch.setattr(cfgmod, "DATA_DIR", data)
    monkeypatch.setattr(accounts, "REGISTRY", data / "accounts.json")
    # activate() 会改这些全局：测试结束后由 monkeypatch 还原
    import jobpilot.bootstrap as boot
    import jobpilot.services.backup as backup
    import jobpilot.web.app as web
    for mod, attrs in ((cfgmod, ("CONFIG_PATH", "DB_PATH", "ACCOUNT_DIR")), (boot, ("DB_PATH",)),
                       (web, ("CONFIG_PATH", "ACCOUNT_DIR")), (backup, ("CONFIG_PATH", "DB_PATH", "ACCOUNT_DIR"))):
        for attr in attrs:
            monkeypatch.setattr(mod, attr, getattr(mod, attr))
    base = AppConfig()
    base.ai.api_key = "sk-test-shared-key-000000000000"
    base.profile.resume_path = str(tmp_path / "a.md")
    base.platforms["51job"].keywords = ["数据分析"]
    base.platforms["51job"].daily_limit = 15
    base.browser.cdp_url = "http://127.0.0.1:9222"
    save_config(base, tmp_path / "config.yaml")
    return tmp_path


def test_b_gets_its_own_folder_port_and_blank_profile_but_shared_ai(sandbox):
    p = accounts.activate("B")
    b = load_config(p.config)
    assert p.config == sandbox / "data" / "accounts" / "B" / "config.yaml"
    assert p.db != accounts.paths_for("A").db and p.chrome_profile != accounts.paths_for("A").chrome_profile
    assert b.browser.cdp_url.endswith(":9334")            # 和 A 的 9333 分开：绝不会连到 A 的浏览器
    assert b.ai.api_key == "sk-test-shared-key-000000000000"   # 共用
    assert b.platforms["51job"].daily_limit == 15             # 风控规则的数值共用
    assert b.profile.resume_path == ""                        # 简历各管各的
    assert b.platforms["51job"].keywords != ["数据分析"]      # 搜索条件各管各的：不照抄 A（B 是默认起点）
    assert cfgmod.CONFIG_PATH == p.config and cfgmod.DB_PATH == p.db


def test_switch_back_and_forth_points_every_module_at_the_right_files(sandbox):
    import jobpilot.bootstrap as boot
    import jobpilot.web.app as web

    accounts.activate("B")
    assert boot.DB_PATH == accounts.paths_for("B").db and web.CONFIG_PATH == accounts.paths_for("B").config
    accounts.activate("A")
    assert boot.DB_PATH == accounts.paths_for("A").db and web.ACCOUNT_DIR == sandbox / "data"
    assert accounts.active_id() == "A"
    with pytest.raises(ValueError):
        accounts.activate("C")


def test_applies_and_jobs_are_counted_per_account(sandbox):
    from jobpilot.bootstrap import build_context
    from jobpilot.models import Job

    accounts.activate("A")
    a = build_context()
    a.repo.upsert_job(Job(id="51job-1", platform="51job", external_id="1", title="t", company="c"))
    a.repo.add_event("51job-1", "applied", "applied")
    accounts.activate("B")
    b = build_context()
    assert b.repo.get_job("51job-1") is None                  # B 的岗位池里没有 A 的岗位
    assert b.policy.applied_today() == 0                      # A 投的不占 B 的额度
    assert a.policy.applied_today() == 1


def test_saving_shared_settings_syncs_to_the_other_account_only(sandbox):
    accounts.activate("B")
    accounts.activate("A")
    a = load_config(accounts.paths_for("A").config)
    a.ai.model = "deepseek-chat"
    a.safety.max_pages_per_hour = 40
    a.platforms["liepin"].daily_limit = 8
    a.platforms["liepin"].keywords = ["只属于 A"]
    save_config(a, accounts.paths_for("A").config)
    assert accounts.sync_shared(a, "A") == ["B"]
    b = load_config(accounts.paths_for("B").config)
    assert b.ai.model == "deepseek-chat" and b.safety.max_pages_per_hour == 40
    assert b.platforms["liepin"].daily_limit == 8             # 平台上限数值共用
    assert "只属于 A" not in b.platforms["liepin"].keywords     # 关键词不共用


def test_switch_api_refuses_while_a_task_runs(sandbox):
    from fastapi.testclient import TestClient

    from jobpilot.bootstrap import build_context
    from jobpilot.web.app import create_app

    class Busy:
        running = True

    client = TestClient(create_app(build_context, runner=Busy()), base_url="http://127.0.0.1:8700")
    res = client.post("/api/accounts/switch", json={"id": "B"})
    assert res.status_code == 409 and "任务" in res.json()["error"]
    assert accounts.active_id() == "A"


def test_old_default_port_is_moved_off_9222_but_custom_port_is_kept(sandbox):
    # sandbox 里 A 的配置是旧的 9222：激活时自动换成 9333，其它内容一字不动
    before = accounts.paths_for("A").config.read_text(encoding="utf-8")
    accounts.activate("A")
    after = accounts.paths_for("A").config.read_text(encoding="utf-8")
    assert load_config(accounts.paths_for("A").config).browser.cdp_url == "http://127.0.0.1:9333"
    assert before.replace("http://127.0.0.1:9222", "http://127.0.0.1:9333") == after
    # 用户自己填的端口不碰
    cfg = load_config(accounts.paths_for("A").config)
    cfg.browser.cdp_url = "http://127.0.0.1:9555"
    save_config(cfg, accounts.paths_for("A").config)
    assert accounts.migrate_port("A") is False
    assert load_config(accounts.paths_for("A").config).browser.cdp_url.endswith(":9555")
