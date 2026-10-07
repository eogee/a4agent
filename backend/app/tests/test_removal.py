"""移除托管配置（v0.6.0）的测试。

重点验证三条红线：
1. 只删 a4a_p* 与指向本工具代理的 llm-deepseek 段，绝不误伤用户自有配置
2. 每个动作前先快照，出错即中止，不留半完成状态
3. 判定依据不足时不动（宁可留下也不误删）
"""
import json

import pytest

from backend.app import config_manager, removal


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """把三端的配置路径全部指向临时目录，绝不碰用户真实配置。"""
    monkeypatch.setenv("A4AGENT_DSH_SETTINGS_PATH", str(tmp_path / "dsh.yaml"))
    monkeypatch.setenv("A4AGENT_DSH_CREDENTIALS_PATH", str(tmp_path / "dsh_cred.yaml"))
    monkeypatch.setenv("A4AGENT_ZCODE_CLI_CONFIG_PATH", str(tmp_path / "zcli.json"))
    monkeypatch.setenv("A4AGENT_ZCODE_V2_CONFIG_PATH", str(tmp_path / "zv2.json"))
    monkeypatch.setenv("A4AGENT_PI_MODELS_PATH", str(tmp_path / "pmodels.json"))
    monkeypatch.setenv("A4AGENT_PI_SETTINGS_PATH", str(tmp_path / "psettings.json"))
    # 清掉上一轮测试可能残留的 dsh home 覆盖
    monkeypatch.delenv("A4AGENT_ZCODE_HOME", raising=False)
    return tmp_path


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_removes_managed_provider_and_clears_dangling_model(fake_env, monkeypatch):
    """删 a4a_p* 条目；model 指向它时一并清空，避免留下悬空引用。"""
    _write(fake_env / "zcli.json", {
        "provider": {
            "a4a_p3": {"name": "DS", "kind": "openai-compatible", "options": {}},
            "user-provider": {"name": "Mine"},
        },
        "model": "a4a_p3/deepseek-v4-flash",
        "hooks": {"keep": True},
    })
    _write(fake_env / "zv2.json", {
        "provider": {"a4a_p3": {"name": "DS"}, "mine": {"name": "Mine"}}
    })
    report = removal._clean_zcode(dry_run=False)

    assert "cli:a4a_p3" in report["removed"]
    assert "v2:a4a_p3" in report["removed"]

    cli = config_manager.read_zcode_cli_config()
    assert "a4a_p3" not in cli["provider"]
    # 用户的条目与 hooks 必须原样保留
    assert "user-provider" in cli["provider"]
    assert cli["hooks"] == {"keep": True}
    assert cli["model"] == ""
    assert "model" in " ".join(report["cleared_fields"])


def test_keeps_model_when_pointing_to_user_provider(fake_env):
    """model 指向用户自己的 provider 时不能清空——那是用户的正常选择。"""
    _write(fake_env / "zcli.json", {
        "provider": {"a4a_p3": {"name": "DS"}, "mine": {"name": "Mine"}},
        "model": "mine/gpt",
    })
    _write(fake_env / "zv2.json", {})
    removal._clean_zcode(dry_run=False)
    assert config_manager.read_zcode_cli_config()["model"] == "mine/gpt"


def test_pi_clears_default_provider_only_when_managed(fake_env):
    """defaultProvider 指向托管条目才清；指向用户自己的（如本地 strata）不动。"""
    _write(fake_env / "pmodels.json", {
        "providers": {
            "a4a_p3": {"name": "DS", "baseUrl": "https://x"},
            "strata": {"name": "S", "baseUrl": "http://localhost:8080"},
        },
        "theme": "dark",
    })
    _write(fake_env / "psettings.json", {
        "defaultProvider": "a4a_p3", "defaultModel": "deepseek-v4-flash",
        "enabledSkills": ["a"],
    })
    report = removal._clean_pi(dry_run=False)
    assert "models:a4a_p3" in report["removed"]

    settings = config_manager.read_pi_settings()
    assert settings["defaultProvider"] == ""
    assert settings["defaultModel"] == ""
    assert settings["enabledSkills"] == ["a"]

    models = config_manager.read_pi_models_config()
    assert "strata" in models["providers"]
    assert models["theme"] == "dark"


