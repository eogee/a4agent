"""B 侧状态写入测试：原子写、路径清洗、失败不抛、禁用开关。"""
import json

from backend.app.devices import status_report, status_schema


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_report_writes_atomic_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_STATUS_REPORT", "1")
    assert status_report.report(
        event="hook.stop", state="done", session_id="s-1",
        agent="Claude Code", workspace=r"C:\work\项目A",
        summary="任务已完成", root=tmp_path) is True
    data = _read(tmp_path / "s-1.json")
    assert data["schema"] == status_schema.SCHEMA_VERSION
    assert data["session_id"] == "s-1"
    assert data["event"] == "hook.stop"
    assert data["state"] == "done"
    assert data["workspace"] == "项目A"          # 只留目录名，不带完整路径
    assert data["summary"] == "任务已完成"
    assert data["seq"] > 0
    assert not list(tmp_path.glob("*.tmp"))


def test_report_overwrites_same_session_and_seq_grows(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_STATUS_REPORT", "1")
    status_report.report(event="hook.question", state="waiting",
                         session_id="s-1", root=tmp_path)
    first = _read(tmp_path / "s-1.json")
    status_report.report(event="hook.stop", state="done",
                         session_id="s-1", root=tmp_path)
    second = _read(tmp_path / "s-1.json")
    assert second["event"] == "hook.stop"
    assert second["seq"] > first["seq"]


def test_report_sanitizes_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_STATUS_REPORT", "1")
    status_report.report(event="hook.stop", state="done",
                         session_id="../../evil/../x", root=tmp_path)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert ".." not in files[0].name


def test_report_returns_false_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_STATUS_REPORT", "1")
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    assert status_report.report(event="hook.stop", state="done",
                                session_id="s", root=blocked) is False


def test_report_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_STATUS_REPORT", "0")
    assert status_report.report(event="hook.stop", state="done",
                                session_id="s", root=tmp_path) is False
    assert not list(tmp_path.glob("*.json"))


def test_parse_payload_tolerates_bom():
    """PowerShell 等工具写出的 UTF-8 BOM 不应让合法状态被判为损坏。"""
    raw = ('\ufeff{"session_id": "s", "event": "hook.stop", '
           '"state": "done", "seq": 1, "ts": 2}')
    payload = status_schema.parse_payload(raw)
    assert payload["session_id"] == "s"
    assert payload["event"] == "hook.stop"


def test_parse_payload_rejects_invalid_and_truncates():
    assert status_schema.parse_payload("not json") is None
    assert status_schema.parse_payload({"session_id": "s"}) is None
    payload = status_schema.parse_payload({
        "session_id": "s", "event": "hook.stop", "state": "done",
        "seq": "12", "ts": "34.5", "summary": "x" * 300,
        "workspace": r"C:\a\b"})
    assert payload["seq"] == 12
    assert payload["ts"] == 34.5
    assert len(payload["summary"]) == status_schema.MAX_SUMMARY
    assert payload["workspace"] == "b"
