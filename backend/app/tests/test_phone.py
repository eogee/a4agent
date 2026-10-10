"""手机通知测试：配置读写校验、ntfy 推送桩、终态桥接、API 路由。"""
import io
import threading
from urllib.error import HTTPError, URLError

import pytest

from backend.app import task_notify
from backend.app.api.v1 import phone as phone_api
from backend.app.phone import config as phone_config
from backend.app.phone import ntfy as phone_ntfy
from backend.app.phone import notifier


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(phone_config, "get_data_dir", lambda: tmp_path)
    return tmp_path


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------- 配置 ----------------

def test_load_defaults_when_no_file(data_dir):
    cfg = phone_config.load()
    assert cfg["enabled"] is False
    assert cfg["server"] == "https://ntfy.sh"
    assert cfg["topic"] == ""
    assert cfg["events"] == {"success": True, "failed": True,
                             "timeout": True, "cancelled": False}


def test_ensure_topic_generates_and_persists(data_dir):
    first = phone_config.ensure_topic(phone_config.load())
    assert first["topic"].startswith("a4ag-")
    # 二次读取话题稳定不变（二维码不能漂）
    assert phone_config.load()["topic"] == first["topic"]


def test_save_load_roundtrip(data_dir):
    saved = phone_config.save({
        "enabled": True, "server": "https://ntfy.example.com/",
        "topic": "a4ag-abc", "token": "tk_1",
        "events": {"success": False, "failed": True},
    })
    assert saved["server"] == "https://ntfy.example.com"  # 尾斜杠归一
    loaded = phone_config.load()
    assert loaded["enabled"] is True
    assert loaded["token"] == "tk_1"
    # 未提到的时机保持默认值，不被整组覆盖丢掉
    assert loaded["events"]["failed"] is True
    assert loaded["events"]["success"] is False
    assert loaded["events"]["timeout"] is True


def test_validate_rejects_bad_server_and_topic(data_dir):
    assert phone_config.validate("ftp://x", "a4ag-abc")
    assert phone_config.validate("not-a-url", "")
    assert phone_config.validate("https://ntfy.sh", "ab")  # 太短
    assert phone_config.validate("https://ntfy.sh", "话题名")  # 非法字符
    assert phone_config.validate("https://ntfy.sh", "") == ""  # 空话题放行，由 ensure_topic 补
    assert phone_config.validate("https://ntfy.sh", "a4ag-abc") == ""


def test_urls_and_qr_payload(data_dir):
    cfg = {"server": "https://ntfy.sh", "topic": "a4ag-abc"}
    assert phone_config.subscribe_url(cfg) == "https://ntfy.sh/a4ag-abc"
    assert phone_config.qr_payload(cfg) == "ntfy://ntfy.sh/a4ag-abc"


# ---------------- ntfy 推送 ----------------

def test_publish_ok(data_dir, monkeypatch):
    monkeypatch.setattr(phone_ntfy, "urlopen", lambda req, timeout: _Resp(200))
    ok, reason = phone_ntfy.publish(
        {"server": "https://ntfy.sh", "topic": "a4ag-abc", "token": ""},
        "标题", "正文")
    assert ok is True and reason == ""