def test_pi_keeps_user_default_provider(fake_env):
    """用户默认指向自己的 strata（实测本机就是这个情况）时不能动。"""
    _write(fake_env / "pmodels.json", {
        "providers": {"a4a_p3": {"baseUrl": "https://x"}, "strata": {"baseUrl": "http://s"}},
    })
    _write(fake_env / "psettings.json", {"defaultProvider": "strata", "defaultModel": "qwen3.8"})
    removal._clean_pi(dry_run=False)
    settings = config_manager.read_pi_settings()
    assert settings["defaultProvider"] == "strata"
    assert settings["defaultModel"] == "qwen3.8"


def test_legacy_prefix_is_also_removed(fake_env):
    """v0.3.x 及更早用 a4api_p* 前缀，同样要清。"""
    _write(fake_env / "pmodels.json", {"providers": {"a4api_p2": {"baseUrl": "x"}}})
    report = removal._clean_pi(dry_run=False)
    assert "models:a4api_p2" in report["removed"]
    assert config_manager.read_pi_models_config()["providers"] == {}


def test_dsh_removes_llm_deepseek_only_when_pointing_at_proxy(fake_env):
    """baseURL 指向 17890 才删该段，且绝不能碰 llm-pi-ai。"""
    _write(fake_env / "dsh.yaml", {
        "llm-deepseek": {"baseURL": "http://127.0.0.1:17890", "maxTokens": 131072},
        "llm-pi-ai": {"providers": {"my-gateway": {"baseUrl": "https://gw.example/v1"}}},
        "agent-default-model": {"provider": "deepseek-official", "model": "dots3"},
        "ui-onboarding": {"welcomeNoticeVersion": "2026-08-13.1"},
    })
    report = removal._clean_dsh(dry_run=True)
    assert any(config_manager.DSH_LLM_NS in r for r in report["removed"])

    removal._clean_dsh(dry_run=False)
    data = config_manager.read_dsh_settings()
    assert config_manager.DSH_LLM_NS not in data
    # 用户界面配的供应商、onboarding 段必须原样保留
    assert data["llm-pi-ai"]["providers"]["my-gateway"]["baseUrl"] == "https://gw.example/v1"
    assert data["ui-onboarding"]["welcomeNoticeVersion"] == "2026-08-13.1"
    # 指向该段的默认模型也要清，否则留下悬空引用
    assert data["agent-default-model"]["provider"] == ""


def test_dsh_skips_when_baseurl_is_not_our_proxy(fake_env):
    """判定依据不足时不动：段名相同但指向别处，可能是用户自己配的。"""
    _write(fake_env / "dsh.yaml", {
        "llm-deepseek": {"baseURL": "https://my-gateway.example/v1"},
        "llm-pi-ai": {"providers": {"x": {"baseUrl": "https://x"}}},
    })
    report = removal._clean_dsh(dry_run=False)
    assert any("判定为用户自有配置" in x for x in report["skipped"])
    assert config_manager.DSH_LLM_NS in config_manager.read_dsh_settings()


def test_dsh_skips_when_no_section(fake_env):
    """没有该段时如实报告跳过，不报错。"""
    _write(fake_env / "dsh.yaml", {"llm-pi-ai": {"providers": {"x": {}}}})
    report = removal._clean_dsh(dry_run=False)
    assert any("未发现" in x for x in report["skipped"])


def test_dry_run_never_writes(fake_env):
    """dry-run 只报告不写盘——这是用户确认前的安全保证。"""
    _write(fake_env / "pmodels.json", {"providers": {"a4a_p3": {"baseUrl": "x"}}})
    _write(fake_env / "psettings.json", {"defaultProvider": "a4a_p3"})
    removal._clean_pi(dry_run=True)
    assert "a4a_p3" in config_manager.read_pi_models_config()["providers"]
    assert config_manager.read_pi_settings()["defaultProvider"] == "a4a_p3"


