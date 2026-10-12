from __future__ import annotations

import json
import re
import time
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
        m = re.search(r"\{.*\}", text or "", re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except (json.JSONDecodeError, ValueError):
                return {}
    return {}


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chat(
    cfg: dict,
    system: str,
    user: str,
    temperature: float,
    *,
    max_attempts: int | None = None,
    timeout: float | None = None,
) -> str:
    """调用 OpenAI 兼容接口，带超时与指数退避重试；最终失败抛 RuntimeError。"""
    ai = cfg.get("ai", {}) or {}
    api_key = str(ai.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError("未配置 AI API Key")
    base = str(ai.get("base_url") or "").rstrip("/")
    if not base:
        raise RuntimeError("未配置 AI base_url")
    sc = cfg.get("scoring", {}) or {}
    if timeout is None:
        timeout = _num(ai.get("timeout_seconds"), _num(sc.get("timeout_seconds"), 30.0))
    if max_attempts is None:
        max_attempts = max(1, min(int(_num(sc.get("max_attempts"), 2)), 5))

    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            r = httpx.post(
                f"{base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
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
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt + 1 < max_attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last))


def score_job(cfg: dict, job: dict, resume_text: str) -> tuple[int, str]:
    ai = cfg.get("ai", {}) or {}
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
        content = _chat(cfg, SYSTEM_PROMPT, prompt, 0.2)
        data = _extract_json(content)
        score = int(_num(data.get("score"), 0))
        return max(0, min(score, 100)), str(data.get("reason", ""))
    except Exception as e:  # noqa: BLE001 - 评分失败不应中断批量流程
        return 0, f"评分失败: {e}"


def generate_text(cfg: dict, system: str, user: str, temperature: float = 0.4) -> str | None:
    """通用 AI 文本生成（招呼语/建议回复等）。无 Key 返回 None，调用失败返回错误串。"""
    ai = cfg.get("ai", {}) or {}
    if not ai.get("api_key"):
        return None
    try:
        return _chat(cfg, system, user, temperature).strip()
    except Exception as e:  # noqa: BLE001
        return f"[生成失败: {e}]"