def test_publish_http_error_reason(data_dir, monkeypatch):
    def _raise(req, timeout):
        raise HTTPError("https://ntfy.sh/", 403, "Forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr(phone_ntfy, "urlopen", _raise)
    ok, reason = phone_ntfy.publish(
        {"server": "https://ntfy.sh", "topic": "a4ag-abc", "token": "bad"},
        "标题", "正文")
    assert ok is False and "403" in reason

    def _raise_429(req, timeout):
        raise HTTPError("https://ntfy.sh/", 429, "Too Many Requests", {}, io.BytesIO(b""))

    monkeypatch.setattr(phone_ntfy, "urlopen", _raise_429)
    ok, reason = phone_ntfy.publish(
        {"server": "https://ntfy.sh", "topic": "a4ag-abc", "token": ""},
        "标题", "正文")
    assert ok is False and "限速" in reason


def test_publish_network_error_never_raises(data_dir, monkeypatch):
    def _raise(req, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr(phone_ntfy, "urlopen", _raise)
    ok, reason = phone_ntfy.publish(
        {"server": "https://ntfy.sh", "topic": "a4ag-abc", "token": ""},
        "标题", "正文")
    assert ok is False and "connection refused" in reason


def test_publish_requires_topic(data_dir):
    ok, reason = phone_ntfy.publish({"server": "https://ntfy.sh", "topic": ""}, "t", "m")
    assert ok is False and "话题" in reason


# ---------------- 终态桥接 ----------------

def test_compose_success_has_no_error_line():
    title, message = notifier.compose({
        "id": 7, "tool": "claude", "status": "success",
        "prompt": "审查错误处理", "error": "",
    })
    # 统一样式：标题=话题名称，正文=通知类型\n应用名称（任务编号保留）
    assert title == "审查错误处理"
    assert message == "已完成\n任务 #7 · Claude Code"


def test_compose_failed_includes_error():
    title, message = notifier.compose({
        "id": 8, "tool": "codex", "status": "failed",
        "prompt": "跑测试", "error": "引擎无产出",
    })
    assert title == "跑测试"
    assert message.startswith("失败\n任务 #8 · Codex\n引擎无产出")


def test_compose_session_event_has_no_task_number():
    """OpenCode 会话事件没有本地任务号，第三行就是应用名。"""
    title, message = notifier.compose({
        "id": "oc:1234abcd", "tool": "opencode", "status": "success",
        "prompt": "重构登录页", "session_id": "ses_x", "error": "",
    })
    assert title == "重构登录页"
    assert message == "已完成\nOpenCode"


def test_compose_unknown_tool_falls_back():
    _, message = notifier.compose({"id": 1, "tool": "xx", "status": "success",
                                   "prompt": "p", "error": ""})
    assert "xx" in message


def test_compose_has_no_status_icon():
    """终态通知纯文字（用户决议去图标）：任何状态都不拼 emoji。"""
    for status in ("success", "failed", "timeout", "cancelled", "其他"):
        _, message = notifier.compose({"id": 1, "tool": "claude", "status": status,
                                       "prompt": "p", "error": ""})
        assert not any(ch in message for ch in ("✅", "❌", "⏱", "🚫", "🔔"))


def test_compose_last_output_only_on_request():
    """「AI 最后输出」只进手机推送（include_last_output=True）；桌面横幅短版不带。"""
    event = {"id": 1, "tool": "claude", "status": "success", "prompt": "p",
             "error": "", "last_output": "结论：可行"}
    _, short = notifier.compose(event)
    assert "AI 最后输出" not in short
    _, full = notifier.compose(event, include_last_output=True)
    assert "AI 最后输出" in full and "结论：可行" in full


def test_compose_last_output_truncated():
    event = {"id": 1, "tool": "claude", "status": "success", "prompt": "p",
             "error": "", "last_output": "字" * 1500}
    _, message = notifier.compose(event, include_last_output=True)
    assert ("字" * notifier._LAST_OUTPUT_MAX + "...") in message
    assert len(message) < notifier._LAST_OUTPUT_MAX + 100


def test_compose_without_last_output_unchanged():
    event = {"id": 1, "tool": "claude", "status": "success", "prompt": "p", "error": ""}
    _, message = notifier.compose(event, include_last_output=True)
    assert message == "已完成\n任务 #1 · Claude Code"  # 没有最后输出时不出现空段落


def test_format_notice_fallback_title():
    """话题名称为空兜底「a4agent 通知」；hook 链与任务链共用这一份拼装。"""
    title, message = notifier.format_notice("", "已完成", "Claude Code")
    assert title == "a4agent 通知"
    assert message == "已完成\nClaude Code"
    title, message = notifier.format_notice("  ", "权限申请", "OpenCode")
    assert title == "a4agent 通知"
    assert message == "权限申请\nOpenCode"


def test_on_event_disabled_skips(data_dir, monkeypatch):
    delivered = []
    monkeypatch.setattr(notifier, "_deliver", lambda cfg, ev: delivered.append(ev))
    notifier._on_event({"id": 1, "tool": "pi", "status": "success", "prompt": "p", "error": ""})
    assert delivered == []


def test_on_event_delivers_matching_status(data_dir, monkeypatch):
    delivered = []
    monkeypatch.setattr(notifier, "_deliver", lambda cfg, ev: delivered.append(ev))
    phone_config.save({"enabled": True, "topic": "a4ag-abc",
                       "events": {"success": True, "cancelled": False}})

    class _SyncThread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(notifier.threading, "Thread", _SyncThread)

    notifier._on_event({"id": 1, "tool": "pi", "status": "success", "prompt": "p", "error": ""})
    notifier._on_event({"id": 2, "tool": "pi", "status": "cancelled", "prompt": "p", "error": ""})
    assert [e["id"] for e in delivered] == [1]


def test_register_is_idempotent():
    before = len(task_notify._listeners)
    notifier.register()
    notifier.register()
    after = len(task_notify._listeners)
    assert after - before == 1
    assert notifier._on_event in task_notify._listeners


# ---------------- API 路由 ----------------

def test_api_get_config_ensures_topic(data_dir):
    out = phone_api.get_config()
    assert out.topic.startswith("a4ag-")
    assert out.enabled is False
    assert out.subscribe_url.endswith(out.topic)
    assert out.token_set is False


def test_api_put_config_partial_update(data_dir):
    phone_api.get_config()  # 先落一个话题
    out = phone_api.put_config(phone_api.schemas.PhoneConfigIn(
        enabled=True, events=phone_api.schemas.PhoneEventsIn(success=False)))
    assert out.enabled is True
    assert out.events["success"] is False
    assert out.events["timeout"] is True  # 未提及时的机保持原值
    loaded = phone_config.load()
    assert loaded["topic"]  # 话题未被清掉


def test_api_put_config_rejects_bad_server(data_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        phone_api.put_config(phone_api.schemas.PhoneConfigIn(server="not-a-url"))
    assert ei.value.status_code == 422


def test_api_put_config_token_set_and_clear(data_dir):
    out = phone_api.put_config(phone_api.schemas.PhoneConfigIn(token="tk_x"))
    assert out.token_set is True
    out = phone_api.put_config(phone_api.schemas.PhoneConfigIn(token=""))
    assert out.token_set is False


def test_api_regenerate_topic_changes_it(data_dir):
    old = phone_api.get_config().topic
    new = phone_api.regenerate_topic().topic
    assert new != old
    assert phone_config.load()["topic"] == new


def test_api_test_push_reports_result(data_dir, monkeypatch):
    calls = []

    def _fake_publish(cfg, title, message, timeout=8.0):
        calls.append((title, message))
        return True, ""

    monkeypatch.setattr(phone_ntfy, "publish", _fake_publish)
    out = phone_api.test_push()
    assert out.sent is True and out.detail == ""
    assert len(calls) == 1

    monkeypatch.setattr(phone_ntfy, "publish",
                        lambda cfg, t, m, timeout=8.0: (False, "HTTP 429（触发免费服务限速）"))
    out = phone_api.test_push()
    assert out.sent is False and "限速" in out.detail


def test_notifier_real_thread_delivery(data_dir, monkeypatch):
    """非桩线程的真派发：_deliver 在守护线程里被调用且不抛异常。"""
    phone_config.save({"enabled": True, "topic": "a4ag-abc"})
    done = threading.Event()
    monkeypatch.setattr(phone_ntfy, "urlopen", lambda req, timeout: _Resp(200))
    real_deliver = notifier._deliver

    def _spy(cfg, ev):
        real_deliver(cfg, ev)
        done.set()

    monkeypatch.setattr(notifier, "_deliver", _spy)
    notifier._on_event({"id": 9, "tool": "claude", "status": "success",
                        "prompt": "p", "error": ""})
    assert done.wait(timeout=5)


# ---------------- 桌面横幅通道 ----------------

def test_api_config_exposes_desktop_switch(data_dir):
    """桌面开关走同一份配置，GET 默认开、PUT 可关且落盘。"""
    out = phone_api.get_config()
    assert out.desktop is True

    off = phone_api.put_config(phone_api.schemas.PhoneConfigIn(desktop=False))
    assert off.desktop is False
    assert phone_config.load()["desktop"] is False
    assert phone_api.get_config().desktop is False

    phone_api.put_config(phone_api.schemas.PhoneConfigIn(desktop=True))
    assert phone_api.get_config().desktop is True


def test_hook_status_exposes_desktop(data_dir):
    phone_config.save({"topic": "a4ag-abc", "desktop": False})
    assert phone_api.hook_status().desktop is False


def test_test_desktop_respects_switch(data_dir, monkeypatch):
    """开关关着时点测试横幅，要给出可读原因而不是静默失败。"""
    phone_config.save({"topic": "a4ag-abc", "desktop": False})
    monkeypatch.setattr(phone_api, "_win_toast_probe", lambda: None, raising=False)
    out = phone_api.test_desktop()
    assert out.sent is False
    assert "关闭" in out.detail


def test_test_desktop_calls_show(data_dir, monkeypatch):
    import backend.app.win_toast as wt

    phone_config.save({"topic": "a4ag-abc", "desktop": True})
    shown = []
    monkeypatch.setattr(wt, "AVAILABLE", True)
    monkeypatch.setattr(wt, "show", lambda t, m, on_click=None: shown.append((t, m)) or True)
    out = phone_api.test_desktop()
    assert out.sent is True
    assert shown and "a4agent" in shown[0][0]
