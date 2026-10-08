"""无头任务执行器测试：用真实子进程当引擎，覆盖状态机、超时、取消与回收。"""
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import task_engines, task_notify, task_runner
from backend.app.database import Base
from backend.app.models import AgentTask


@pytest.fixture(autouse=True)
def _reset_runner_globals():
    """任务 id 在各测试的独立库里都从 1 重新计数，而 _cancelled / _shutdown_reason
    是模块级单例——前一个文件的取消测试留下的残留会把这里同 id 的任务误判为取消。"""
    task_runner._cancelled.clear()
    task_runner._shutdown_reason.clear()
    yield
    task_runner._cancelled.clear()
    task_runner._shutdown_reason.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'runner.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", maker)
    events = []
    monkeypatch.setattr(task_notify, "notify", lambda event: events.append(event))
    yield SimpleNamespace(maker=maker, events=events, tmp=tmp_path)
    maker().close()


def _fake_engine(monkeypatch, code):
    """把 build_argv 换成执行 Python 片段，stdout 即引擎产出。"""
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: sys.executable)
    monkeypatch.setattr(task_engines, "build_argv", lambda tool, prompt: [sys.executable, "-c", code])


def _new_task(db, **kw):
    kw.setdefault("status", task_runner.PENDING)
    kw.setdefault("timeout_seconds", 60)
    task = AgentTask(prompt=kw.pop("prompt", "干活"), tool=kw.pop("tool", "pi"), **kw)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _pi_jsonl(text, stop="stop"):
    """生成「假引擎」脚本：像 pi 一样显式按 UTF-8 输出一行 JSONL。

    不加 reconfigure 时，Windows 上的 Python 子进程会按 GBK 落盘，
    而引擎产出按约定始终是 UTF-8。
    """
    import json

    line = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "stopReason": stop,
                "usage": {"totalTokens": 10, "cost": {"total": 0.001}},
            },
        },
        ensure_ascii=False,
    )
    return (
        "import sys;"
        "sys.stdout.reconfigure(encoding='utf-8');"
        "print(" + json.dumps(line, ensure_ascii=False) + ")"
    )


def test_success_path_writes_output_and_notifies(env, monkeypatch):
    _fake_engine(monkeypatch, _pi_jsonl("任务完成"))
    db = env.maker()
    task = _new_task(db)

    task_runner._execute(task.id)

    db.expire_all()
    row = db.get(AgentTask, task.id)
    assert row.status == task_runner.SUCCESS
    assert row.exit_code == 0
    assert row.error_summary == ""
    assert row.finished_at is not None
    assert "任务完成" in row.output_path or row.output_path.endswith(".jsonl")
    from pathlib import Path

    assert "任务完成" in Path(row.output_path).read_text(encoding="utf-8")
    assert env.events and env.events[0]["status"] == task_runner.SUCCESS
    db.close()


def test_model_error_marks_task_failed(env, monkeypatch):
    _fake_engine(monkeypatch, _pi_jsonl("", stop="error"))
    db = env.maker()
    task = _new_task(db)

    task_runner._execute(task.id)
    db.expire_all()
    row = db.get(AgentTask, task.id)
    assert row.status == task_runner.FAILED
    assert "引擎执行出错" in row.error_summary
    assert env.events[0]["status"] == task_runner.FAILED
    db.close()


def test_timeout_kills_process_and_records_timeout(env, monkeypatch):
    _fake_engine(monkeypatch, "import time; time.sleep(30); print('too late')")
    monkeypatch.setattr(task_runner, "_clamp_timeout", lambda value: 1)
    db = env.maker()
    task = _new_task(db, timeout_seconds=30)

    task_runner._execute(task.id)
    db.expire_all()
    row = db.get(AgentTask, task.id)
    assert row.status == task_runner.TIMEOUT
    assert "1 秒" in row.error_summary
    assert task_runner.running_count() == 0
    db.close()


