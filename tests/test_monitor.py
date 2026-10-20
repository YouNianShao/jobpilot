"""BOSS 聊天列表监测：只读列表、绝不进会话。

判断规则不是拍脑袋来的 —— 下面的样本直接抄自 2026-10-11 从真机聊天列表页抓到的 40 条会话。
"""
from __future__ import annotations

import json

from conftest import open_time_gates

from jobpilot.browser.fake import FakeBrowser, FakePage
from jobpilot.config import AppConfig
from jobpilot.db import Repo, connect
from jobpilot.models import Job
from jobpilot.platforms.base import DriverContext, StopSignal
from jobpilot.platforms.boss import CHAT_ROW, CHAT_URL, BossDriver, classify_chat_row
from jobpilot.safety.policy import Policy

# 实机样本（裁剪）：每一条代表一类典型会话
ROWS = [
    {"name": "谢女士", "company": "上海咏佳乐商务咨询",
     "last": "看到贵司的这个职位，觉得我挺合适，是否可以聊聊呢？", "status": "[送达]"},
    {"name": "苗女士", "company": "润和软件",
     "last": "期望20k 随时可以到港", "status": "[已读]"},
    {"name": "齐女士", "company": "Cognizant",
     "last": "没看到Databricks的经验", "status": ""},
    {"name": "陶女士", "company": "橙天嘉禾",
     "last": "经过认真考虑，我们认为您的经验与我们的职位需求并不完全一致。祝您在BOSS直聘找到您理想的工作！", "status": ""},
    {"name": "张芳云", "company": "上海汇正财经",
     "last": "您的附件简历 张三-简历 已发送给Boss点击查看附件", "status": ""},
    {"name": "邱先生", "company": "益盟股份",
     "last": "张三-简历.pdf", "status": ""},
    {"name": "熊先生", "company": "上海能良",
     "last": "您正在与Boss熊先生沟通", "status": ""},
]


def make_ctx(tmp_path, rows=None, jobs=()):
    cfg = AppConfig()
    cfg.platforms["boss"].enabled = True
    cfg.safety.platforms = {}
    cfg.safety.daily_pages.monitor = 40
    cfg.safety.page_interval_min_seconds = 0
    cfg.safety.page_interval_max_seconds = 0
    open_time_gates(cfg)                       # 时间闸门（时间窗 / 开页面时段 / 随机休息日）测试里一律关掉
    repo = Repo(connect(tmp_path / "m.db"))
    for job in jobs:
        repo.upsert_job(job)
        repo.set_status(job.id, job.status)        # upsert 不一定照搬状态：这里明确写进去
    payload = json.dumps(rows if rows is not None else ROWS, ensure_ascii=False)
    page = FakePage(url=CHAT_URL, selector_text={CHAT_ROW: ""}, evals={CHAT_ROW: payload})
    browser = FakeBrowser([page])
    ctx = DriverContext(
        config=cfg, repo=repo, browser=browser,
        policy=Policy(repo, cfg), stop=StopSignal(), platform="boss",
    )
    return ctx, page, repo


def test_classify_chat_row_reads_the_real_samples():
    """7 条样本各自对应一类真实情况，全在真机上见过。
    """
    by_name = {row["name"]: classify_chat_row(row) for row in ROWS}
    assert by_name["谢女士"] is None            # 我发的、对方还没看
    assert by_name["苗女士"][0] == "viewed"     # 我发的、对方看过了
    assert by_name["齐女士"][0] == "replied"    # HR 回了一句
    assert by_name["陶女士"][0] == "rejected"   # 回绝模板
    assert by_name["张芳云"] is None            # 系统提示：简历已发送
    assert by_name["邱先生"] is None            # 我自己发的附件（只剩文件名）
    assert by_name["熊先生"] is None            # 系统占位文案


def test_monitor_never_clicks_into_a_conversation(tmp_path):
    """最容易犯的错就是为了读消息点进会话 —— 那会把未读标成已读，而且撤不回来。
    """
    ctx, page, _ = make_ctx(tmp_path)
    BossDriver().monitor(ctx)

    assert page.clicks == []
    assert page.fills == {}


def test_monitor_writes_progress_and_matches_job_by_company(tmp_path):
    job = Job(id="boss-43f0", platform="boss", external_id="43f0",
              title="数据分析师", company="润和软件", status="applied",   # 监测只拿 BOSS 上投出去了的岗位
              url="https://www.zhipin.com/job_detail/43f0.html")
    ctx, page, repo = make_ctx(tmp_path, jobs=[job])

    result = BossDriver().monitor(ctx)

    assert result.checked == len(ROWS)
    assert page.clicks == []
    rows = {row["hr_name"]: row for row in repo.list_interactions()}
    assert rows["苗女士"]["state"] == "viewed"
    assert rows["苗女士"]["job_id"] == "boss-43f0"   # 靠公司名对回岗位池
    assert rows["苗女士"]["company"] == "润和软件"
    assert rows["齐女士"]["state"] == "replied"
    assert rows["齐女士"]["job_id"] is None          # 对不上就不硬认
    assert rows["陶女士"]["state"] == "rejected"
    assert "谢女士" not in rows                    # [送达] 不写库
    assert ctx.policy.budget.used("boss", "monitor") == 1


