"""三件事：任务日志给人看 · 岗位池确认不按平台误跳过 · 多服务商模型适配。"""
from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from jobpilot.ai import llm as llm_module
from jobpilot.ai.llm import AnthropicClient, OpenAiCompatClient, build_llm
from jobpilot.ai.providers import PROVIDERS, resolve_provider
from jobpilot.config import AiCfg, AppConfig
from jobpilot.models import Job, Status
from jobpilot.services import confirmer
from jobpilot.tasks import humanize, problem_of, run_problem, summarize

# ---------- 1. 任务日志可读 ----------

def test_humanize_replaces_machine_tags():
    assert humanize("[boss] 上海/AI 第 1 页 20 条") == "BOSS直聘：上海/AI 第 1 页 20 条"
    assert humanize("[safety] 放慢节奏").startswith("风控：")
    assert humanize("[51job] 这次只用：城市 上海").startswith("前程无忧：")
    assert humanize("没有前缀的话原样") == "没有前缀的话原样"


def test_collect_overrides_are_logged_in_words(repo):
    from jobpilot.services.collector import collect

    class Ctx:
        config = AppConfig()
        logs: list[str] = []

        def log(self, message):
            self.logs.append(message)

        def create_browser(self):
            raise RuntimeError("stop here")

    ctx = Ctx()
    ctx.config.platforms.setdefault("boss", ctx.config.platforms.get("boss"))
    try:
        collect(ctx, "boss", registry=_registry_with_collect(), cities=["上海"], max_pages=3)
    except RuntimeError:
        pass
    line = ctx.logs[0]
    assert "{" not in line and "'" not in line
    assert "城市 上海" in line and "翻 3 页" in line


def _registry_with_collect():
    class Driver:
        key = "boss"
        capabilities = frozenset({"collect"})

    return {"boss": Driver()}


def test_completed_task_with_error_is_reported_as_not_done():
    result = {"platform": "boss", "error": "[boss] 搜索页 上海/AI 未找到预期内容（可能改版或被拦截）",
              "blocked": "page_failures", "locked_until": "2026-10-12 21:56:18"}
    text = problem_of(result)
    assert text.startswith("BOSS直聘：搜索页")
    assert "10-12 21:56" in text
    # 历史记录：旧行 error 为空、原因在 result 里，也要能读出来
    assert run_problem({"error": "", "result": json.dumps(result, ensure_ascii=False)}) == text
    assert problem_of({"steps": {"collect": result}}) == text


def test_summary_is_words_not_dicts():
    assert summarize({"new": 20, "duplicate": 40, "filtered": 0}) == "新增 20 个岗位，40 个已在库里"
    assert summarize({"steps": {"score": {"scored": 6, "prefiltered": 1}}}) == "评分 6 个，1 个预筛淘汰"


# ---------- 2. 确认门 ----------

def _job(job_id, platform, status):
    return Job(id=job_id, platform=platform, external_id=job_id, title="t" + job_id,
               company="c" + job_id, status=status)


def test_confirm_by_ids_ignores_default_platform(repo):
    """岗位池全平台混排：请求带默认平台 boss，勾的是 51job 的待确认岗位，也要确认上。"""
    repo.upsert_job(_job("51job-1", "51job", Status.PENDING_CONFIRM.value))
    repo.upsert_job(_job("51job-2", "51job", Status.NEW.value))
    repo.upsert_job(_job("boss-1", "boss", Status.FILTERED.value))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.repo = repo
    res = confirmer.approve(ctx, "boss", job_ids=["51job-1", "51job-2", "boss-1", "nope"])
    assert res["confirmed"] == 1
    assert repo.get_job("51job-1").status == Status.APPROVED.value
    assert res["skipped"] == {"new": 1, "filtered": 1, "missing": 1}
    # 新采到、没评分的岗位不能直接确认（确认门只认「待确认」）
    assert repo.get_job("51job-2").status == Status.NEW.value


def test_confirm_without_ids_still_scoped_to_platform(repo):
    repo.upsert_job(_job("51job-1", "51job", Status.PENDING_CONFIRM.value))
    repo.upsert_job(_job("boss-1", "boss", Status.PENDING_CONFIRM.value))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.repo = repo
    assert confirmer.approve(ctx, "boss")["job_ids"] == ["boss-1"]


# ---------- 3. 多服务商 ----------

