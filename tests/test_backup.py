"""备份：库、配置、简历、manifest 都要在，而且是合法 zip。"""
from __future__ import annotations

import json
import zipfile

from jobpilot.services import backup as backup_mod


def test_backup_contains_db_config_and_manifest(tmp_path, monkeypatch):
    db = tmp_path / "jobpilot.db"
    db.write_bytes(b"")                      # 内容不重要，够 sqlite 打开即可
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO jobs VALUES ('boss-1')")
    conn.commit()
    conn.close()

    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  model: test-model\n", encoding="utf-8")
    resumes = tmp_path / "resumes"
    resumes.mkdir()
    (resumes / "master.md").write_text("# 我的简历", encoding="utf-8")

    monkeypatch.setattr(backup_mod, "DB_PATH", db)
    monkeypatch.setattr(backup_mod, "CONFIG_PATH", cfg)
    monkeypatch.setattr(backup_mod, "ACCOUNT_DIR", tmp_path)

    dest = tmp_path / "out.zip"
    info = backup_mod.build_backup(dest)

    assert info["bytes"] > 0
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert "data/jobpilot.db" in names
        assert "config.yaml" in names
        assert "data/resumes/master.md" in names
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["model"] == "test-model"
        assert "boss" in manifest["platforms"]
        # 库真的被拷进去了（一致性快照里有那条岗位）
        copied = tmp_path / "copied.db"
        copied.write_bytes(zf.read("data/jobpilot.db"))
        check = sqlite3.connect(copied)
        assert check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        check.close()


def test_backup_without_config_or_resumes(tmp_path, monkeypatch):
    """没配置、没简历也不该崩 —— 备份要能在任何中间状态下跑。"""
    db = tmp_path / "empty.db"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backup_mod, "DB_PATH", db)
    monkeypatch.setattr(backup_mod, "CONFIG_PATH", tmp_path / "nope.yaml")
    monkeypatch.setattr(backup_mod, "ACCOUNT_DIR", tmp_path / "no-data")

    dest = tmp_path / "out.zip"
    info = backup_mod.build_backup(dest)
    with zipfile.ZipFile(dest) as zf:
        assert "data/jobpilot.db" in zf.namelist()
        assert "config.yaml" not in zf.namelist()
    assert info["bytes"] > 0
