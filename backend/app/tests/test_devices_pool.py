"""设备发现池测试（契约 §19.2 口径：帧校验、限频去重、离线判定、手动目标）。"""
import time

import pytest

from backend.app.devices import pool as pool_mod


def frame(**kw):
    """构造一组合法帧字段（可覆盖）。"""
    base = {
        "magic": "XDCAST", "proto": 1, "device_id": "a1b2c3d4e5f6",
        "hostname": "PC-A", "port": 51888, "version": "0.5.0",
        "caps": ["exec", "screen"], "ts": time.time(),
    }
    base.update(kw)
    return base


def encoded(**kw) -> bytes:
    import json
    return json.dumps(frame(**kw), ensure_ascii=False).encode("utf-8")


# ---------------- parse_frame 校验清单（§19.2 全量） ----------------

def test_parse_frame_ok():
    out = pool_mod.parse_frame(encoded(), "192.168.3.10")
    assert out is not None
    assert out["device_id"] == "a1b2c3d4e5f6"
    assert out["ip"] == "192.168.3.10"
    assert out["hostname"] == "PC-A"
    assert out["port"] == 51888


def test_parse_frame_rejects_oversize():
    big = encoded(hostname="x" * 600)
    assert len(big) > 512
    assert pool_mod.parse_frame(big, "192.168.3.10") is None


def test_parse_frame_rejects_bad_json_and_multiline():
    assert pool_mod.parse_frame(b"not json", "192.168.3.10") is None
    import json
    two = (json.dumps(frame()) + "\n" + json.dumps(frame())).encode("utf-8")
    assert pool_mod.parse_frame(two, "192.168.3.10") is None


def test_parse_frame_rejects_bad_magic_and_proto():
    assert pool_mod.parse_frame(encoded(magic="OTHER"), "1.2.3.4") is None
    assert pool_mod.parse_frame(encoded(proto=2), "1.2.3.4") is None


def test_parse_frame_rejects_bad_device_id():
    assert pool_mod.parse_frame(encoded(device_id=""), "1.2.3.4") is None
    assert pool_mod.parse_frame(encoded(device_id="g" * 12), "1.2.3.4") is None   # 非 hex
    assert pool_mod.parse_frame(encoded(device_id="a" * 13), "1.2.3.4") is None   # 超长


def test_parse_frame_rejects_empty_and_non_utf8():
    assert pool_mod.parse_frame(b"", "1.2.3.4") is None
    assert pool_mod.parse_frame(b"\xff\xfe\xfa", "1.2.3.4") is None


def test_parse_frame_lenient_on_display_fields():
    # 契约只要求五项校验；hostname/port/version 畸形不丢帧（不信任、仅展示）
    out = pool_mod.parse_frame(encoded(hostname=None, port="x", caps="nope"), "1.2.3.4")
    assert out is not None
    assert out["hostname"] == ""
    assert out["port"] == 0
    assert out["caps"] == []


# ---------------- 限频去重与池刷新 ----------------

def _tick_factory(start=1000.0):
    state = {"now": start}
    return lambda: state["now"], state


def test_pool_rate_limit_one_frame_per_second():
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    f = pool_mod.parse_frame(encoded(), "192.168.3.10")
    assert p.accept(f, "192.168.3.10") is True
    state["now"] += 0.5
    assert p.accept(f, "192.168.3.10") is False      # 1 秒内重复帧丢弃
    state["now"] += 0.6
    assert p.accept(f, "192.168.3.10") is True       # 跨秒接受


def test_pool_rate_limit_is_per_ip_and_device():
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    f = pool_mod.parse_frame(encoded(), "192.168.3.10")
    f2 = pool_mod.parse_frame(encoded(device_id="ffffffffffff"), "192.168.3.10")
    f3 = pool_mod.parse_frame(encoded(), "192.168.3.11")
    assert p.accept(f, "192.168.3.10") is True
    assert p.accept(f2, "192.168.3.10") is True      # 不同 device_id 不互相限频
    assert p.accept(f3, "192.168.3.11") is True      # 不同源 IP 不互相限频


