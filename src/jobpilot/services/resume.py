"""定制简历：基于真实简历，针对某个岗位重新组织（绝不新增事实）。

产物是 Markdown 文件，落盘到 profile.resume_output_dir，路径写回 jobs.resume_path。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..ai.prompting import render, version
from ..config import ROOT
from .context import ServiceContext

PROMPT = "resume.v1.md"
RESUME_SCHEMA = {
    "type": "object",
    "required": ["markdown"],
    "properties": {
        "markdown": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}},
    },
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_URL = re.compile(r"https?://[^\s)>\]]+|www\.[^\s)>\]]+", re.IGNORECASE)
# 只把"带单位的数字"和"多位数字"当作可疑事实，避免把 Markdown 的 `1.` 列表序号误判成编造
_NUM_WITH_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:年|个月|月|%|％|万|人|次|天|周|[Kk]\b)")
_NUM_MULTI = re.compile(r"(?<!\d)(\d{2,})(?!\d)")
_LIST_MARKER = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s+")


def _job_text(job) -> str:
    return (
        f"职位：{job.title}\n公司：{job.company}\n薪资：{job.salary or '未提供'}\n"
        f"城市：{job.city or '未提供'}\nJD：{(job.jd or '')[:2000]}"
    )


def _read_source_resume(ctx: ServiceContext) -> str:
    # 走统一入口：以前这里直接 read_text，换成 Word / PDF 简历就会读出乱码或直接抛错
    text, _problem = ctx.resume_text()
    return text


def _output_dir(ctx: ServiceContext) -> Path:
    raw = (ctx.config.profile.resume_output_dir or "./data/resumes").strip()
    path = Path(raw)
    return path if path.is_absolute() else (ROOT / path)


def _canon(value: str) -> str:
    """数字归一化：03 == 3、3.0 == 3，避免格式差异造成误判。"""
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number == int(number) else repr(number)


def _facts(text: str) -> set[str]:
    """用**同一套规则**从文本里提取事实性数字（源和目标必须一致）。"""
    body = _LIST_MARKER.sub("", text or "")
    raw: set[str] = set()
    raw |= set(_NUMBER.findall(body))
    raw |= set(_NUM_WITH_UNIT.findall(body))
    raw |= set(_NUM_MULTI.findall(body))
    return {_canon(item) for item in raw}


def _ungrounded_numbers(markdown: str, source: str) -> list[str]:
    """找出定制稿里、原始简历里没有出现过的**事实性数字**。

    只查两类：带单位的数字（6 年 / 3.2% / 40 人 / 20K）和多位数（40、1287）。
    单数字 + 无单位的一律放行，否则 Markdown 的 `1.` 列表序号会被误杀。
    """
    known = _facts(source)
    body = _LIST_MARKER.sub("", markdown or "")
    candidates = set(_NUM_WITH_UNIT.findall(body)) | set(_NUM_MULTI.findall(body))
    return sorted(n for n in candidates if _canon(n) not in known)


def generate_resume(ctx: ServiceContext, job_id: str) -> dict:
    job = ctx.repo.get_job(job_id)
    if job is None:
        return {"error": "岗位不存在"}

    source = _read_source_resume(ctx)
    if not source.strip():
        return {"error": "未配置简历（profile.resume_path），无法生成定制简历"}

    def call(critique: str):
        return ctx.llm.complete(
            system="你是简历顾问，严格按 JSON Schema 输出，不得编造任何事实。",
            user=render(PROMPT, job=_job_text(job), resume=source[:6000], critique=critique),
            schema=RESUME_SCHEMA,
            temperature=0.3,
        )

    result = call("")
    if not result.ok or not result.data:
        return {"error": result.error or "模型未返回定制简历"}

    markdown = str(result.data.get("markdown") or "").strip()
    if not markdown:
        return {"error": "模型返回的简历为空"}

    # 防编造：带单位的数字与多位数字必须在原始简历里出现过
    ungrounded = _ungrounded_numbers(markdown, source)
    if ungrounded:
        critique = (
            "\n# 上一版被拦下的问题（必须修正）\n"
            "- 以下数字在你提供的简历里**不存在**：" + "、".join(ungrounded[:10]) + "\n"
            "- 请删除它们，或改回简历原文中的数字；**不要新增任何事实**\n"
        )
        retry = call(critique)
        retry_markdown = str((retry.data or {}).get("markdown") or "").strip() if retry.ok else ""
        if retry_markdown and not _ungrounded_numbers(retry_markdown, source):
            markdown, ungrounded = retry_markdown, []
        # 仍不合规：保留草稿 + 警告，由用户决定（不再直接丢弃整份简历）

    out_dir = _output_dir(ctx)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_id = re.sub(r"[^0-9A-Za-z_.-]", "_", job.id)
    path = out_dir / f"{safe_id}-{stamp}.md"
    path.write_text(markdown, encoding="utf-8")

    ctx.repo.set_resume_path(job.id, str(path))
    suffix = f"（警告：含简历原文没有的数字 {'、'.join(ungrounded[:5])}）" if ungrounded else ""
    ctx.repo.add_event(job.id, "resume", f"{path.name}{suffix} [{PROMPT} {version(PROMPT)}]")

    return {
        "job_id": job.id,
        "path": str(path),
        "markdown": markdown,
        "highlights": result.data.get("highlights") or [],
        "warnings": ungrounded,
        "prompt": f"{PROMPT} {version(PROMPT)}",
    }
