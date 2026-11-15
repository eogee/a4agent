"""node 多实例守护测试（B40）：instances 设置校验、具名实例 spawn 参数
（含 --instance、不含 --config、LOCALAPPDATA 覆盖）、守护重启与退出码路径、
清单同步、端点行为与默认单实例回归。
全部用临时目录与假进程（Popen 打桩），不弹真进程、不污染 %APPDATA%。
"""
import subprocess
import threading
import time

import pytest
from fastapi import HTTPException

from backend.app.api.v1 import devices as devices_api
from backend.app.devices import node_proc
from backend.app.devices.node_proc import (INSTANCE_NAME_RE, NodeGuardian,
                                           resolve_settings, save_settings)


_PID = iter(range(42000, 42999))


class _FakeProc:
    """假子进程：记录 spawn 参数；无超时 wait 阻塞到 kill（模拟长驻 node），
    带超时 wait 快速超时（守护/停止路径立即收尾，不真等 8 秒）。"""

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = next(_PID)
        self.stdout = iter(())          # pump 线程立即结束（无输出）
        self.returncode = None
        self._dead = threading.Event()

    def poll(self):
        return self.returncode if self._dead.is_set() else None

    def wait(self, timeout=None):
        if timeout is not None:
            if not self._dead.wait(0.05):
                raise subprocess.TimeoutExpired("fake-node", timeout)
            return self.returncode
        self._dead.wait()
        return self.returncode

    def kill(self):
        self._dead.set()
        self.returncode = 1