def test_monitor_does_not_move_a_line_backwards(tmp_path):
    """人工已经记到「约面试」，监测读到的却是更早的「已回复」—— 不能往回退。
    """
    ctx, _, repo = make_ctx(tmp_path)
    repo.upsert_interaction(key="auto:boss:齐女士|Cognizant", platform="boss",
                            job_id=None, hr_name="齐女士", state="interview",
                            last_text="约了周三下午", deeplink="")

    BossDriver().monitor(ctx)

    row = [r for r in repo.list_interactions() if r["hr_name"] == "齐女士"][0]
    assert row["state"] == "interview"


def _job(job_id, company, *, platform="boss", status="applied"):
    return Job(id=job_id, platform=platform, external_id=job_id, title="数据分析师",
               company=company, status=status)


def test_matching_only_uses_boss_jobs_that_were_applied(tmp_path):
    """同名公司在别的平台 / 还没投的岗位不算：以前全库按公司名比，一撞名就对不上（2026-10-20）。"""
    jobs = [
        _job("boss-1", "润和软件"),
        _job("51job-1", "润和软件", platform="51job"),     # 别的平台
        _job("boss-2", "润和软件", status="new"),          # BOSS 上还没投
    ]
    ctx, _, repo = make_ctx(tmp_path, jobs=jobs)
    BossDriver().monitor(ctx)
    row = [r for r in repo.list_interactions() if r["hr_name"] == "苗女士"][0]
    assert row["job_id"] == "boss-1"


def test_two_applied_boss_jobs_at_same_company_stay_unmatched(tmp_path):
    jobs = [_job("boss-1", "Cognizant"), _job("boss-2", "Cognizant", status="already")]
    ctx, _, repo = make_ctx(tmp_path, jobs=jobs)
    BossDriver().monitor(ctx)
    row = [r for r in repo.list_interactions() if r["hr_name"] == "齐女士"][0]
    assert row["job_id"] is None
    assert repo.list_events("boss-1") == [] and repo.list_events("boss-2") == []


def test_reply_writes_hr_reply_event_once_so_follow_up_stops(tmp_path):
    from jobpilot.bootstrap import build_context
    from jobpilot.services.followup import classify

    ctx, _, repo = make_ctx(tmp_path, jobs=[_job("boss-1", "Cognizant")])
    repo.add_event("boss-1", "applied", "applied")
    BossDriver().monitor(ctx)
    assert [e["kind"] for e in repo.list_events("boss-1")].count("hr_reply") == 1

    # 同样的列表再看一次：状态没往前走，不再记事件
    ctx2, _, _ = make_ctx(tmp_path)
    BossDriver().monitor(ctx2)
    assert [e["kind"] for e in repo.list_events("boss-1")].count("hr_reply") == 1

    service = build_context(AppConfig(), repo=repo)
    assert classify(service, repo.get_job("boss-1"))["state"] == "answered"


def test_new_message_updates_text_and_unread_but_keeps_higher_state(tmp_path):
    rows = [{"name": "齐女士", "company": "Cognizant", "last": "没看到Databricks的经验",
             "status": "", "time": "昨天", "unread": ""}]
    ctx, _, repo = make_ctx(tmp_path, rows=rows)
    BossDriver().monitor(ctx)
    key = "auto:boss:齐女士|Cognizant"
    # 人工改成了「约面试」
    repo.upsert_interaction(key=key, platform="boss", job_id=None, hr_name="齐女士",
                            state="interview", last_text="没看到Databricks的经验", deeplink="")
    first = repo.get_interaction(key)

    rows2 = [{**rows[0], "last": "那你方便周三来聊聊吗", "time": "10:21", "unread": "1"}]
    ctx2, _, _ = make_ctx(tmp_path, rows=rows2)
    result = BossDriver().monitor(ctx2)

    row = repo.get_interaction(key)
    assert row["state"] == "interview"              # 不往回退
    assert row["last_text"] == "那你方便周三来聊聊吗"   # 但新消息写进来了
    assert row["unread"] == 1 and row["msg_time"] == "10:21"
    assert row["updated_at"] >= first["updated_at"]
    assert result.events == {}                       # 状态没变 = 不算一条进展


def test_unread_badge_parsing():
    from jobpilot.platforms.boss import unread_of

    assert unread_of({"unread": "3"}) and unread_of({"unread": "99+"})
    assert not unread_of({"unread": ""}) and not unread_of({"unread": "0"}) and not unread_of({})
