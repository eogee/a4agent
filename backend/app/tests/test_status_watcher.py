"""A 侧拉取器测试：拉取、入库、提醒、单设备失败隔离、staging 缓存。"""
import json
from urllib.parse import unquote

from backend.app.devices import status_watcher


def _device(name="pc-b", ip="192.168.1.2"):
    return {"name": name, "ip": ip, "port": 51888,
            "cert_fingerprint": "f" * 64, "hostname": "PC-B"}


class _Store:
    def __init__(self, devices):
        self._devices = devices

    def list(self):
        return list(self._devices)

    def token_of(self, name):
        return "token"


class _Client:
    def __init__(self, ip, port, token, fingerprint=None):
        self.ip = ip
        self.port = port
        self.token = token
        self.fingerprint = fingerprint
        self.paths = []

    def request(self, method, path, body=None, timeout=10):
        self.paths.append(path)
        decoded = unquote(path)
        if decoded.startswith("/v1/info"):
            return {"workspace_staging": r"C:\tmp"}
        if decoded.startswith("/v1/fs/list"):
            if decoded.endswith("tmp\\status"):
                return {"entries": [
                    {"name": "s1.json", "dir": False,
                     "path": r"C:\tmp\status\s1.json", "mtime": 10},
                    {"name": "ignore.txt", "dir": False,
                     "path": r"C:\tmp\status\ignore.txt", "mtime": 9},
                ]}
            return {"entries": [
                {"name": "status", "dir": True,
                 "path": r"C:\tmp\status", "mtime": 10},
            ]}
        if decoded.startswith("/v1/fs/read"):
            return {"text": json.dumps({
                "schema": 1, "session_id": "s1", "seq": 7,
                "event": "hook.stop", "state": "done",
                "ts": 100.0, "agent": "Claude Code",
                "workspace": "proj", "summary": "",
            })}
        raise AssertionError(decoded)


class _NoStatusClient(_Client):
    def request(self, method, path, body=None, timeout=10):
        decoded = unquote(path)
        if decoded.startswith("/v1/info"):
            return {"workspace_staging": r"C:\tmp"}
        if decoded.startswith("/v1/fs/list"):
            return {"entries": []}
        raise AssertionError(decoded)


class _SelectiveClient(_Client):
    def __init__(self, ip, port, token, fingerprint=None):
        if ip == "bad":
            raise RuntimeError("boom")
        super().__init__(ip, port, token, fingerprint)


def _patch_common(monkeypatch, watcher, devices, client_cls, cfg=None):
    monkeypatch.setattr(status_watcher, "get_store", lambda: _Store(devices))
    monkeypatch.setattr(status_watcher.node_client, "NodeClient", client_cls)
    monkeypatch.setattr(status_watcher.phone_config, "load",
                        lambda: cfg or {"desktop": False, "enabled": False})


def test_tick_pulls_and_stores(monkeypatch):
    watcher = status_watcher.StatusWatcher()
    _patch_common(monkeypatch, watcher, [_device()], _Client)
    calls, notified = [], []
    monkeypatch.setattr(
        status_watcher.status_store, "upsert",
        lambda device, payload: (calls.append((device, payload)) or (True, True)))
    monkeypatch.setattr(
        status_watcher.status_store, "mark_notified",
        lambda *args: notified.append(args))
    watcher._tick()
    assert calls[0][0] == "pc-b"
    assert calls[0][1]["session_id"] == "s1"
    assert calls[0][1]["event"] == "hook.stop"
    assert notified == [("pc-b", "s1", 7)]
    assert watcher.errors() == {}


def test_tick_notifies_desktop_and_ntfy_without_path(monkeypatch):
    watcher = status_watcher.StatusWatcher()
    _patch_common(monkeypatch, watcher, [_device()], _Client,
                  cfg={"desktop": True, "enabled": True, "topic": "t"})
    monkeypatch.setattr(status_watcher.status_store, "upsert",
                        lambda device, payload: (True, True))
    monkeypatch.setattr(status_watcher.status_store, "mark_notified",
                        lambda *args: None)
    sent = []
    monkeypatch.setattr(
        status_watcher.win_toast, "show",
        lambda title, message: (sent.append(("desktop", title, message))
                                or True))
    monkeypatch.setattr(
        status_watcher.ntfy, "publish",
        lambda cfg, title, message: (sent.append(("ntfy", title, message))
                                     or (True, "")))
    watcher._tick()
    assert [s[0] for s in sent] == ["desktop", "ntfy"]
    assert "任务完成" in sent[0][2]
    assert "C:\\" not in sent[0][2]


def test_tick_without_status_dir_is_quiet(monkeypatch):
    watcher = status_watcher.StatusWatcher()
    _patch_common(monkeypatch, watcher, [_device()], _NoStatusClient)
    calls = []
    monkeypatch.setattr(status_watcher.status_store, "upsert",
                        lambda device, payload: (calls.append(device)
                                                 or (True, False)))
    watcher._tick()
    assert calls == []
    assert watcher.errors() == {}


def test_tick_isolates_device_failure(monkeypatch):
    watcher = status_watcher.StatusWatcher()
    _patch_common(monkeypatch, watcher,
                  [_device("bad", "bad"), _device("good", "192.168.1.3")],
                  _SelectiveClient)
    calls = []
    monkeypatch.setattr(
        status_watcher.status_store, "upsert",
        lambda device, payload: (calls.append(device) or (True, False)))
    watcher._tick()
    assert calls == ["good"]
    assert watcher.errors()["bad"] == "boom"


def test_staging_path_cached(monkeypatch):
    watcher = status_watcher.StatusWatcher()
    clients = []

    class _CountingClient(_Client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    _patch_common(monkeypatch, watcher, [_device()], _CountingClient)
    monkeypatch.setattr(status_watcher.status_store, "upsert",
                        lambda device, payload: (True, False))
    watcher._tick()
    watcher._tick()
    info_calls = sum(1 for c in clients for p in c.paths
                     if p.startswith("/v1/info"))
    assert info_calls == 1
