"""会话交互（hook）测试：transcript 抽取、桌面队列、响应订阅、三类处理器、分发器、五端注册。"""
import io
import json
from pathlib import Path

import pytest

from backend.app.hooks import deskqueue, dispatch, handlers, register, response
from backend.app.hooks.transcript import (clamp_output, extract_last_output,
                                          resolve_last_output)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from backend.app.phone import config as phone_config
    from backend.app.database import get_data_dir as _g

    monkeypatch.setattr(phone_config, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(handlers, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(deskqueue, "get_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def hook_cfg(data_dir):
    from backend.app.phone import config as phone_config

    return phone_config.save({
        "enabled": True, "topic": "a4ag-test",
        "hook": {"mode": "out", "timeout": 30},
    })


# ---------------- transcript ----------------

def test_clamp_output():
    assert clamp_output("  ") is None
    assert clamp_output("abc") == "abc"
    assert len(clamp_output("x" * 2000)) == 1003


def test_extract_claude_and_zcode_style(tmp_path):
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": []}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "第一轮"}]}}),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1"},
            {"type": "text", "text": "最终回答"}]}}),
    ]
    f = tmp_path / "t.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")
    assert extract_last_output(str(f)) == "最终回答"


def test_extract_workbuddy_style(tmp_path):
    f = tmp_path / "w.jsonl"
    f.write_text(json.dumps({"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": "wb 输出"}]}), encoding="utf-8")
    assert extract_last_output(str(f)) == "wb 输出"


def test_extract_codex_style(tmp_path):
    lines = [
        json.dumps({"type": "response_item", "payload": {"type": "message",
                    "role": "assistant", "content": [{"type": "output_text", "text": "codex 消息"}]}}),
        json.dumps({"type": "event_msg", "payload": {"type": "task_complete",
                    "last_agent_message": "codex 完整"}}),
    ]
    f = tmp_path / "c.jsonl"
    f.write_text("\n".join(lines), encoding="utf-8")
    # message 即时写入为首选；task_complete 仅在文件稳定时覆盖为兜底
    assert extract_last_output(str(f)) == "codex 完整"
    f.write_text(lines[0], encoding="utf-8")
    assert extract_last_output(str(f)) == "codex 消息"


def test_resolve_prefers_direct_field():
    assert resolve_last_output({"last_assistant_message": "直送", "transcript_path": None}) == "直送"


# ---------------- 桌面队列 ----------------

def test_deskqueue_write_and_consume(data_dir):
    shown = []
    assert deskqueue.queue_notify("Claude Code", "有提问需要处理") is True
    assert deskqueue.queue_notify("Claude Code", "有权限请求需要处理") is True
    assert deskqueue.process_queue(lambda t, m: shown.append((t, m))) == 2
    assert shown == [("Claude Code", "有提问需要处理"), ("Claude Code", "有权限请求需要处理")]
    assert deskqueue.process_queue(lambda t, m: None) == 0  # 已清空


def test_deskqueue_drops_expired_and_corrupt(data_dir, monkeypatch):
    shown = []
    deskqueue.queue_notify("旧通知", "过期")
    deskqueue.queue_notify("新通知", "有效")
    # 把第一条的 ts 改到 2 小时前
    files = sorted((deskqueue.queue_dir()).glob("*.json"))
    old = json.loads(files[0].read_text(encoding="utf-8"))
    old["ts"] -= 2 * 3600 * 1000
    files[0].write_text(json.dumps(old), encoding="utf-8")
    corrupt = deskqueue.queue_dir() / "notify-broken.json"
    corrupt.write_text("{broken", encoding="utf-8")

    assert deskqueue.process_queue(lambda t, m: shown.append(t)) == 1
    assert shown == ["新通知"]


# ---------------- 响应订阅 ----------------

class _FakeStream:
    def __init__(self, lines):
        self._lines = [json.dumps(l).encode("utf-8") for l in lines]

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_parse_message_scalar_is_text():
    assert response._parse_message("4") == "4"
    assert response._parse_message('{"a":1}') == {"a": 1}
    assert response._parse_message("随手写的") == "随手写的"


