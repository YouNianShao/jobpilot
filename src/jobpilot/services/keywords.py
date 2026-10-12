"""搜索关键词建议：让模型看着简历 + 你实际采到的结果，给一批能直接用的搜索词。

为什么要做这个：搜索词决定了你会看到什么岗位。搜 "AI" 会捞出一堆"AI 获客/销售"，
而这是**纯亏**的 —— 每个词都要花页面预算，换回来的却是零匹配。
这个服务不碰浏览器，只读简历和已有数据，一次模型调用。
"""

from __future__ import annotations

from ..ai.prompting import render, version
from .context import ServiceContext

PROMPT = "keywords.v1.md"
KEYWORDS_SCHEMA = {
    "type": "object",
    "required": ["keywords"],
    "properties": {
        "keywords": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "avoid": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
    },
}


def _current_keywords(ctx: ServiceContext) -> list[str]:
    words: list[str] = []
    for cfg in (ctx.config.platforms or {}).values():
        words.extend(cfg.keywords or [])
    seen = set()
    out = []
    for word in words:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _samples(ctx: ServiceContext, limit: int = 24) -> str:
    """采到的岗位标题 + 分数：让模型看到"这个词实际捞回了什么"。"""
    jobs = ctx.repo.list_jobs(limit=limit)
    if not jobs:
        return "（还没有采到任何岗位）"
    lines = []
    for job in jobs:
        score = "未评分" if job.score is None else f"{job.score} 分"
        lines.append(f"- {job.title} @ {job.company}（{score}）")
    return "\n".join(lines)


def _evidence(ctx: ServiceContext) -> str:
    stats = ctx.repo.keyword_stats(ctx.config.scoring.threshold)
    if not stats:
        return "（还没有按关键词统计过效果）"
    lines = []
    for row in stats:
        avg = "未评分" if row["avg_score"] is None else f"平均 {row['avg_score']} 分"
        lines.append(f"- {row['keyword']}：采到 {row['jobs']} 个岗位，{avg}，{row['passed']} 个过线")
    return "\n".join(lines)


def suggest_keywords(ctx: ServiceContext, *, limit: int = 8) -> dict:
    resume, problem = ctx.resume_text()
    if problem:
        return {"error": problem}

    current = _current_keywords(ctx)
    result = ctx.llm.complete(
        system="你是求职搜索顾问。严格按用户给的 JSON Schema 输出。",
        user=render(
            PROMPT,
            resume=resume[:5000],
            current="、".join(current) or "（还没有配置）",
            samples=_samples(ctx),
            evidence=_evidence(ctx),
        ),
        schema=KEYWORDS_SCHEMA,
        temperature=0.3,
    )
    if not result.ok or not result.data:
        return {"error": result.error or "模型没有返回可用结果"}

    def clean(items) -> list[dict]:
        out = []
        for item in items or []:
            text = str((item or {}).get("text", "")).strip()
            if not text or text in {row["text"] for row in out}:
                continue
            out.append({"text": text, "reason": str((item or {}).get("reason", "")).strip()[:40]})
        return out[:limit]

    return {
        "keywords": clean(result.data.get("keywords")),
        "avoid": clean(result.data.get("avoid")),
        "current": current,
        "model": result.model or "",
        "prompt": f"{PROMPT} {version(PROMPT)}",
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "latency_ms": result.latency_ms,
    }
