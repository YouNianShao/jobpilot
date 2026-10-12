"""Web 接口冒烟测试（用 TestClient，不需要真的起服务）。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.ai.llm import FakeLlm  # noqa: E402
from jobpilot.browser.fake import FakeBrowser  # noqa: E402
from jobpilot.config import AppConfig  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.models import Job  # noqa: E402
from jobpilot.platforms.base import StopSignal  # noqa: E402
from jobpilot.safety.policy import Policy  # noqa: E402
from jobpilot.services.context import ServiceContext  # noqa: E402
from jobpilot.web.app import create_app  # noqa: E402


def main() -> int:
    db_path = Path(tempfile.mkdtemp()) / "web.db"

    def factory():
        cfg = AppConfig()
        repo = Repo(connect(db_path))
        return ServiceContext(config=cfg, repo=repo, llm=FakeLlm(), policy=Policy(repo, cfg),
                              stop=StopSignal(), browser_factory=lambda: FakeBrowser([]))

    seed = factory()
    seed.repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1",
                             title="AI 产品经理", company="甲公司", salary="20-30K", city="上海",
                             url="https://jobs.51job.com/all/173851192.html"))
    seed.repo.set_score("boss-1", 88, "匹配", "pending_confirm")
    seed.repo.set_greeting("boss-1", "你好，我做过推荐系统落地。")
    seed.repo.conn.close()

    from fastapi.testclient import TestClient

    client = TestClient(create_app(factory), base_url="http://127.0.0.1:8700")

    status = client.get("/api/status")
    print("GET /api/status ->", status.status_code, sorted(status.json().keys()))
    print("  pending:", len(status.json()["pending_confirm"]), " platforms:", sorted(status.json()["platforms"].keys()))
    print("  locks:", status.json()["locks"], " applied_today:", status.json()["applied_today"])

    jobs = client.get("/api/jobs")
    print("GET /api/jobs ->", jobs.status_code, len(jobs.json()), "条")

    health = client.get("/api/health")
    print("GET /api/health ->", health.status_code, health.json())

    confirm = client.post("/api/jobs/confirm", json={"platform": "boss"})
    print("POST /api/jobs/confirm ->", confirm.status_code, confirm.json())

    tasks = client.post("/api/tasks", json={"mode": "confirm", "platform": "boss"})
    print("POST /api/tasks ->", tasks.status_code, tasks.json().get("mode"), tasks.json().get("status"))

    index = client.get("/")
    print("GET / ->", index.status_code, "bytes:", len(index.content))

    export = client.get("/api/export")
    csv_text = export.content.decode("utf-8-sig")
    print("GET /api/export ->", export.status_code, "content-type:", export.headers.get("content-type"),
          "bytes:", len(export.content))
    print("  CSV 表头:", csv_text.splitlines()[0])
    print("  CSV 数据行:", csv_text.splitlines()[1] if len(csv_text.splitlines()) > 1 else "(空)")
    print("  含岗位链接:", "jobs.51job.com/all/173851192.html" in csv_text)

    config = client.get("/api/config")
    print("GET /api/config ->", config.status_code, "ai_key_masked:",
          "*" in (config.json().get("ai", {}).get("api_key") or ""))

    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