def test_wait_for_response_matches_request_id(hook_cfg, monkeypatch):
    stream = _FakeStream([
        {"event": "open"},
        {"event": "keepalive"},
        {"event": "message", "message": json.dumps({"requestId": "wrong", "answer": "x"})},
        {"event": "message", "message": json.dumps({"requestId": "rid", "approved": True})},
    ])
    monkeypatch.setattr(response, "urlopen", lambda req, timeout: stream)
    msg = response.wait_for_response(hook_cfg, "rid", 5)
    assert msg == {"requestId": "rid", "approved": True}


def test_wait_for_response_accepts_free_text(hook_cfg, monkeypatch):
    stream = _FakeStream([{"event": "message", "message": "就选第二个吧"}])
    monkeypatch.setattr(response, "urlopen", lambda req, timeout: stream)
    msg = response.wait_for_response(hook_cfg, "rid", 5)
    assert msg == {"answer": "就选第二个吧"}


def test_wait_for_response_ignores_control_events(hook_cfg, monkeypatch):
    stream = _FakeStream([{"event": "open"}, {"event": "keepalive"}])
    monkeypatch.setattr(response, "urlopen", lambda req, timeout: stream)
    assert response.wait_for_response(hook_cfg, "rid", 1) is None


# ---------------- 处理器 ----------------

def _stub_ask(monkeypatch, published, answers):
    def fake_publish(cfg, title, message, timeout=8.0, actions=None):
        published.append((title, message, actions))
        return True, ""

    seq = iter(answers)
    monkeypatch.setattr(handlers.ntfy, "publish", fake_publish)
    monkeypatch.setattr(handlers.response, "wait_for_response",
                        lambda cfg, rid, t: next(seq))


def test_ask_claude_injects_updated_input(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [{"requestId": "r", "answer": "方案 B"}])
    input_payload = {"tool_input": {"questions": [{
        "question": "选哪个方案？", "header": "方案",
        "options": [{"label": "方案 A"}, {"label": "方案 B"}, {"label": "方案 C"}],
    }]}}
    out = handlers.handle_ask_user_question(input_payload, "Claude Code", "claude", 30, hook_cfg)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {"选哪个方案？": "方案 B"}
    # ≤3 选项走按钮：3 个 http 回执按钮
    assert len(published[0][2]) == 3
    assert published[0][0] == "Claude Code: 方案"


def test_ask_number_reply_maps_to_label(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [{"requestId": "r", "answer": "3"}])
    input_payload = {"tool_input": {"questions": [{
        "question": "Q", "options": [{"label": "甲"}, {"label": "乙"},
                                     {"label": "丙"}, {"label": "丁"}],
    }]}}
    out = handlers.handle_ask_user_question(input_payload, "Claude Code", "claude", 30, hook_cfg)
    assert out["hookSpecificOutput"]["updatedInput"]["answers"] == {"Q": "丙"}
    assert published[0][2] is None  # >3 选项降级为编号列表


def test_ask_codex_blocks_with_reason(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [{"requestId": "r", "answer": "用递归"}])
    input_payload = {"tool_input": {"questions": [{"question": "怎么写？",
                                                   "options": [{"label": "递归"}, {"label": "迭代"}]}]}}
    out = handlers.handle_ask_user_question(input_payload, "Codex", "codex", 30, hook_cfg)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "用递归" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_ask_timeout_falls_back_to_terminal(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [None])
    input_payload = {"tool_input": {"questions": [{"question": "Q",
                                                   "options": [{"label": "A"}]}]}}
    assert handlers.handle_ask_user_question(
        input_payload, "Claude Code", "claude", 30, hook_cfg) is None


def test_permission_approve_and_always(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [{"requestId": "r", "approved": True, "alwaysAllow": True}])
    input_payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"},
                     "permission_suggestions": [{"type": "allow", "commands": ["rm*"]}]}
    out = handlers.handle_permission_request(input_payload, "Claude Code", 30, hook_cfg)
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert out["hookSpecificOutput"]["decision"]["updatedPermissions"] == input_payload["permission_suggestions"]
    # Always Approve 按钮插在中间
    labels = [a["label"] for a in published[0][2]]
    assert labels == ["Approve", "Always Approve", "Deny"]


