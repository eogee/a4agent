"""预设推荐 / 引擎目录推荐 / 局域网地址 / 配置持久化 / 命令行拼装测试。"""
import json

import pytest

from backend.app.llama import catalog, presets
from backend.app.llama.config import (InferParams, LlamaConfig, ModelEntry,
                                      load_config, save_config)
from backend.app.llama.gpu import GpuInfo
from backend.app.llama.lan import _is_api_pa, _is_private, candidate_addresses
from backend.app.llama.server import (MTP_LEGACY, MTP_NONE, MTP_SPEC_TYPE,
                                      build_args)


# ───────────────────────── 预设 ─────────────────────────

@pytest.mark.parametrize("vram,gpu,label,ctx,kv", [
    (0, False, "CPU 兜底", 8192, "q4_0"),
    (1.5, True, "CPU 兜底", 8192, "q4_0"),
    (4, True, "入门档 (<6GB)", 8192, "q4_0"),
    (8, True, "主流档 (6–12GB)", 32768, "q4_0"),
    (14, True, "进阶档 (12–16GB)", 32768, "q4_0"),
    (22, True, "高端档 (16–24GB)", 65536, "q4_0"),
    (48, True, "旗舰档 (≥24GB)", 131072, "q8_0"),
])
def test_preset_recommend(vram, gpu, label, ctx, kv):
    p = presets.recommend(vram, gpu)
    assert p.label == label
    assert p.context_tokens == ctx
    assert p.kv_level == kv


def test_assess_risk_levels():
    from backend.app.llama.gguf import GgufModelInfo

    m = GgufModelInfo("x", int(20 * 1024 ** 3))
    m.ok = True

    # 20GB 权重 vs 22GB 显存：need≈20.9 → 20.9 > 22*0.88=19.36 → 余量偏小警告
    warn = presets.assess_risk(m, 0, "f16", 22.0, True)
    assert warn is not None and "余量偏小" in warn

    # 需求远小于显存 → 无警告
    m2 = GgufModelInfo("x", int(2 * 1024 ** 3))
    m2.ok = True
    assert presets.assess_risk(m2, 8192, "q4_0", 22.0, True) is None

    # 无 GPU / 解析失败 → 不评估
    assert presets.assess_risk(m, 0, "f16", 0, False) is None
    m3 = GgufModelInfo("x", 0)
    assert presets.assess_risk(m3, 0, "f16", 22.0, True) is None


# ───────────────────────── 引擎目录 ─────────────────────────

def _nvidia(driver):
    return GpuInfo("NVIDIA", "RTX Test", 22 * 1024 ** 3, driver)


@pytest.mark.parametrize("driver,expected", [
    ("591.86", "cuda134"),
    ("580.10", "cuda134"),
    ("560.35", "cuda124"),
    ("550.54", "cuda124"),
    ("546.33", "vulkan"),  # 驱动过旧降级
])
def test_engine_recommend_nvidia(driver, expected):
    assert catalog.recommend(_nvidia(driver)).pack_id == expected


def test_engine_recommend_non_nvidia():
    amd = GpuInfo("AMD", "Radeon Test", 16 * 1024 ** 3, "32.0.11021")
    assert catalog.recommend(amd).pack_id == "vulkan"
    assert catalog.recommend(None).pack_id == "cpu"


def test_engine_catalog_pinned_tag():
    assert catalog.REPO_OWNER == "ggml-org"
    assert catalog.REPO_NAME == "llama.cpp"
    assert catalog.find("vulkan").zips == ("llama-b11370-bin-win-vulkan-x64.zip",)
    url = catalog.pack_url(catalog.find("vulkan"), 0)
    assert url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/")


def test_engine_catalog_legacy_pack_id():
    # 旧配置里存的 cuda133 自动映射到改名后的 cuda134
    assert catalog.find("cuda133").pack_id == "cuda134"
    assert catalog.find("cuda134").pack_id == "cuda134"
    assert catalog.find("cuda124").pack_id == "cuda124"
    assert catalog.find("nope") is None


def test_size_label():
    assert catalog.find("vulkan").size_label.endswith("MB")
    assert catalog.find("cuda124").size_label.endswith("GB")


# ───────────────────────── 局域网地址 ─────────────────────────

