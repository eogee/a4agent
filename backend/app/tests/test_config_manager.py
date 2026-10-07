import json
from types import SimpleNamespace

import pytest
import yaml

from backend.app import config_manager


def _provider(api_type="anthropic", api_base="https://api.example.com"):
    return SimpleNamespace(api_type=api_type, api_base=api_base)


def test_build_settings_preserves_existing_keys():
    """合并式生成：hooks / permissions / 其他 env 等用户已有键原样保留。"""
    existing = {
        "model": "old-model",
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "a4p hook claude"}]}
            ]
        },
        "permissions": {"allow": ["Bash(echo *)"]},
        "env": {
            "CUSTOM_VAR": "keep-me",
            "ANTHROPIC_BASE_URL": "https://old.example.com",
        },
        "alwaysThinkingEnabled": True,
    }
    out = config_manager.build_settings(
        existing, _provider(), "sk-123", "new-model"
    )

    assert out["model"] == "new-model"
    assert out["hooks"] == existing["hooks"]
    assert out["permissions"] == existing["permissions"]
    assert out["env"]["CUSTOM_VAR"] == "keep-me"
    assert out["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-123"
    assert out["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert out["alwaysThinkingEnabled"] is False


def test_build_settings_without_existing():
    """无现有配置时生成全新的三键内容。"""
    out = config_manager.build_settings(None, _provider(), "sk-123", "m")
    assert out == {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": "sk-123",
            "ANTHROPIC_BASE_URL": "https://api.example.com",
        },
        "model": "m",
        "alwaysThinkingEnabled": False,
    }


def test_build_settings_openai_requires_proxy():
    """OpenAI 类型服务商未提供本地代理时应报错。"""
    with pytest.raises(ValueError, match="本地翻译代理"):
        config_manager.build_settings(None, _provider("openai"), "sk-123", "m")


def test_build_settings_openai_with_proxy():
    """OpenAI 类型服务商写入指向本地代理的 env。"""
    out = config_manager.build_settings(
        None,
        _provider("openai"),
        "sk-123",
        "m",
        proxy={"base_url": "http://127.0.0.1:17890", "token": "t"},
    )
    assert out["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:17890"
    assert out["env"]["ANTHROPIC_AUTH_TOKEN"] == "t"


def test_atomic_write_settings(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "settings.json"
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(target))
    data = {"model": "test-model", "env": {"KEY": "value"}}
    config_manager.atomic_write_settings(data)
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == data
    leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


# ---------------- dsh（DeepSeek Harness） ----------------


def test_target_list_only_claude_and_codex():
    """v0.5.2 起只代管 claude / codex；旧数据里的三端值被静默丢弃。"""
    assert config_manager.target_list("claude,codex") == ["claude", "codex"]
    # 旧库里的 dsh/zcode/pi 不再是合法目标，全部剔除后回退 claude
    assert config_manager.target_list("claude,dsh,codex") == ["claude", "codex"]
    assert config_manager.target_list("dsh") == ["claude"]
    assert config_manager.target_list("zcode,pi") == ["claude"]
    # 无效值同样回退，保证调用方永远拿到非空列表
    assert config_manager.target_list("") == ["claude"]
    assert config_manager.target_list(None) == ["claude"]


def test_build_dsh_settings_merges_and_rtrips_baseurl():
    """合并式生成：保留 ui-onboarding 等其它段；baseURL 去掉尾部斜杠。"""
    existing = {"ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"}}
    out = config_manager.build_dsh_settings(
        existing, _provider("openai", "https://api.example.com/v1/"), "deepseek-v4-flash"
    )
    assert out["ui-onboarding"] == existing["ui-onboarding"]
    assert out["llm-deepseek"]["baseURL"] == "https://api.example.com/v1"
    assert out["llm-deepseek"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
    assert out["llm-deepseek"]["maxTokens"] == 131072
    assert out["agent-default-model"] == {
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
    }


def test_build_dsh_settings_without_existing():
    out = config_manager.build_dsh_settings(None, _provider("openai"), "m1")
    assert out["llm-deepseek"]["baseURL"] == "https://api.example.com"
    assert out["llm-deepseek"]["maxTokens"] == 131072
    assert out["agent-default-model"]["model"] == "m1"


def test_build_dsh_settings_preserves_existing_max_tokens():
    """未显式填写 max_tokens 时，保留用户已手动设置过的 maxTokens。"""
    existing = {"llm-deepseek": {"maxTokens": 8192}}
    out = config_manager.build_dsh_settings(existing, _provider("openai"), "m1")
    assert out["llm-deepseek"]["maxTokens"] == 8192


def test_build_dsh_settings_resets_stale_adapter_default_max_tokens():
    """回归：既有 maxTokens 恰为 dsh 适配器默认 256000 时视为历史遗留，
    未显式填写时回落安全默认——避免旧值跟随切换带到限制更严的上游
    （实测 Console Go 限 [1,131072]）被打回 400 [1210]。"""
    existing = {"llm-deepseek": {"maxTokens": 256000}}
    out = config_manager.build_dsh_settings(existing, _provider("openai"), "m1")
    assert out["llm-deepseek"]["maxTokens"] == config_manager.DSH_DEFAULT_MAX_TOKENS


def test_build_dsh_settings_explicit_max_tokens_overrides():
    """a4agent 显式填写的 max_tokens 优先于既有手动值。"""
    existing = {"llm-deepseek": {"maxTokens": 8192}}
    out = config_manager.build_dsh_settings(
        existing, _provider("openai"), "m1", max_tokens=100000
    )
    assert out["llm-deepseek"]["maxTokens"] == 100000


def test_build_dsh_credentials_preserves_other_refs():
    """保留 refs 下其它凭证键；DEEPSEEK_API_KEY 写入 refs（version-1 布局）。"""
    existing = {"version": 1, "refs": {"OTHER_SECRET": "keep"}}
    out = config_manager.build_dsh_credentials(existing, "sk-123")
    assert out == {
        "version": 1,
        "refs": {"OTHER_SECRET": "keep", "DEEPSEEK_API_KEY": "sk-123"},
    }


def test_build_dsh_credentials_migrates_legacy_flat_layout():
    """回归：旧版扁平文档的顶层凭证键并入 refs。

    dsh 顶层只认 version/refs/records，未知顶层键（如误写到顶层的
    DEEPSEEK_API_KEY）会让 dsh 启动时抛 unknown top-level key 拒绝引导。
    """
    existing = {
        "version": 1,
        "refs": {"LLAMA_CPP_API_KEY": "1"},
        "OTHER_SECRET": "keep",
        "EMPTY": "",
    }
    out = config_manager.build_dsh_credentials(existing, "sk-123")
    assert out == {
        "version": 1,
        "refs": {
            "LLAMA_CPP_API_KEY": "1",
            "OTHER_SECRET": "keep",
            "DEEPSEEK_API_KEY": "sk-123",
        },
    }


def test_build_dsh_credentials_preserves_records_section():
    """records 段（如 OAuth 记录）存在时原样保留。"""
    records = {"oauth/deepseek": {"kind": "grant", "payload": {"access_token": "t"}}}
    existing = {"version": 1, "refs": {}, "records": records}
    out = config_manager.build_dsh_credentials(existing, "sk-123")
    assert out["records"] == records


def test_build_dsh_settings_uses_proxy_base_url():
    """回归：dsh 经本地翻译代理连接上游时，baseURL 应指向代理透传端点。"""
    existing = {"ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"}}
    proxy = {"base_url": "http://127.0.0.1:17890", "token": "proxy-token", "port": 17890}
    out = config_manager.build_dsh_settings(
        existing, _provider("openai", "https://api.example.com/v1/"), "deepseek-v4-flash",
        proxy=proxy,
    )
    assert out["ui-onboarding"] == existing["ui-onboarding"]
    assert out["llm-deepseek"]["baseURL"] == "http://127.0.0.1:17890"
    assert out["llm-deepseek"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
    assert out["llm-deepseek"]["maxTokens"] == 131072
    assert out["agent-default-model"] == {
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
    }


def test_build_dsh_credentials_prefers_proxy_token():
    """回归：dsh 走代理时凭证里应写代理 token 而非真实上游 key。"""
    out = config_manager.build_dsh_credentials(None, "sk-real-key", proxy_token="proxy-token")
    assert out == {"version": 1, "refs": {"DEEPSEEK_API_KEY": "proxy-token"}}


def test_atomic_write_dsh_settings(tmp_path, monkeypatch):
    target = tmp_path / "settings.yaml"
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(target))
    data = {
        "llm-deepseek": {"baseURL": "https://x.example.com"},
        "ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"},
    }
    config_manager.atomic_write_dsh_settings(data)
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == data
    leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


