"""AI 招呼语生成：为待确认/已通过岗位生成一句可直接发送的开场招呼语。

结果写入 jobs.greeting，供仪表盘展示与复制；若平台支持沟通框，也可直接粘贴发送。
"""
from __future__ import annotations

from typing import Any

from jobpilot.ai.scorer import generate_text, load_resume
from jobpilot.control import yield_point
from jobpilot.db import get_conn, jobs_by_status, log_history, set_greeting
from jobpilot.progress import job as prog_job
from jobpilot.progress import phase as prog_phase

_PLATFORM_LABEL = {"51job": "前程无忧", "liepin": "猎聘", "zhaopin": "智联招聘"}

SYSTEM = (
    "你是求职助手。请根据候选人简历与目标岗位，写一句发给 HR 的开场招呼语。"
    "要求：中文，40-90字，最多3个短句；只讲一项与岗位最相关的真实经历，"
    "不得编造经历或头衔；不堆砌术语和套话；用陈述句，不要用问句收尾；"
    "只输出招呼语正文，不要任何解释或标记。"
)

DEFAULT_STATUSES = ("pending_confirm", "approved")


def build_prompt(job: dict, resume: str) -> str:
    platform = _PLATFORM_LABEL.get(str(job.get("platform") or ""), "招聘平台")
    return f"""# 目标岗位（{platform}）
- 职位：{job.get('title', '')}
- 公司：{job.get('company', '')}
- 城市：{job.get('city', '')}
- 薪资：{job.get('salary', '')}
- JD：{(job.get('jd') or '')[:600]}

# 我的简历
{(resume or '（未配置简历，请写通用但克制的求职招呼语）')[:1500]}"""


def generate_greeting(cfg: dict, job: dict, resume: str) -> str | None:
    """生成一条招呼语；AI 不可用或返回失败时返回 None。"""
    text = generate_text(cfg, SYSTEM, build_prompt(job, resume), temperature=0.6)
    if not text:
        return None
    text = text.strip().strip('"').strip()
    if not text or text.startswith("[生成失败"):
        return None
    return text


def ensure_greetings(
    cfg: dict[str, Any],
    platform: str,
    *,
    job_ids: list[str] | None = None,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
) -> dict[str, Any]:
    """为缺招呼语的待确认/已通过岗位生成招呼语。返回统计汇总。"""
    if not (cfg.get("greeting", {}) or {}).get("enabled", True):
        return {"greeting": 0, "skipped": "greeting disabled"}
    if not (cfg.get("ai", {}) or {}).get("api_key"):
        return {"greeting": 0, "skipped": "未配置 AI API Key"}

    conn = get_conn()
    try:
        jobs = jobs_by_status(conn, statuses, platform=platform, job_ids=job_ids)
        todo = [job for job in jobs if not str(job.get("greeting") or "").strip()]
        resume = load_resume((cfg.get("profile", {}) or {}).get("resume_path", ""))
        prog_phase("greet", len(todo), 0, "生成招呼语…")
        count = 0
        failed = 0
        for i, job in enumerate(todo, 1):
            yield_point()
            prog_job(i, len(todo), f"{job['title']}@{job['company']}")
            text = generate_greeting(cfg, job, resume)
            if not text:
                failed += 1
                continue
            set_greeting(conn, job["id"], text)
            log_history(conn, job["id"], "greeting", text[:60])
            count += 1
        return {"greeting": count, "greeting_failed": failed, "greeting_total": len(todo)}
    finally:
        conn.close()