def test_old_configs_are_recognised_by_base_url():
    assert resolve_provider("", "https://api.deepseek.com/v1") == "deepseek"
    assert resolve_provider("", "https://ark.cn-beijing.volces.com/api/v3") == "doubao"
    assert resolve_provider("", "http://localhost:11434/v1") == "custom"
    assert resolve_provider("anthropic", "") == "anthropic"


def test_every_preset_is_well_formed():
    for key, preset in PROVIDERS.items():
        assert preset["protocol"] in {"openai", "anthropic"}, key
        if key != "custom":
            assert preset["base_url"].startswith("https://") and preset["models"], key


def test_build_llm_picks_protocol():
    assert isinstance(build_llm(AiCfg(provider="anthropic", base_url="https://api.anthropic.com/v1")), AnthropicClient)
    assert isinstance(build_llm(AiCfg(provider="qwen")), OpenAiCompatClient)
    assert isinstance(build_llm(AiCfg()), OpenAiCompatClient)    # 默认 DeepSeek，老配置不受影响


def _capture(monkeypatch, responses):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": dict(json)})
        status, body = responses.pop(0)
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(llm_module.httpx, "post", fake_post)
    monkeypatch.setattr(llm_module.time, "sleep", lambda s: None)
    return calls


def test_anthropic_client_speaks_messages_api(monkeypatch):
    calls = _capture(monkeypatch, [(200, {
        "model": "claude-haiku-5-5",
        "content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": '{"score": 80}'}],
        "usage": {"input_tokens": 12, "output_tokens": 5},
    })])
    cfg = AiCfg(provider="anthropic", base_url="https://api.anthropic.com/v1", model="claude-haiku-5-5", api_key="k")
    result = AnthropicClient(cfg).complete(system="sys", user="u", schema={"type": "object"})
    assert result.ok and result.data == {"score": 80}
    assert (result.tokens_in, result.tokens_out) == (12, 5)
    sent = calls[0]
    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert sent["headers"]["x-api-key"] == "k" and "anthropic-version" in sent["headers"]
    assert "Authorization" not in sent["headers"]
    body = sent["json"]
    assert body["system"].startswith("sys") and body["messages"] == [{"role": "user", "content": "u"}]
    assert body["max_tokens"] > 0 and "temperature" not in body   # 新模型拒收自定义 temperature


def test_openai_compat_retries_without_temperature(monkeypatch):
    calls = _capture(monkeypatch, [
        (400, {"error": {"message": "Unsupported value: 'temperature' does not support 0.2"}}),
        (200, {"model": "gpt-x", "choices": [{"message": {"content": "hi"}}], "usage": {}}),
    ])
    result = OpenAiCompatClient(AiCfg(api_key="k", model="gpt-x")).complete(system="s", user="u")
    assert result.ok and result.text == "hi"
    assert "temperature" in calls[0]["json"] and "temperature" not in calls[1]["json"]


def test_http_errors_carry_the_providers_reason(monkeypatch):
    _capture(monkeypatch, [(404, {"error": {"message": "Model Not Exist"}})])
    result = OpenAiCompatClient(AiCfg(api_key="k")).complete(system="s", user="u")
    assert result.error == "HTTP 404：Model Not Exist" and result.error_kind == "request"


def test_config_api_exposes_providers_and_test_endpoint(tmp_path, monkeypatch):
    from jobpilot import config as cfgmod
    from jobpilot.web import app as web

    path = tmp_path / "config.yaml"
    cfgmod.save_config(AppConfig(), path)
    monkeypatch.setattr(cfgmod, "CONFIG_PATH", path)
    monkeypatch.setattr(web, "CONFIG_PATH", path)
    client = TestClient(web.create_app(lambda: (_ for _ in ()).throw(AssertionError("不应该用到 ctx"))), base_url="http://127.0.0.1:8700")
    data = client.get("/api/config").json()
    assert data["ai"]["provider"] == "deepseek"
    assert {p["key"] for p in data["_providers"]} >= {"deepseek", "anthropic", "qwen", "doubao"}
    assert all("api_key" not in p for p in data["_providers"])

    seen = {}

    class Stub:
        def complete(self, **kwargs):
            seen["kwargs"] = kwargs
            return llm_module.LlmResult(text="{}", data={}, model="claude-haiku-5-5", latency_ms=300)

    def fake_build(cfg):
        seen["cfg"] = cfg
        return Stub()

    monkeypatch.setattr(web, "build_llm", fake_build)
    res = client.post("/api/ai/test", json={"provider": "anthropic", "model": "claude-haiku-5-5",
                                             "base_url": "https://api.anthropic.com/v1", "api_key": "sk-a****bcde"})
    assert res.json()["ok"] is True
    assert seen["cfg"].provider == "anthropic" and seen["cfg"].api_key == ""   # 掩码不当真 Key 用
    assert cfgmod.load_config(path).ai.provider == ""                           # 测试连接不保存


