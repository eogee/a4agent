"""引擎下载器解压/原子换入 + runtime 管理器（模型库/向导/设置）测试。"""
import io
import zipfile

import pytest

import backend.app.llama.runtime as rt_mod
from backend.app.llama.downloader import (EngineDownloader, _extract_flat,
                                          is_engine_present)


# ───────────────────────── 下载器：解压与原子换入 ─────────────────────────

def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _gguf_minimal() -> bytes:
    """最小可解析 GGUF（仅 magic+version+计数）。"""
    b = io.BytesIO()
    b.write(b"GGUF")
    b.write(b"\x00" * 4)   # version (无效但 downloader 不解析)
    return b.getvalue()


def test_is_engine_present(tmp_path):
    assert not is_engine_present(tmp_path)
    (tmp_path / "llama-server.exe").write_bytes(b"x")
    assert is_engine_present(tmp_path)


def test_extract_flat_merges_and_blocks_traversal(tmp_path):
    import struct

    def gguf():
        b = io.BytesIO()
        b.write(b"GGUF")
        b.write(struct.pack("<I", 3))
        b.write(struct.pack("<Q", 0))
        b.write(struct.pack("<Q", 0))
        return b.getvalue()

    zip1 = tmp_path / "1.zip"
    zip1.write_bytes(_make_zip({"llama-server.exe": "BIN", "a.dll": "A"}))
    zip2 = tmp_path / "2.zip"
    zip2.write_bytes(_make_zip({"b.dll": "B", "llama-server.exe": "SHOULD_NOT_OVERWRITE"}))
    evil = tmp_path / "3.zip"
    evil.write_bytes(_make_zip({"..\\evil.txt": "X"}))

    dest = tmp_path / "engine"
    dest.mkdir()
    import threading
    cancel = threading.Event()
    for z in (zip1, zip2, evil):
        _extract_flat(z, dest, cancel)

    assert (dest / "llama-server.exe").read_text() == "BIN"  # 主包优先
    assert (dest / "a.dll").read_text() == "A"
    assert (dest / "b.dll").read_text() == "B"               # cudart 合并
    assert not (tmp_path / "evil.txt").exists()              # 路径穿越被丢弃


# ───────────────────────── runtime 管理器 ─────────────────────────

@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    """隔离的 LlamaRuntime：配置文件与引擎目录都落在 tmp_path。"""
    import backend.app.llama.config as cfg_mod

    cfg_path = tmp_path / "llama_config.json"

    def fake_load():
        cfg = rt_mod.LlamaConfig()
        try:
            if cfg_path.exists():
                cfg.apply_dict(__import__("json").loads(
                    cfg_path.read_text(encoding="utf-8")))
        except Exception:
            pass
        return cfg

    def fake_save(cfg):
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            __import__("json").dumps(cfg.to_dict(), ensure_ascii=False),
            encoding="utf-8")

    monkeypatch.setattr(cfg_mod, "config_path", lambda: cfg_path)
    monkeypatch.setattr(rt_mod, "load_config", fake_load)
    monkeypatch.setattr(rt_mod, "save_config", fake_save)
    # config.effective_engine_dir 默认走 config.default_engine_dir → get_data_dir()，
    # 这里重定向到 tmp_path，避免测试触碰真实数据目录
    monkeypatch.setattr(cfg_mod, "default_engine_dir",
                        lambda base=None: tmp_path / "engine")
    r = rt_mod.LlamaRuntime()
    yield r
    r.shutdown()


def _gguf_file(path, layers=12, kv_heads=4, klen=64, ctx=4096, size_pad=0):
    import struct

    b = io.BytesIO()
    b.write(b"GGUF")
    b.write(struct.pack("<I", 3))
    b.write(struct.pack("<Q", 0))  # tensors
    pairs = [
        ("llama.block_count", 10, layers),
        ("llama.attention.head_count_kv", 10, kv_heads),
        ("llama.attention.key_length", 10, klen),
        ("llama.attention.value_length", 10, klen),
        ("llama.context_length", 10, ctx),
        ("general.architecture", 8, "llama"),
        ("general.file_type", 10, 15),
    ]
    b.write(struct.pack("<Q", len(pairs)))
    for key, vt, val in pairs:
        b.write(struct.pack("<Q", len(key)))
        b.write(key.encode())
        b.write(struct.pack("<I", vt))
        if vt == 8:
            b.write(struct.pack("<Q", len(val)))
            b.write(val.encode())
        else:
            b.write(struct.pack("<q", val))
    data = b.getvalue()
    with open(path, "wb") as fp:
        fp.write(data)
        fp.write(b"\x00" * size_pad)
    return path


