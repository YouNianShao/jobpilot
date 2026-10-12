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
              title="数据分析师", company="润和软件",
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