# ---------- 任务历史 / 风控事件：库里的原始记录翻成人话 ----------

def test_run_summary_uses_words_for_real_records():
    from jobpilot.readable import run_failed, run_summary

    window = {"platform": "boss", "new": 0, "seen": 0, "stopped":
              "当前不在允许开页面的时段（safety.active_windows）；半夜还在刷招聘本身就是机器特征，要改去设置 → 风控"}
    text = run_summary("collect", window)
    assert "safety." not in text and "设置 → 风控" in text and run_failed(window)
    cooling = {"platform": "boss", "new": 0, "stopped": "boss 处于风险冷却：连续 3 次页面异常（约 12 分钟）"}
    assert run_summary("collect", cooling) == "BOSS直聘 正在冷却：连续 3 次页面异常（约 12 分钟）"
    assert run_summary("score", {"platform": "boss", "scored": 0, "total": 0}) == "没有待评分的岗位"
    assert run_summary("collect", {"new": 20, "seen": 60, "duplicate": 40}) == "新增 20 个岗位，40 个已在库里"
    assert not run_failed({"new": 20, "seen": 60})
    assert run_summary("full", None) == "任务被停止"


def test_event_text_for_real_records():
    from jobpilot.readable import event_text

    assert event_text("collect", "51job new=20 seen=60") == "前程无忧 看到 60 个岗位，新增 20 个"
    assert "没有打开新的搜索页" in event_text("collect", "boss new=0 seen=0")
    assert event_text("risk_lock", "boss/page_failures · 连续 3 次页面异常 · 62 分钟") \
        == "BOSS直聘 暂停 62 分钟：页面连续读不到内容（连续 3 次页面异常）"
    text = event_text("page_error", "boss 第 3 次：[boss] 搜索页 上海/AI 未找到预期内容（可能改版或被拦截）")
    assert text == "BOSS直聘 第 3 次页面异常：搜索页 上海/AI 未找到预期内容（可能改版或被拦截）"
    for raw in ("boss new=0", "boss/page_failures", "[boss]"):
        assert raw not in text


# ---------- 浏览器没开 ----------

def test_cdp_refused_is_explained_in_words():
    from jobpilot.readable import run_summary

    raw = ("Error: BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:9222 Call log: "
           "- <ws preparing> retrieving websocket url from http://127.0.0.1:9222")
    text = run_summary("collect", None, raw)
    assert "connect_over_cdp" not in text and "ECONNREFUSED" not in text
    assert "启动浏览器" in text


def test_launcher_does_nothing_when_port_already_open(monkeypatch, tmp_path):
    from jobpilot.browser import launcher

    monkeypatch.setattr(launcher, "port_open", lambda host, port, timeout=0.6: True)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该再开一个")))
    assert launcher.launch("http://127.0.0.1:9222", tmp_path / "p") == {"ok": True, "already": True, "profile": str(tmp_path / "p")}


def test_launcher_starts_chrome_with_debug_port_and_own_profile(monkeypatch, tmp_path):
    from jobpilot.browser import launcher

    calls, state = [], {"open": False}
    monkeypatch.setattr(launcher, "port_open", lambda host, port, timeout=0.6: state["open"])
    monkeypatch.setattr(launcher, "find_chrome", lambda: r"C:\chrome.exe")

    def fake_popen(args, **kwargs):
        calls.append(args)
        state["open"] = True

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    res = launcher.launch("http://127.0.0.1:9333", tmp_path / "profile")
    assert res["ok"] and res["already"] is False
    assert "--remote-debugging-port=9333" in calls[0]
    assert f"--user-data-dir={tmp_path / 'profile'}" in calls[0]


def test_launcher_refuses_remote_hosts(tmp_path):
    from jobpilot.browser import launcher

    assert launcher.launch("http://10.0.0.5:9222", tmp_path)["ok"] is False
