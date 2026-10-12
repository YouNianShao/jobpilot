"""存储层：去重、状态流转、事件、页面计数、锁、互动。"""
from __future__ import annotations

from jobpilot.models import Job, Status


def make_job(**kw) -> Job:
    base = dict(
        id="51job-1", platform="51job", external_id="1",
        title="AI 产品经理", company="某某科技", salary="20-30K", city="上海",
        url="https://jobs.51job.com/x.html",
    )
    base.update(kw)
    return Job(**base)


def test_insert_and_get(repo):
    inserted, dup = repo.upsert_job(make_job())
    assert inserted is True and dup is None
    job = repo.get_job("51job-1")
    assert job is not None and job.title == "AI 产品经理"
    assert job.status == Status.NEW.value


def test_same_id_is_duplicate(repo):
    repo.upsert_job(make_job())
    inserted, dup = repo.upsert_job(make_job())
    assert inserted is False and dup == "51job-1"


def test_cross_platform_dedup(repo):
    repo.upsert_job(make_job())
    # 换个平台、标题带空格 → 归一化后同名同公司，应判重复
    inserted, dup = repo.upsert_job(
        make_job(id="liepin-9", platform="liepin", external_id="9", title="AI产品经理")
    )
    assert inserted is False and dup == "51job-1"
    assert len(repo.list_jobs()) == 1
    kinds = [e["kind"] for e in repo.list_events("51job-1")]
    assert "cross_platform_dup" in kinds


def test_different_company_kept(repo):
    repo.upsert_job(make_job())
    inserted, _ = repo.upsert_job(make_job(id="liepin-1", platform="liepin", company="另一家"))
    assert inserted is True
    assert len(repo.list_jobs()) == 2


def test_status_score_greeting(repo):
    repo.upsert_job(make_job())
    repo.set_score("51job-1", 82, "很匹配", Status.PENDING_CONFIRM.value)
    repo.set_greeting("51job-1", "你好，我对这个岗位很感兴趣。")
    job = repo.get_job("51job-1")
    assert job.score == 82
    assert job.status == Status.PENDING_CONFIRM.value
    assert job.greeting.startswith("你好")

    repo.set_status("51job-1", Status.APPROVED.value)
    assert repo.get_job("51job-1").status == Status.APPROVED.value
    assert repo.count_status(Status.APPROVED.value, "51job") == 1


def test_events(repo):
    repo.upsert_job(make_job())
    repo.add_event("51job-1", "applied", "投递成功")
    events = repo.list_events("51job-1")
    assert events[0]["kind"] == "applied"


def test_page_access_counting(repo):
    repo.record_page("51job", "collect_search")
    repo.record_page("51job", "collect_search")
    repo.record_page("51job", "apply")
    assert repo.count_pages("51job", stage="collect_search") == 2
    assert repo.count_pages("51job") == 3
    assert repo.count_pages("liepin") == 0     # 平台隔离


def test_locks_are_per_platform(repo):
    repo.set_lock("51job", "验证码", "captcha", "2099-01-01 00:00:00")
    assert repo.get_lock("51job")["kind"] == "captcha"
    assert repo.get_lock("liepin") is None
    repo.clear_lock("51job")
    assert repo.get_lock("51job") is None


def test_interactions_upsert(repo):
    repo.upsert_interaction("51job:1", "51job", "51job-1", "张女士", "interview", "邀面试", "")
    repo.upsert_interaction("51job:1", "51job", "51job-1", "张女士", "replied", "已回复", "")
    rows = repo.list_interactions()
    assert len(rows) == 1 and rows[0]["state"] == "replied"

def test_page_aggregations(repo):
    from datetime import datetime

    repo.record_page("boss", "collect_search")
    repo.record_page("boss", "collect_detail")
    repo.record_page("51job", "collect_search")

    assert repo.pages_by_stage() == {"collect_search": 2, "collect_detail": 1}
    assert repo.pages_by_stage(platform="boss") == {"collect_search": 1, "collect_detail": 1}
    hours = repo.pages_by_hour()
    assert len(hours) == 1 and hours[0]["hour"] == datetime.now().strftime("%H")
    assert hours[0]["count"] == 3
    assert sum(row["count"] for row in repo.pages_by_day(7)) == 3