def test_cleanup_all_aborts_when_snapshot_fails(fake_env, monkeypatch):
    """快照失败必须中止：没有回滚依据就不要动用户文件。"""
    _write(fake_env / "pmodels.json", {"providers": {"a4a_p3": {"baseUrl": "x"}}})
    monkeypatch.setattr(
        "backend.app.removal.removal_backup.take_snapshot",
        lambda dry_run=False: (_ for _ in ()).throw(OSError("磁盘只读")),
    )
    report = removal.cleanup_all(dry_run=False)
    assert any("快照失败" in e for e in report["errors"])
    # 文件必须原样未动
    assert "a4a_p3" in config_manager.read_pi_models_config()["providers"]


def test_needs_cleanup_detects_managed_entries(fake_env):
    assert removal.needs_cleanup() is False
    _write(fake_env / "pmodels.json", {"providers": {"a4a_p3": {"baseUrl": "x"}}})
    assert removal.needs_cleanup() is True


def test_needs_cleanup_ignores_user_only_config(fake_env):
    """只有用户自己配的条目时不该提示需要清理。"""
    _write(fake_env / "pmodels.json", {"providers": {"strata": {"baseUrl": "http://s"}}})
    _write(fake_env / "zcli.json", {"provider": {"mine": {"name": "M"}}})
    _write(fake_env / "zv2.json", {"provider": {"mine": {"name": "M"}}})
    assert removal.needs_cleanup() is False

# ---------------- dsh 双落点（settings.yaml 与 settings.yaml.imported） ----------------


def _write_imported(tmp_path, data):
    """写出 settings.yaml.imported——实测存在的第二种落点。"""
    (tmp_path / "dsh.yaml.imported").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_dsh_cleans_imported_fallback(fake_env):
    """只有 .imported 落点时也要清理，否则留下指向已停代理的断链。"""
    (fake_env / "dsh.yaml").write_text("{}", encoding="utf-8")
    _write_imported(fake_env, {
        "llm-deepseek": {"baseURL": "http://127.0.0.1:17890", "maxTokens": 131072},
        "llm-pi-ai": {"providers": {"mine": {"baseUrl": "https://gw/v1"}}},
    })
    report = removal._clean_dsh(dry_run=True)
    assert any(r.startswith("dsh.yaml.imported:") for r in report["removed"]), report["removed"]

    removal._clean_dsh(dry_run=False)
    data = config_manager.read_doc(fake_env / "dsh.yaml.imported", "yaml")
    assert config_manager.DSH_LLM_NS not in data
    # 用户的 llm-pi-ai 依然完好
    assert "mine" in data["llm-pi-ai"]["providers"]


def test_dsh_cleans_both_candidates(fake_env):
    """两个落点同时存在且都指向代理时，两处都要清。"""
    for name in ("dsh.yaml", "dsh.yaml.imported"):
        (fake_env / name).write_text(
            json.dumps({"llm-deepseek": {"baseURL": "http://127.0.0.1:17890"}}),
            encoding="utf-8",
        )
    removal._clean_dsh(dry_run=False)
    for name in ("dsh.yaml", "dsh.yaml.imported"):
        data = config_manager.read_doc(fake_env / name, "yaml")
        assert config_manager.DSH_LLM_NS not in data, f"{name} 未清理"


def test_dsh_imported_not_ours_is_skipped(fake_env):
    """判定依据不足时 .imported 也不动——宁可留，不可误删。"""
    _write_imported(fake_env, {"llm-deepseek": {"baseURL": "https://my-gateway.example/v1"}})
    report = removal._clean_dsh(dry_run=False)
    assert any("imported" in s and "不做改动" in s for s in report["skipped"])
    data = config_manager.read_doc(fake_env / "dsh.yaml.imported", "yaml")
    assert config_manager.DSH_LLM_NS in data


def test_needs_cleanup_sees_imported(fake_env):
    """检测也要覆盖 .imported，否则界面不会提示用户需要清理。"""
    assert removal.needs_cleanup() is False
    _write_imported(fake_env, {"llm-deepseek": {"baseURL": "http://127.0.0.1:17890"}})
    assert removal.needs_cleanup() is True


def test_snapshot_covers_imported(fake_env):
    """快照必须覆盖 .imported——清理会改它，少备一个就没有回滚依据。"""
    from backend.app import removal_backup

    _write_imported(fake_env, {"llm-deepseek": {"baseURL": "http://x"}})
    snap = removal_backup.take_snapshot()
    assert snap["files"].get("dsh-settings-imported") is not None
