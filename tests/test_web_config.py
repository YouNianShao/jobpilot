"""配置接口：规则下发（给界面实时校验）+ 保存时按字段给出中文原因；简历导入接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobpilot.config import AppConfig, config_problems, field_rules


def test_field_rules_come_from_the_pydantic_model():
    rules = field_rules()
    assert rules["scoring.threshold"] == {"type": "int", "min": 0, "max": 100}
    assert rules["scoring.concurrency"]["max"] == 3
    assert rules["safety.day_off_probability"] == {"type": "float", "min": 0, "max": 1}
    assert rules["platforms.*.max_pages"] == {"type": "int", "min": 1, "max": 20}
    assert rules["safety.send_windows"]["type"] == "list"
    assert rules["confirm.required"]["type"] == "bool"


def test_cross_field_problems():
    cfg = AppConfig()
    cfg.apply.interval_min, cfg.apply.interval_max = 200, 100
    cfg.profile.salary_min, cfg.profile.salary_max = 30, 20
    problems = config_problems(cfg)
    assert set(problems) == {"apply.interval_max", "profile.salary_max"}
    cfg.profile.salary_max = 0                      # 0 = 不限，不算矛盾
    assert "profile.salary_max" not in config_problems(cfg)


@pytest.fixture
def client(tmp_path, monkeypatch):
    import jobpilot.web.app as web

    monkeypatch.setattr(web, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(web, "load_config", lambda: AppConfig())
    app = web.create_app(lambda: (_ for _ in ()).throw(AssertionError("不应该用到 ctx")))
    return TestClient(app, base_url="http://127.0.0.1:8700"), tmp_path / "config.yaml"


def test_post_config_reports_bad_fields_in_chinese(client):
    http, path = client
    res = http.post("/api/config", json={"scoring": {"threshold": 150, "concurrency": 0}})
    assert res.status_code == 400
    body = res.json()
    assert body["fields"] == {"scoring.threshold": "不能大于 100", "scoring.concurrency": "不能小于 1"}
    assert "2 项" in body["error"]
    assert not path.exists()                         # 校验失败一个字节都不写


def test_post_config_rejects_contradictions_without_saving(client):
    http, path = client
    res = http.post("/api/config", json={"apply": {"interval_min": 300, "interval_max": 60}})
    assert res.status_code == 400 and "apply.interval_max" in res.json()["fields"]
    assert not path.exists()


def test_post_config_saves_valid_payload(client):
    http, path = client
    assert http.post("/api/config", json={"scoring": {"threshold": 70}}).json() == {"ok": True}
    assert "threshold: 70" in path.read_text(encoding="utf-8")


def test_get_config_ships_rules(client):
    http, _ = client
    assert http.get("/api/config").json()["_rules"]["scoring.threshold"]["max"] == 100


def test_resume_import_extracts_without_saving(client, tmp_path):
    from test_resume_io import make_docx, para

    http, path = client
    res = http.post("/api/resume/import?filename=简历.docx", content=make_docx(para("6 年数据分析经验")))
    assert res.status_code == 200
    assert res.json()["text"] == "6 年数据分析经验" and res.json()["format"] == "docx"
    assert not path.exists()                         # 只抽取，不改配置、不落盘


def test_resume_import_rejects_unsupported_and_scans(client):
    from test_resume_io import make_pdf

    http, _ = client
    assert http.post("/api/resume/import?filename=a.exe", content=b"MZ").status_code == 400
    res = http.post("/api/resume/import?filename=scan.pdf", content=make_pdf([]))
    assert res.status_code == 400 and "扫描件" in res.json()["error"]
