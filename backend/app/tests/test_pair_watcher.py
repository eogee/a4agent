"""配对请求系统 toast 监听测试（B39）：pending 边沿触发、toast 不可用/异常
不影响轮询、node 未运行不发起请求、approve 成功路径的确认提醒。
全部用临时实例与 monkeypatch 注入，不弹真 toast、不起真 node。
"""
import pytest

from backend.app import win_toast
from backend.app.api.v1 import devices as devices_api
from backend.app.devices import node_client, node_proc
from backend.app.devices import pair_watcher
from backend.app.devices.pair_watcher import PairWatcher, _notify


class _StubGuardian:
    """guardian 替身：状态/端口可控（不触数据库目录）。"""

    def __init__(self, state="running", port=51888):
        self._state = state
        self._port = port

    @property
    def state(self):
        return self._state

    def read_port(self):
        return self._port


def _stub_session(monkeypatch, states):
    """按序返回 /v1/pair/session 快照；校验回环 GET 与 loopback TOFU 口径。"""
    seq = iter(states)

    def fake_pair_request(ip, port, method, path, body=None, timeout=15,
                          expected_fp=None, loopback_tofu=False):
        assert (ip, method, path) == ("127.0.0.1", "GET", "/v1/pair/session")
        assert loopback_tofu is True
        return ({"state": next(seq)}, "f" * 64)

    monkeypatch.setattr(node_client, "pair_request", fake_pair_request)


# ---------------- pending 边沿触发 ----------------

def test_pending_edge_notifies_once(monkeypatch):
    """idle→pending 触发提醒；pending→pending 不重复；再次边沿再提醒。"""
    watcher = PairWatcher()
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="running", port=51888))
    _stub_session(monkeypatch, ["idle", "pending", "pending", "approved",
                                "pending"])
    calls = []
    monkeypatch.setattr(pair_watcher, "_notify_pair_request",
                        lambda: calls.append("pending") or True)
    for _ in range(5):
        watcher._tick()
    assert calls == ["pending", "pending"]
    assert watcher._last_state == "pending"


def test_notify_failure_does_not_break_polling(monkeypatch):
    """提醒注入点抛异常：单拍不外抛、状态记忆推进、后续边沿仍提醒。"""
    watcher = PairWatcher()
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="running", port=51888))
    _stub_session(monkeypatch, ["pending", "idle", "pending"])
    calls = []

    def flaky_notify():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("toast down")
        return True

    monkeypatch.setattr(pair_watcher, "_notify_pair_request", flaky_notify)
    watcher._tick()          # pending 边沿：提醒抛异常，单拍不外抛
    watcher._tick()          # idle：无提醒
    watcher._tick()          # 再 pending：恢复提醒
    assert calls == [1, 1]
    assert watcher._last_state == "pending"


# ---------------- toast 容错口径 ----------------

def test_notify_swallows_toast_failures(monkeypatch):
    """_notify：不可用/抛异常/被拒绝一律返回 False，绝不抛出。"""
    monkeypatch.setattr(win_toast, "AVAILABLE", False)
    assert _notify("t", "m") is False
    monkeypatch.setattr(win_toast, "AVAILABLE", True)

    def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(win_toast, "show", boom)
    assert _notify("t", "m") is False
    monkeypatch.setattr(win_toast, "show", lambda *a, **k: False)
    assert _notify("t", "m") is False
    monkeypatch.setattr(win_toast, "show", lambda *a, **k: True)
    assert _notify("t", "m") is True


# ---------------- node 未运行不请求 ----------------

def test_no_request_when_node_not_running(monkeypatch):
    """node 未运行（stopped）或无端口时不发任何请求、记忆清零。"""

    def forbidden(*args, **kwargs):
        raise AssertionError("node 未运行不应发起请求")

    monkeypatch.setattr(node_client, "pair_request", forbidden)
    watcher = PairWatcher()
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="stopped", port=51888))
    watcher._tick()
    assert watcher._last_state is None
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="running", port=None))
    watcher._tick()
    assert watcher._last_state is None


def test_query_failure_keeps_state_memory(monkeypatch):
    """查询失败不推进状态记忆：恢复后 idle→pending 仍正常提醒、不误报。"""
    watcher = PairWatcher()
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="running", port=51888))
    flag = {"fail": False}

    def flaky_request(*args, **kwargs):
        if flag["fail"]:
            raise node_client.NodeError("UNREACHABLE", "连接失败")
        return ({"state": "idle"}, "f" * 64)

    monkeypatch.setattr(node_client, "pair_request", flaky_request)
    calls = []
    monkeypatch.setattr(pair_watcher, "_notify_pair_request",
                        lambda: calls.append(1) or True)
    watcher._tick()                 # idle → 记忆 idle
    flag["fail"] = True
    watcher._tick()                 # 查询失败：记忆保持 idle
    flag["fail"] = False
    watcher._tick()                 # 恢复 idle：不应触发
    assert calls == []
    assert watcher._last_state == "idle"


def test_loop_survives_tick_exceptions(monkeypatch):
    """守护循环：单拍异常不退出线程，下一拍继续。"""
    monkeypatch.setattr(pair_watcher, "POLL_INTERVAL_SECONDS", 0.01)
    watcher = PairWatcher()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 2:
            raise RuntimeError("boom")
        watcher.stop()

    monkeypatch.setattr(watcher, "_tick", flaky)
    watcher.start()
    watcher._thread.join(timeout=5)
    assert not watcher._thread.is_alive()
    assert len(calls) >= 3


# ---------------- approve 成功路径的确认提醒（devices.py 半边） ----------------

def test_pair_approve_sends_toast_and_tolerates_failure(monkeypatch):
    """approve 成功发第二条 toast（不含配对码）；提醒异常不阻断响应。"""
    monkeypatch.setattr(node_proc, "get_guardian",
                        lambda: _StubGuardian(state="running", port=51888))

    def fake_pair_request(ip, port, method, path, body=None, timeout=15,
                          expected_fp=None, loopback_tofu=False):
        assert (ip, method, path) == ("127.0.0.1", "POST", "/v1/pair/approve")
        assert loopback_tofu is True
        return ({"session_id": "s1", "code": "654321", "expires_at": 888},
                "f" * 64)

    monkeypatch.setattr(node_client, "pair_request", fake_pair_request)
    msgs = []
    monkeypatch.setattr(devices_api, "_notify_pair", msgs.append)
    out = devices_api.pair_approve()
    assert out["code"] == "654321"
    assert msgs == ["配对请求已确认，请在设备页查看配对码"]

    def boom(message):
        raise RuntimeError("toast down")

    monkeypatch.setattr(devices_api, "_notify_pair", boom)
    out2 = devices_api.pair_approve()
    assert out2["code"] == "654321"      # 提醒失败不阻断 approve 响应
