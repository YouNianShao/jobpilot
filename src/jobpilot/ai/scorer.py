from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

SYSTEM_PROMPT = "你是招聘匹配评估助手。根据候选人简历与目标岗位 JD，给出 0-100 的匹配分数与简要理由（中文，≤80字）。只输出 JSON，格式：{\"score\": <int>, \"reason\": \"<string>\"}。"


def load_resume(path: str) -> str:
    if not path or not path.strip():
        return ""
    p = Path(path)
    if not p.exists() or p.is_dir():
        return ""
    return p.read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    return {}


def score_job(cfg: dict, job: dict, resume_text: str) -> tuple[int, str]:
    ai = cfg.get("ai", {})
    if not ai.get("api_key"):
        return 0, "未配置 AI API Key"
    jd = job.get("jd") or ""
    prompt = f"""# 简历
{resume_text}

# 岗位
标题：{job.get('title','')}
公司：{job.get('company','')}
薪资：{job.get('salary','')}
城市：{job.get('city','')}
经验：{job.get('experience','')}
JD：{jd}"""
    try:
        r = httpx.post(
            f"{ai['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {ai['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": ai.get("model", "deepseek-chat"),
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        return int(data.get("score", 0)), data.get("reason", "")
    except Exception as e:  # noqa: BLE001 - 评分失败不应中断批量流程
        return 0, f"评分失败: {e}"


def generate_text(cfg: dict, system: str, user: str, temperature: float = 0.4) -> str | None:
    """通用 AI 文本生成（用于建议回复等）。失败返回 None。"""
    ai = cfg.get("ai", {})
    if not ai.get("api_key"):
        return None
    try:
        r = httpx.post(
            f"{ai['base_url'].rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {ai['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": ai.get("model", "deepseek-chat"),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # noqa: BLE001
        return f"[生成失败: {e}]"