@pytest.fixture()
def fake_spawn(monkeypatch):
    """替换 subprocess.Popen 捕获 spawn 参数；taskkill 打桩（绝不杀真进程）。"""
    captured = []

    def fake_popen(args, **kwargs):
        proc = _FakeProc(args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(node_proc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(node_proc, "_kill_tree", lambda pid: None)
    return captured


def allow_certs(monkeypatch):
    """证书保障放行（spawn 类测试用；preflight 测试保留真实 ensure_certs）。"""
    monkeypatch.setattr(node_proc, "ensure_certs",
                        lambda tool, tls_dir, log: True)


def fake_settings(tmp_path, monkeypatch):
    """node_path/python 指向临时假文件；instances 始终读真实设置文件，
    保证 save_settings / sync_instances / instances_status 三方口径一致。"""
    node_py = tmp_path / "node.py"
    node_py.write_text("print('fake node')\n", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"MZ")
    base = {"auto_start": False, "node_path": str(node_py),
            "python": str(python), "cert_tool": "", "extra_args": []}
    real_resolve = node_proc.resolve_settings

    def resolve(data_dir):
        out = dict(base)
        out["instances"] = real_resolve(data_dir).get("instances") or []
        return out

    monkeypatch.setattr(node_proc, "resolve_settings", resolve)
    return base


# ---------------- 设置：清单增删与非法名校验 ----------------

def test_instances_setting_roundtrip(tmp_path):
    save_settings(tmp_path, {"instances": ["lab", "dev-2", "lab"]})
    assert resolve_settings(tmp_path)["instances"] == ["lab", "dev-2"]  # 去重保序
    save_settings(tmp_path, {"instances": []})
    assert resolve_settings(tmp_path)["instances"] == []
    # 未提供的键保持原值
    save_settings(tmp_path, {"instances": ["lab"]})
    save_settings(tmp_path, {"auto_start": True})
    cfg = resolve_settings(tmp_path)
    assert cfg["instances"] == ["lab"] and cfg["auto_start"] is True


def test_instances_setting_validation(tmp_path):
    save_settings(tmp_path, {"instances": ["ok-1"]})
    for bad in (["bad name"], ["x" * 33], ["中文名"], "lab"):
        with pytest.raises(ValueError):
            save_settings(tmp_path, {"instances": bad})
    # 非法输入不落盘
    assert resolve_settings(tmp_path)["instances"] == ["ok-1"]
    # resolve 读取兜底：手改出的脏数据被过滤、去重
    (tmp_path / "device_runtime.json").write_text(
        '{"instances": ["ok-1", "bad name!", "ok-1"]}', encoding="utf-8")
    assert resolve_settings(tmp_path)["instances"] == ["ok-1"]


def test_instance_name_pattern_matches_node():
    """实例名规则与 node.py --instance 同规（[A-Za-z0-9_-]{1,32}）。"""
    for ok in ("lab", "A-b_c9", "x" * 32):
        assert INSTANCE_NAME_RE.fullmatch(ok)
    for bad in ("", "has space", "x" * 33, "中"):
        assert not INSTANCE_NAME_RE.fullmatch(bad)


# ---------------- 具名实例 spawn 参数 ----------------

def test_instance_spawn_args(tmp_path, monkeypatch, fake_spawn):
    """spawn 参数：--instance 在列、无 --config、无 extra_args；
    LOCALAPPDATA 覆盖到托管 node 目录；TLS 显式指向共享证书目录。"""
    settings = fake_settings(tmp_path, monkeypatch)
    allow_certs(monkeypatch)
    monkeypatch.setattr(node_proc, "RESTART_DELAY_SECONDS", 0.01)
    save_settings(tmp_path / "data", {"instances": ["lab"]})
    g = NodeGuardian(tmp_path / "data")
    out = g.start_instance("lab")
    assert out["state"] == "starting"
    assert out["pid"] is not None
    assert len(fake_spawn) == 1
    proc = fake_spawn[0]
    args = proc.args
    assert args[:2] == [settings["python"], settings["node_path"]]
    assert args[2:4] == ["--instance", "lab"]
    assert "--config" not in args
    assert args[4:] == ["--cert", str(g.config_dir / "tls" / "cert.pem"),
                        "--key", str(g.config_dir / "tls" / "key.pem")]
    env = proc.kwargs["env"]
    assert env["LOCALAPPDATA"] == str(g.config_dir)
    assert proc.kwargs["cwd"] == str(g.config_dir / node_proc.INSTANCE_DIR)
    # 默认实例未被牵动
    assert g.state == "stopped"
    # 手动停止收尾
    g.stop_instance("lab")
    assert g.instances_status()[0]["state"] == "stopped"


def test_start_instance_preflight_failures(tmp_path, monkeypatch, fake_spawn):
    """node.py/解释器缺失或证书不可用 → 置 failed，绝不 spawn。"""
    node_py = tmp_path / "node.py"
    node_py.write_text("print('x')\n", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"MZ")
    base = {"auto_start": False, "node_path": str(node_py),
            "python": str(python), "cert_tool": "", "extra_args": []}

    def settings_of(**over):
        merged = dict(base, **over)
        return lambda data_dir: dict(merged, instances=["lab"])

    # 1) node.py 缺失
    monkeypatch.setattr(node_proc, "resolve_settings",
                        settings_of(node_path=str(tmp_path / "nope.py")))
    g1 = NodeGuardian(tmp_path / "data1")
    out1 = g1.start_instance("lab")
    assert out1["state"] == "failed"
    assert any("node.py" in line for line in out1["log"])
    # 2) 解释器缺失
    monkeypatch.setattr(node_proc, "resolve_settings",
                        settings_of(python=str(tmp_path / "nope.exe")))
    g2 = NodeGuardian(tmp_path / "data2")
    assert g2.start_instance("lab")["state"] == "failed"
    # 3) 证书不可用（真实 ensure_certs：cert_tool 缺失 → False，不跑 PowerShell）
    monkeypatch.setattr(node_proc, "resolve_settings", settings_of())
    g3 = NodeGuardian(tmp_path / "data3")
    out3 = g3.start_instance("lab")
    assert out3["state"] == "failed"
    assert any("gen_cert.ps1" in line for line in out3["log"])
    assert fake_spawn == []


def test_instance_guard_restarts_and_records_exit(tmp_path, monkeypatch,
                                                  fake_spawn):
    """退出码与守护路径：连杀 5 个假进程（exit=1）→ 每次记录退出码并自动重启，
    第 5 次快速崩溃触发熔断（不再重启）；手动停不再重启。"""
    fake_settings(tmp_path, monkeypatch)
    allow_certs(monkeypatch)
    monkeypatch.setattr(node_proc, "RESTART_DELAY_SECONDS", 0.01)
    save_settings(tmp_path / "data", {"instances": ["lab"]})
    g = NodeGuardian(tmp_path / "data")
    g.start_instance("lab")
    for i in range(node_proc.MAX_CRASH_STREAK):
        deadline = time.time() + 5
        while len(fake_spawn) <= i and time.time() < deadline:
            time.sleep(0.02)
        assert len(fake_spawn) == i + 1
        fake_spawn[i].kill()                # 假进程退出（exit=1）
    # 连击达上限：守护熔断，状态 failed、退出码已记录、不再 spawn
    deadline = time.time() + 5
    while g.instances_status()[0]["state"] != "failed" and time.time() < deadline:
        time.sleep(0.02)
    st = g.instances_status()[0]
    assert st["state"] == "failed"
    assert st["exit_code"] == 1
    assert len(fake_spawn) == node_proc.MAX_CRASH_STREAK


def test_start_stop_unknown_or_bad_name(tmp_path, monkeypatch, fake_spawn):
    """不在清单 → KeyError（端点 404）；非法名 → ValueError（端点 400）。"""
    fake_settings(tmp_path, monkeypatch)
    g = NodeGuardian(tmp_path / "data")
    with pytest.raises(KeyError):
        g.start_instance("ghost")
    with pytest.raises(KeyError):
        g.stop_instance("ghost")
    with pytest.raises(ValueError):
        g.start_instance("bad name")
    with pytest.raises(ValueError):
        g.stop_instance("bad name")
    assert fake_spawn == []


def test_sync_instances_removes_guard_state(tmp_path, monkeypatch, fake_spawn):
    """清单同步（生产顺序：先落盘再对账）：移除的实例停进程并清理守护状态；
    重新加回不自动拉起、全新 stopped 槽位。"""
    fake_settings(tmp_path, monkeypatch)
    allow_certs(monkeypatch)
    monkeypatch.setattr(node_proc, "RESTART_DELAY_SECONDS", 0.01)
    save_settings(tmp_path / "data", {"instances": ["lab", "dev2"]})
    g = NodeGuardian(tmp_path / "data")
    g.start_instance("lab")
    g.start_instance("dev2")
    assert len(fake_spawn) == 2
    save_settings(tmp_path / "data", {"instances": ["dev2"]})   # 先落盘
    g.sync_instances(["dev2"])                                  # 再对账
    assert [s["name"] for s in g.instances_status()] == ["dev2"]
    assert g.stop_instance("dev2")["state"] == "stopped"
    save_settings(tmp_path / "data", {"instances": ["dev2", "lab"]})
    g.sync_instances(["dev2", "lab"])
    slots = g.instances_status()
    assert [s["name"] for s in slots] == ["dev2", "lab"]
    assert slots[1]["state"] == "stopped" and slots[1]["pid"] is None


# ---------------- 默认单实例回归 ----------------

def test_default_instance_regression(tmp_path, monkeypatch, fake_spawn):
    """无 instances 清单时默认实例行为与现状一致（spawn 参数/env/状态键）。"""
    settings = fake_settings(tmp_path, monkeypatch)
    allow_certs(monkeypatch)
    g = NodeGuardian(tmp_path / "data")
    st = g.start()
    assert st["state"] == "starting"
    assert len(fake_spawn) == 1
    proc = fake_spawn[0]
    assert proc.args == [settings["python"], settings["node_path"],
                         "--config", str(g.config_file)]
    assert "env" not in proc.kwargs         # 默认实例不注入环境覆盖
    assert set(g.status().keys()) == {
        "state", "pid", "exit_code", "port", "config_dir", "config_file",
        "node_path", "python", "cert_tool", "auto_start", "cert_ready", "log"}
    assert g.instances_status() == []
    g.stop()
    assert g.state == "stopped"


# ---------------- 端点行为 ----------------

@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """数据目录隔离 + guardian 单例复位（端点走真实设置文件）。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(data_dir))
    saved = node_proc.guardian
    node_proc.guardian = None
    yield data_dir
    node_proc.guardian = saved


def test_runtime_instances_endpoints(isolated, tmp_path, monkeypatch, fake_spawn):
    """runtime 响应增 instances（默认 []）；start/stop 行为与 404/400 映射。"""
    node_py = tmp_path / "node.py"
    node_py.write_text("print('fake node')\n", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"MZ")
    devices_api.runtime_save_config({
        "node_path": str(node_py), "python": str(python),
        "cert_tool": "X:\\no\\gen_cert.ps1"})
    allow_certs(monkeypatch)
    monkeypatch.setattr(node_proc, "RESTART_DELAY_SECONDS", 0.01)

    st = devices_api.runtime_status()
    assert st["instances"] == []
    assert {"state", "pid", "exit_code", "port", "config_dir", "config_file",
            "node_path", "python", "cert_tool", "auto_start", "cert_ready",
            "log", "listener", "instances"} <= set(st.keys())

    devices_api.runtime_save_config({"instances": ["lab", "lab"]})   # 去重
    ins = devices_api.runtime_status()["instances"]
    assert [i["name"] for i in ins] == ["lab"]
    assert set(ins[0].keys()) == {"name", "state", "pid", "exit_code", "port",
                                  "config_dir", "config_file", "cert_ready",
                                  "log"}
    assert ins[0]["state"] == "stopped" and ins[0]["pid"] is None

    out = devices_api.runtime_instance_start("lab")     # 经假进程拉起
    assert out["state"] == "starting" and out["pid"] is not None
    assert len(fake_spawn) == 1
    assert "--instance" in fake_spawn[0].args
    assert "--config" not in fake_spawn[0].args
    assert devices_api.runtime_status()["instances"][0]["state"] == "starting"

    assert devices_api.runtime_instance_stop("lab")["state"] == "stopped"

    devices_api.runtime_save_config({"instances": []})   # 清单移除 → 守护清理
    assert devices_api.runtime_status()["instances"] == []

    with pytest.raises(HTTPException) as ei:
        devices_api.runtime_instance_start("ghost")
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        devices_api.runtime_instance_stop("ghost")
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        devices_api.runtime_instance_start("bad name!")
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        devices_api.runtime_save_config({"instances": ["bad name!"]})
    assert ei.value.status_code == 400
