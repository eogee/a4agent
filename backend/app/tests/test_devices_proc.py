"""node 运行时守护测试（D29）：设置解析、证书保障、命令拼装、探活、
启停守护（子进程用 sleep 脚本假扮 node，不改 node 代码）、解释器解析
（C8 方案 A：打包态首选安装目录内嵌 python-embed）。"""
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from backend.app.devices import node_proc
from backend.app.devices.node_proc import (NodeGuardian, ensure_certs,
                                           probe_alive, resolve_settings,
                                           save_settings)


# ---------------- 设置解析 ----------------

def test_resolve_settings_env_overrides(tmp_path, monkeypatch):
    monkeypatch.delenv("A4AGENT_NODE_PATH", raising=False)
    monkeypatch.delenv("A4AGENT_NODE_PYTHON", raising=False)
    monkeypatch.delenv("A4AGENT_CERT_TOOL", raising=False)
    cfg = resolve_settings(tmp_path)      # 无配置无环境 → 走默认候选
    assert set(cfg.keys()) == set(node_proc.SETTINGS_KEYS)
    assert cfg["auto_start"] is False

    monkeypatch.setenv("A4AGENT_NODE_PATH", "X:\\nd\\node.py")
    monkeypatch.setenv("A4AGENT_NODE_PYTHON", "X:\\py\\python.exe")
    monkeypatch.setenv("A4AGENT_CERT_TOOL", "X:\\nd\\gen_cert.ps1")
    cfg = resolve_settings(tmp_path)
    assert cfg["node_path"] == "X:\\nd\\node.py"
    assert cfg["python"] == "X:\\py\\python.exe"
    assert cfg["cert_tool"] == "X:\\nd\\gen_cert.ps1"


def test_resolve_settings_config_file(tmp_path, monkeypatch):
    save_settings(tmp_path, {"node_path": "C:\\nd\\node.py", "extra_args": ["--port", "51888"],
                             "auto_start": True})
    monkeypatch.delenv("A4AGENT_NODE_PATH", raising=False)
    cfg = resolve_settings(tmp_path)
    assert cfg["node_path"] == "C:\\nd\\node.py"
    assert cfg["extra_args"] == ["--port", "51888"]
    assert cfg["auto_start"] is True


def test_save_settings_partial_update(tmp_path):
    save_settings(tmp_path, {"node_path": "C:\\a\\node.py"})
    out = save_settings(tmp_path, {"python": "C:\\b\\python.exe"})
    assert out["node_path"] == "C:\\a\\node.py"       # 未提供的键保持原值
    assert out["python"] == "C:\\b\\python.exe"


# ---------------- 解释器解析（C8 方案 A：内嵌 python-embed 最优先） ----------------