def test_atomic_write_dsh_credentials(tmp_path, monkeypatch):
    target = tmp_path / ".credentials.yaml"
    monkeypatch.setenv("A4AGENT_DSH_CREDENTIALS_PATH", str(target))
    data = {"DEEPSEEK_API_KEY": "sk-123"}
    config_manager.atomic_write_dsh_credentials(data)
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == data
    leftovers = [p for p in target.parent.iterdir() if p.name != target.name]
    assert leftovers == []


def test_read_dsh_selection(tmp_path, monkeypatch):
    target = tmp_path / "settings.yaml"
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(target))
    assert config_manager.read_dsh_selection() == (None, None)
    config_manager.atomic_write_dsh_settings(
        {
            "agent-default-model": {
                "provider": "deepseek-official",
                "model": "deepseek-v4-pro",
            }
        }
    )
    assert config_manager.read_dsh_selection() == ("deepseek-v4-pro", "deepseek-official")

def test_build_codex_settings_cleans_legacy_prefix():
    """改名前 a4api_p* 托管条目在切换时被一并清理，用户手工条目不动。"""
    provider = SimpleNamespace(
        id=7, name="P", api_type="anthropic", api_base="https://api.example.com"
    )
    existing = {
        "model_providers": {
            "a4api_p3": {"name": "旧托管", "base_url": "https://old"},
            "manual": {"name": "手工", "base_url": "https://keep"},
        }
    }
    out = config_manager.build_codex_settings(existing, provider, "sk-123", "m1")
    assert "a4api_p3" not in out["model_providers"]
    assert "a4a_p7" in out["model_providers"]
    assert "manual" in out["model_providers"]
    assert out["model_provider"] == "a4a_p7"


