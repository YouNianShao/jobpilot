r"""把旧版 JobPilot（E:\JobPilot）的 ai / profile 配置迁移到新版 JobPilot，并把浏览器指向已登录的 Chrome。

用法: python tools\import_old_config.py [旧config路径] [cdpUrl]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import yaml  # noqa: E402

from jobpilot.config import CONFIG_PATH, AiCfg, AppConfig, ProfileCfg, load_config, save_config  # noqa: E402


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else r"E:\JobPilot\config.yaml")
    cdp = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9222"

    cfg: AppConfig = load_config()
    if src.exists():
        data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
        cfg.ai = AiCfg(**{**cfg.ai.model_dump(),
                          **{k: v for k, v in (data.get("ai") or {}).items() if k in AiCfg.model_fields}})
        cfg.profile = ProfileCfg(**{**cfg.profile.model_dump(),
                                    **{k: v for k, v in (data.get("profile") or {}).items() if k in ProfileCfg.model_fields}})
    cfg.browser.cdp_url = cdp
    save_config(cfg, CONFIG_PATH)

    print("已写入", CONFIG_PATH)
    print("  cdp_url :", cfg.browser.cdp_url)
    print("  ai      :", cfg.ai.model, "| api_key:", "已配置" if cfg.ai.api_key else "未配置")
    print("  resume  :", cfg.profile.resume_path or "未配置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