def test_permission_deny_and_message_format(hook_cfg, monkeypatch):
    published = []
    _stub_ask(monkeypatch, published, [{"requestId": "r", "approved": False}])
    input_payload = {"tool_name": "Edit", "tool_input": {"file_path": "D:\\x\\y.py"}}
    out = handlers.handle_permission_request(input_payload, "Claude Code", 30, hook_cfg)
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert published[0][1] == "D:\\x\\y.py"


def test_permission_asku_auto_allows(hook_cfg, monkeypatch):
    out = handlers.handle_permission_request(
        {"tool_name": "AskUserQuestion", "tool_input": {}}, "ZCode", 30, hook_cfg)
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_stop_pushes_with_last_output(hook_cfg, data_dir, monkeypatch):
    published = []
    monkeypatch.setattr(handlers.deskqueue, "queue_notify", lambda t, m: True)
    monkeypatch.setattr(handlers.ntfy, "publish",
                        lambda cfg, title, message, timeout=8.0, actions=None:
                        published.append((title, message)) or (True, ""))
    input_payload = {"session_id": "abcdef123456", "cwd": "D:\\proj",
                     "last_assistant_message": "修好了三处"}
    assert handlers.handle_stop(input_payload, "Claude Code", hook_cfg) is None
    assert published[0][0] == "Claude Code"
    assert "AI 最后输出" in published[0][1] and "修好了三处" in published[0][1]
    last = json.loads((data_dir / "last_session.json").read_text(encoding="utf-8"))
    assert last["session_id"] == "abcdef123456" and last["agent"] == "Claude Code"


def test_stop_resumed_skip(hook_cfg, monkeypatch):
    published = []
    monkeypatch.setattr(handlers.deskqueue, "queue_notify", lambda t, m: True)
    monkeypatch.setattr(handlers.ntfy, "publish",
                        lambda cfg, title, message, timeout=8.0, actions=None:
                        published.append(1) or (True, ""))
    handlers.handle_stop({"session_id": "x", "_resumed": True}, "Claude Code", hook_cfg)
    assert published == []


# ---------------- 分发器 ----------------

def test_dispatch_home_mode_does_not_block(hook_cfg, data_dir, monkeypatch):
    from backend.app.phone import config as phone_config
    phone_config.save({"hook": {"mode": "home"}})
    queued = []
    monkeypatch.setattr(dispatch.deskqueue, "queue_notify", lambda t, m: queued.append(m))
    out = dispatch.dispatch({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                             "tool_input": {"questions": [{"question": "Q"}]}}, "claude")
    assert out is None
    assert queued == ["有提问需要处理"]


def test_dispatch_zcode_asku_permission_skips_queue(data_dir, monkeypatch):
    from backend.app.phone import config as phone_config
    phone_config.save({"hook": {"mode": "out"}})
    queued = []
    monkeypatch.setattr(dispatch.deskqueue, "queue_notify", lambda t, m: queued.append(m))
    monkeypatch.setattr(dispatch.handlers, "handle_permission_request",
                        lambda inp, name, wait, cfg: {"allow": True})
    out = dispatch.dispatch({"hook_event_name": "PermissionRequest",
                             "tool_name": "AskUserQuestion"}, "zcode")
    assert out == {"allow": True}
    assert queued == []  # 提问的提醒归 PreToolUse，此处不重复


def test_dispatch_workbuddy_wait_clamped(hook_cfg, monkeypatch):
    from backend.app.phone import config as phone_config
    phone_config.save({"hook": {"mode": "out", "timeout": 120}})
    captured = {}
    monkeypatch.setattr(dispatch.handlers, "handle_permission_request",
                        lambda inp, name, wait, cfg: captured.update(wait=wait) or None)
    dispatch.dispatch({"hook_event_name": "PermissionRequest", "tool_name": "Bash"}, "workbuddy")
    assert captured["wait"] == 45  # 120 被收窄到宿主安全上限


