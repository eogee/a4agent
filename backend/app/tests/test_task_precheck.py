"""下发前预检测试：三类服务商分支、端配置第二跳与引擎失败的短路。"""
import json
import urllib.error

import pytest

from backend.app import config_manager, task_engines, task_precheck
from types import SimpleNamespace


def _provider(api_type="anthropic", api_base="https://api.example.com"):
    return SimpleNamespace(name="prov", api_type=api_type, api_base=api_base)


@pytest.fixture()
def calls(monkeypatch):
    """桩掉网络与代理探活，记录每次请求。"""
    log = {"http": [], "proxy": True}

    def fake_http(method, url, headers, body=None):
        log["http"].append({"method": method, "url": url, "headers": headers, "body": body})
        return log.get("status", 200), log.get("text", "")

    monkeypatch.setattr(task_precheck, "_http", fake_http)
    monkeypatch.setattr(task_precheck, "_proxy_alive", lambda port: log["proxy"])
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\pi.cmd")
    monkeypatch.setattr(task_engines, "_probe_cache", {})
    monkeypatch.setattr(
        task_engines.subprocess, "run",
        lambda argv, **kw: type("R", (), {"returncode": 0, "stdout": "1.0.2", "stderr": ""})(),
    )
    return log


# ---------------- 服务商连通 ----------------


def test_anthropic_uses_minimal_messages_request(calls):
    result = task_precheck.check_service(_provider("anthropic"), "sk-1", "deepseek-chat")
    assert result["ok"] is True
    req = calls["http"][0]
    assert req["url"] == "https://api.example.com/v1/messages"
    assert req["method"] == "POST"
    assert req["body"]["max_tokens"] == 1
    assert req["body"]["model"] == "deepseek-chat"
    assert req["headers"]["x-api-key"] == "sk-1"


def test_openai_uses_models_endpoint(calls):
    result = task_precheck.check_service(_provider("openai", "https://api.deepseek.com/"), "sk-1")
    assert result["ok"] is True
    assert calls["http"][0]["url"] == "https://api.deepseek.com/models"
    assert calls["http"][0]["headers"]["Authorization"] == "Bearer sk-1"


def test_http_401_is_reported_as_key_problem(calls):
    calls["status"] = 401
    bad = task_precheck.check_service(_provider("openai"), "sk-1")
    assert bad["ok"] is False
    assert "密钥无效" in bad["detail"]


def test_models_404_falls_back_to_chat_completion(calls):
    """不支持 /models 的 OpenAI 兼容服务：退一次最小 chat 请求再判定。"""
    seq = iter([(404, "no route"), (200, "{}")])
    calls.clear()
    calls["http"] = []

    def http(method, url, headers, body=None):
        calls["http"].append({"method": method, "url": url})
        return next(seq)

    import backend.app.task_precheck as tp

    original = tp._http
    tp._http = http
    try:
        result = tp.check_service(_provider("openai"), "sk-1")
    finally:
        tp._http = original
    assert result["ok"] is True
    assert [c["url"] for c in calls["http"]] == [
        "https://api.example.com/models",
        "https://api.example.com/chat/completions",
    ]


def test_network_error_is_unreachable(calls):
    def raise_url_error(method, url, headers, body=None):
        raise urllib.error.URLError("connection refused")

    import backend.app.task_precheck as tp

    original = tp._http
    tp._http = raise_url_error
    try:
        result = tp.check_service(_provider("anthropic"), "sk-1")
    finally:
        tp._http = original
    assert result["ok"] is False
    assert "暂不可达" in result["detail"]


def test_local_llama_requires_no_key_and_reports_startup(calls):
    """本地模型服务商：探 /v1/models，失败文案是「本地推理服务未启动」。"""
    base = "http://127.0.0.1:8080/v1"
    assert task_precheck.check_service(_provider("openai", base), "none")["ok"] is True

    def boom(method, url, headers, body=None):
        raise urllib.error.URLError("connection refused")

    import backend.app.task_precheck as tp

    original = tp._http
    tp._http = boom
    try:
        result = tp.check_service(_provider("openai", base), "none")
    finally:
        tp._http = original
    assert result["ok"] is False
    assert "本地推理服务未启动" in result["detail"]


def test_proxy_port_reports_proxy_not_running(calls):
    base = "http://127.0.0.1:17893/v1"
    calls["proxy"] = True
    assert task_precheck.check_service(_provider("openai", base), "sk")["ok"] is True
    calls["proxy"] = False
    bad = task_precheck.check_service(_provider("openai", base), "sk")
    assert bad["ok"] is False and "翻译代理" in bad["detail"]