def test_recent_events_can_filter_global_only(repo):
    repo.add_event("boss-1", "collected", "AI")
    repo.add_event(None, "risk_lock", "boss/captcha · 请完成验证 · 42 分钟")
    assert len(repo.list_recent_events(10)) == 2
    globals_only = repo.list_recent_events(10, job_id_null_only=True)
    assert len(globals_only) == 1 and globals_only[0]["kind"] == "risk_lock"
    assert repo.count_events_since("risk_lock", 7) == 1
    assert repo.count_events_since("collected", 7) == 1

def test_list_jobs_tolerates_none_limit(repo):
    """绑 None 到 LIMIT 会让 SQLite 抛 "datatype mismatch"，必须被兜住。"""
    repo.upsert_job(Job(id="boss-1", platform="boss", external_id="1", title="t", company="c"))
    assert len(repo.list_jobs(limit=None)) == 1
    assert len(repo.list_jobs(offset=None)) == 1

def _job(job_id: str, title: str = "数据分析师") -> Job:
    return Job(id=job_id, platform="boss", external_id=job_id, title=title, company="某公司")


def test_pool_funnel(repo):
    repo.upsert_job(_job("boss-1"))
    repo.upsert_job(_job("boss-2"))
    repo.set_score("boss-1", 88, "匹配", "pending_confirm")
    repo.set_score("boss-2", 20, "不匹配", "filtered")

    assert repo.pool_funnel(60) == {
        "total": 2, "scored": 2, "passed": 1, "confirmed": 0, "applied": 0,
    }
    repo.set_status("boss-1", "applied")
    assert repo.pool_funnel(60)["applied"] == 1


def test_keyword_stats_normalizes_platform_formats(repo):
    """采集事件里 `城市/关键词`、`关键词`、`关键词（第 N 批）` 三种格式都要归一到关键词。"""
    repo.upsert_job(_job("boss-1"))
    repo.upsert_job(_job("boss-2"))
    repo.upsert_job(_job("boss-3"))
    repo.add_event("boss-1", "collected", "上海/数据分析")
    repo.add_event("boss-2", "collected", "上海/数据分析")          # 同一个词两个岗位
    repo.add_event("boss-3", "collected", "AI（第 2 批）")          # 智联的批次后缀
    repo.set_score("boss-1", 88, "匹配", "pending_confirm")
    repo.set_score("boss-2", 52, "一般", "filtered")
    repo.set_score("boss-3", 20, "不匹配", "filtered")

    rows = {row["keyword"]: row for row in repo.keyword_stats(60)}
    assert set(rows) == {"数据分析", "AI"}
    assert rows["数据分析"]["jobs"] == 2
    assert rows["数据分析"]["avg_score"] == 70.0        # (88+52)/2
    assert rows["数据分析"]["best"] == 88
    assert rows["数据分析"]["passed"] == 1
    assert rows["AI"]["avg_score"] == 20.0
    assert rows["AI"]["passed"] == 0
    # 平均分高的排前面
    assert [row["keyword"] for row in repo.keyword_stats(60)][0] == "数据分析"


def test_company_stats_groups_by_company(repo):
    '''同一家公司挂一堆岗位是外包 / 中介的典型特征，所以要能按公司聚合。
    '''
    for index, (company, score) in enumerate([
        ('批量招聘公司', 20), ('批量招聘公司', 25), ('批量招聘公司', 30),
        ('正常公司', 88), ('没打分的公司', None),
    ]):
        repo.upsert_job(make_job(id='51job-' + str(index), external_id=str(index),
                            company=company, score=score))

    rows = repo.company_stats(threshold=60)
    assert [row['company'] for row in rows] == [
        '批量招聘公司', '正常公司', '没打分的公司']
    top = rows[0]
    assert top['jobs'] == 3 and top['passed'] == 0
    assert top['best'] == 30 and top['avg_score'] == 25.0
    assert rows[1]['passed'] == 1
    assert rows[2]['avg_score'] is None      # 没打分不能算成 0 分
