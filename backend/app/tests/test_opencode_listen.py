"""OpenCode 服务事件监听测试：终局路由、下发去重、权限转推与批办、遗留配置迁移。"""
import json

import pytest

from backend.app import opencode_client, opencode_listen
from backend.app.hooks import deskqueue


@pytest.fixture(autouse=True)
def _reset_pending():
    opencode_listen._pending.clear()
    opencode_listen._watcher_alive = False
    opencode_client._dispatched.clear()
    yield
    opencode_listen._pending.clear()
    opencode_listen._watcher_alive = False
    opencode_client._dispatched.clear()


@pytest.fixture(autouse=True)
def _stub_session_fetch(monkeypatch):
    """权限横幅要拉会话标题；默认桩掉防真实 HTTP，具体用例按需覆盖。"""
    monkeypatch.setattr(opencode_client, "get_session",
                        lambda *a, **k: {"title": "演示会话"})


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from backend.app import database
    from backend.app.phone import config as phone_config

    monkeypatch.setattr(phone_config, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(deskqueue, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(database, "get_data_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture
def out_cfg(data_dir):
    from backend.app.phone import config as phone_config

    return phone_config.save({
        "enabled": True, "topic": "a4ag-test",
        "hook": {"mode": "out", "timeout": 30},
    })


def _patch_conn(monkeypatch, base="http://127.0.0.1:49374", pwd="pw"):
    monkeypatch.setattr(opencode_client, "resolve_connection", lambda: (base, pwd))


# ---------------- 终局提醒 ----------------

def test_execution_event_notifies_with_mapped_status(monkeypatch):
    _patch_conn(monkeypatch)
    monkeypatch.setattr(opencode_client, "get_session", lambda *a, **k: {"title": "我的会话"})
    monkeypatch.setattr(opencode_client, "list_messages",
                        lambda *a, **k: [{"type": "assistant",
                                          "content": [{"type": "text", "text": "干完了"}]}])
    got = []
    monkeypatch.setattr(opencode_listen.task_notify, "notify", got.append)
    opencode_listen._on_event({"type": "session.execution.failed",
                               "data": {"sessionID": "ses_12345678abcd"}})
    assert len(got) == 1
    assert got[0]["status"] == "failed"
    assert got[0]["tool"] == "opencode"
    assert got[0]["session_id"] == "ses_12345678abcd"
    assert got[0]["last_output"] == "干完了"


def test_execution_event_survives_message_fetch_failure(monkeypatch):
    """消息流拉不到：退回只推标题，提醒本身不能丢。"""
    _patch_conn(monkeypatch)
    monkeypatch.setattr(opencode_client, "get_session", lambda *a, **k: {"title": "T"})

    def boom(*a, **k):
        raise opencode_client.OpenCodeError("消息流不可用")

    monkeypatch.setattr(opencode_client, "list_messages", boom)
    got = []
    monkeypatch.setattr(opencode_listen.task_notify, "notify", got.append)
    opencode_listen._on_event({"type": "session.execution.succeeded",
                               "data": {"sessionID": "ses_12345678abcd"}})
    assert len(got) == 1
    assert got[0]["last_output"] == ""


def test_execution_event_skips_dispatched_session(monkeypatch):
    """a4agent 下发的会话已有任务终态通知，事件订阅不得重复推。"""
    _patch_conn(monkeypatch)
    got = []
    monkeypatch.setattr(opencode_listen.task_notify, "notify", got.append)
    opencode_client.mark_dispatched("ses_dispatched1")
    opencode_listen._on_event({"type": "session.execution.succeeded",
                               "data": {"sessionID": "ses_dispatched1"}})
    assert got == []


def test_unrelated_events_are_ignored(monkeypatch):
    got = []
    monkeypatch.setattr(opencode_listen.task_notify, "notify", got.append)
    opencode_listen._on_event({"type": "session.idle", "data": {"sessionID": "ses_x"}})
    opencode_listen._on_event({"type": "server.connected", "data": {}})
    assert got == []


# ---------------- 权限批办 ----------------

def test_permission_asked_home_mode_only_desktop(data_dir, monkeypatch):
    """终端优先：只发桌面横幅，手机不参与——与六端语义一致。"""
    from backend.app.phone import config as phone_config

    phone_config.save({"enabled": True, "topic": "a4ag-test", "hook": {"mode": "home"}})
    pushed = []
    monkeypatch.setattr(opencode_listen.ntfy, "publish",
                        lambda *a, **k: pushed.append(a) or (True, ""))
    _patch_conn(monkeypatch)
    opencode_listen._handle_permission_asked(
        {"id": "per_1", "sessionID": "ses_1", "action": "shell",
         "resources": ["echo hi"]})
    assert pushed == []
    assert opencode_listen._pending == {}
    # 桌面横幅照发，统一样式：标题=会话标题，正文=类型\n应用名
    queued = list((data_dir / "notify-queue").glob("*.json"))
    assert len(queued) == 1
    req = json.loads(queued[0].read_text(encoding="utf-8"))
    assert req == {"title": "演示会话", "message": "权限申请\nOpenCode",
                   "ts": req["ts"]}


def test_permission_asked_out_mode_pushes_three_buttons(data_dir, out_cfg, monkeypatch):
    pushed = {}
    monkeypatch.setattr(opencode_listen.ntfy, "publish",
                        lambda cfg, title, message, actions=None:
                        pushed.update(title=title, message=message, actions=actions) or (True, ""))
    monkeypatch.setattr(opencode_listen, "_ensure_watcher", lambda: None)
    _patch_conn(monkeypatch, pwd="secret")
    opencode_listen._handle_permission_asked(
        {"id": "per_1", "sessionID": "ses_1", "action": "shell",
         "resources": ["echo hi", "rm -rf /"]})

    assert pushed["title"] == "OpenCode: shell"
    assert "echo hi" in pushed["message"]
    replies = [json.loads(a["body"])["reply"] for a in pushed["actions"]]
    assert replies == ["once", "always", "reject"]
    rid = json.loads(pushed["actions"][0]["body"])["requestId"]
    entry = opencode_listen._pending[rid]
    assert entry["permission_id"] == "per_1"
    assert entry["session_id"] == "ses_1"
    assert entry["password"] == "secret"


def test_permission_asked_without_topic_not_registered(data_dir):
    """手机推送未配置（无话题）：不登记待批，桌面横幅照发，交由 OpenCode 界面兜底。"""
    from backend.app.phone import config as phone_config

    phone_config.save({"enabled": True, "topic": "", "hook": {"mode": "out"}})
    opencode_listen._handle_permission_asked(
        {"id": "per_1", "sessionID": "ses_1", "action": "shell", "resources": []})
    assert opencode_listen._pending == {}
    assert list((data_dir / "notify-queue").glob("*.json"))


def test_permission_replied_clears_pending(data_dir, out_cfg, monkeypatch):
    monkeypatch.setattr(opencode_listen.ntfy, "publish", lambda *a, **k: (True, ""))
    monkeypatch.setattr(opencode_listen, "_ensure_watcher", lambda: None)
    _patch_conn(monkeypatch)
    opencode_listen._handle_permission_asked(
        {"id": "per_1", "sessionID": "ses_1", "action": "shell", "resources": []})
    assert opencode_listen._pending
    opencode_listen._handle_permission_replied({"requestID": "per_1"})
    assert opencode_listen._pending == {}


def test_match_reply_calls_reply_api(monkeypatch):
    calls = []

    def fake_api(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return 204, None

    monkeypatch.setattr(opencode_client, "api", fake_api)
    opencode_listen._pending["rid-1"] = {
        "session_id": "ses_1", "permission_id": "per_1",
        "base": "http://x", "password": "pw", "ts": 0}
    opencode_listen._match_reply(json.dumps({"requestId": "rid-1", "reply": "once"}))
    assert calls == [("POST",
                      "/api/session/ses_1/permission/per_1/reply",
                      {"decision": "once"})]
    assert opencode_listen._pending == {}


def test_match_reply_unknown_reply_defaults_to_reject(monkeypatch):
    """未知回执宁可拒绝，不能误放行。"""
    calls = []
    monkeypatch.setattr(opencode_client, "api",
                        lambda m, p, b=None, **k: calls.append((p, b)) or (204, None))
    opencode_listen._pending["rid-2"] = {
        "session_id": "ses_2", "permission_id": "per_2",
        "base": "http://x", "password": "pw", "ts": 0}
    opencode_listen._match_reply(json.dumps({"requestId": "rid-2", "reply": "huh"}))
    assert calls[0][1] == {"decision": "reject"}


def test_match_reply_ignores_free_text_and_unknown_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(opencode_client, "api",
                        lambda m, p, b=None, **k: calls.append(p) or (204, None))
    opencode_listen._match_reply("好的我同意")  # 自由文本无法映射，忽略
    opencode_listen._match_reply(json.dumps({"requestId": "nope", "reply": "once"}))
    assert calls == []


# ---------------- 遗留配置迁移 ----------------

def test_migrate_legacy_enabled_flag(data_dir):
    legacy = data_dir / "opencode.json"
    legacy.write_text(json.dumps({"enabled": True, "base_url": "", "remote": False}),
                      encoding="utf-8")
    opencode_listen.migrate_legacy_enabled()
    assert opencode_listen.listen_enabled() is True
    assert not legacy.exists()


def test_migrate_legacy_disabled_leaves_flag_off(data_dir):
    legacy = data_dir / "opencode.json"
    legacy.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    opencode_listen.migrate_legacy_enabled()
    assert opencode_listen.listen_enabled() is False
    assert not legacy.exists()  # 旧配置无论开关与否都已废弃，一并清理


def test_migrate_without_legacy_file_is_noop(data_dir):
    opencode_listen.migrate_legacy_enabled()
    assert opencode_listen.listen_enabled() is False
