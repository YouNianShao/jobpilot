"""备份：把整条求职流水线打包成一个 zip。

包含：数据库（在线一致性备份）、config.yaml、已生成的定制简历、manifest。
数据库不能直接拷文件 —— SQLite 正在写入时可能拷到"写了一半"的库，
所以走 sqlite3 的在线备份 API。恢复：停掉看板 → 解压覆盖 → 重启。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from ..config import CONFIG_PATH, DATA_DIR, DB_PATH, load_config


def _snapshot_db(dest: Path) -> int:
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return dest.stat().st_size


def build_backup(dest: Path) -> dict:
    cfg = load_config(CONFIG_PATH)   # 用本模块这个路径，保证"读的配置"和"打包的配置"是同一份
    written: list[str] = []
    resume_dir = DATA_DIR / "resumes"

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        db_copy = dest.with_suffix(".db")
        size = _snapshot_db(db_copy)
        zf.write(db_copy, "data/jobpilot.db")
        db_copy.unlink(missing_ok=True)
        written.append(f"data/jobpilot.db({size}B)")

        if CONFIG_PATH.exists():
            zf.write(CONFIG_PATH, "config.yaml")
            written.append("config.yaml")

        if resume_dir.exists():
            for path in sorted(resume_dir.glob("*.md")):
                zf.write(path, f"data/resumes/{path.name}")
                written.append(f"data/resumes/{path.name}")

        manifest = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "JobPilot 备份：解压覆盖同名文件即可恢复（先停掉看板）",
            "model": cfg.ai.model,
            "platforms": sorted(cfg.platforms.keys()),
            "files": written,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    return {"path": str(dest), "bytes": dest.stat().st_size, "files": written}
