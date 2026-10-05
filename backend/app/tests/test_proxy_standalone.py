"""代理进程生命周期测试：token 只能发给写它的那个进程。

回归的是真机踩到过的假可用：升级/崩溃后端口被旧代理占着，新代理写了状态文件，
`ensure_proxy_running()` 照发旧 token，dsh / Codex 一律 401 invalid bearer token，
而 `/proxy/status` 却报「运行中」。
"""
import json

import pytest

from backend.app import proxy_standalone as ps


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    status = tmp_path / "proxy.json"
    monkeypatch.setattr(ps, "_status_file", lambda: status)
    monkeypatch.setattr(ps, "_refresh_upstream", lambda port: None)
    killed = []
    monkeypatch.setattr(ps, "_kill_owner", lambda port: killed.append(port))
    return {"status": status, "killed": killed}


def _write_status(path, **kw):
    path.write_text(json.dumps({"port": 17890, "token": "tok-old", "pid": 22092, **kw}), encoding="utf-8")


@pytest.mark.parametrize(
    "owner,file_pid,expect",
    [
        (22092, 22092, True),     # 属主就是写 token 的进程
        (26120, 22092, False),    # 孤儿占端口：token 没人认
        (None, 22092, True),      # 反查不到属主 → 不误杀
        (26120, None, True),      # 状态文件没记 pid → 维持原判定
    ],
)
def test_token_owner_ok_rules(monkeypatch, owner, file_pid, expect):
    monkeypatch.setattr(ps, "_port_owner_pid", lambda port: owner)
    assert ps._token_owner_ok({"port": 17890, "pid": file_pid}) is expect


def test_stale_owner_forces_proxy_restart(sandbox, monkeypatch):
    _write_status(sandbox["status"])
    monkeypatch.setattr(ps, "_port_alive", lambda port: True)
    monkeypatch.setattr(ps, "_proxy_compatible", lambda port: True)
    # 端口属主已经不是写 token 的那个进程
    monkeypatch.setattr(ps, "_port_owner_pid", lambda port: 26120)

    def fake_spawn():
        _write_status(sandbox["status"], token="tok-new", pid=26120)

    monkeypatch.setattr(ps, "_spawn", fake_spawn)

    result = ps.ensure_proxy_running()
    assert sandbox["killed"] == [17890]
    assert result["token"] == "tok-new"
    assert result["base_url"] == "http://127.0.0.1:17890"


def test_matching_owner_reuses_running_proxy(sandbox, monkeypatch):
    _write_status(sandbox["status"])
    monkeypatch.setattr(ps, "_port_alive", lambda port: True)
    monkeypatch.setattr(ps, "_proxy_compatible", lambda port: True)
    monkeypatch.setattr(ps, "_port_owner_pid", lambda port: 22092)
    monkeypatch.setattr(ps, "_spawn", lambda: pytest.fail("不应重启代理"))

    result = ps.ensure_proxy_running()
    assert result["token"] == "tok-old"
    assert sandbox["killed"] == []


def test_incompatible_build_still_killed(sandbox, monkeypatch):
    """原有行为不回退：端口上是不支持 Responses 的旧构建仍要杀掉重启。"""
    _write_status(sandbox["status"])
    state = {"compatible": False}

    def fake_compat(port):
        return state["compatible"]

    def fake_spawn():
        state["compatible"] = True          # 换上新构建才兼容
        _write_status(sandbox["status"], token="tok-new")

    monkeypatch.setattr(ps, "_port_alive", lambda port: True)
    monkeypatch.setattr(ps, "_proxy_compatible", fake_compat)
    monkeypatch.setattr(ps, "_port_owner_pid", lambda port: 22092)
    monkeypatch.setattr(ps, "_spawn", fake_spawn)

    assert ps.ensure_proxy_running()["token"] == "tok-new"
    assert sandbox["killed"] == [17890]
