"""切换接口的目标应用与协议约束测试。"""
import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import config_manager, crud, schemas
from backend.app.api.v1 import switch
from backend.app.crypto import encrypt_text
from backend.app.database import Base
from backend.app.models import Configuration, Provider

_PROXY = {"base_url": "http://127.0.0.1:17890", "token": "proxy-token"}


def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'switch_test.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed(db, *, api_type="anthropic", native_responses=False, targets="claude,codex"):
    p = Provider(
        name=f"provider-{api_type}",
        api_base="https://api.example.com",
        api_type=api_type,
        native_responses=native_responses,
        is_custom=True,
    )
    db.add(p)
    db.flush()
    c = Configuration(
        name="双目标方案",
        provider_id=p.id,
        api_key_encrypted=encrypt_text("sk-test-123"),
        model="test-model",
        targets=targets,
    )
    db.add(c)
    db.commit()
    return c


def _isolate_paths(tmp_path, monkeypatch):
    """把 settings / codex 配置 / dsh / zcode / pi / 备份目录 / 模型目录全部指到临时目录。"""
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("A4AGENT_CODEX_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("A4AGENT_CODEX_CATALOG_PATH", str(tmp_path / "models.json"))
    monkeypatch.setenv("A4AGENT_ZCODE_CLI_CONFIG_PATH", str(tmp_path / "zcode-cli.json"))
    monkeypatch.setenv(
        "A4AGENT_ZCODE_V2_CONFIG_PATH", str(tmp_path / "zcode-v2.json")
    )
    monkeypatch.setenv("A4AGENT_PI_AGENT_DIR", str(tmp_path / "pi-agent"))
    # pi 的 contextWindow 提示取自 llama 运行时单例；默认桩为「非本地服务」，
    # 需要本地模型场景的用例在 _isolate_paths 之后再覆盖它
    monkeypatch.setattr(
        switch.llama_runtime,
        "runtime",
        lambda: SimpleNamespace(served_context_for=lambda api_base, model: None),
    )


def test_anthropic_provider_with_both_targets_fails_before_any_write(tmp_path, monkeypatch):
    """Anthropic 原生服务商 + 同时勾选 Claude/Codex：应在任何写入前干净失败。

    回归：原先 Codex 协议校验在写 settings.json 之后才执行，导致 Claude 配置
    已被覆盖、配置被标为生效，但界面却报“切换失败”——半生效的误导状态。
    """
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="anthropic")

    with pytest.raises(HTTPException) as exc:
        switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)

    assert exc.value.status_code == 500
    assert "Codex 需要 OpenAI 兼容" in str(exc.value.detail)
    # 未标记生效、未写任何配置文件
    assert crud.get_active_config(db) is None
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "config.toml").exists()
    db.close()


@pytest.mark.parametrize("native_responses", [False, True])
def test_openai_provider_with_both_targets_writes_both_configs(
    tmp_path, monkeypatch, native_responses
):
    """OpenAI 兼容服务商 + 同时勾选 Claude/Codex：两份配置都写入、方案标记生效。

    非原生 Responses 时 Codex 与 Claude 共用本地代理；原生时 Codex 直连上游。
    """
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        switch.proxy_standalone, "ensure_proxy_running", lambda: dict(_PROXY)
    )
    monkeypatch.setattr(switch, "is_claude_running", lambda: True)

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", native_responses=native_responses)

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)

    assert result.success is True
    assert crud.get_active_config(db) is not None

    # Claude Code settings.json：OpenAI 服务商一律经本地代理
    settings = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert settings["model"] == "test-model"
    assert settings["env"]["ANTHROPIC_BASE_URL"] == _PROXY["base_url"]
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == _PROXY["token"]

    # Codex config.toml：模型与 a4agent 托管的服务商条目均正确
    codex = config_manager.read_codex_settings()
    assert codex["model"] == "test-model"
    provider_key = f"a4a_p{cfg.provider_id}"
    assert codex["model_provider"] == provider_key
    entry = codex["model_providers"][provider_key]
    assert entry["wire_api"] == "responses"
    if native_responses:
        assert entry["base_url"] == "https://api.example.com"
        assert entry["experimental_bearer_token"] == "sk-test-123"
    else:
        assert entry["base_url"] == _PROXY["base_url"]
        assert entry["experimental_bearer_token"] == _PROXY["token"]
    db.close()


