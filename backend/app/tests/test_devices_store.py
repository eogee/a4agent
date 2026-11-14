"""已配对设备存储测试（B10 DPAPI：令牌加密落盘、接口不回传明文、原子读写）。"""
import json

import pytest

from backend.app.devices import store as store_mod
from backend.app.devices.store import DeviceStore, unique_name


@pytest.fixture()
def store(tmp_path):
    return DeviceStore(tmp_path / "devices.json")


# ---------------- 命名 ----------------

def test_unique_name_suffix():
    assert unique_name(set(), "nb") == "nb"
    assert unique_name({"nb"}, "nb") == "nb-2"
    assert unique_name({"nb", "nb-2"}, "nb") == "nb-3"


def test_unique_name_fallback():
    assert unique_name(set(), "") == "device"
    assert unique_name({"device"}, "") == "device-2"


# ---------------- 登记 / 查询 ----------------

def test_register_and_list_roundtrip(store):
    entry = store.register(
        name=None, device_id="a1b2c3d4e5f6", ip="192.168.3.10", port=51888,
        identity="a4agent", token="ab" * 32, cert_fingerprint="f" * 64,
        hostname="PC-A")
    assert entry["name"] == "PC-A"
    assert "token_enc" not in entry
    assert "ab" * 32 not in json.dumps(entry)
    items = store.list()
    assert len(items) == 1
    assert items[0]["device_id"] == "a1b2c3d4e5f6"
    assert "token_enc" not in items[0]
    # 明文令牌不落盘
    raw = store.path.read_text(encoding="utf-8")
    assert "ab" * 32 not in raw
    assert "token_enc" in raw


def test_register_name_conflict_auto_suffix(store):
    store.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                   identity="a4agent", token="t1", cert_fingerprint="f" * 64,
                   hostname="PC-A")
    entry2 = store.register(name=None, device_id="b" * 12, ip="192.168.3.11",
                            port=51888, identity="a4agent", token="t2",
                            cert_fingerprint="f" * 64, hostname="PC-A")
    assert entry2["name"] == "PC-A-2"
    assert len(store.list()) == 2


def test_register_same_device_updates_in_place(store):
    first = store.register(name=None, device_id="a" * 12, ip="192.168.3.10",
                           port=51888, identity="a4agent", token="t1",
                           cert_fingerprint="f" * 64, hostname="PC-A")
    again = store.register(name=None, device_id="a" * 12, ip="192.168.3.99",
                           port=51900, identity="a4agent", token="t2",
                           cert_fingerprint="e" * 64, hostname="PC-A")
    assert again["name"] == first["name"]
    items = store.list()
    assert len(items) == 1
    assert items[0]["ip"] == "192.168.3.99"
    assert items[0]["port"] == 51900


def test_token_roundtrip_dpapi(store):
    store.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                   identity="a4agent", token="cd" * 32,
                   cert_fingerprint="f" * 64, hostname="PC-A")
    assert store.token_of("PC-A") == "cd" * 32
    assert store.token_of("no-such") == ""


def test_fingerprint_and_move(store):
    store.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                   identity="a4agent", token="t", cert_fingerprint="f" * 64,
                   hostname="PC-A")
    store.update_fingerprint("PC-A", "E" * 64)
    assert store.fingerprint_of("PC-A") == "e" * 64     # 统一小写
    store.move("PC-A", "192.168.3.77", 51999)
    entry = store.get("PC-A")
    assert entry["ip"] == "192.168.3.77"
    assert entry["port"] == 51999
    assert entry["last_seen"] is not None


def test_touch_and_remove(store):
    store.register(name=None, device_id="a" * 12, ip="192.168.3.10", port=51888,
                   identity="a4agent", token="t", cert_fingerprint="f" * 64,
                   hostname="PC-A")
    before = store.get("PC-A")["last_seen"]
    store.touch("PC-A")
    assert store.get("PC-A")["last_seen"] >= before
    assert store.remove("PC-A") is True
    assert store.remove("PC-A") is False
    assert store.get("PC-A") is None


def test_default_store_uses_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    saved = store_mod.store
    store_mod.store = None
    try:
        s = store_mod.get_store()
        assert s.path.parent == (tmp_path / "data")
        assert s.path.name == "devices.json"
        assert store_mod.get_store() is s        # 单例
    finally:
        store_mod.store = saved