def test_is_private_and_api_pa():
    assert _is_private("192.168.3.29")
    assert _is_private("10.0.0.1")
    assert _is_private("172.16.0.1")
    assert not _is_private("8.8.8.8")
    assert not _is_private("172.32.0.1")
    assert _is_api_pa("169.254.10.1")


def test_candidate_addresses_no_loopback():
    ips = candidate_addresses()
    assert "127.0.0.1" not in ips
    assert all(not ip.startswith("169.254.") for ip in ips)


# ───────────────────────── 配置持久化 ─────────────────────────

def test_config_roundtrip(tmp_path, monkeypatch):
    import backend.app.llama.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "config_path", lambda: tmp_path / "llama_config.json")
    cfg = LlamaConfig()
    cfg.wizard_done = True
    cfg.port = 9090
    cfg.host = "0.0.0.0"
    cfg.engine_pack_id = "vulkan"
    cfg.model_dirs = ["C:/models"]
    cfg.models = [ModelEntry(path="C:/models/a.gguf", alias="a").__dict__]
    cfg.default_model_path = "C:/models/a.gguf"
    cfg.infer.context_tokens = 65536
    cfg.infer.kv_level = "q8_0"
    cfg.run.trim_delay_seconds = 120
    save_config(cfg)

    loaded = load_config()
    assert loaded.wizard_done
    assert loaded.port == 9090
    assert loaded.host == "0.0.0.0"
    assert loaded.engine_pack_id == "vulkan"
    assert loaded.model_dirs == ["C:/models"]
    assert loaded.models[0]["alias"] == "a"
    assert loaded.infer.context_tokens == 65536
    assert loaded.infer.kv_level == "q8_0"
    assert loaded.run.trim_delay_seconds == 120


def test_config_corrupt_json_falls_back(tmp_path, monkeypatch):
    import backend.app.llama.config as cfg_mod

    p = tmp_path / "llama_config.json"
    p.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "config_path", lambda: p)
    cfg = load_config()
    assert cfg.port == 8080  # 全新默认
    assert not cfg.wizard_done


def test_config_file_is_valid_json(tmp_path, monkeypatch):
    import backend.app.llama.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "config_path", lambda: tmp_path / "c.json")
    cfg = LlamaConfig()
    save_config(cfg)
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert data["infer"]["context_tokens"] == 32768


# ───────────────────────── 命令行拼装 ─────────────────────────

def _infer(**kw):
    p = InferParams()
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_build_args_base():
    args = build_args(_infer(), "C:/m/model.gguf", "127.0.0.1", 8080,
                      include_mtp=False, mtp_style=MTP_NONE)
    assert args[args.index("-m") + 1] == "C:/m/model.gguf"
    assert args[args.index("--host") + 1] == "127.0.0.1"
    assert args[args.index("--port") + 1] == "8080"
    assert args[args.index("-c") + 1] == "32768"
    assert args[args.index("-ctk") + 1] == "q4_0"
    assert args[args.index("-ctv") + 1] == "q4_0"
    assert args[args.index("-fa") + 1] == "on"
    assert args[args.index("--alias") + 1] == "model"
    assert "--reasoning" in args and "off" in args
    assert "-ub" not in args  # 512 为默认不传
    assert "--api-key" not in args


def test_build_args_mtp_styles():
    args = build_args(_infer(mtp_steps=3), "m.gguf", "127.0.0.1", 8080,
                      include_mtp=True, mtp_style=MTP_LEGACY)
    assert args[args.index("--mtp") + 1] == "3"

    args = build_args(_infer(mtp_steps=3), "m.gguf", "127.0.0.1", 8080,
                      include_mtp=True, mtp_style=MTP_SPEC_TYPE)
    assert args[args.index("--spec-type") + 1] == "draft-mtp"
    assert args[args.index("--spec-draft-n-max") + 1] == "3"
    assert "--mtp" not in args

    # include_mtp=False 时不出现任何 MTP 参数
    args = build_args(_infer(mtp_steps=3), "m.gguf", "127.0.0.1", 8080,
                      include_mtp=False, mtp_style=MTP_LEGACY)
    assert "--mtp" not in args


def test_build_args_extra():
    args = build_args(_infer(extra_args="--no-mmap  --mlock"), "m.gguf",
                      "127.0.0.1", 8080, include_mtp=False, mtp_style=MTP_NONE)
    assert "--no-mmap" in args and "--mlock" in args
