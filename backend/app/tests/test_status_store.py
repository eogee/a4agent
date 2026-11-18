"""A 侧状态存储测试：序号去重、每会话取最新、提醒去重、留存清理。"""
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.devices import status_schema, status_store
from backend.app.models import DeviceStatus


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'status.db'}")
    DeviceStatus.__table__.create(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _payload(session_id="s1", seq=1, ts=100.0,
             event="hook.stop", state="done"):
    return {
        "session_id": session_id, "seq": seq, "event": event,
        "state": state, "agent": "Claude Code", "workspace": "proj",
        "summary": "ok", "ts": ts,
    }


def test_upsert_newer_and_discard_older(db):
    changed, notify = status_store.upsert("pc-b", _payload(seq=2, ts=200), db)
    assert (changed, notify) == (True, True)
    changed, notify = status_store.upsert("pc-b", _payload(seq=1, ts=100), db)
    assert (changed, notify) == (False, False)
    sessions = status_store.list_sessions("pc-b", db=db)
    assert len(sessions) == 1
    assert sessions[0]["ts"] == 200
    assert sessions[0]["event_label"] == "任务完成"


def test_list_sessions_latest_per_session(db):
    status_store.upsert("pc-b", _payload(
        session_id="s1", seq=1, ts=100, event="hook.question"), db)
    status_store.upsert("pc-b", _payload(
        session_id="s1", seq=2, ts=200, event="hook.stop"), db)
    status_store.upsert("pc-b", _payload(
        session_id="s2", seq=1, ts=150, event="hook.permission"), db)
    sessions = status_store.list_sessions("pc-b", db=db)
    assert [s["session_id"] for s in sessions] == ["s1", "s2"]
    assert sessions[0]["event"] == "hook.stop"
    assert sessions[0]["event_label"] == "任务完成"
    assert sessions[0]["state_label"] == "已完成"


def test_stale_flag(db):
    now = time.time()
    status_store.upsert("pc-b", _payload(ts=now - 60), db)
    assert status_store.list_sessions("pc-b", db=db)[0]["stale"] is True


def test_notify_only_for_key_events_and_after_notified_seq(db):
    changed, notify = status_store.upsert(
        "pc-b", _payload(seq=1, event="hook.foo"), db)
    assert changed and not notify
    changed, notify = status_store.upsert(
        "pc-b", _payload(seq=2, event="hook.stop"), db)
    assert changed and notify
    status_store.mark_notified("pc-b", "s1", 2, db)
    changed, notify = status_store.upsert(
        "pc-b", _payload(seq=3, event="hook.stop"), db)
    assert changed and notify
    status_store.mark_notified("pc-b", "s1", 3, db)
    changed, notify = status_store.upsert(
        "pc-b", _payload(seq=3, event="hook.stop"), db)
    assert not changed and not notify


def test_cleanup_removes_old_rows(db):
    now = time.time()
    status_store.upsert("pc-b", _payload(
        session_id="old", ts=now - 8 * 86400), db)
    status_store.upsert("pc-b", _payload(session_id="new", ts=now), db)
    status_store.cleanup(now=now, db=db)
    sessions = status_store.list_sessions("pc-b", db=db)
    assert [s["session_id"] for s in sessions] == ["new"]


def test_cleanup_enforces_max_rows(db, monkeypatch):
    monkeypatch.setattr(status_schema, "MAX_ROWS_PER_DEVICE", 2)
    now = time.time()
    for i in range(3):
        status_store.upsert("pc-b", _payload(
            session_id=f"s{i}", ts=now + i), db)
    status_store.cleanup(now=now + 10, db=db)
    sessions = status_store.list_sessions("pc-b", db=db)
    assert len(sessions) == 2
