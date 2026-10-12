"""推理层：供应商无关的 LLM 客户端。

原则：**AI 只做判断，不做流程**。这里只负责「发提示词 → 收结构化结果 → 记账」，
不决定下一步做什么，也不碰任何副作用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from ..config import AiCfg


@dataclass
class LlmResult:
    text: str = ""
    data: dict[str, Any] | None = None
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: int = 0
    error: str = ""
    error_kind: str = ""  # auth / rate_limit / network / request / parse / empty

    @property
    def ok(self) -> bool:
        return not self.error


@runtime_checkable
class LlmClient(Protocol):
    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LlmResult: ...


def extract_json(text: str) -> dict | None:
    """从模型回复里取 JSON；取不到返回 None（**不猜、不编**）。"""
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            data, _ = decoder.raw_decode(text[index:])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _classify_status(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "network"
    return "request"


def _error_detail(response: httpx.Response) -> str:
    """把服务商的报错原因带出来（「模型不存在」「Key 无效」），只取前 160 字，不含请求内容。"""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = (error.get("message") if isinstance(error, dict) else error) or body.get("message")
    except (ValueError, AttributeError):
        message = response.text
    message = " ".join(str(message or "").split())[:160]
    return f"：{message}" if message else ""


class OpenAiCompatClient:
    """OpenAI Chat Completions 兼容：DeepSeek / 豆包 / Qwen / GLM / Kimi / 硅基流动 / OpenAI / Gemini。"""

    def __init__(self, cfg: AiCfg) -> None:
        self.cfg = cfg
        self.base = (cfg.base_url or "").rstrip("/")
        self._usage = {"calls": 0, "tokens_in": 0, "tokens_out": 0}

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> LlmResult:
        started = time.time()

        def fail(message: str, kind: str) -> LlmResult:
            return LlmResult(error=message, error_kind=kind, model=self.cfg.model,
                             latency_ms=int((time.time() - started) * 1000))

        if not self.cfg.api_key:
            return fail("未配置 AI API Key", "auth")
        if not self.base:
            return fail("未配置 AI base_url", "auth")

        sys_prompt = system
        if schema is not None:
            sys_prompt += "\n\n只输出 JSON，且必须满足此 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens

        attempts = max(1, int(self.cfg.max_attempts))
        last = "请求失败"
        for attempt in range(attempts):
            try:
                response = httpx.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.cfg.timeout_seconds,
                )
                if response.status_code == 400 and "temperature" in payload and "temperature" in response.text:
                    # 推理类模型（OpenAI GPT-5 系等）只接受默认 temperature：去掉它重发，不算一次失败
                    payload.pop("temperature")
                    response = httpx.post(
                        f"{self.base}/chat/completions",
                        headers={"Authorization": f"Bearer {self.cfg.api_key}", "Content-Type": "application/json"},
                        json=payload,
                        timeout=self.cfg.timeout_seconds,
                    )
                if response.status_code >= 400:
                    kind = _classify_status(response.status_code)
                    last = f"HTTP {response.status_code}" + _error_detail(response)
                    if kind in {"auth", "request"}:
                        return fail(last, kind)
                    time.sleep(1.5 * (attempt + 1))
                    continue

                body = response.json()
                choices = body.get("choices") or [{}]
                text = ((choices[0] or {}).get("message") or {}).get("content") or ""
                usage = body.get("usage") or {}
                result = LlmResult(
                    text=text,
                    model=body.get("model") or self.cfg.model,
                    tokens_in=int(usage.get("prompt_tokens") or 0),
                    tokens_out=int(usage.get("completion_tokens") or 0),
                    latency_ms=int((time.time() - started) * 1000),
                )
                self._usage["calls"] += 1
                self._usage["tokens_in"] += result.tokens_in
                self._usage["tokens_out"] += result.tokens_out
                if not text.strip():
                    result.error, result.error_kind = "模型返回空内容", "empty"
                    return result
                if schema is not None:
                    data = extract_json(text)
                    if data is None:
                        result.error, result.error_kind = "模型未返回合法 JSON", "parse"
                    else:
                        result.data = data
                return result
            except Exception as exc:  # noqa: BLE001 - 网络异常要重试
                last = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
        return fail(last, "network")


class AnthropicClient:
    """Claude 原生 Messages API（不兼容 OpenAI 格式）。

    和 OpenAI 兼容接口的差别：system 是顶层字段；max_tokens 必填；
    回复是内容块列表（可能先有 thinking 块，只取 text 块）；
    新一代模型不接受自定义 temperature（会 400），所以**不发** temperature。
    """

    VERSION = "2023-06-01"
    DEFAULT_MAX_TOKENS = 4096

    def __init__(self, cfg: AiCfg) -> None:
        self.cfg = cfg
        self.base = (cfg.base_url or "https://api.anthropic.com/v1").rstrip("/")
        self._usage = {"calls": 0, "tokens_in": 0, "tokens_out": 0}

    @property
    def usage(self) -> dict[str, int]:
        return dict(self._usage)

    def complete(self, *, system: str, user: str, schema: dict | None = None,
                 temperature: float = 0.2, max_tokens: int | None = None) -> LlmResult:
        started = time.time()

        def fail(message: str, kind: str) -> LlmResult:
            return LlmResult(error=message, error_kind=kind, model=self.cfg.model,
                             latency_ms=int((time.time() - started) * 1000))

        if not self.cfg.api_key:
            return fail("未配置 AI API Key", "auth")

        sys_prompt = system
        if schema is not None:
            sys_prompt += "\n\n只输出 JSON，且必须满足此 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)
        payload = {
            "model": self.cfg.model,
            "max_tokens": int(max_tokens or self.DEFAULT_MAX_TOKENS),
            "system": sys_prompt,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self.cfg.api_key, "anthropic-version": self.VERSION,
                   "content-type": "application/json"}

        attempts = max(1, int(self.cfg.max_attempts))
        last = "请求失败"
        for attempt in range(attempts):
            try:
                response = httpx.post(f"{self.base}/messages", headers=headers, json=payload,
                                      timeout=self.cfg.timeout_seconds)
                if response.status_code >= 400:
                    # 529 = Anthropic 过载，和 5xx 一样等一下重试
                    kind = _classify_status(response.status_code)
                    last = f"HTTP {response.status_code}" + _error_detail(response)
                    if kind in {"auth", "request"}:
                        return fail(last, kind)
                    time.sleep(1.5 * (attempt + 1))
                    continue

                body = response.json()
                text = "".join(block.get("text") or "" for block in body.get("content") or []
                               if isinstance(block, dict) and block.get("type") == "text")
                usage = body.get("usage") or {}
                result = LlmResult(
                    text=text, model=body.get("model") or self.cfg.model,
                    tokens_in=int(usage.get("input_tokens") or 0),
                    tokens_out=int(usage.get("output_tokens") or 0),
                    latency_ms=int((time.time() - started) * 1000),
                )
                self._usage["calls"] += 1
                self._usage["tokens_in"] += result.tokens_in
                self._usage["tokens_out"] += result.tokens_out
                if not text.strip():
                    result.error, result.error_kind = "模型返回空内容", "empty"
                    return result
                if schema is not None:
                    data = extract_json(text)
                    if data is None:
                        result.error, result.error_kind = "模型未返回合法 JSON", "parse"
                    else:
                        result.data = data
                return result
            except Exception as exc:  # noqa: BLE001 - 网络异常要重试
                last = f"{type(exc).__name__}: {exc}"
                if attempt + 1 < attempts:
                    time.sleep(1.5 * (attempt + 1))
        return fail(last, "network")


def build_llm(cfg: AiCfg) -> LlmClient:
    """按服务商选协议：Claude 走原生接口，其余都是 OpenAI 兼容。"""
    from .providers import PROVIDERS, resolve_provider

    provider = resolve_provider(cfg.provider, cfg.base_url)
    if PROVIDERS[provider]["protocol"] == "anthropic":
        return AnthropicClient(cfg)
    return OpenAiCompatClient(cfg)


class FakeLlm:
    """测试用：按顺序返回预置响应；不需要 Key、不发网络请求。"""

    def __init__(self, responses: list[Any] | None = None, *, default: Any = None) -> None:
        self.responses = list(responses or [])
        self.default = default
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system, user, schema=None, temperature=0.2, max_tokens=None) -> LlmResult:
        self.calls.append({"system": system, "user": user, "schema": schema})
        item = self.responses.pop(0) if self.responses else self.default
        if isinstance(item, LlmResult):
            return item
        if isinstance(item, dict):
            return LlmResult(data=item, text=json.dumps(item, ensure_ascii=False))
        if isinstance(item, str):
            return LlmResult(text=item, data=extract_json(item) if schema is not None else None)
        return LlmResult(error="FakeLlm 没有更多响应", error_kind="empty")
