"""前程无忧 / 猎聘 / 智联的 HR 进展监测：只读「我的投递」列表，按标题 + 公司对上已投递岗位。"""
from __future__ import annotations

from jobpilot.models import Job
from jobpilot.platforms.record_monitor import match_rows, state_of


def _job(title, company, platform="zhaopin"):
    return Job(id=f"{platform}-1", platform=platform, external_id="1", title=title, company=company)


def test_state_words_from_real_pages():
    assert state_of("数据主管 上海软科 已查看 2026-10-16 20:31") == "viewed"
    assert state_of("某岗位 暂不合适 2026-10-10") == "rejected"
    assert state_of("某岗位 企业对你有意 2026-10-16") == "replied"
    assert state_of("某岗位 邀面试 2026-10-16") == "interview"
    assert state_of("某岗位 成功投递 2026-10-16") == ""          # 只是投出去了，不算进展



def test_most_advanced_state_wins():
    job = _job("AI解决方案工程师", "示例软件")
    rows = ["AI解决方案工程师 2.5-3万 示例软件 成功投递 2026-10-16",
            "AI解决方案工程师 2.5-3万 示例软件 被查看 2026-10-16"]
    assert [s for _, s, _ in match_rows(rows, [job])] == ["viewed"]


def test_company_must_match_too():
    rows = ["AI解决方案工程师 2.5-3万 示例软件 被查看 2026-10-16"]
    assert match_rows(rows, [_job("AI解决方案工程师", "别的公司")]) == []




def test_liepin_title_prefix_and_company_suffix_are_ignored():
    job = _job("招聘AI数据分析", "示例汽车 整车制造", platform="liepin")
    rows = ["AI数据分析 【上海】 20-30k 示例汽车分公司 已查看 2026-10-16"]
    assert [s for _, s, _ in match_rows(rows, [job])] == ["viewed"]


# ---------------- 智联「消息」聊天列表（2026-10-20 核对过 .im-session-item 结构） ----------------
def test_im_rows_skip_template_and_read_plain_messages():
    from jobpilot.platforms.record_monitor import classify_im_rows

    tpl = "您好，我对这个职位很感兴趣"                       # 同一句出现在很多会话里 = 平台替我发的模板话
    rows = [{"company": f"公司{i}", "last": tpl, "muted": True} for i in range(5)]
    rows += [
        {"company": "甲", "last": "方便发一下作品集吗", "badge": "1"},           # 未读 → HR 回复
        {"company": "乙", "last": "好的收到", "muted": True},                    # 已读普通话：看不出谁说的，跳过
        {"company": "丙", "last": "明天下午来公司面试可以吗", "muted": True},     # 有关键词：照样记
        {"company": "丁", "last": "", "badge": "2"},                             # 空预览跳过
        {"company": "戊", "last": "在吗", "badge": "0"},                         # 角标是 0、也没变灰 → 未读的新消息
    ]
    got = {r["company"]: (state, unread) for r, state, unread in classify_im_rows(rows)}
    assert got["甲"] == ("replied", True)
    assert "乙" not in got and "丁" not in got and not any(k.startswith("公司") for k in got)
    assert got["丙"][0] == "interview"
    assert got["戊"] == ("replied", False)


def test_zhaopin_chat_writes_interactions_and_matches_unique_company(tmp_path):
    from conftest import open_time_gates

    from jobpilot.browser.fake import FakeBrowser, FakePage
    from jobpilot.config import AppConfig
    from jobpilot.db import Repo, connect
    from jobpilot.models import MonitorResult, Status
    from jobpilot.platforms.base import DriverContext, StopSignal
    from jobpilot.platforms.record_monitor import ZP_IM_URL, monitor_zhaopin_chat
    from jobpilot.safety.policy import Policy

    cfg = open_time_gates(AppConfig())
    cfg.safety.page_interval_min_seconds = cfg.safety.page_interval_max_seconds = 0
    repo = Repo(connect(tmp_path / "z.db"))
    for jid, company in (("zhaopin-1", "上海示例软件科技"), ("zhaopin-2", "同名集团"), ("zhaopin-3", "同名集团")):
        repo.upsert_job(Job(id=jid, platform="zhaopin", external_id=jid, title=f"岗位{jid}", company=company))
        repo.set_status(jid, Status.APPLIED.value)
    sessions = [{"name": "王女士", "company": "上海示例软件科技", "last": "简历收到了，方便电话聊吗", "time": "10:21", "badge": "1"},
                {"name": "李先生", "company": "同名集团", "last": "您好，在的", "time": "昨天", "badge": "1"}]
    page = FakePage(url=ZP_IM_URL, selector_text={".im-session-item": "x"}, evals={"im-session-item": sessions})
    stop = StopSignal()
    stop.sleep = lambda s: None
    ctx = DriverContext(platform="zhaopin", config=cfg, repo=repo, policy=Policy(repo, cfg),
                        browser=FakeBrowser([page]), stop=stop)
    result = MonitorResult()
    monitor_zhaopin_chat(ctx, result)
    one = repo.get_interaction("zhaopin:zhaopin-1")
    assert one["state"] == "replied" and one["unread"] and one["msg_time"] == "10:21" and one["hr_name"] == "王女士"
    assert repo.get_interaction("zhaopin:im:李先生|同名集团")["job_id"] is None   # 同名公司有两个岗位：不乱认
    assert result.checked == 2 and result.events.get("replied") == 2
    # 只读：没点任何东西
    assert page.clicks == []