def test_pool_rejected_frame_does_not_count_or_refresh():
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    f = pool_mod.parse_frame(encoded(port=51888), "192.168.3.10")
    assert p.accept(f, "192.168.3.10") is True
    state["now"] += 0.2
    f2 = pool_mod.parse_frame(encoded(port=60000), "192.168.3.10")
    assert p.accept(f2, "192.168.3.10") is False     # 限频帧不刷新池
    snap = p.snapshot()
    assert snap[0]["port"] == 51888
    assert snap[0]["frames"] == 1


def test_pool_refresh_updates_entry_and_frames():
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    f = pool_mod.parse_frame(encoded(), "192.168.3.10")
    p.accept(f, "192.168.3.10")
    state["now"] += 2
    f2 = pool_mod.parse_frame(encoded(port=51900, hostname="PC-A2"), "192.168.3.10")
    p.accept(f2, "192.168.3.10")
    snap = p.snapshot()
    assert len(snap) == 1
    assert snap[0]["port"] == 51900
    assert snap[0]["hostname"] == "PC-A2"
    assert snap[0]["frames"] == 2


def test_pool_offline_after_30s():
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    p.accept(pool_mod.parse_frame(encoded(), "192.168.3.10"), "192.168.3.10")
    assert p.snapshot()[0]["online"] is True
    state["now"] += 29
    assert p.snapshot()[0]["online"] is True
    state["now"] += 2       # 共 31 秒无帧
    assert p.snapshot()[0]["online"] is False


def test_pool_find_by_device_id():
    now, _ = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    assert p.find("a1b2c3d4e5f6") is None
    p.accept(pool_mod.parse_frame(encoded(), "192.168.3.10"), "192.168.3.10")
    hit = p.find("a1b2c3d4e5f6")
    assert hit and hit["hostname"] == "PC-A"


# ---------------- 手动目标 ----------------

def test_manual_targets_add_and_dedupe():
    m = pool_mod.ManualTargets()
    key = m.add("192.168.3.20", 51888)
    assert key == "192.168.3.20:51888"
    m.add("192.168.3.20", 51888)         # 重复添加不产生第二条
    assert len(m.list()) == 1
    with pytest.raises(ValueError):
        m.add("not-an-ip", 51888)
    with pytest.raises(ValueError):
        m.add("192.168.3.20", 0)
    with pytest.raises(ValueError):
        m.add("192.168.3.20", "abc")


def test_manual_targets_remove():
    m = pool_mod.ManualTargets()
    m.add("192.168.3.20", 51888)
    assert m.remove("192.168.3.20", 51888) is True
    assert m.remove("192.168.3.20", 51888) is False


def test_pool_snapshot_prunes_stale_entries():
    """快照时移除离线超过 PRUNE_AFTER_SECONDS 的条目（死设备残影清理）。"""
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    p.accept(pool_mod.parse_frame(encoded(), "192.168.3.10"), "192.168.3.10")
    p.accept(pool_mod.parse_frame(
        encoded(device_id="b2c3d4e5f6a1", hostname="PC-B"), "192.168.3.11"), "192.168.3.11")
    state["now"] += pool_mod.PRUNE_AFTER_SECONDS + 1     # 两台都超过移除阈值
    assert p.snapshot(now=state["now"]) == []
    assert p.find("a1b2c3d4e5f6") is None


def test_pool_snapshot_exclude_device_id():
    """快照排除本机 node 条目（exclude_device_id：设备页已有"本机被控端"区块）。"""
    now, state = _tick_factory()
    p = pool_mod.DevicePool(now_fn=now)
    p.accept(pool_mod.parse_frame(
        encoded(device_id="a1b2c3d4e5f6"), "127.0.0.1"), "127.0.0.1")
    p.accept(pool_mod.parse_frame(
        encoded(device_id="b2c3d4e5f6a1", hostname="PC-B"), "192.168.3.11"), "192.168.3.11")
    out = p.snapshot(now=state["now"], exclude_device_id="a1b2c3d4e5f6")
    assert [e["device_id"] for e in out] == ["b2c3d4e5f6a1"]
    out2 = p.snapshot(now=state["now"])                  # 不传排除参数时两台都在
    assert len(out2) == 2