def _patch_frozen(monkeypatch, exe_dir):
    """模拟 PyInstaller 打包态目录布局：exe 位于 exe_dir，_MEIPASS 同置此处
    （onedir 安装形态 {app}；meipass/exe 双候选指向同一位），并屏蔽系统
    base_prefix/PATH 探测，只留内嵌候选与显式注入的候选可命中。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(exe_dir), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "a4agent.exe"))
    monkeypatch.setattr(sys, "base_prefix", str(exe_dir / "_nonexistent_prefix"))
    monkeypatch.setattr(node_proc.shutil, "which", lambda name: None)


def test_default_python_prefers_embedded(tmp_path, monkeypatch):
    """打包态安装目录含 node_runtime/python/python.exe 时首选内嵌解释器。"""
    exe_dir = tmp_path / "app"
    embed = exe_dir / "node_runtime" / "python"
    embed.mkdir(parents=True)
    (embed / "python.exe").write_bytes(b"MZ")    # 假 exe：候选只做 is_file 判定
    _patch_frozen(monkeypatch, exe_dir)
    out = node_proc._default_python()
    assert Path(out) == (embed / "python.exe").resolve()


def test_default_python_falls_back_without_embed(tmp_path, monkeypatch):
    """打包态内嵌解释器缺席（未暂存/降级包）→ 回退既有候选（base_prefix）。"""
    exe_dir = tmp_path / "app"
    exe_dir.mkdir()
    real_py = tmp_path / "venv" / "python.exe"
    real_py.parent.mkdir(parents=True)
    real_py.write_bytes(b"MZ")
    _patch_frozen(monkeypatch, exe_dir)
    monkeypatch.setattr(sys, "base_prefix", str(real_py.parent))
    out = node_proc._default_python()
    assert Path(out) == real_py.resolve()


def test_default_python_dev_mode_ignores_embed(tmp_path, monkeypatch):
    """dev 态（未 frozen）不启用内嵌候选：即使 exe 旁有 node_runtime/python
    也不选它（回归护栏：frozen 门控不因重构丢失）。"""
    embed = tmp_path / "node_runtime" / "python"
    embed.mkdir(parents=True)
    (embed / "python.exe").write_bytes(b"MZ")
    fake_exe = tmp_path / "python.exe"
    fake_exe.write_bytes(b"MZ")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "_nonexistent_prefix"))
    monkeypatch.setattr(node_proc.shutil, "which", lambda name: None)
    out = node_proc._default_python()
    assert Path(out) == fake_exe.resolve()


# ---------------- 证书保障 ----------------

def test_ensure_certs_skips_when_present(tmp_path):
    tls = tmp_path / "tls"
    tls.mkdir()
    (tls / "cert.pem").write_text("CERT", encoding="utf-8")
    (tls / "key.pem").write_text("KEY", encoding="utf-8")
    assert ensure_certs("X:\\nonexistent.ps1", tls, lambda m: None) is True


def test_ensure_certs_fails_without_tool(tmp_path):
    logs = []
    assert ensure_certs("", tmp_path / "tls", logs.append) is False
    assert any("gen_cert.ps1" in m for m in logs)


def test_ensure_certs_builds_powershell_command(tmp_path, monkeypatch):
    """证书缺失 → 等价 xd cert init 底层命令（tools/gen_cert.ps1 -OutDir）。"""
    tls = tmp_path / "tls"
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        tls.mkdir(parents=True, exist_ok=True)
        (tls / "cert.pem").write_text("CERT", encoding="utf-8")
        (tls / "key.pem").write_text("KEY", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(node_proc.subprocess, "run", fake_run)
    monkeypatch.setattr(node_proc.shutil, "which", lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe")
    tool = tmp_path / "gen_cert.ps1"
    tool.write_text("# tool", encoding="utf-8")
    assert ensure_certs(str(tool), tls, lambda m: None) is True
    cmd = calls["cmd"]
    assert cmd[0].endswith("powershell.exe")
    assert "-File" in cmd and str(tool) in cmd
    assert "-OutDir" in cmd and str(tls) in cmd
    assert "-NoProfile" in cmd and "-ExecutionPolicy" in cmd and "Bypass" in cmd


# ---------------- 探活 ----------------

def test_probe_alive_with_http_response():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    port = server.getsockname()[1]

    def serve_plain():
        # 探活先试 TLS 再退明文：两次连接都按"收请求 → 回状态行"应答
        for _ in range(2):
            try:
                conn, _ = server.accept()
                conn.recv(1024)
                conn.sendall(b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                conn.close()
            except OSError:
                return

    threading.Thread(target=serve_plain, daemon=True).start()
    assert probe_alive(port) is True          # 401 即存活（watchdog 同款口径）
    server.close()


def test_probe_alive_refused():
    # 没有监听者的端口
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_alive(port) is False


# ---------------- 守护启停（假 node = sleep 脚本） ----------------

@pytest.fixture()
def fake_node(tmp_path, monkeypatch):
    node_py = tmp_path / "node.py"
    node_py.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    settings = {
        "auto_start": False,
        "node_path": str(node_py),
        "python": sys.executable,
        "cert_tool": "",
        "extra_args": [],
    }
    monkeypatch.setattr(node_proc, "resolve_settings", lambda data_dir: dict(settings))
    monkeypatch.setattr(node_proc, "ensure_certs",
                        lambda tool, tls_dir, log: True)
    return node_py


def test_guardian_start_stop(fake_node, tmp_path):
    g = NodeGuardian(tmp_path / "data")
    st = g.start()
    assert st["state"] == "starting"
    assert st["pid"]
    assert st["node_path"] == str(fake_node)
    time.sleep(0.2)
    # 进程仍在跑
    assert g._proc and g._proc.poll() is None
    out = g.stop()
    assert out["state"] == "stopped"
    assert g._proc is None
    time.sleep(0.2)
    assert g._exit_code is not None


def test_guardian_start_idempotent(fake_node, tmp_path):
    g = NodeGuardian(tmp_path / "data")
    g.start()
    try:
        pid1 = g.status()["pid"]
        st = g.start()          # starting 态重复 start 不再拉起
        assert st["pid"] == pid1
    finally:
        g.stop()


def test_guardian_start_fails_without_node(tmp_path):
    g = NodeGuardian(tmp_path / "data")
    settings = {"auto_start": False, "node_path": "", "python": sys.executable,
                "cert_tool": "", "extra_args": []}
    orig = node_proc.resolve_settings
    node_proc.resolve_settings = lambda data_dir: dict(settings)
    try:
        st = g.start()
        assert st["state"] == "failed"
        assert any("node.py" in line for line in st["log"])
    finally:
        node_proc.resolve_settings = orig


def test_guardian_stop_when_not_running(tmp_path):
    g = NodeGuardian(tmp_path / "data")
    assert g.stop()["state"] == "stopped"


def test_read_port_from_config(tmp_path):
    g = NodeGuardian(tmp_path / "data")
    assert g.read_port() is None            # node.json 尚不存在
    (g.config_dir).mkdir(parents=True, exist_ok=True)
    (g.config_file).write_text('{"port": 51999}', encoding="utf-8")
    assert g.read_port() == 51999
    (g.config_file).write_text('{"port": "x"}', encoding="utf-8")
    assert g.read_port() is None
