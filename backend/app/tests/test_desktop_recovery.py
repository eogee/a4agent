"""desktop_recovery 唤醒兜底逻辑单测。

不启窗口、不发真实 HTTP、不真杀进程：pid 活性与确认弹窗全部 monkeypatch，
只验证决策分支与调用次序（唤醒失败 → 清障 → 重启接管 / 退回提示）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import pytest

from backend.app import desktop_recovery as dr


@pytest.fixture()
def instance_record(monkeypatch, tmp_path):
    """把实例记录指向临时文件，返回写记录的小工具。"""
    path = tmp_path / "desktop.json"

    def _write(record: dict | None) -> None:
        if record is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(json.dumps(record), encoding="utf-8")

    monkeypatch.setattr(dr, "instance_file", lambda: path)
    return _write


@pytest.fixture()
def no_sleep(monkeypatch):
    """跳过兜底流程里的竞态等待，加快测试。"""
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def test_wake_stale_pid_fails_without_http(instance_record, monkeypatch):
    """记录指向已死 pid（陈旧记录）：直接失败，绝不发 HTTP 空等 3s。"""
    instance_record({"port": 18900, "pid": 424242})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: False)

    def _boom(*_a, **_kw):  # pragma: no cover - 被调用即失败
        raise AssertionError("陈旧记录不应发起 HTTP 唤醒")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert dr.wake_running_instance() is False


def test_wake_alive_pid_posts_and_restores(instance_record, monkeypatch):
    """记录 pid 验明正身：正常 POST 唤醒并按响应返回。"""
    instance_record({"port": 59950, "pid": 43948})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: True)

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"restored": true}'

    seen = {}

    def _fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    assert dr.wake_running_instance() is True
    assert seen["url"].startswith("http://127.0.0.1:59950/api/v1/desktop/wake")


def test_wake_without_record_is_false(instance_record):
    """无实例记录（文件缺失）：直接失败。"""
    instance_record(None)
    assert dr.wake_running_instance() is False


def test_wake_corrupt_record_is_false(instance_record, tmp_path):
    """记录损坏（非 JSON）：直接失败而不是抛异常。"""
    path = tmp_path / "desktop.json"
    path.write_text("not-json{", encoding="utf-8")
    assert dr.wake_running_instance() is False


def test_recover_restarts_on_stale_record(instance_record, monkeypatch, no_sleep):
    """实测事故形态：记录 pid 已死 → 清掉陈旧记录并重启自己接管。"""
    instance_record({"port": 18900, "pid": 424242})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: False)
    cleared, relaunched = [], []
    monkeypatch.setattr(dr, "clear_instance", lambda: cleared.append(True))
    monkeypatch.setattr(dr, "relaunch_detached", lambda: relaunched.append(True))

    assert dr.recover_or_restart() is True
    assert cleared == [True]
    assert relaunched == [True]


def test_recover_asks_before_killing_live_instance(instance_record, monkeypatch):
    """pid 活着且验明是同名进程：必须征询，拒绝时不杀也不重启。"""
    instance_record({"port": 59950, "pid": 43948})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: True)
    monkeypatch.setattr(dr, "confirm_kill", lambda _pid, **_kw: False)
    killed, relaunched = [], []
    monkeypatch.setattr(dr, "kill_process", lambda pid: killed.append(pid))
    monkeypatch.setattr(dr, "relaunch_detached", lambda: relaunched.append(True))

    assert dr.recover_or_restart() is False
    assert killed == []
    assert relaunched == []


def test_recover_kills_confirmed_live_instance(instance_record, monkeypatch, no_sleep):
    """用户确认后：杀进程树 → 等退出 → 清记录 → 重启接管。"""
    instance_record({"port": 59950, "pid": 43948})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: True)
    monkeypatch.setattr(dr, "confirm_kill", lambda pid, **_kw: pid == 43948)
    killed, cleared, relaunched = [], [], []
    monkeypatch.setattr(dr, "kill_process", lambda pid: killed.append(pid) or True)
    monkeypatch.setattr(dr, "wait_gone", lambda _pid, **_kw: True)
    monkeypatch.setattr(dr, "clear_instance", lambda: cleared.append(True))
    monkeypatch.setattr(dr, "relaunch_detached", lambda: relaunched.append(True))

    assert dr.recover_or_restart() is True
    assert killed == [43948]
    assert cleared == [True]
    assert relaunched == [True]


def test_recover_frozen_name_kill_path(instance_record, monkeypatch, no_sleep):
    """frozen 且记录陈旧：探测到同名残余 → 征询后按映像名清障再重启。"""
    from pathlib import Path

    exe_name = Path(sys.executable).name  # 探测按「当前入口同名」匹配
    instance_record({"port": 18900, "pid": 424242})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def _fake_run(cmd, **_kw):
        class _R:
            returncode = 0
            stdout = f"{exe_name}        43948 Console\n".encode()

        if cmd[0] == "tasklist":
            return _R()
        seen_calls.append(cmd)
        return _R()

    seen_calls: list[list[str]] = []
    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(dr, "confirm_kill_stale_by_name", lambda: True)
    cleared, relaunched = [], []
    monkeypatch.setattr(dr, "clear_instance", lambda: cleared.append(True))
    monkeypatch.setattr(dr, "relaunch_detached", lambda: relaunched.append(True))

    assert dr.recover_or_restart() is True
    assert cleared == [True]
    assert relaunched == [True]
    assert len(seen_calls) == 1  # 只剩 taskkill（tasklist 是探测）
    assert seen_calls[0][0] == "taskkill"
    assert "/IM" in seen_calls[0]


def test_recover_frozen_declined_name_kill(instance_record, monkeypatch, no_sleep):
    """frozen 且用户拒绝按名清障：不杀、不重启，交回调用方弹提示。"""
    from pathlib import Path

    exe_name = Path(sys.executable).name
    instance_record({"port": 18900, "pid": 424242})
    monkeypatch.setattr(dr, "pid_is_a4agent", lambda _pid: False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    def _fake_run(cmd, **_kw):
        class _R:
            returncode = 0
            stdout = f"{exe_name}        43948 Console\n".encode()

        return _R()

    import subprocess

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(dr, "confirm_kill_stale_by_name", lambda: False)
    relaunched = []
    monkeypatch.setattr(dr, "relaunch_detached", lambda: relaunched.append(True))

    assert dr.recover_or_restart() is False
    assert relaunched == []


def test_pid_image_name_rejects_invalid_pid():
    """非法/不可能存在的 pid：返回空串而不是抛异常。"""
    assert dr.pid_image_name(-1) == ""
    assert dr.pid_image_name(0) == ""
