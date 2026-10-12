"""面试准备：针对某个岗位预测面试问题，并指出该用哪段真实经历回答。

和评分/招呼语同一条纪律：AI 只出内容，不编造经历 ——
prompt 里明确要求 angle 必须来自简历，简历里没有的写"如实说明"。
不碰浏览器，一次模型调用。
"""

from __future__ import annotations

from ..ai.prompting import render, version
from .context import ServiceContext

PROMPT = "interview.v1.md"
INTERVIEW_SCHEMA = {
    "type": "object",
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["q"],
                "properties": {
                    "q": {"type": "string"},
                    "why": {"type": "string"},
                    "angle": {"type": "string"},
                },
            },
        },
        "watchouts": {"type": "array", "items": {"type": "string"}},
    },
}


def _progress_note(ctx: ServiceContext, job_id: str) -> str:
    """把"约面试"这类已记录的进展带上，模型能据此调整重点。"""
    rows = [row for row in ctx.repo.list_interactions(limit=200) if row.get("job_id") == job_id]
    if not rows:
        return "（还没有记录任何进展）"
    labels = {"viewed": "已查看", "replied": "已回复", "interview": "约面试",
              "interviewed": "面试完", "offer": "拿到 offer", "rejected": "被拒"}
    row = rows[0]
    text = row.get("last_text") or ""
    return f"{labels.get(row.get('state'), row.get('state'))}{('：' + text) if text else ''}"


def prepare_interview(ctx: ServiceContext, job_id: str) -> dict:
    job = ctx.repo.get_job(job_id)
    if job is None:
        return {"error": f"没有这个岗位：{job_id}"}

    resume, problem = ctx.resume_text()
    if problem:
        return {"error": problem}
    if not (job.jd or "").strip():
        return {"error": f"「{job.title}」没有 JD，面试准备会变成通用题清单；"
                         f"到设置里打开「打开详情页抓 JD」再采一次，或者手动补一段 JD"}

    result = ctx.llm.complete(
        system="你是面试教练。严格按用户给的 JSON Schema 输出。",
        user=render(
            PROMPT,
            resume=resume[:5000],
            job=f"{job.title} @ {job.company}（{job.city or '城市未知'} · {job.salary or '薪资未知'}）",
            jd=job.jd[:3000],
            progress=_progress_note(ctx, job_id),
        ),
        schema=INTERVIEW_SCHEMA,
        temperature=0.4,
    )
    if not result.ok or not result.data:
        return {"error": result.error or "模型没有返回可用结果"}

    questions = []
    for item in result.data.get("questions") or []:
        text = str((item or {}).get("q", "")).strip()
        if not text:
            continue
        questions.append({
            "q": text,
            "why": str((item or {}).get("why", "")).strip()[:60],
            "angle": str((item or {}).get("angle", "")).strip()[:200],
        })

    ctx.repo.add_event(job_id, "interview_prep", f"{len(questions)} 个问题 [{PROMPT} {version(PROMPT)}]")
    return {
        "job_id": job_id, "title": job.title, "company": job.company,
        "questions": questions[:10],
        "watchouts": [str(x).strip()[:80] for x in (result.data.get("watchouts") or [])][:3],
        "model": result.model or "",
        "prompt": f"{PROMPT} {version(PROMPT)}",
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "latency_ms": result.latency_ms,
    }
