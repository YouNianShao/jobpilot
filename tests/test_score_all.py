"""点一次评分 = 全部评完（以前固定 100 个、预筛淘汰的也算在里面，300 个只评掉七八十个）。"""
from __future__ import annotations

from test_chain import ScriptedLlm, wired  # noqa: F401  (wired 是 fixture)

from jobpilot.ai.llm import LlmResult
from jobpilot.models import Job, Status
from jobpilot.services.greeter import greet_jobs
from jobpilot.services.scorer import score_jobs


def _seed(ctx, n, *, every_nth_bad=0):
    for i in range(n):
        title = "销售代表" if every_nth_bad and i % every_nth_bad == 0 else f"AI 产品经理 {i}"
        ctx.repo.upsert_job(Job(id=f"fake-{i}", platform="fake", external_id=str(i), title=title,
                                company=f"公司{i}", jd="负责 AI 产品"))


def test_one_click_scores_all_300(wired):  # noqa: F811
    ctx, _ = wired
    _seed(ctx, 300, every_nth_bad=4)                       # 1/4 命中黑名单「销售」→ 预筛淘汰
    out = score_jobs(ctx, None, resume_text="简历")
    assert out["scored"] + out["prefiltered"] == 300
    assert ctx.repo.list_jobs(platform="fake", status=Status.NEW.value, limit=1000) == []


def test_all_calls_failing_stops_instead_of_looping(wired):  # noqa: F811
    ctx, _ = wired

    class Down:
        calls = 0

        def complete(self, **kw):
            Down.calls += 1
            return LlmResult(text="", data={}, error="服务不可用")

    ctx.llm = Down()
    _seed(ctx, 250)
    out = score_jobs(ctx, None, resume_text="简历")
    assert out["failed"] == 100 and Down.calls == 100       # 第一批全失败就停，不对着同一批空转


def test_explicit_limit_still_respected(wired):  # noqa: F811
    ctx, _ = wired
    _seed(ctx, 50)
    assert score_jobs(ctx, None, limit=10, resume_text="简历")["scored"] == 10


def test_greetings_reach_every_pending_job_even_in_a_big_pool(wired):  # noqa: F811
    ctx, _ = wired
    ctx.llm = ScriptedLlm()
    for i in range(400):                                    # 一大堆别的状态的岗位排在前面
        ctx.repo.upsert_job(Job(id=f"old-{i}", platform="fake", external_id=f"o{i}", title=f"旧岗{i}", company=f"旧司{i}"))
        ctx.repo.set_score(f"old-{i}", 99, "x", Status.FILTERED.value)
    for i in range(5):
        ctx.repo.upsert_job(Job(id=f"p-{i}", platform="fake", external_id=f"p{i}", title=f"待确认{i}", company=f"司{i}"))
        ctx.repo.set_score(f"p-{i}", 80, "ok", Status.PENDING_CONFIRM.value)
    assert greet_jobs(ctx, None)["greeted"] == 5