def test_cancel_running_task(env, monkeypatch):
    _fake_engine(monkeypatch, "import time; time.sleep(20); print('x')")
    db = env.maker()
    task = _new_task(db)
    db.close()

    import threading

    worker = threading.Thread(target=task_runner._execute, args=(task.id,))
    worker.start()
    deadline = datetime.now().timestamp() + 15
    while task_runner.running_count() == 0 and datetime.now().timestamp() < deadline:
        import time

        time.sleep(0.05)

    control = env.maker()
    result = task_runner.cancel(task.id, control)
    assert result["cancelled"] is True
    worker.join(timeout=30)

    check = env.maker()
    row = check.get(AgentTask, task.id)
    assert row.status == task_runner.CANCELLED
    assert task_runner.running_count() == 0
    control.close()
    check.close()


def test_shutdown_all_marks_running_tasks_failed(env, monkeypatch):
    _fake_engine(monkeypatch, "import time; time.sleep(20); print('x')")
    db = env.maker()
    task = _new_task(db)
    db.close()

    import threading

    worker = threading.Thread(target=task_runner._execute, args=(task.id,))
    worker.start()
    deadline = datetime.now().timestamp() + 15
    while task_runner.running_count() == 0 and datetime.now().timestamp() < deadline:
        import time

        time.sleep(0.05)

    killed = task_runner.shutdown_all("应用退出")
    worker.join(timeout=30)
    assert killed == 1

    check = env.maker()
    row = check.get(AgentTask, task.id)
    assert row.status in (task_runner.FAILED, task_runner.CANCELLED)
    if row.status == task_runner.FAILED:
        assert "应用退出" in row.error_summary
    check.close()


def test_shutdown_all_marks_pending_tasks_failed(env):
    """退出时没开跑的排队任务要落终态，不能留永远 pending 的僵尸行。"""
    db = env.maker()
    task = _new_task(db)  # pending，从未提交执行
    assert task_runner.outstanding_count() == 1
    db.close()

    killed = task_runner.shutdown_all("应用退出")

    check = env.maker()
    row = check.get(AgentTask, task.id)
    assert row.status == task_runner.FAILED
    assert "未执行" in row.error_summary
    assert killed == 1
    assert task_runner.outstanding_count() == 0
    check.close()


def test_execute_refuses_to_start_during_shutdown(env, monkeypatch):
    """_shutting_down 置位后，线程池里残余的 worker 不得再拉起引擎进程。"""
    spawned = []

    def fake_popen(argv, **kw):
        spawned.append(argv)
        raise AssertionError("退出期间不允许 spawn 引擎进程")

    monkeypatch.setattr(task_runner.subprocess, "Popen", fake_popen)
    db = env.maker()
    task = _new_task(db)
    db.close()

    task_runner._shutting_down.set()
    try:
        task_runner._execute(task.id)
    finally:
        task_runner._shutting_down.clear()

    check = env.maker()
    row = check.get(AgentTask, task.id)
    assert row.status == task_runner.FAILED
    assert "未执行" in row.error_summary
    check.close()


def test_missing_row_is_ignored(env, monkeypatch):
    _fake_engine(monkeypatch, "print('x')")
    db = env.maker()
    task = _new_task(db)
    db.delete(task)
    db.commit()
    db.close()

    task_runner._execute(task.id)  # 不应抛错
    assert env.events == []


def test_payload_and_tail_output(env, monkeypatch, tmp_path):
    out = tmp_path / "3.jsonl"
    out.write_text('{"type":"agent_end"}\n', encoding="utf-8")
    db = env.maker()
    task = _new_task(
        db, status=task_runner.SUCCESS, output_path=str(out),
        started_at=datetime.now(), finished_at=datetime.now(),
    )
    payload = task_runner.task_payload(task, include_output=True)
    assert payload["tool_label"] == "pi"
    assert payload["status"] == task_runner.SUCCESS
    assert payload["duration_seconds"] == 0
    assert "agent_end" in payload["output"]
    assert payload["output_truncated"] is False
    db.close()
