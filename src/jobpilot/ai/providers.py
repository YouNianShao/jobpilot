"""模型服务商预设：选一个服务商 → 接口地址、协议、推荐模型自动带出。

接口地址与模型名取自各家官方文档（2026-10-12 核对）。模型名只是**推荐**：
各家上新很快，配置页的「模型」仍然可以手填任何名字。

协议只有两种：
- ``openai``：OpenAI Chat Completions 兼容（绝大多数国内服务商都是这个）
- ``anthropic``：Claude 原生 Messages API（不兼容 OpenAI 格式，单独一个客户端）
"""

from __future__ import annotations

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek", "protocol": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-flash", "deepseek-v4-pro"],   # 老名字 deepseek-chat 仍可用（实测会路由到 flash）
        "key_hint": "platform.deepseek.com → API Keys",
    },
    "doubao": {
        "name": "豆包（火山方舟）", "protocol": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-seed-2-1-pro-260915"],
        "key_hint": "火山方舟控制台 → API Key 管理；模型名也可以填你创建的推理接入点 ID（ep-…）",
    },
    "qwen": {
        "name": "通义千问（阿里云百炼）", "protocol": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.7-plus", "qwen3.8-max", "qwen3.5-flash"],
        "key_hint": "百炼控制台 → API-KEY（Key 分地域，北京地域的 Key 只能配北京地址）",
    },
    "glm": {
        "name": "智谱 GLM", "protocol": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-5.3", "glm-5.2"],
        "key_hint": "open.bigmodel.cn → API Keys",
    },
    "kimi": {
        "name": "Kimi（月之暗面）", "protocol": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["kimi-k2.6", "kimi-k3"],
        "key_hint": "platform.moonshot.cn → API Key 管理",
    },
    "siliconflow": {
        "name": "硅基流动", "protocol": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V4-Flash", "Pro/deepseek-ai/DeepSeek-V4", "Pro/zai-org/GLM-5.2"],
        "key_hint": "cloud.siliconflow.cn → API 密钥",
    },
    "openai": {
        "name": "OpenAI", "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-6-astra"],
        "key_hint": "platform.openai.com → API keys（国内网络通常需要代理）",
    },
    "anthropic": {
        "name": "Claude（Anthropic）", "protocol": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"],
        "key_hint": "console.anthropic.com → API Keys（国内网络通常需要代理）",
    },
    "gemini": {
        "name": "Gemini（Google）", "protocol": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "models": ["gemini-3.8-flash"],
        "key_hint": "aistudio.google.com → Get API key（国内网络通常需要代理）",
    },
    "custom": {
        "name": "自定义（OpenAI 兼容）", "protocol": "openai",
        "base_url": "", "models": [],
        "key_hint": "任何兼容 OpenAI Chat Completions 的服务（含本地 Ollama / vLLM）",
    },
}


def resolve_provider(provider: str, base_url: str) -> str:
    """配置里没写 provider（老配置）时按接口地址认出来；认不出就是自定义。"""
    if provider in PROVIDERS:
        return provider
    host = (base_url or "").lower()
    for key, preset in PROVIDERS.items():
        url = preset["base_url"].lower()
        if url and url.split("/")[2] in host:
            return key
    return "custom"


def provider_options() -> list[dict]:
    """给配置页的下拉框用（不含任何 Key）。"""
    return [
        {"key": key, "name": p["name"], "protocol": p["protocol"], "base_url": p["base_url"],
         "models": list(p["models"]), "key_hint": p["key_hint"]}
        for key, p in PROVIDERS.items()
    ]
