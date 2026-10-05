"""「接入配置方案」接口测试：多端目标选择、既有方案目标跟随、本地方案的可用 Key。"""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import schemas
from backend.app.api.v1 import llama as llama_api
from backend.app.api.v1 import switch as switch_api
from backend.app.crypto import decrypt_text
from backend.app.database import Base
from backend.app.models import Configuration, Provider


class _FakeRuntime:
    """llama 运行时桩：/integrate 要 cfg 与 engine_present，switch 要 served_context_for。

    llama_api.rt 与 switch_api.llama_runtime 是同一个模块对象，桩只装一次。
    """

    def __init__(self, cfg, served_context=None):
        self.cfg = cfg
        self.served_context = served_context

    def engine_present(self):
        return True

    def served_context_for(self, api_base, model):
        return self.served_context


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("A4AGENT_PI_AGENT_DIR", str(tmp_path / "pi-agent"))
    model = tmp_path / "qwen-coder.Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    cfg = SimpleNamespace(
        port=8080, host="127.0.0.1", default_model_path=str(model),
        infer=SimpleNamespace(api_key="", context_tokens=262144),
    )
    monkeypatch.setattr(
        llama_api.rt, "runtime", lambda: _FakeRuntime(cfg, served_context=262144)
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'integrate.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield SimpleNamespace(db=db, model=model.stem, cfg=cfg, tmp=tmp_path)
    db.close()


def _integrate(db, targets):
    return llama_api.integrate(
        body=llama_api.IntegrateBody(targets=targets, activate=False), db=db
    )


def test_integrate_creates_plan_with_selected_targets(ctx):
    """勾选多端时，新建的配置方案带上完整目标串。"""
    result = _integrate(ctx.db, "claude,pi")
    cfg = ctx.db.get(Configuration, result["config_id"])
    assert cfg.targets == "claude,pi"
    assert cfg.model == ctx.model
    provider = ctx.db.get(Provider, result["provider_id"])
    assert provider.api_base == "http://127.0.0.1:8080/v1"
    assert provider.api_type == "openai"


def test_integrate_updates_targets_of_existing_plan(ctx):
    """重复接入时，既有方案的勾选跟随更新（本方案由接入功能托管）。"""
    first = _integrate(ctx.db, "claude")
    second = _integrate(ctx.db, "pi,zcode")
    assert second["config_id"] == first["config_id"]
    assert second["created"] is False
    cfg = ctx.db.get(Configuration, second["config_id"])
    assert cfg.targets == "pi,zcode"
    assert ctx.db.query(Configuration).count() == 1


def test_integrate_normalizes_unknown_target_names(ctx):
    """未知端名被丢弃；全部非法时回落为 claude，不写入废目标。"""
    result = _integrate(ctx.db, "gpt4all,,claude")
    assert ctx.db.get(Configuration, result["config_id"]).targets == "claude"
    result2 = _integrate(ctx.db, "nonsense")
    assert ctx.db.get(Configuration, result2["config_id"]).targets == "claude"


def test_integrate_writes_usable_api_key(ctx):
    """本地方案不能是空 Key：无鉴权写占位 none，开了 --api-key 则沿用服务密钥。"""
    result = _integrate(ctx.db, "claude")
    cfg = ctx.db.get(Configuration, result["config_id"])
    assert decrypt_text(cfg.api_key_encrypted) == "none"

    ctx.cfg.infer.api_key = "local-secret"
    result2 = _integrate(ctx.db, "pi")
    assert decrypt_text(ctx.db.get(Configuration, result2["config_id"]).api_key_encrypted) == "local-secret"


def test_integrate_heals_legacy_empty_key(ctx):
    """早期接入留下的空 Key 方案，再次点「接入」时被补上，切换不再失败。"""
    result = _integrate(ctx.db, "claude")
    cfg = ctx.db.get(Configuration, result["config_id"])
    cfg.api_key_encrypted = ""
    ctx.db.commit()
    _integrate(ctx.db, "claude")
    assert decrypt_text(ctx.db.get(Configuration, result["config_id"]).api_key_encrypted) == "none"


def test_local_plan_switches_to_pi_with_context_window(ctx):
    """本地模型 → pi：切换成功，pi 条目带上 none Key 与服务实际 -c 的窗口。"""
    result = _integrate(ctx.db, "pi")
    out = switch_api.switch_config(
        result["config_id"], schemas.SwitchRequest(restart=False), ctx.db
    )
    assert out.success is True
    models = json.loads(
        (ctx.tmp / "pi-agent" / "models.json").read_text(encoding="utf-8")
    )
    entry = models["providers"][f"a4a_p{result['provider_id']}"]
    assert entry["api"] == "openai-completions"
    assert entry["apiKey"] == "none"
    assert entry["models"] == [{"id": ctx.model, "contextWindow": 262144}]
    settings = json.loads(
        (ctx.tmp / "pi-agent" / "settings.json").read_text(encoding="utf-8")
    )
    assert settings["defaultModel"] == ctx.model
