"""「启动浏览器」遇到端口被无头 Chrome 占着时，要说清楚，不能静默当成「已经开着」。"""
from __future__ import annotations

import io
import json

import jobpilot.browser.launcher as launcher


def _fake_version(ua: str):
    def fake(url, timeout=0):
        return io.BytesIO(json.dumps({"User-Agent": ua}).encode())
    return fake


def test_headless_squatter_is_reported_not_treated_as_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: True)
    monkeypatch.setattr("urllib.request.urlopen", _fake_version("Mozilla/5.0 HeadlessChrome/154.0.0.0"))
    res = launcher.launch("http://127.0.0.1:9222", tmp_path / "p")
    assert res["ok"] is False and res["occupied"] is True
    assert "9222" in res["error"] and "无头" in res["error"]


def test_real_chrome_on_port_is_already_open(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: True)
    monkeypatch.setattr("urllib.request.urlopen", _fake_version("Mozilla/5.0 Chrome/154.0.0.0 Safari/537.36"))
    res = launcher.launch("http://127.0.0.1:9222", tmp_path / "p")
    assert res == {"ok": True, "already": True, "profile": str(tmp_path / "p")}


def test_unreachable_version_endpoint_is_not_headless(monkeypatch):
    def boom(url, timeout=0):
        raise OSError("refused")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert launcher.is_headless("127.0.0.1", 9222) is False