def test_build_zcode_settings_cleans_legacy_prefix():
    """zcode cli/v2 中的 a4api_p* 遗留托管条目同样在切换时清理。"""
    provider = SimpleNamespace(
        id=8, name="P", api_type="anthropic", api_base="https://api.example.com"
    )
    cli_existing = {"provider": {"a4api_p1": {"name": "旧托管"}}}
    v2_existing = {"provider": {"a4api_p1": {"name": "旧托管", "models": {}}}}
    cli, v2 = config_manager.build_zcode_settings(
        cli_existing, v2_existing, provider, "sk-123", "m1"
    )
    assert "a4api_p1" not in cli["provider"]
    assert "a4api_p1" not in v2["provider"]
    assert f"a4a_p{provider.id}" in cli["provider"]
    assert cli["model"] == f"a4a_p{provider.id}/m1"


def test_env_first_prefers_new_name(monkeypatch):
    """环境变量新旧双认：新名优先，缺省回退旧名。"""
    from backend.app.env_compat import env_first

    monkeypatch.delenv("A4AGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("A4API_DATA_DIR", raising=False)
    assert env_first("A4AGENT_DATA_DIR", "A4API_DATA_DIR") is None
    monkeypatch.setenv("A4API_DATA_DIR", "legacy")
    assert env_first("A4AGENT_DATA_DIR", "A4API_DATA_DIR") == "legacy"
    monkeypatch.setenv("A4AGENT_DATA_DIR", "modern")
    assert env_first("A4AGENT_DATA_DIR", "A4API_DATA_DIR") == "modern"


# ---------------- pi 的 contextWindow 提示 ----------------


def _pi_provider(pid=3, api_type="openai", api_base="http://127.0.0.1:8080/v1", native=False):
    return SimpleNamespace(
        id=pid, name=f"p{pid}", api_type=api_type, api_base=api_base, native_responses=native
    )


def test_pi_model_entry_gets_context_window_when_new():
    """新写出的模型条目带上本地服务的实际窗口，不再是只靠 pi 兜底 128k。"""
    models, settings = config_manager.build_pi_settings(
        {}, {}, _pi_provider(), "none", "qwen-local", context_window=262144
    )
    assert models["providers"]["a4a_p3"]["models"] == [
        {"id": "qwen-local", "contextWindow": 262144}
    ]


def test_pi_model_entry_refreshes_own_and_empty_only():
    """窗口刷新只发生在本工具托管条目与缺窗口的条目上，用户手工设定的值不覆盖。"""
    manual = {"providers": {"manual": {"models": [{"id": "m-x", "contextWindow": 65536, "cost": {"input": 1}}]}}}
    models, _ = config_manager.build_pi_settings(
        manual, {}, _pi_provider(), "none", "m-x", context_window=131072
    )
    entry = models["providers"]["a4a_p3"]["models"][0]
    assert entry["contextWindow"] == 65536  # 手工值优先
    assert entry["cost"] == {"input": 1}

    # 本工具自己写的托管条目会随 -c 刷新
    ours = {"providers": {"a4a_p99": {"models": [{"id": "m-x", "contextWindow": 32768}]}}}
    models2, _ = config_manager.build_pi_settings(
        ours, {}, _pi_provider(), "none", "m-x", context_window=131072
    )
    assert models2["providers"]["a4a_p3"]["models"][0]["contextWindow"] == 131072

    # 有定义但没写窗口的条目被补上
    partial = {"providers": {"manual": {"models": [{"id": "m-y", "name": "Y"}]}}}
    models3, _ = config_manager.build_pi_settings(
        partial, {}, _pi_provider(), "none", "m-y", context_window=262144
    )
    assert models3["providers"]["a4a_p3"]["models"][0]["contextWindow"] == 262144


def test_pi_model_entry_unchanged_without_hint():
    """远程服务商不给窗口提示：保持原有最小条目写法。"""
    models, _ = config_manager.build_pi_settings({}, {}, _pi_provider(), "k", "glm-4")
    assert models["providers"]["a4a_p3"]["models"] == [{"id": "glm-4"}]


def test_qoder_is_not_an_api_target():
    """Qoder 自带完整供应商配置界面，无需 a4agent 代管。

    注意判定依据不是「配置读不到」（那是技术障碍，不是产品理由），
    而是「用户自己能配」——这是 v0.5.2 收敛判定标准的基准用例。
    """
    assert config_manager.target_list("claude,qoder") == ["claude"]
    assert "qoder" not in config_manager.target_list("qoder")


def test_qoder_skills_root_follows_home_env(tmp_path, monkeypatch):
    """Qoder 全局 skill 根默认为 <配置目录>/skills，且可被 A4AGENT_QODER_SKILLS_PATH 覆盖。"""
    for name in ("A4AGENT_QODER_HOME", "A4AGENT_QODER_SKILLS_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("A4AGENT_QODER_HOME", str(tmp_path / "qoder"))
    assert config_manager.qoder_skills_root() == tmp_path / "qoder" / "skills"
    monkeypatch.setenv("A4AGENT_QODER_SKILLS_PATH", str(tmp_path / "elsewhere"))
    assert config_manager.qoder_skills_root() == tmp_path / "elsewhere"