def test_runtime_model_flow(runtime, tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    _gguf_file(str(model_dir / "a.gguf"), size_pad=1024)

    payload = runtime.add_model_dir(str(model_dir))
    assert str(model_dir) in payload["dirs"]
    assert len(payload["models"]) == 1
    m = payload["models"][0]
    assert m["ok"] and m["arch"] == "llama" and m["quant_label"] == "Q4_K_M"

    result = runtime.set_default_model(m["file_path"])
    assert result["ok"]
    assert runtime.cfg.default_model_path == m["file_path"]
    assert runtime.models_payload()["models"][0]["is_default"]

    risk = runtime.model_risk(m["file_path"])
    assert risk["model"]["ok"]
    assert risk["estimated_vram_gb"] is not None

    # 移除目录
    payload = runtime.remove_model_dir(str(model_dir))
    assert payload["dirs"] == []
    assert payload["models"] == []


def test_runtime_wizard_finish(runtime, tmp_path):
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    f = _gguf_file(str(model_dir / "m.gguf"))
    runtime.add_model_dir(str(model_dir))

    result = runtime.wizard_finish(
        port=9090, default_model_path=str(f),
        preset={"context_tokens": 16384, "kv_level": "q8_0", "label": "测试"})
    assert result["ok"]
    assert runtime.cfg.wizard_done
    assert runtime.cfg.port == 9090
    assert runtime.cfg.infer.context_tokens == 16384
    assert runtime.cfg.infer.kv_level == "q8_0"
    assert runtime.cfg.default_model_path.endswith("m.gguf")
    assert len(runtime.cfg.models) == 1

    # 非法端口被拒绝
    assert not runtime.wizard_finish(port=80, default_model_path=str(f))["ok"]


def test_runtime_settings_validation(runtime):
    # 运行中不能改监听
    runtime.engine._state = "running"
    bad = runtime.save_settings({"port": 9999})
    assert not bad["ok"]
    runtime.engine._state = "stopped"

    ok = runtime.save_settings({"port": 8181, "host": "0.0.0.0",
                                "infer": {"context_tokens": 4096,
                                          "kv_level": "f16"}})
    assert ok["ok"]
    assert runtime.cfg.port == 8181
    assert runtime.cfg.host == "0.0.0.0"
    assert runtime.cfg.infer.context_tokens == 4096

    bad = runtime.save_settings({"host": "192.168.1.1"})
    assert not bad["ok"]


def test_runtime_status_and_connect(runtime, tmp_path):
    f = _gguf_file(str(tmp_path / "solo.gguf"))
    runtime.cfg.default_model_path = str(f)
    runtime.cfg.wizard_done = True

    s = runtime.status()
    assert s["state"] == "stopped"
    assert s["wizard_done"]
    assert s["default_model_name"] == "solo"

    c = runtime.connect()
    assert c["base_url"] == "http://127.0.0.1:8080"
    assert c["chat_url"].endswith("/v1/chat/completions")
    assert c["model"] == "solo"
    assert "curl" in c["curl_example"]
    assert "OpenAI" in c["sdk_example"]
    assert c["lan_base_url"] is None  # host 默认仅本机

    runtime.cfg.host = "0.0.0.0"
    c2 = runtime.connect()
    assert c2["lan_base_url"] is not None and "8080" in c2["lan_base_url"]


def test_locate_engine_prefers_backend_over_mtime(runtime, tmp_path, monkeypatch):
    """配置为空时，NVIDIA 卡优先 CUDA 引擎，而不是选中文件更新的 Vulkan 引擎。"""
    import os
    import time

    from backend.app.llama.gpu import GpuInfo
    import backend.app.llama.catalog as catalog

    root = tmp_path / "Programs"
    vulkan = root / "a4agent-vulkan" / "engine"
    cuda = root / "a4agent-cu" / "engine"
    for d, dll in ((vulkan, "ggml-vulkan.dll"), (cuda, "ggml-cuda.dll")):
        d.mkdir(parents=True)
        (d / "llama-server.exe").write_bytes(b"x")
        (d / dll).write_bytes(b"x")

    # Vulkan 的 exe 更新 → 旧逻辑（纯 mtime）会选它
    now = time.time()
    os.utime(cuda / "llama-server.exe", (now - 9000, now - 9000))

    monkeypatch.setattr(rt_mod, "_programs_root", lambda: root)
    runtime.cfg.engine_dir = ""  # 走数据目录（fixture 已重定向到 tmp_path/engine，不存在）

    monkeypatch.setattr(rt_mod.gpu, "detect", lambda: [
        GpuInfo("NVIDIA", "RTX Test", 22 * 1024 ** 3, "591.86")])
    assert runtime.locate_engine() == str(cuda)
    assert runtime.engine_backend() == "cuda"
    assert runtime.status()["engine_backend_label"] == "CUDA"

    # 非 NVIDIA 卡：Vulkan 优先
    monkeypatch.setattr(rt_mod.gpu, "detect", lambda: [
        GpuInfo("AMD", "Radeon Test", 16 * 1024 ** 3, "32.0.11021")])
    assert runtime.locate_engine() == str(vulkan)
    assert runtime.engine_backend() == "vulkan"

    # 认不出后端的目录仍可兜底（排在已知后端之后）
    (cuda / "ggml-cuda.dll").unlink()
    monkeypatch.setattr(rt_mod.gpu, "detect", lambda: [
        GpuInfo("NVIDIA", "RTX Test", 22 * 1024 ** 3, "591.86")])
    assert runtime.locate_engine() == str(vulkan)
    assert catalog.backend_preference(rt_mod.gpu.best())[0] == "cuda"


def test_locate_engine_config_wins(runtime, tmp_path, monkeypatch):
    """显式配置的引擎目录绝对优先，不会被后端偏好改掉。"""
    own = tmp_path / "pinned" / "engine"
    own.mkdir(parents=True)
    (own / "llama-server.exe").write_bytes(b"x")
    (own / "ggml-vulkan.dll").write_bytes(b"x")

    other = tmp_path / "Programs" / "a4agent" / "engine"
    other.mkdir(parents=True)
    (other / "llama-server.exe").write_bytes(b"x")
    (other / "ggml-cuda.dll").write_bytes(b"x")
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: tmp_path / "Programs")

    runtime.cfg.engine_dir = str(own)
    assert runtime.locate_engine() == str(own)
    assert runtime.engine_backend() == "vulkan"