def test_claude_switch_preserves_existing_hooks(tmp_path, monkeypatch):
    """切换不抹掉 settings.json 里已有的 hooks / permissions / 其他 env。"""
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        switch.proxy_standalone, "ensure_proxy_running", lambda: dict(_PROXY)
    )
    monkeypatch.setattr(switch, "is_claude_running", lambda: True)

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": "stale",
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "a4p hook claude"}]}
                    ]
                },
                "permissions": {"allow": ["Bash(echo *)"]},
                "env": {"CUSTOM_VAR": "keep"},
            }
        ),
        encoding="utf-8",
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", native_responses=True, targets="claude")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["model"] == "test-model"
    assert settings["hooks"]["Stop"][0]["hooks"][0]["command"] == "a4p hook claude"
    assert settings["permissions"]["allow"] == ["Bash(echo *)"]
    assert settings["env"]["CUSTOM_VAR"] == "keep"
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == _PROXY["token"]
    assert settings["env"]["ANTHROPIC_BASE_URL"] == _PROXY["base_url"]
    db.close()


def test_dsh_target_writes_proxy_base_url_and_token(tmp_path, monkeypatch):
    """回归：dsh 目标经本地翻译代理透传（baseURL 指向代理、凭证写代理 token），
    而非直连上游——规避上游流式分片 null 字段导致 dsh 工具名被覆盖。"""
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(tmp_path / "settings.yaml"))
    monkeypatch.setenv("A4AGENT_DSH_CREDENTIALS_PATH", str(tmp_path / "credentials.yaml"))
    monkeypatch.setattr(
        switch.proxy_standalone, "ensure_proxy_running", lambda: dict(_PROXY)
    )
    monkeypatch.setattr(switch, "is_claude_running", lambda: True)

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="dsh")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    assert crud.get_active_config(db) is not None

    dsh = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
    assert dsh["llm-deepseek"]["baseURL"] == _PROXY["base_url"]
    assert dsh["llm-deepseek"]["apiKeyEnv"] == "DEEPSEEK_API_KEY"
    assert dsh["agent-default-model"]["provider"] == "deepseek-official"
    assert dsh["agent-default-model"]["model"] == "test-model"

    creds = yaml.safe_load((tmp_path / "credentials.yaml").read_text(encoding="utf-8"))
    # dsh 要求 version-1 布局：凭证必须嵌在 refs 下，顶层出现未知键会让 dsh 拒绝启动
    assert creds == {"version": 1, "refs": {"DEEPSEEK_API_KEY": _PROXY["token"]}}
    db.close()


# ---------------- zcode 目标 ----------------

_ZCODE_V2_PREEXISTING = {
    "provider": {
        "builtin:bigmodel": {
            "name": "Bigmodel - API Key",
            "kind": "anthropic",
            "options": {"apiKey": "", "baseURL": "https://open.bigmodel.cn/api/anthropic"},
            "enabled": False,
            "source": "custom",
            "models": {},
        }
    }
}


@pytest.mark.parametrize("api_type", ["anthropic", "openai"])
def test_zcode_target_writes_both_configs_duplex_direct(
    tmp_path, monkeypatch, api_type
):
    """zcode 目标：CLI 与桌面端两份配置都写入，直连上游、不经本地翻译代理。

    anthropic 服务商 → kind=anthropic；openai 服务商 → kind=openai-compatible；
    model 格式为 "<a4a_p<id>>/<model>"，与 zcode 现行格式一致。
    """
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(switch, "is_claude_running", lambda: True)
    # 该方案同时含 claude 目标：openai 服务商下 Claude Code 需本地代理，mock 独立代理进程
    monkeypatch.setattr(
        switch.proxy_standalone, "ensure_proxy_running", lambda: dict(_PROXY)
    )
    # 预置 v2 配置，验证合并式写入：其它 provider 原样保留、hooks 不被抹掉
    (tmp_path / "zcode-v2.json").write_text(
        json.dumps(_ZCODE_V2_PREEXISTING), encoding="utf-8"
    )
    (tmp_path / "zcode-cli.json").write_text(
        json.dumps({"hooks": {"enabled": True}, "model": "stale"}), encoding="utf-8"
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type=api_type, targets="claude,zcode")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    assert result.zcode_backup_path is not None
    assert crud.get_active_config(db) is not None

    provider_key = f"a4a_p{cfg.provider_id}"
    cli = json.loads((tmp_path / "zcode-cli.json").read_text(encoding="utf-8"))
    assert cli["model"] == f"{provider_key}/test-model"
    entry = cli["provider"][provider_key]
    assert entry["name"] == f"provider-{api_type}"
    assert entry["kind"] == ("anthropic" if api_type == "anthropic" else "openai-compatible")
    assert entry["options"]["apiKey"] == "sk-test-123"
    assert entry["options"]["baseURL"] == "https://api.example.com"
    # hooks 保留
    assert cli["hooks"]["enabled"] is True

    v2 = json.loads((tmp_path / "zcode-v2.json").read_text(encoding="utf-8"))
    # 其它 provider 原样保留，本工具旧托管条目被整体替换
    assert "builtin:bigmodel" in v2["provider"]
    v2_entry = v2["provider"][provider_key]
    assert v2_entry["kind"] == entry["kind"]
    assert v2_entry["source"] == "custom"
    assert "test-model" in v2_entry["models"]
    # 直连上游：代理没有被启动（不依赖本地翻译代理进程）
    assert entry["options"]["baseURL"].startswith("https://api.example.com")
    db.close()