def test_run_hook_cli_outputs_decision(hook_cfg, monkeypatch, capsys):
    from backend.app.phone import config as phone_config
    phone_config.save({"hook": {"mode": "out", "timeout": 30}})
    monkeypatch.setattr(dispatch.handlers, "handle_permission_request",
                        lambda inp, name, wait, cfg: {"ok": 1})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"hook_event_name": "PermissionRequest", "tool_name": "Bash"})))
    assert dispatch.run_hook_cli("claude") == 0
    assert json.loads(capsys.readouterr().out) == {"ok": 1}


def test_run_hook_cli_bad_payload_exits_silently(hook_cfg, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not-json"))
    assert dispatch.run_hook_cli("claude") == 0
    assert capsys.readouterr().out == ""


# ---------------- 注册 ----------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(register, "_settings_home", lambda: tmp_path)
    return tmp_path


def test_default_hook_command_dev_mode_passes_script_path(monkeypatch):
    """开发态 sys.executable 是 python.exe，必须带 desktop.py 路径。

    否则宿主执行 `python.exe hook workbuddy`，Python 会把 hook 当成待执行的
    脚本文件名，报 can't open file，hook 静默失效（实测坑）。
    """
    monkeypatch.setattr(register.sys, "frozen", False, raising=False)
    cmd = register.default_hook_command("workbuddy")
    assert "hook workbuddy" in cmd
    assert "desktop.py" in cmd, cmd
    # python.exe 与脚本路径各自带引号（路径含空格时不炸）
    assert cmd.startswith('"') and cmd.count('"') == 4


def test_default_hook_command_frozen_uses_exe(monkeypatch):
    monkeypatch.setattr(register.sys, "frozen", True, raising=False)
    monkeypatch.setattr(register.sys, "executable", r"C:\Program Files\a4agent\a4agent.exe")
    cmd = register.default_hook_command("workbuddy")
    assert cmd == '"C:\\Program Files\\a4agent\\a4agent.exe" hook workbuddy'
    assert "desktop.py" not in cmd


def test_register_repairs_stale_command(fake_home):
    """历史写坏的命令应被当前命令替换，而不是因幂等跳过留在宿主里。"""
    path = fake_home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    broken = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command",
         "command": '"C:\\ProgramMine\\a4agent\\.venv\\Scripts\\python.exe" hook'}]}]}}
    path.write_text(json.dumps(broken), encoding="utf-8")
    good = '"C:\\x\\a4agent.exe" hook'
    assert register.register_engine("claude", good)["changed"] is True
    data = json.loads(path.read_text(encoding="utf-8"))
    cmds = [h["command"] for b in data["hooks"]["Stop"] for h in b["hooks"]]
    assert cmds == [good]
    assert register.register_engine("claude", good)["changed"] is False  # 收敛后幂等


def test_register_and_unregister_claude(fake_home):
    cmd = '"C:\\Program Files\\a4agent\\a4agent.exe" hook'
    assert register.register_engine("claude", cmd)["changed"] is True
    assert register.register_engine("claude", cmd)["changed"] is False  # 幂等
    assert register.registration_status()["claude"]["registered"] is True
    settings = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "AskUserQuestion"
    assert register.unregister_engine("claude")["unregistered"] is True
    assert register.registration_status()["claude"]["registered"] is False


def test_register_does_not_touch_a4phone_commands(fake_home):
    path = fake_home / ".qoder" / "settings.json"
    path.parent.mkdir(parents=True)
    a4p_settings = {"hooks": {"Stop": [{"matcher": "*", "hooks": [
        {"type": "command", "command": "a4p hook qoder"}]}]}}
    path.write_text(json.dumps(a4p_settings), encoding="utf-8")
    register.register_engine("qoder", '"C:\\x\\a4agent.exe" hook qoder')
    data = json.loads(path.read_text(encoding="utf-8"))
    stop_cmds = [h["command"] for b in data["hooks"]["Stop"] for h in b["hooks"]]
    assert stop_cmds == ["a4p hook qoder", '"C:\\x\\a4agent.exe" hook qoder']
    register.unregister_engine("qoder")
    data = json.loads(path.read_text(encoding="utf-8"))
    stop_cmds = [h["command"] for b in data["hooks"]["Stop"] for h in b["hooks"]]
    assert stop_cmds == ["a4p hook qoder"]  # a4phone 的命令原样保留


