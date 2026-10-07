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


# ---------------- 配置就绪（读用户自己的配置，非托管条目） ----------------
#
# v0.5.1 的关键转变：这一跳不再比对 a4a_p* 托管条目，而是读用户自己在应用内
# 配的内容。因此测试基准也从「托管条目长什么样」变成「用户的任意配置都能认」。


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


def test_pi_user_config_is_ready_without_managed_prefix(pi_env):
    """用户自己起的 provider 名（非 a4a_p*）也必须被认——这是解耦的核心。"""
    _write_pi(
        pi_env,
        {"providers": {"strata": {"baseUrl": "http://127.0.0.1:9/v1", "models": [{"id": "qwen3.8"}]}}},
        {"defaultProvider": "strata", "defaultModel": "qwen3.8"},
    )
    ready, detail = task_precheck._read_user_config_signal("pi")
    assert ready is True
    assert "strata" in detail


def test_pi_missing_selection_blocks_dispatch(pi_env):
    _write_pi(pi_env, {"providers": {}}, {})
    ready, detail = task_precheck._read_user_config_signal("pi")
    assert ready is False and "models.json" in detail


def test_dsh_reads_official_namespace_only(tmp_path, monkeypatch):
    """dsh 只认界面配置的 llm-pi-ai；旧的 llm-deepseek 侧门不再算就绪。"""
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        json.dumps({"llm-pi-ai": {"providers": {"my-gateway": {"baseUrl": "https://gw/v1"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(settings))
    ready, detail = task_precheck._read_user_config_signal("dsh")
    assert ready is True and "1 个供应商" in detail


def test_dsh_legacy_sidepath_is_not_ready(tmp_path, monkeypatch):
    """只有 llm-deepseek 段时不算就绪——那是我们旧的侧门写入路径。"""
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        json.dumps({"llm-deepseek": {"baseURL": "http://127.0.0.1:17890"}}), encoding="utf-8"
    )
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(settings))
    ready, detail = task_precheck._read_user_config_signal("dsh")
    assert ready is False and "Add a custom provider" in detail


def test_dsh_without_config_blocks_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(tmp_path / "missing.yaml"))
    ready, detail = task_precheck._read_user_config_signal("dsh")
    assert ready is False and "Add a custom provider" in detail


def test_claude_and_codex_read_user_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_AUTH_TOKEN": "sk-1"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(settings))
    ready, _ = task_precheck._read_user_config_signal("claude")
    assert ready is True

    codex = tmp_path / "config.toml"
    codex.write_text('model = "m"\nmodel_provider = "p"\n', encoding="utf-8")
    monkeypatch.setenv("A4AGENT_CODEX_CONFIG_PATH", str(codex))
    ready, detail = task_precheck._read_user_config_signal("codex")
    assert ready is True and "m" in detail


def test_qoder_config_is_readable_false_but_honest():
    """Qoder 配置加密读不到：如实说「以实测为准」，不假装校验通过。"""
    ready, detail = task_precheck._read_user_config_signal("qoder")
    assert ready is False
    assert "无法读取" in detail and "实测" in detail


def test_zcode_reads_user_provider(tmp_path, monkeypatch):
    cli = tmp_path / "zc.json"
    cli.write_text(
        json.dumps({"provider": {"mine": {"name": "M"}}, "model": "mine/gpt"}), encoding="utf-8"
    )
    monkeypatch.setenv("A4AGENT_ZCODE_CLI_CONFIG_PATH", str(cli))
    ready, detail = task_precheck._read_user_config_signal("zcode")
    assert ready is True and "mine/gpt" in detail


# ---------------- 真实连通（smoke） ----------------


def test_smoke_run_passes_when_engine_answers(monkeypatch):
    """实测成功：真实跑一次拿到产出即算通过。"""
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\pi.cmd")
    monkeypatch.setattr(
        task_precheck.subprocess, "run",
        lambda argv, **kw: type("R", (), {
            "returncode": 0,
            "stdout": '{"type":"message_end","message":{"role":"assistant","stopReason":"stop","content":[{"type":"text","text":"ok"}]}}',
            "stderr": "",
        })(),
    )
    result = task_precheck.smoke_run("pi")
    assert result["ok"] is True
    assert "实测通过" in result["detail"]


def test_smoke_run_reports_failure_reason(monkeypatch):
    """实测失败时要把引擎的错误说清楚，而不是只报「不可用」。"""
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\pi.cmd")
    monkeypatch.setattr(
        task_precheck.subprocess, "run",
        lambda argv, **kw: type("R", (), {
            "returncode": 1, "stdout": "", "stderr": "上游 401 unauthorized",
        })(),
    )
    result = task_precheck.smoke_run("pi")
    assert result["ok"] is False
    assert "401" in result["detail"]


def test_smoke_run_timeout_is_reported(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\pi.cmd")

    def boom(argv, **kw):
        raise sp.TimeoutExpired(argv, 90)

    monkeypatch.setattr(task_precheck.subprocess, "run", boom)
    result = task_precheck.smoke_run("pi")
    assert result["ok"] is False and "超时" in result["detail"]


def test_smoke_run_missing_command_is_reported(monkeypatch):
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: None)
    result = task_precheck.smoke_run("zcode")
    assert result["ok"] is False and "未探测到" in result["detail"]


# ---------------- 三步编排 ----------------


def test_run_reports_all_three_steps(calls, monkeypatch, tmp_path):
    """三步顺序：引擎可用 → 配置就绪 → 真实连通（不再有服务商连通直连探测）。"""
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://x", "ANTHROPIC_AUTH_TOKEN": "sk"}}), encoding="utf-8",
    )
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(settings))
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\claude.cmd")
    monkeypatch.setattr(
        task_engines, "_probe_cache", {},
    )
    monkeypatch.setattr(
        task_precheck.subprocess, "run",
        lambda argv, **kw: type("R", (), {"returncode": 0, "stdout": '{"result":"ok"}', "stderr": ""})(),
    )
    result = task_precheck.run(None, "claude", _provider("anthropic"), "sk-1", "deepseek-chat")
    assert [s["name"] for s in result["steps"]] == ["engine", "config_sync", "reachability"]
    assert result["ok"] is True


def test_run_skips_smoke_when_config_not_ready(calls, monkeypatch, tmp_path):
    """配置都没配好时不再跑实测——跑必然失败，白白增加等待。"""
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\bin\claude.cmd")
    monkeypatch.setattr(task_engines, "_probe_cache", {})
    result = task_precheck.run(None, "claude", _provider("anthropic"), "sk-1", "m")
    assert [s["name"] for s in result["steps"]] == ["engine", "config_sync"]
    assert result["ok"] is False
    assert "ANTHROPIC_BASE_URL" in result["reason"]


def test_run_short_circuits_after_engine_failure(monkeypatch, calls):
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: None)
    monkeypatch.setattr(task_engines, "_probe_cache", {})
    result = task_precheck.run(None, "pi", _provider("openai"), "sk-1", "m")
    assert [s["name"] for s in result["steps"]] == ["engine"]
    assert result["ok"] is False and "未探测到" in result["reason"]