def test_engine_dir_for_is_backend_named(runtime, tmp_path, monkeypatch):
    """下载落点按后端命名，目录名即内容语义；CUDA 的两个版本共用一个目录。"""
    from backend.app.llama import catalog

    monkeypatch.setattr(rt_mod, "_programs_root", lambda: tmp_path / "Programs")
    assert runtime.engine_dir_for(catalog.find("cuda134")).name == "engine"
    assert runtime.engine_dir_for(catalog.find("cuda134")).parts[-2] == "a4agent-cuda"
    assert runtime.engine_dir_for(catalog.find("cuda124")) == \
        runtime.engine_dir_for(catalog.find("cuda134"))
    assert runtime.engine_dir_for(catalog.find("vulkan")).parts[-2] == "a4agent-vulkan"
    assert runtime.engine_dir_for(catalog.find("cpu")).parts[-2] == "a4agent-cpu"


def test_after_engine_download_switches_only_on_success(runtime, tmp_path, monkeypatch):
    """装好才切换指针；失败/取消保持原引擎目录不动。"""
    from backend.app.llama import catalog

    monkeypatch.setattr(rt_mod, "_programs_root", lambda: tmp_path / "Programs")
    target = runtime.engine_dir_for(catalog.find("cuda134"))
    target.mkdir(parents=True)
    (target / "llama-server.exe").write_bytes(b"x")
    (target / "ggml-cuda.dll").write_bytes(b"x")

    # 先指向别处（模拟当前用 Vulkan）
    old = tmp_path / "old-engine"
    old.mkdir()
    (old / "llama-server.exe").write_bytes(b"x")
    runtime.cfg.engine_dir = str(old)

    runtime._after_engine_download(target, "下载不完整")   # 失败：不切换
    assert runtime.cfg.engine_dir == str(old)

    runtime._after_engine_download(target, "")             # 成功：切过去
    assert runtime.cfg.engine_dir == str(target)
    assert runtime.engine_backend() == "cuda"
    assert any("已切换到 CUDA" in l["text"] for l in runtime.logs.after(0))

    # 目录被清空（半个引擎）时也不切换
    (target / "llama-server.exe").unlink()
    runtime.cfg.engine_dir = str(old)
    runtime._after_engine_download(target, "")
    assert runtime.cfg.engine_dir == str(old)