def test_register_zcode_sets_enabled(fake_home):
    register.register_engine("zcode", '"C:\\x\\a4agent.exe" hook zcode')
    cfg = json.loads((fake_home / ".zcode" / "cli" / "config.json").read_text(encoding="utf-8"))
    assert cfg["hooks"]["enabled"] is True
    assert cfg["hooks"]["events"]["PreToolUse"][0]["matcher"] == "AskUserQuestion"


def _zcode_stop_cmands(path) -> list:
    events = json.loads(path.read_text(encoding="utf-8"))["hooks"]["events"]
    return [h["command"] for b in events["Stop"] for h in b["hooks"]]


def test_zcode_status_reflects_nested_events(fake_home):
    """ZCode 事件挂在 hooks.events 下，状态检测必须能识别，否则点「注册」没反应。"""
    assert register.registration_status()["zcode"]["registered"] is False
    register.register_engine("zcode", '"C:\\x\\a4agent.exe" hook zcode')
    assert register.registration_status()["zcode"]["registered"] is True
    register.unregister_engine("zcode")
    assert register.registration_status()["zcode"]["registered"] is False


def test_zcode_register_converges_duplicates(fake_home):
    """重复点「注册」不应把同一事件追加成多条 a4agent 规则。"""
    path = fake_home / ".zcode" / "cli" / "config.json"
    register.register_engine("zcode", '"C:\\x\\a4agent.exe" hook zcode')
    for _ in range(3):
        assert register.register_engine("zcode", '"C:\\x\\a4agent.exe" hook zcode')["changed"] is False
    assert _zcode_stop_cmands(path) == ['"C:\\x\\a4agent.exe" hook zcode']


def test_zcode_unregister_keeps_other_hooks(fake_home):
    """卸载只摘 a4agent 的块，用户自己的 a4p / 第三方 hook 原样保留。"""
    path = fake_home / ".zcode" / "cli" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"enabled": True, "events": {"Stop": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "a4p hook zcode"}]}]}},
        "model": "glm/config.json"}), encoding="utf-8")
    register.register_engine("zcode", '"C:\\x\\a4agent.exe" hook zcode')
    assert _zcode_stop_cmands(path) == ["a4p hook zcode", '"C:\\x\\a4agent.exe" hook zcode']
    register.unregister_engine("zcode")
    assert _zcode_stop_cmands(path) == ["a4p hook zcode"]
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["hooks"]["enabled"] is True and cfg["model"] == "glm/config.json"


def test_register_codex_toml_and_unregister(fake_home):
    toml_path = fake_home / ".codex" / "config.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text('# 用户手写配置\nmodel = "gpt-5"\n\n[features]\nother = 1\n',
                         encoding="utf-8")
    assert register.register_engine("codex", "C:\\x\\a4agent.exe hook codex")["changed"] is True
    text = toml_path.read_text(encoding="utf-8")
    assert "model = \"gpt-5\"" in text  # 原配置保留
    assert text.count("[features]") == 1  # 不重复表头
    assert "hooks = true" in text
    assert register.register_engine("codex", "C:\\x\\a4agent.exe hook codex")["changed"] is False
    register.unregister_engine("codex")
    text = toml_path.read_text(encoding="utf-8")
    assert "a4agent" not in text
    assert "model = \"gpt-5\"" in text


def test_codex_marker_block_refreshes_stale_command(fake_home):
    """marker 块在但命令已过期（如开发态补 desktop.py 路径）时必须重写，
    不能见到 marker 就 return False 让旧命令永久留在宿主里。"""
    toml_path = fake_home / ".codex" / "config.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text(
        'model = "gpt-5"\n\n'
        f"{register.CODEX_MARKER_START}\n"
        '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
        "command = '\"C:\\\\old\\\\python.exe\" hook codex'\n"
        f"{register.CODEX_MARKER_END}\n", encoding="utf-8")
    assert register.register_engine("codex", '"C:\\new\\a4agent.exe" hook codex')["changed"] is True
    text = toml_path.read_text(encoding="utf-8")
    assert '"C:\\new\\a4agent.exe" hook codex' in text
    assert "old" not in text
    assert 'model = "gpt-5"' in text  # 原配置保留
    assert text.count(register.CODEX_MARKER_START) == 1  # 不重复块
    # 内容一致则幂等，不再重写
    assert register.register_engine("codex", '"C:\\new\\a4agent.exe" hook codex')["changed"] is False


