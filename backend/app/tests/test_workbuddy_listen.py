"""WorkBuddy 会话雷达测试：增量读取、待作答判定、去重与容错。

雷达存在的唯一理由是时序：WorkBuddy 在提问面板期间不调 hook（详见模块头注释），
所以测试重点不是「能不能解析」，而是「同一提问只提醒一次、已作答的不提醒、
历史内容不回溯补弹」。
"""
import json
import time
from pathlib import Path

import pytest

from backend.app import workbuddy_listen as radar


def _now_ms() -> int:
    return int(time.time() * 1000)


def _call(call_id: str = "call_1", row_id: str = "fc-1",
          ts: int | None = None, name: str = "AskUserQuestion") -> dict:
    return {"id": row_id, "parentId": "parent", "timestamp": ts or _now_ms(),
            "type": "function_call", "name": name, "callId": call_id}


def _result(call_id: str = "call_1", parent: str = "fc-1") -> dict:
    return {"id": f"{parent}-r", "parentId": parent, "timestamp": _now_ms(),
            "type": "function_call_result", "name": "AskUserQuestion",
            "callId": call_id, "status": "completed"}


def _user(text: str) -> dict:
    return {"id": "m1", "timestamp": _now_ms(), "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}]}


def _append(path: Path, *rows: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    """一份已存在的活跃会话文件 + 隔离的提醒出口。"""
    projects = tmp_path / "projects"
    project = projects / "c-project"
    project.mkdir(parents=True)
    path = project / "session-1.jsonl"
    path.write_text("", encoding="utf-8")
    _append(path, _user("把详情页改成左右双栏"))

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(radar, "projects_dir", lambda: projects)
    monkeypatch.setattr(radar.deskqueue, "queue_notify",
                        lambda title, message: queued.append((title, message)) or True)
    monkeypatch.setattr(radar, "_workbuddy_registered", lambda: True)
    monkeypatch.setattr(radar, "_phone_cfg", lambda: {"desktop": True})
    radar.reset_state()
    return path, queued


def test_reminds_once_when_panel_opens(session_file):
    path, queued = session_file
    assert radar.scan_once() == 0  # 首次见面：历史内容不回溯
    _append(path, _call())
    assert radar.scan_once() == 1
    assert queued == [("把详情页改成左右双栏", "问题作答\nWorkBuddy")]
    assert radar.scan_once() == 0  # 同一调用不重复提醒
    assert len(queued) == 1


def test_no_remind_when_answer_already_arrived(session_file):
    path, queued = session_file
    radar.scan_once()
    _append(path, _call(), _result())  # 极速作答：call 与 result 落在同一批
    assert radar.scan_once() == 0
    assert queued == []


def test_result_after_reminder_is_silent(session_file):
    path, queued = session_file
    radar.scan_once()
    _append(path, _call())
    assert radar.scan_once() == 1
    _append(path, _result())
    assert radar.scan_once() == 0
    assert len(queued) == 1


def test_other_tools_ignored(session_file):
    path, queued = session_file
    radar.scan_once()
    _append(path, _call(name="TaskCreate"), _call(call_id="call_2", row_id="fc-2", name="Bash"))
    assert radar.scan_once() == 0
    assert queued == []


def test_partial_line_waits_for_completion(session_file):
    path, queued = session_file
    radar.scan_once()
    line = json.dumps(_call(), ensure_ascii=False) + "\n"
    head, tail = line[:30], line[30:]
    with path.open("a", encoding="utf-8") as fh:
        fh.write(head)  # 宿主写到一半
    assert radar.scan_once() == 0
    with path.open("a", encoding="utf-8") as fh:
        fh.write(tail)
    assert radar.scan_once() == 1


def test_truncated_file_restarts_without_crash(session_file):
    path, queued = session_file
    _append(path, _call())
    radar.scan_once()  # 记下基线位置
    path.write_text("", encoding="utf-8")  # 会话被重写（换实现/清空）
    assert radar.scan_once() == 0
    _append(path, _call(call_id="call_9", row_id="fc-9"))
    assert radar.scan_once() == 1


def test_history_before_startup_not_replayed(session_file):
    path, queued = session_file
    radar.scan_once()
    stale = _now_ms() - radar._HISTORY_SKIP_MS - 60_000
    _append(path, _call(ts=stale))
    assert radar.scan_once() == 0
    assert queued == []


def test_desktop_switch_off_means_no_queue(session_file, monkeypatch):
    path, queued = session_file
    radar.scan_once()
    _append(path, _call())
    monkeypatch.setattr(radar, "_phone_cfg", lambda: {"desktop": False})
    assert radar.scan_once() == 1  # scan_once 只负责判定，开关由 _loop 守着
    assert len(queued) == 1


def test_topic_falls_back_when_no_user_message(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    project = projects / "c-project"
    project.mkdir(parents=True)
    path = project / "session-2.jsonl"
    path.write_text("", encoding="utf-8")
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(radar, "projects_dir", lambda: projects)
    monkeypatch.setattr(radar.deskqueue, "queue_notify",
                        lambda title, message: queued.append((title, message)) or True)
    monkeypatch.setattr(radar, "_workbuddy_registered", lambda: True)
    monkeypatch.setattr(radar, "_phone_cfg", lambda: {"desktop": True})
    radar.reset_state()
    radar.scan_once()
    _append(path, _call())
    assert radar.scan_once() == 1
    assert queued[0][0] == "a4agent 通知"  # 无用户消息时退回兜底标题