# ───────────────────────── 设置页切换引擎后端 ─────────────────────────

def _install_engine(root, dirname, dll):
    d = root / dirname / "engine"
    d.mkdir(parents=True)
    (d / "llama-server.exe").write_bytes(b"x")
    (d / dll).write_bytes(b"x")
    return d


def test_installed_backends_and_switch(runtime, tmp_path, monkeypatch):
    """已装后端固定排序；标准目录缺失时旧命名目录兜底。"""
    root = tmp_path / "Programs"
    legacy_cuda = _install_engine(root, "a4agent-cu12.4", "ggml-cuda.dll")
    vulkan = _install_engine(root, "a4agent-vulkan", "ggml-vulkan.dll")
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: root)

    assert runtime.installed_backends() == ["cuda", "vulkan"]

    ok = runtime.switch_backend("cuda")
    assert ok["ok"]
    assert runtime.cfg.engine_dir == str(legacy_cuda)
    assert runtime.engine_backend() == "cuda"

    ok = runtime.switch_backend("vulkan")
    assert ok["ok"]
    assert runtime.cfg.engine_dir == str(vulkan)

    assert not runtime.switch_backend("tpu")["ok"]


def test_switch_backend_requires_installed(runtime, tmp_path, monkeypatch):
    """未安装的后端拒绝切换且不动配置；下载中拒绝；auto 清空指针。"""
    root = tmp_path / "Programs"
    vulkan = _install_engine(root, "a4agent-vulkan", "ggml-vulkan.dll")
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: root)

    bad = runtime.switch_backend("cuda")
    assert not bad["ok"] and "尚未安装" in bad["error"]
    assert runtime.cfg.engine_dir == ""

    assert runtime.switch_backend("vulkan")["ok"]

    monkeypatch.setattr(runtime.downloader, "progress", lambda: {"running": True})
    blocked = runtime.switch_backend("auto")
    assert not blocked["ok"] and "下载" in blocked["error"]
    assert runtime.cfg.engine_dir == str(vulkan)

    monkeypatch.setattr(runtime.downloader, "progress", lambda: {"running": False})
    assert runtime.switch_backend("auto")["ok"]
    assert runtime.cfg.engine_dir == ""
    assert runtime.settings_payload()["engine_mode"] == "auto"


def test_settings_payload_engine_mode(runtime, tmp_path, monkeypatch):
    """指针指向的目录里真有引擎才算钉定；死指针如实标 auto。"""
    root = tmp_path / "Programs"
    vulkan = _install_engine(root, "a4agent-vulkan", "ggml-vulkan.dll")
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: root)

    runtime.cfg.engine_dir = str(vulkan)
    st = runtime.settings_payload()
    assert st["engine_mode"] == "vulkan"
    assert st["engine_backend"] == "vulkan"

    dead = tmp_path / "gone" / "engine"          # 指针还挂着但目录已空
    dead.mkdir(parents=True)
    runtime.cfg.engine_dir = str(dead)
    st = runtime.settings_payload()
    assert st["engine_mode"] == "auto"
    assert st["engine_backend"] == "vulkan"      # 实际会被自动接管到 vulkan


