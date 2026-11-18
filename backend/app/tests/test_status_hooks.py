"""hook/DSH 状态上报接入测试：关键事件写入、续聊跳过、WorkBuddy 跳过。"""
from backend.app.hooks import dispatch, dsh


def _cfg(**extra):
    cfg = {"hook": {"mode": "home"}, "desktop": False}
    cfg.update(extra)
    return cfg


def test_dispatch_stop_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    monkeypatch.setattr(dispatch.handlers, "handle_stop",
                        lambda *a, **kw: None)
    dispatch.dispatch({"hook_event_name": "Stop", "session_id": "s1",
                       "cwd": r"C:\work\proj"}, "claude", cfg=_cfg())
    assert calls[0]["event"] == "hook.stop"
    assert calls[0]["state"] == "done"
    assert calls[0]["session_id"] == "s1"
    assert calls[0]["workspace"] == r"C:\work\proj"


def test_dispatch_stop_resumed_skips(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    monkeypatch.setattr(dispatch.handlers, "handle_stop",
                        lambda *a, **kw: None)
    dispatch.dispatch({"hook_event_name": "Stop", "_resumed": True},
                      "claude", cfg=_cfg())
    assert calls == []


def test_dispatch_question_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    dispatch.dispatch({"hook_event_name": "PreToolUse",
                       "tool_name": "AskUserQuestion",
                       "session_id": "s1"}, "claude", cfg=_cfg())
    assert calls[0]["event"] == "hook.question"
    assert calls[0]["state"] == "waiting"


def test_dispatch_question_skips_workbuddy(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    dispatch.dispatch({"hook_event_name": "PreToolUse",
                       "tool_name": "AskUserQuestion",
                       "session_id": "s1"}, "workbuddy", cfg=_cfg())
    assert calls == []


def test_dispatch_permission_reports(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    dispatch.dispatch({"hook_event_name": "PermissionRequest",
                       "tool_name": "Bash",
                       "session_id": "s1"}, "claude", cfg=_cfg())
    assert calls[0]["event"] == "hook.permission"
    assert calls[0]["summary"] == "Bash"


def test_dispatch_ask_permission_does_not_double_report(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch.status_report, "report",
                        lambda **kw: calls.append(kw))
    dispatch.dispatch({"hook_event_name": "PermissionRequest",
                       "tool_name": "AskUserQuestion",
                       "session_id": "s1"}, "claude", cfg=_cfg())
    assert calls == []


def test_dsh_events_report(monkeypatch):
    calls = []
    monkeypatch.setattr(dsh.status_report, "report",
                        lambda **kw: calls.append(kw))
    monkeypatch.setattr(dsh.phone_config, "load", lambda: {"desktop": False})
    dsh.handle_task_complete({"sessionId": "d1", "turn": 1})
    dsh.handle_ask_user_question({"sessionId": "d1", "questions": []})
    dsh.handle_permission_request({"sessionId": "d1", "toolName": "Bash"})
    assert [c["event"] for c in calls] == [
        "dsh.complete", "dsh.question", "dsh.approval"]
    assert calls[0]["state"] == "done"
    assert calls[1]["state"] == "waiting"
    assert calls[2]["summary"] == "Bash"