def test_status_reports_all_engines(fake_home):
    status = register.registration_status()
    assert set(status) == {"claude", "codex", "zcode", "qoder", "workbuddy"}
    assert all(not v["registered"] for v in status.values())


# ---------------- 桌面横幅总开关 ----------------

def test_desktop_default_on_and_persist(data_dir):
    """老配置（无 desktop 字段）默认开，不能因为新增开关就变静默。"""
    from backend.app.phone import config as phone_config

    assert phone_config.save({"hook": {"mode": "home"}})["desktop"] is True
    assert phone_config.save({"desktop": False})["desktop"] is False
    assert phone_config.save({"desktop": True})["desktop"] is True


def test_dispatch_skips_desktop_when_off(hook_cfg, monkeypatch):
    """桌面横幅关掉后不入队，但手机推送路径不受影响。"""
    from backend.app.phone import config as phone_config

    phone_config.save({"hook": {"mode": "home"}, "desktop": False})
    queued = []
    monkeypatch.setattr(dispatch.deskqueue, "queue_notify", lambda t, m: queued.append(m))
    out = dispatch.dispatch({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                             "tool_input": {"questions": [{"question": "Q"}]}}, "claude")
    assert out is None
    assert queued == []


def test_dispatch_permission_skips_desktop_when_off(hook_cfg, monkeypatch):
    from backend.app.phone import config as phone_config

    phone_config.save({"hook": {"mode": "home"}, "desktop": False})
    queued = []
    monkeypatch.setattr(dispatch.deskqueue, "queue_notify", lambda t, m: queued.append(m))
    dispatch.dispatch({"hook_event_name": "PermissionRequest",
                       "tool_name": "Bash"}, "claude")
    assert queued == []


def test_handle_stop_desktop_off_still_pushes_mobile(hook_cfg, monkeypatch):
    """关桌面只掐桌面弹窗，手机推送照发。"""
    published = []
    monkeypatch.setattr(handlers.deskqueue, "queue_notify", lambda t, m: True)
    monkeypatch.setattr(handlers.ntfy, "publish",
                        lambda cfg, title, message, timeout=8.0, actions=None:
                        published.append(title) or (True, ""))
    handlers.handle_stop({"session_id": "s1"}, "Claude Code", hook_cfg, desktop_on=False)
    assert published == ["Claude Code"]


# ---------------- 桌面队列容错 ----------------

def test_process_queue_survives_unlink_oserror(data_dir, monkeypatch):
    """删除失败抛OSError 时不能中断整轮循环。

    本机有 safe-delete shim 会把 unlink 改走回收站，失败时抛 OSError 而非
    FileNotFoundError，missing_ok=True 拦不住。一旦抛出，后续条目全部卡死。
    """
    deskqueue.queue_notify("Claude Code", "第一条")
    deskqueue.queue_notify("Codex", "第二条")

    real = Path.unlink
    calls = {"n": 0}

    def flaky(self, missing_ok=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(0x80070002, "系统找不到指定的文件")  # 首次删除失败
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky)
    shown = []
    assert deskqueue.process_queue(lambda t, m: shown.append((t, m))) == 2
    assert [t for t, _ in shown] == ["Claude Code", "Codex"]  # 第二条未被阻断


def test_process_queue_survives_show_error(data_dir, monkeypatch):
    """单条 show 抛异常不阻断后续条目，也不阻断删除。"""
    deskqueue.queue_notify("A", "一")
    deskqueue.queue_notify("B", "二")

    def flaky_show(title, message):
        if title == "A":
            raise RuntimeError("toast 失败")

    shown = []
    assert deskqueue.process_queue(
        lambda t, m: (shown.append(t), None)[1] if t == "B" else flaky_show(t, m)) == 1
    assert shown == ["B"]
    assert list(deskqueue.queue_dir().glob("*.json")) == []
