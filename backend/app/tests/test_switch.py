"""切换接口的目标应用与协议约束测试。

v0.5.1 起只覆盖 Claude Code 与 Codex 两个目标——dsh / ZCode / pi 已改为
在应用内自配，不再由本工具写入，它们的残留清理由 test_removal.py 覆盖。
"""
import json

import pytest
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
    """把 settings / codex 配置 / 备份目录 / 模型目录全部指到临时目录。"""
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("A4AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("A4AGENT_CODEX_CONFIG_PATH", str(tmp_path / "config.toml"))
    monkeypatch.setenv("A4AGENT_CODEX_CATALOG_PATH", str(tmp_path / "models.json"))


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
