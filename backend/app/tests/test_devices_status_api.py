"""设备状态查询 API 测试：聚合返回、错误透出、未配对 404。"""
import pytest
from fastapi import HTTPException

from backend.app.api.v1 import devices as devices_api


class _Store:
    def __init__(self, entries):
        self._entries = entries

    def list(self):
        return list(self._entries)

    def get(self, name):
        for entry in self._entries:
            if entry["name"] == name:
                return dict(entry)
        return None


def _session():
    return {
        "session_id": "s1", "event": "hook.stop",
        "event_label": "任务完成", "state": "done",
        "state_label": "已完成", "ts": 1.0, "age_seconds": 5,
        "stale": False, "agent": "Claude Code", "summary": "",
    }


def test_device_status_all(monkeypatch):
    monkeypatch.setattr(devices_api, "get_store", lambda: _Store([
        {"name": "pc-b", "hostname": "PC-B"},
    ]))
    monkeypatch.setattr(devices_api.status_store, "list_sessions",
                        lambda name: [_session()])
    watcher = devices_api.status_watcher.get_watcher()
    monkeypatch.setattr(watcher, "errors", lambda: {"pc-b": "boom"})
    out = devices_api.device_status_all()
    assert out["devices"][0]["device"] == "pc-b"
    assert out["devices"][0]["hostname"] == "PC-B"
    assert out["devices"][0]["last_error"] == "boom"
    assert out["devices"][0]["sessions"][0]["event_label"] == "任务完成"


def test_device_status_one(monkeypatch):
    monkeypatch.setattr(devices_api, "get_store", lambda: _Store([
        {"name": "pc-b", "hostname": "PC-B"},
    ]))
    monkeypatch.setattr(devices_api.status_store, "list_sessions",
                        lambda name: [_session()])
    watcher = devices_api.status_watcher.get_watcher()
    monkeypatch.setattr(watcher, "errors", lambda: {})
    out = devices_api.device_status("pc-b")
    assert out["device"] == "pc-b"
    assert out["sessions"][0]["state_label"] == "已完成"


def test_device_status_unknown(monkeypatch):
    monkeypatch.setattr(devices_api, "get_store", lambda: _Store([]))
    with pytest.raises(HTTPException) as exc:
        devices_api.device_status("missing")
    assert exc.value.status_code == 404
