"""薪资解析与「月薪范围」筛选：单位跟着每个数字走（前程无忧「2-3.5万」「8千-1.2万」）。"""
from __future__ import annotations

import pytest

from jobpilot.config import AppConfig
from jobpilot.models import Job
from jobpilot.prefilter import parse_salary, prefilter


@pytest.mark.parametrize(("text", "expected"), [
    ("20-25K", (20, 25)), ("14-24K·13薪", (14, 24)), ("50-80k", (50, 80)), ("15K", (15, 15)),
    ("2-3.5万", (20, 35)), ("2.5-3.5万·13薪", (25, 35)), ("1.8-3.6万", (18, 36)),   # 前程无忧：万 = 月薪
    ("8千-1.2万", (8, 12)),                                                      # 两个数字各有单位
    ("18-30万/年", (15, 25)), ("200-250元/天", (4.35, 5.44)),
])
def test_parse_salary_to_monthly_k(text, expected):
    low, high = parse_salary(text)
    assert (round(low, 2), round(high, 2)) == expected


def test_unparseable_salary_is_unknown():
    assert parse_salary("面议") == (None, None)
    assert parse_salary("") == (None, None)


def test_range_filter_no_longer_drops_high_paying_51job_jobs():
    """以前「2-3.5万」被当成 2-3.5K：设了 15K 下限，月薪两三万的岗位反而被筛掉。"""
    cfg = AppConfig()
    cfg.profile.salary_min, cfg.profile.salary_max = 15, 30

    def job(salary):
        return Job(id="x", platform="51job", external_id="1", title="数据分析", company="c", salary=salary)

    assert prefilter(job("2-3.5万"), cfg) == (False, "")
    assert prefilter(job("8千-1.2万"), cfg)[0] is True                      # 上限 12K < 15K
    assert prefilter(job("40-60K"), cfg)[0] is True                         # 下限 40K > 30K
    assert prefilter(job("面议"), cfg) == (False, "")                       # 没写薪资不筛


def test_salary_preview_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from jobpilot.db import Repo, connect
    from jobpilot.safety.policy import Policy
    from jobpilot.services.context import ServiceContext
    from jobpilot.web.app import create_app

    cfg = AppConfig()

    def factory():
        repo = Repo(connect(tmp_path / "s.db"))
        return ServiceContext(config=cfg, repo=repo, llm=None, policy=Policy(repo, cfg))

    seed = factory()
    for i, salary in enumerate(["20-25K", "8千-1.2万", "40-60K", "面议"]):
        seed.repo.upsert_job(Job(id=f"j{i}", platform="boss", external_id=str(i), title=f"岗{i}", company=f"司{i}", salary=salary))
    seed.repo.conn.close()

    class Idle:
        running = False

    client = TestClient(create_app(factory, runner=Idle()), base_url="http://127.0.0.1:8700")
    res = client.get("/api/salary-preview?low=15&high=30").json()
    assert res == {"total": 4, "inside": 1, "below": 1, "above": 1, "unknown": 1}