def test_zcode_target_does_not_require_openai_provider(tmp_path, monkeypatch):
    """zcode 与 Codex/dsh 不同：Anthropic 服务商也可选 zcode 目标，无需 OpenAI。"""
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="anthropic", targets="zcode")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    cli = json.loads((tmp_path / "zcode-cli.json").read_text(encoding="utf-8"))
    assert cli["provider"][f"a4a_p{cfg.provider_id}"]["kind"] == "anthropic"
    db.close()


def test_zcode_switch_preserves_user_providers_and_replaces_managed(tmp_path, monkeypatch):
    """切换只替换 a4agent 托管条目（a4a_p*），用户手工添加的 provider 不动。"""
    _isolate_paths(tmp_path, monkeypatch)
    (tmp_path / "zcode-v2.json").write_text(
        json.dumps(
            {
                "provider": {
                    "my-custom": {
                        "name": "手工添加",
                        "kind": "openai-compatible",
                        "options": {"apiKey": "keep", "baseURL": "https://keep.example"},
                        "source": "custom",
                        "models": {},
                    },
                    "a4a_p999": {
                        "name": "旧托管",
                        "kind": "anthropic",
                        "options": {"apiKey": "stale", "baseURL": "https://stale"},
                        "source": "custom",
                        "models": {},
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="zcode")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    v2 = json.loads((tmp_path / "zcode-v2.json").read_text(encoding="utf-8"))
    assert v2["provider"]["my-custom"]["options"]["apiKey"] == "keep"
    assert "a4a_p999" not in v2["provider"]
    assert f"a4a_p{cfg.provider_id}" in v2["provider"]
    db.close()


def test_zcode_model_entry_reuses_existing_model_capabilities(tmp_path, monkeypatch):
    """v2 配置中已存在同名模型时复制其能力元数据，而非重建默认。"""
    _isolate_paths(tmp_path, monkeypatch)
    (tmp_path / "zcode-v2.json").write_text(
        json.dumps(
            {
                "provider": {
                    "builtin:bigmodel": {
                        "name": "Bigmodel",
                        "kind": "anthropic",
                        "options": {"apiKey": "", "baseURL": "https://bigmodel"},
                        "source": "custom",
                        "models": {
                            "test-model": {
                                "reasoning": {"enabled": True, "variants": ["low", "max"]},
                                "limit": {"context": 1000000, "output": 128000},
                                "modalities": {"input": ["text", "image"], "output": ["text"]},
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="anthropic", targets="zcode")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    v2 = json.loads((tmp_path / "zcode-v2.json").read_text(encoding="utf-8"))
    entry = v2["provider"][f"a4a_p{cfg.provider_id}"]["models"]["test-model"]
    assert entry["limit"]["context"] == 1000000
    assert entry["modalities"]["input"] == ["text", "image"]
    db.close()


# ---------------- pi（本地 pi coding agent）----------------


def _pi_files(tmp_path):
    models = json.loads((tmp_path / "pi-agent" / "models.json").read_text(encoding="utf-8"))
    settings = json.loads((tmp_path / "pi-agent" / "settings.json").read_text(encoding="utf-8"))
    return models, settings


def test_pi_switch_writes_managed_provider_and_default(tmp_path, monkeypatch):
    """pi 端切换：models.json 写 a4a_p<id> 托管 provider，settings.json 切默认模型。"""
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="anthropic", targets="pi")

    result = switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    assert result.success is True
    models, settings = _pi_files(tmp_path)
    entry = models["providers"][f"a4a_p{cfg.provider_id}"]
    assert entry["api"] == "anthropic-messages"
    assert entry["baseUrl"] == "https://api.example.com"
    assert entry["apiKey"] == "sk-test-123"
    assert entry["models"] == [{"id": "test-model"}]
    assert settings["defaultProvider"] == f"a4a_p{cfg.provider_id}"
    assert settings["defaultModel"] == "test-model"
    assert "pi 配置已写入" in result.message
    db.close()


def test_pi_api_kind_follows_provider_protocol(tmp_path, monkeypatch):
    """openai → openai-completions；原生 Responses 的上游 → openai-responses。"""
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    models, _ = _pi_files(tmp_path)
    assert models["providers"][f"a4a_p{cfg.provider_id}"]["api"] == "openai-completions"

    cfg.provider.native_responses = True
    db.commit()
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    models, _ = _pi_files(tmp_path)
    assert models["providers"][f"a4a_p{cfg.provider_id}"]["api"] == "openai-responses"
    db.close()


def test_pi_switch_preserves_user_providers_and_other_settings(tmp_path, monkeypatch):
    """只替换本工具托管条目，用户手工 provider 与 settings 其它键（theme 等）保留。"""
    _isolate_paths(tmp_path, monkeypatch)
    agent = tmp_path / "pi-agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "my-selfhost": {
                        "name": "Strata Local",
                        "baseUrl": "http://127.0.0.1:8080/v1",
                        "api": "openai-completions",
                        "apiKey": "none",
                        "compat": {"supportsDeveloperRole": False},
                        "models": [{"id": "qwen-local", "contextWindow": 262144}],
                    },
                    "a4a_p999": {"name": "旧托管", "baseUrl": "https://stale", "api": "openai-completions", "apiKey": "stale", "models": []},
                }
            }
        ),
        encoding="utf-8",
    )
    (agent / "settings.json").write_text(
        json.dumps({"theme": "dark", "defaultProvider": "my-selfhost", "defaultModel": "qwen-local"}),
        encoding="utf-8",
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)

    models, settings = _pi_files(tmp_path)
    assert models["providers"]["my-selfhost"]["compat"]["supportsDeveloperRole"] is False
    assert "a4a_p999" not in models["providers"]
    assert f"a4a_p{cfg.provider_id}" in models["providers"]
    assert settings["theme"] == "dark"
    db.close()


def test_pi_switch_reuses_existing_model_metadata(tmp_path, monkeypatch):
    """pi models.json 里已有同名模型定义时复用其元数据，不丢上下文长度等信息。"""
    _isolate_paths(tmp_path, monkeypatch)
    agent = tmp_path / "pi-agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "local": {
                        "name": "Local",
                        "baseUrl": "http://127.0.0.1:8080/v1",
                        "api": "openai-completions",
                        "models": [{"id": "test-model", "contextWindow": 262144, "reasoning": True}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    models, _ = _pi_files(tmp_path)
    entry = models["providers"][f"a4a_p{cfg.provider_id}"]["models"][0]
    assert entry["contextWindow"] == 262144
    assert entry["reasoning"] is True
    db.close()


def test_pi_selection_shows_in_status(tmp_path, monkeypatch):
    """状态接口回显 pi 当前生效的 provider 与 model。"""
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="anthropic", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)

    status = switch.get_status(db=db)
    assert status.pi_file_exists is True
    assert status.current_pi_provider == f"a4a_p{cfg.provider_id}"
    assert status.current_pi_model == "test-model"
    db.close()


def test_pi_switch_writes_local_llama_context(tmp_path, monkeypatch):
    """服务商是本机 llama-server 时，pi 的模型条目带上服务实际 -c 的窗口。"""
    _isolate_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        switch.llama_runtime,
        "runtime",
        lambda: SimpleNamespace(served_context_for=lambda api_base, model: 262144),
    )
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    models, _ = _pi_files(tmp_path)
    entry = models["providers"][f"a4a_p{cfg.provider_id}"]["models"][0]
    assert entry == {"id": "test-model", "contextWindow": 262144}
    db.close()


def test_pi_switch_keeps_minimal_entry_for_remote_provider(tmp_path, monkeypatch):
    """远程服务商不给窗口提示，条目仍是只写 id 的最小形态。"""
    _isolate_paths(tmp_path, monkeypatch)
    Session = _make_session(tmp_path)
    db = Session()
    cfg = _seed(db, api_type="openai", targets="pi")
    switch.switch_config(cfg.id, schemas.SwitchRequest(restart=False), db)
    models, _ = _pi_files(tmp_path)
    assert models["providers"][f"a4a_p{cfg.provider_id}"]["models"] == [{"id": "test-model"}]
    db.close()
