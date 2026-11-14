"""设备页接口测试（B23/B33/B34 的 a4agent 半边）：发现池接口、配对编排、
操作代理错误映射、漂移兜底。node 侧全部打桩（不要求起真 node，
真 node 全链自测另行执行）。"""
import json

import pytest
from fastapi import HTTPException

from backend.app.api.v1 import devices as devices_api
from backend.app.devices import node_client, node_proc
from backend.app.devices import store as store_mod
from backend.app.devices.node_client import NodeError
from backend.app.devices.pool import discovery


@pytest.fixture()
def isolated(monkeypatch, tmp_path):
    """数据目录隔离 + 单例复位（store / guardian 惰性绑定数据目录）。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(data_dir))
    saved_store = store_mod.store
    saved_guardian = node_proc.guardian
    store_mod.store = None
    node_proc.guardian = None
    yield data_dir
    store_mod.store = saved_store
    node_proc.guardian = saved_guardian


def seed_pool_entry(device_id="a1b2c3d4e5f6", ip="192.168.3.10", port=51888,
                    hostname="PC-A"):
    import time as _t
    discovery.pool.accept({
        "device_id": device_id, "ip": ip, "hostname": hostname, "port": port,
        "version": "0.5.0", "caps": ["exec"],
    }, ip)
    # accept 内部使用 time.time()，这里补刷 last_seen 语义即可（帧内容已注册）


# ---------------- 运行时接口 ----------------

def test_runtime_status_and_config(isolated):
    st = devices_api.runtime_status()
    assert st["state"] == "stopped"
    assert st["listener"]["port"] == 51889
    cfg = devices_api.runtime_config()
    assert set(cfg.keys()) == set(node_proc.SETTINGS_KEYS)
    assert cfg["auto_start"] is False


def test_runtime_config_save_and_validation(isolated):
    out = devices_api.runtime_save_config({"node_path": "X:\\n\\node.py",
                                           "auto_start": True})
    assert out["node_path"] == "X:\\n\\node.py"
    assert out["auto_start"] is True
    with pytest.raises(HTTPException) as ei:
        devices_api.runtime_save_config({"nope": 1})
    assert ei.value.status_code == 400


# ---------------- 发现池接口 ----------------

def test_pool_manual_add_list_remove(isolated):
    devices_api.pool_manual_add({"ip": "192.168.3.20", "port": 51888})
    snap = devices_api.pool_snapshot()
    assert any(m["ip"] == "192.168.3.20" for m in snap["manual"])
    assert snap["listener"]["bound"] in (True, False)   # 视环境端口占用而定
    devices_api.pool_manual_remove({"ip": "192.168.3.20", "port": 51888})
    snap = devices_api.pool_snapshot()
    assert not any(m["ip"] == "192.168.3.20" for m in snap["manual"])
    with pytest.raises(HTTPException) as ei:
        devices_api.pool_manual_remove({"ip": "192.168.3.20", "port": 51888})
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        devices_api.pool_manual_add({"ip": "bad ip", "port": 51888})
    assert ei.value.status_code == 400


# ---------------- 配对编排（控制端） ----------------

def test_pair_begin_maps_pair_errors(isolated, monkeypatch):
    def fake_begin(ip, port, timeout=15):
        return {"session_id": "s1", "state": "pending", "expires_at": 123}
    monkeypatch.setattr(node_client, "pair_begin", fake_begin)
    out = devices_api.pair_begin({"ip": "192.168.3.10", "port": 51888})
    assert out["session_id"] == "s1"

    def active(ip, port, timeout=15):
        raise NodeError("PAIR_SESSION_ACTIVE", "已存在活跃配对会话", 409)
    monkeypatch.setattr(node_client, "pair_begin", active)
    with pytest.raises(HTTPException) as ei:
        devices_api.pair_begin({"ip": "192.168.3.10", "port": 51888})
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "PAIR_SESSION_ACTIVE"
    with pytest.raises(HTTPException) as ei:
        devices_api.pair_begin({"ip": "", "port": 0})
    assert ei.value.status_code == 400


def test_pair_confirm_registers_device(isolated, monkeypatch):
    seed_pool_entry()
    captured = {}

    def fake_confirm(ip, port, code, name, timeout=15):
        captured.update(ip=ip, port=port, code=code, name=name)
        return {"token": "ee" * 32, "identity": "a4agent",
                "device_id": "a1b2c3d4e5f6", "cert_fingerprint": "f" * 64,
                "node_version": "0.5.0"}
    monkeypatch.setattr(node_client, "pair_confirm", fake_confirm)
    entry = devices_api.pair_confirm({"ip": "192.168.3.10", "port": 51888,
                                      "code": "123456"})
    # 固定身份名（B33）：confirm 的 name 恒为 a4agent
    assert captured["name"] == "a4agent"
    assert captured["code"] == "123456"
    # 登记名称取设备池 hostname；令牌经 DPAPI 可解
    assert entry["name"] == "PC-A"
    assert entry["hostname"] == "PC-A"
    assert "token_enc" not in entry
    st = devices_api.get_store()
    assert st.token_of("PC-A") == "ee" * 32
    assert st.fingerprint_of("PC-A") == "f" * 64
    assert entry["identity"] == "a4agent"
    # 明文令牌不落盘
    raw = st.path.read_text(encoding="utf-8")
    assert "ee" * 32 not in raw


def test_pair_confirm_requires_code(isolated, monkeypatch):
    monkeypatch.setattr(node_client, "pair_confirm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用")))
    with pytest.raises(HTTPException) as ei:
        devices_api.pair_confirm({"ip": "192.168.3.10", "port": 51888, "code": ""})
    assert ei.value.status_code == 400


def test_pair_confirm_tofu_mismatch_maps_error(isolated, monkeypatch):
    seed_pool_entry()

    def mismatch(ip, port, code, name, timeout=15):
        raise NodeError("TOFU_MISMATCH", "TOFU 配对确认指纹与实际连接不一致", None)
    monkeypatch.setattr(node_client, "pair_confirm", mismatch)
    with pytest.raises(HTTPException) as ei:
        devices_api.pair_confirm({"ip": "192.168.3.10", "port": 51888,
                                  "code": "123456"})
    assert ei.value.status_code == 502      # 客户端侧拒绝无 HTTP 语义
    assert ei.value.detail["code"] == "TOFU_MISMATCH"
    assert devices_api.get_store().list() == []   # 已放弃登记


# ---------------- 配对编排（被控端回环确认） ----------------

class _StubGuardian:
    def __init__(self, port):
        self._port = port

    @property
    def state(self):
        return "running"

    def read_port(self):
        return self._port


def test_pair_incoming_states(isolated, monkeypatch):
    # node 未运行 → idle / available=False
    out = devices_api.pair_incoming()
    assert out == {"available": False, "state": "idle"}
    # node 运行 → 透传 session 快照
    monkeypatch.setattr(node_proc, "get_guardian", lambda: _StubGuardian(51888))

    def fake_pair_request(ip, port, method, path, body=None, timeout=15,
                          expected_fp=None, loopback_tofu=False):
        assert (ip, method, path) == ("127.0.0.1", "GET", "/v1/pair/session")
        assert loopback_tofu is True
        return ({"state": "pending", "session_id": "s1", "source_ip": "192.168.3.9",
                 "expires_at": 999}, "f" * 64)
    monkeypatch.setattr(node_client, "pair_request", fake_pair_request)
    out = devices_api.pair_incoming()
    assert out["available"] is True
    assert out["state"] == "pending"
    assert out["source_ip"] == "192.168.3.9"


def test_pair_approve_and_reject(isolated, monkeypatch):
    monkeypatch.setattr(node_proc, "get_guardian", lambda: _StubGuardian(51888))

    def fake_pair_request(ip, port, method, path, body=None, timeout=15,
                          expected_fp=None, loopback_tofu=False):
        assert ip == "127.0.0.1" and loopback_tofu is True
        if path == "/v1/pair/approve":
            return ({"session_id": "s1", "code": "654321", "expires_at": 888}, "f" * 64)
        if path == "/v1/pair/reject":
            return ({"rejected": True, "session_id": "s1"}, "f" * 64)
        raise AssertionError(path)
    monkeypatch.setattr(node_client, "pair_request", fake_pair_request)
    assert devices_api.pair_approve()["code"] == "654321"
    assert devices_api.pair_reject()["rejected"] is True


# ---------------- 设备列表 / 忘记 / 指纹重置 ----------------

def test_device_list_forget_trust(isolated, monkeypatch):
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")
    out = devices_api.device_list()
    assert [d["name"] for d in out["devices"]] == ["PC-A"]
    devices_api.device_trust_reset("PC-A")
    assert st.fingerprint_of("PC-A") == ""
    devices_api.device_forget("PC-A")
    assert st.list() == []
    with pytest.raises(HTTPException) as ei:
        devices_api.device_forget("PC-A")
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        devices_api.device_ping("PC-A")
    assert ei.value.status_code == 404


# ---------------- 操作代理 ----------------

def test_exec_validation(isolated, monkeypatch):
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")
    with pytest.raises(HTTPException) as ei:
        devices_api.device_exec("PC-A", {"cmd": ""})
    assert ei.value.status_code == 400

    def fake_call(name, fn):
        return {"job_id": "job0001"}
    monkeypatch.setattr(devices_api, "_call", fake_call)
    out = devices_api.device_exec("PC-A", {"cmd": "echo hi"})
    assert out == {"job_id": "job0001"}


def test_screen_save_requires_valid_dir(isolated, monkeypatch):
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")
    with pytest.raises(HTTPException) as ei:
        devices_api.device_screen_save("PC-A", {"save_dir": "X:\\no\\such\\dir"})
    assert ei.value.status_code == 400


def test_get_requires_valid_save_dir(isolated):
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")
    with pytest.raises(HTTPException) as ei:
        devices_api.device_get("PC-A", path="C:\\x.txt", save_dir="")
    assert ei.value.status_code == 400


def test_call_drift_fallback_via_pool_hint(isolated):
    """UNREACHABLE 时用发现池同 device_id 线索重试一次，成功后更新存储地址（C4）。"""
    st = devices_api.get_store()
    st.register(name=None, device_id="a1b2c3d4e5f6", ip="192.168.3.10",
                port=51888, identity="a4agent", token="t1",
                cert_fingerprint="f" * 64, hostname="PC-A")
    seed_pool_entry(ip="192.168.3.99", port=51900)
    calls = []

    def fn(entry, client):
        calls.append((client.ip, client.port))
        if client.ip == "192.168.3.10":
            raise NodeError("UNREACHABLE", "无法连接 192.168.3.10:51888")
        return {"ok": True, "from": client.ip}
    out = devices_api._call("PC-A", fn)
    assert out["from"] == "192.168.3.99"
    assert calls == [("192.168.3.10", 51888), ("192.168.3.99", 51900)]
    moved = st.get("PC-A")
    assert moved["ip"] == "192.168.3.99"
    assert moved["port"] == 51900


def test_call_tofu_error_maps_without_drift(isolated):
    st = devices_api.get_store()
    st.register(name=None, device_id="a1b2c3d4e5f6", ip="192.168.3.10",
                port=51888, identity="a4agent", token="t1",
                cert_fingerprint="f" * 64, hostname="PC-A")
    seed_pool_entry(ip="192.168.3.99", port=51900)

    def fn(entry, client):
        raise NodeError("TOFU_MISMATCH", "TOFU 证书指纹变更", None)
    with pytest.raises(HTTPException) as ei:
        devices_api._call("PC-A", fn)
    assert ei.value.detail["code"] == "TOFU_MISMATCH"


def test_call_protocol_error_maps_status(isolated):
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")

    def fn(entry, client):
        raise NodeError("SANDBOX_DENIED", "工作空间白名单越界", 403)
    with pytest.raises(HTTPException) as ei:
        devices_api._call("PC-A", fn)
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "SANDBOX_DENIED"


def test_tasks_proxy_passes_identity_free_query(isolated, monkeypatch):
    """任务历史按协议 identity 口径：不传 identity = 按请求者身份过滤（§15.2）。"""
    st = devices_api.get_store()
    st.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                hostname="PC-A")
    seen = {}

    # _call 内部构造 NodeClient；拦截 NodeClient.request 最直接
    def fake_request(self, method, path, body=None, timeout=30, with_auth=True):
        seen["path"] = path
        return {"entries": [{"id": "j1", "cmd": "echo", "status": "done"}],
                "total": 1}
    monkeypatch.setattr(node_client.NodeClient, "request", fake_request)
    out = devices_api.device_tasks("PC-A", status="done", limit=10, offset=0)
    assert out["total"] == 1
    assert "identity=" not in seen["path"]       # identity 省略 = 请求者本人
    assert "status=done" in seen["path"]
    assert "limit=10" in seen["path"]