# ---------------- 端配置第二跳 ----------------


@pytest.fixture()
def pi_env(tmp_path, monkeypatch):
    agent = tmp_path / "pi-agent"
    agent.mkdir()
    monkeypatch.setenv("A4AGENT_PI_AGENT_DIR", str(agent))
    monkeypatch.delenv("A4AGENT_PI_MODELS_PATH", raising=False)
    monkeypatch.delenv("A4AGENT_PI_SETTINGS_PATH", raising=False)
    return agent


def _write_pi(agent, models, settings):
    (agent / "models.json").write_text(json.dumps(models, ensure_ascii=False), encoding="utf-8")
    (agent / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")


def test_pi_missing_selection_blocks_dispatch(pi_env):
    _write_pi(pi_env, {"providers": {}}, {})
    result = task_precheck.check_engine_config("pi", _provider(api_base="http://127.0.0.1:8080/v1"), "m")
    assert result["ok"] is False and "尚未指向" in result["detail"]


def test_pi_base_mismatch_blocks_dispatch(pi_env):
    _write_pi(
        pi_env,
        {"providers": {"strata": {"baseUrl": "http://127.0.0.1:9/v1", "models": [{"id": "m"}]}}},
        {"defaultProvider": "strata", "defaultModel": "m"},
    )
    result = task_precheck.check_engine_config("pi", _provider("openai", "https://api.example.com"), "m")
    assert result["ok"] is False and "不一致" in result["detail"]


def test_pi_model_absent_blocks_dispatch(pi_env):
    _write_pi(
        pi_env,
        {"providers": {"a4a_p1": {"baseUrl": "https://api.example.com", "models": [{"id": "other"}]}}},
        {"defaultProvider": "a4a_p1", "defaultModel": "other"},
    )
    result = task_precheck.check_engine_config("pi", _provider("openai", "https://api.example.com"), "deepseek-chat")
    assert result["ok"] is False and "没有模型" in result["detail"]


def test_pi_aligned_config_passes(pi_env):
    _write_pi(
        pi_env,
        {"providers": {"a4a_p1": {"baseUrl": "https://api.example.com/v1/", "models": [{"id": "deepseek-chat"}]}}},
        {"defaultProvider": "a4a_p1", "defaultModel": "deepseek-chat"},
    )
    ok = task_precheck.check_engine_config("pi", _provider("openai", "https://api.example.com/v1"), "deepseek-chat")
    assert ok["ok"] is True


def test_dsh_without_config_blocks_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(tmp_path / "missing.yaml"))
    result = task_precheck.check_engine_config("dsh", _provider("openai"), "m")
    assert result["ok"] is False and "尚未写入" in result["detail"]


def test_dsh_proxy_down_blocks_dispatch(tmp_path, monkeypatch, calls):
    settings = tmp_path / "settings.yaml"
    settings.write_text("llm-deepseek:\n  baseURL: http://127.0.0.1:17890/v1\n", encoding="utf-8")
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(settings))
    calls["proxy"] = False
    result = task_precheck.check_engine_config("dsh", _provider("openai"), "m")
    assert result["ok"] is False and "未运行" in result["detail"]

    calls["proxy"] = True
    assert task_precheck.check_engine_config("dsh", _provider("openai"), "m")["ok"] is True


# ---------------- 三步编排 ----------------


def test_run_reports_all_three_steps(calls):
    # anthropic 型：连通请求体里必须带生效方案的真实模型名（openai 型走 GET /models 无 body）
    result = task_precheck.run(None, "pi", _provider("anthropic"), "sk-1", "deepseek-chat")
    assert [s["name"] for s in result["steps"]] == ["engine", "reachability", "config_sync"]
    assert result["ok"] is False  # 默认没有 pi 端配置文件，第三跳会拦下
    assert result["reason"] == result["steps"][-1]["detail"]
    assert calls["http"][0]["body"]["model"] == "deepseek-chat"


def test_run_short_circuits_after_engine_failure(monkeypatch, calls):
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: None)
    monkeypatch.setattr(task_engines, "_probe_cache", {})
    result = task_precheck.run(None, "pi", _provider("openai"), "sk-1", "m")
    assert [s["name"] for s in result["steps"]] == ["engine"]
    assert result["ok"] is False and "未探测到" in result["reason"]