def test_save_settings_engine_backend(runtime, tmp_path, monkeypatch):
    """save_settings 路由 engine_backend：成功落盘，失败保持原状。"""
    root = tmp_path / "Programs"
    vulkan = _install_engine(root, "a4agent-vulkan", "ggml-vulkan.dll")
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: root)

    ok = runtime.save_settings({"engine_backend": "vulkan", "port": 8081})
    assert ok["ok"]
    assert runtime.cfg.engine_dir == str(vulkan)
    assert runtime.cfg.port == 8081

    bad = runtime.save_settings({"engine_backend": "cuda"})
    assert not bad["ok"]
    assert runtime.cfg.engine_dir == str(vulkan)   # 失败不落盘

    # engine_backend 缺省/为空时忽略，其余设置照常保存（旧前端兼容）
    assert runtime.save_settings({})["ok"]
    assert runtime.save_settings({"engine_backend": "", "port": 8082})["ok"]
    assert runtime.cfg.port == 8082


def test_wizard_packs_reports_target_dir(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(rt_mod, "_programs_root", lambda: tmp_path / "Programs")
    monkeypatch.setattr(rt_mod.gpu, "detect", lambda: [])
    packs = {p["id"]: p for p in runtime.wizard_packs()["packs"]}
    assert packs["cuda134"]["target_dir"].endswith("a4agent-cuda\\engine") or \
        packs["cuda134"]["target_dir"].endswith("a4agent-cuda/engine")
    assert packs["vulkan"]["target_dir"] != packs["cuda134"]["target_dir"]


def test_log_ring(runtime):
    runtime.logs.clear()
    for i in range(5):
        runtime.logs.append(f"line {i}")
    # id 从 1 开始单调递增；after(n) 返回 id > n 的增量
    lines = runtime.logs.after(0)
    assert [l["text"] for l in lines] == [f"line {i}" for i in range(5)]
    assert lines[0]["id"] == 1
    assert [l["text"] for l in runtime.logs.after(3)] == ["line 3", "line 4"]
    assert [l["text"] for l in runtime.logs.after(4)] == ["line 4"]
    assert runtime.logs.after(5) == []
    runtime.logs.clear()
    assert runtime.logs.after(0) == []


# ───────────────────────── pi 的上下文窗口提示 ─────────────────────────

def test_served_context_for_only_matches_local_service(runtime, tmp_path, monkeypatch):
    """只有方案确实指向本机当前 llama-server 与当前默认模型时，才给出窗口值。"""
    model_path = tmp_path / "models" / "qwen-coder.Q4_K_M.gguf"
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"GGUF")
    runtime.cfg.default_model_path = str(model_path)
    runtime.cfg.port = 8080
    runtime.cfg.host = "127.0.0.1"
    runtime.cfg.infer.context_tokens = 262144
    model = model_path.stem
    local = f"http://127.0.0.1:{runtime.cfg.port}/v1"

    assert runtime.served_context_for(local, model) == 262144
    assert runtime.served_context_for(f"http://localhost:{runtime.cfg.port}/v1", model) == 262144
    # 端口不符 / 远程服务商 / 别的模型 → 不给提示，pi 维持原有写法
    assert runtime.served_context_for("http://127.0.0.1:9090/v1", model) is None
    assert runtime.served_context_for("https://api.deepseek.com/anthropic", model) is None
    assert runtime.served_context_for(local, "another-model") is None
    # 未设默认模型
    runtime.cfg.default_model_path = ""
    assert runtime.served_context_for(local, model) is None


def test_served_context_for_matches_lan_address(runtime, tmp_path, monkeypatch):
    """监听 0.0.0.0 时，局域网地址同样被视为本机服务。"""
    model_path = tmp_path / "qwen.gguf"
    model_path.write_bytes(b"GGUF")
    runtime.cfg.default_model_path = str(model_path)
    runtime.cfg.port = 8080
    runtime.cfg.host = "0.0.0.0"
    runtime.cfg.infer.context_tokens = 131072
    monkeypatch.setattr(rt_mod.lan, "get_best_ipv4", lambda: "192.168.1.20")

    assert runtime.served_context_for("http://192.168.1.20:8080/v1", "qwen") == 131072
    assert runtime.served_context_for("http://10.0.0.5:8080/v1", "qwen") is None
