r"""临时演示数据：给界面截图用；--clear 精确清除（只删 demo- 前缀）。

用法: python tools\seed_demo.py [--clear]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobpilot.config import DB_PATH  # noqa: E402
from jobpilot.db import Repo, connect  # noqa: E402
from jobpilot.models import Job  # noqa: E402

DEMO_JD = ("1. 负责业务数据体系搭建，完成核心指标口径梳理、日常数据提取与监控看板；"
           "2. 围绕交付效率、客户留存、人效产出开展专项分析，输出可落地的优化策略；"
           "3. 熟练使用 SQL 完成数据提取与处理，会使用 Python / BI 工具（FineBI、Tableau）者优先。")

DEMOS = [
    ("boss", "boss-demo1", "资深数据分析师", "柯莱特集团", "20-28K", "上海", 88,
     "SQL/Python 建模、A/B 实验与归因经验扎实，6 年数据经验与岗位核心要求高度重合。",
     "您好，我做了 6 年数据分析，最近用 K-Means 和 XGBoost 搭建银保代理人成交预测模型，"
     "4 周 A/B 测试把转化率从 3.2% 提到 5.7%，日常用 SQL 和 Python 处理千万级数据。"),
    ("51job", "51job-demo2", "数据分析师（上海 全额五险一金）", "上海核子企业管理咨询", "1.2-1.7万", "上海", 76,
     "技能栈匹配，薪资区间落在期望范围内，公司规模与行业与过往经历接近。",
     "您好，我有 6 年保险数据分析经验，做过续保流失预警模型（AUC 0.82），"
     "用 Uplift 双模型区分可挽回客户，续保率提升 3.8 个百分点，熟悉 Python 与 XGBoost。"),
    ("liepin", "liepin-demo3", "数据分析专员（运营分析方向）", "某上海大型保险公司", "15-25k", "上海", 71,
     "行业与岗位方向契合，缺少私域营销经验，属于可以在沟通中弥补的差距。",
     "您好，我在保险行业做了 6 年数据分析，负责过代理人分层运营与经营诊断，"
     "产出过跨部门落地的优化策略，对运营指标拆解和归因分析有实操经验。"),
]


def clear(repo: Repo) -> int:
    row = repo.conn.execute("SELECT COUNT(*) FROM jobs WHERE id LIKE 'demo%' OR id LIKE '%demo%'").fetchone()
    jobs = int(row[0])
    repo.conn.execute("DELETE FROM events WHERE job_id LIKE '%demo%'")
    repo.conn.execute("DELETE FROM interactions WHERE key LIKE '%demo%'")
    repo.conn.execute("DELETE FROM jobs WHERE id LIKE '%demo%'")
    repo.conn.commit()
    return jobs


def main() -> int:
    repo = Repo(connect(DB_PATH))
    if "--clear" in sys.argv:
        removed = clear(repo)
        total = len(repo.list_jobs(limit=1000))
        print(f"已清除演示岗位 {removed} 条；当前岗位总数 {total}")
        repo.conn.close()
        return 0

    for platform, job_id, title, company, salary, city, score, reason, greeting in DEMOS:
        repo.upsert_job(Job(id=job_id, platform=platform, external_id=job_id,
                            title=title, company=company, salary=salary, city=city,
                            url="https://example.com/" + job_id, jd=DEMO_JD))
        repo.set_score(job_id, score, reason, "pending_confirm")
        repo.set_greeting(job_id, greeting)
        repo.add_event(job_id, "collected", "演示")
        repo.add_event(job_id, "scored", f"{score} {reason[:20]}")
    # 一个"已投递 3 天、至今无回复"的岗位，用来演示自动跟进
    repo.upsert_job(Job(id="boss-demo4", platform="boss", external_id="demo4",
                        title="数据产品经理", company="某互联网大厂", salary="25-35K", city="上海",
                        url="https://example.com/boss-demo4", jd=DEMO_JD))
    repo.set_score("boss-demo4", 79, "数据产品经验匹配，缺少大厂背景。", "applied")
    from datetime import datetime, timedelta
    when = (datetime.now() - timedelta(hours=72)).strftime("%Y-%m-%d %H:%M:%S")
    repo.conn.execute(
        "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
        ("boss-demo4", "applied", "applied", when),
    )
    repo.conn.commit()

    repo.upsert_interaction("51job:demo1", "51job", "51job-demo2", "张女士", "interview", "邀请您参加面试", "")
    repo.upsert_interaction("boss:demo2", "boss", "boss-demo1", "李先生", "viewed", "已查看您的简历", "")
    print(f"已写入演示岗位 {len(DEMOS)} 条（前缀 demo）")
    print("当前岗位总数", len(repo.list_jobs(limit=1000)))
    repo.conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
