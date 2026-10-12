"""投递之后的进展记录：HR 回复 / 约面 / offer。"""
from __future__ import annotations

from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import StopSignal
from jobpilot.safety.policy import Policy
from jobpilot.services.context import ServiceContext
from jobpilot.services.progress import INTERVIEW_STATES, record_progress, state_labels


def make_ctx(tmp_path):
    cfg = AppConfig()
    repo = Repo(connect(tmp_path / "p.db"))
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1", title="数据分析师",
                        company="某公司", url="https://example.com/1"))
    ctx = ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg), stop=StopSignal())
    return ctx, repo


def test_record_progress_writes_interaction_and_event(tmp_path):
    ctx, repo = make_ctx(tmp_path)

    result = record_progress(ctx, "boss-1", "interview", note="约了周三下午")

    assert result["ok"] and result["label"] == "约面试"
    rows = repo.list_interactions()
    assert len(rows) == 1
    row = rows[0]
    assert row["state"] == "interview" and row["job_id"] == "boss-1"
    assert row["last_text"] == "约了周三下午"
    assert row["title"] == "数据分析师" and row["company"] == "某公司"   # JOIN 出来的
    assert [e["kind"] for e in repo.list_events("boss-1")] == ["progress"]


def test_record_progress_is_a_moving_state_not_append_only(tmp_path):
    """同一个岗位再记一次是"更新进展"，不该堆出一堆重复记录。"""
    ctx, repo = make_ctx(tmp_path)
    record_progress(ctx, "boss-1", "viewed")
    record_progress(ctx, "boss-1", "offer", note="已发 offer")

    rows = repo.list_interactions()
    assert len(rows) == 1
    assert rows[0]["state"] == "offer"


def test_record_progress_rejects_unknown_state_and_job(tmp_path):
    ctx, _ = make_ctx(tmp_path)
    assert "未知的进展状态" in record_progress(ctx, "boss-1", "飞升")["error"]
    assert "没有这个岗位" in record_progress(ctx, "nope", "offer")["error"]


def test_funnel_counts_interview_and_offer(tmp_path):
    ctx, repo = make_ctx(tmp_path)
    repo.upsert_job(Job(id="boss-2", platform="boss", external_id="2", title="BI 工程师", company="另一家"))
    record_progress(ctx, "boss-1", "interview")
    record_progress(ctx, "boss-2", "offer")

    assert repo.count_interactions_by_states(INTERVIEW_STATES) == 2   # 两个都算"进面试"
    assert repo.count_interactions_by_states(("offer",)) == 1


def test_state_labels_are_complete():
    labels = state_labels()
    assert set(labels) == set(INTERVIEW_STATES) | {"viewed", "replied", "rejected"}
    assert all(labels.values())
