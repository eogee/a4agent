"""无头任务接口测试：预检拦截、下发即返回、详情产出、取消与删除约束。"""
import json
from datetime import datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import task_engines, task_precheck, task_runner
from backend.app.api.v1 import tasks as tasks_api
from backend.app.crypto import encrypt_text
from backend.app.database import Base
from backend.app.models import AgentTask, Configuration, Provider

GOOD_PRECHECK = {
    "ok": True,
    "reason": "",
    "steps": [
        {"name": "engine", "label": "引擎可用", "ok": True, "detail": "pi 1.0.2", "ms": 12},
        {"name": "reachability", "label": "服务商连通", "ok": True, "detail": "HTTP 200", "ms": 40},
        {"name": "config_sync", "label": "端配置一致", "ok": True, "detail": "pi 正在使用 a4a_p1", "ms": 3},
    ],
    "ms": 55,
}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 不打真机：探测、连通与端配置全部桩化（真实行为在 test_task_precheck 覆盖）
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: r"C:\npm\pi.cmd")
    monkeypatch.setattr(task_precheck, "run", lambda *a, **kw: dict(GOOD_PRECHECK))
    monkeypatch.setattr(task_runner, "submit", lambda task_id: None)
    yield session
    session.close()


def _seed_config(db, *, api_type="anthropic"):
    p = Provider(name=f"prov-{api_type}", api_base="https://api.example.com", api_type=api_type)
    db.add(p)
    db.flush()
    c = Configuration(
        name="方案", provider_id=p.id, api_key_encrypted=encrypt_text("sk-1"),
        model="deepseek-chat", targets="pi", is_active=True,
    )
    db.add(c)
    db.commit()
    return c


def _body(prompt="审查这个仓库", tool="pi"):
    return tasks_api.schemas.TaskCreate(prompt=prompt, tool=tool)


# ---------------- 引擎探测 ----------------


def test_engines_endpoint_lists_p0_engines(db, monkeypatch):
    _seed_config(db)
    monkeypatch.setattr(task_engines, "_probe_cache", {})
    monkeypatch.setattr(
        task_engines.subprocess, "run",
        lambda argv, **kw: type("R", (), {"returncode": 0, "stdout": "1.0.2", "stderr": ""})(),
    )
    data = tasks_api.engines(db=db)
    tools = [e["tool"] for e in data["engines"]]
    assert tools == list(task_engines.ENGINES)
    assert data["engines"][0]["installed"] is True
    assert data["concurrency"] == task_runner.max_workers()
    assert data["active_config"]["model"] == "deepseek-chat"


# ---------------- 下发 ----------------


def test_create_task_requires_active_config(db):
    with pytest.raises(HTTPException) as exc:
        tasks_api.create_task(_body(), db=db)
    assert exc.value.status_code == 409
    assert "生效" in exc.value.detail


def test_create_task_returns_immediately_and_queues(db, monkeypatch):
    _seed_config(db)
    submitted = []
    monkeypatch.setattr(task_runner, "submit", lambda task_id: submitted.append(task_id))

    out = tasks_api.create_task(_body(), db=db)
    row = db.get(AgentTask, out["id"])
    assert row.status == task_runner.PENDING
    assert submitted == [row.id]
    assert out["status"] == task_runner.PENDING
    assert out["tool_label"] == "pi"
    assert out["precheck"]["ok"] is True
    assert out["timeout_seconds"] == task_runner.DEFAULT_TIMEOUT_SECONDS


def test_create_task_blocked_by_precheck_records_failed_row(db, monkeypatch):
    _seed_config(db)
    failed = {
        "ok": False,
        "reason": "本地推理服务未启动（http://127.0.0.1:8080/v1：Connection refused）",
        "steps": GOOD_PRECHECK["steps"],
        "ms": 8000,
    }
    monkeypatch.setattr(task_precheck, "run", lambda *a, **kw: failed)

    with pytest.raises(HTTPException) as exc:
        tasks_api.create_task(_body(), db=db)
    assert exc.value.status_code == 422
    assert "本地推理服务未启动" in exc.value.detail

    row = db.query(AgentTask).one()
    assert row.status == task_runner.PRECHECK_FAILED
    assert row.finished_at is not None
    assert json.loads(row.precheck_result)["ok"] is False


def test_create_task_rejects_unknown_engine(db):
    _seed_config(db)
    # v0.5.2 起六端合法（claude/codex/zcode/qoder/dsh/pi），
    # 只有名单外的名字该被 pydantic 拒绝
    with pytest.raises(Exception):
        tasks_api.schemas.TaskCreate(prompt="x", tool="not-a-tool")


def test_create_task_accepts_all_six_engines(db):
    """六端都必须能被接受——这是任务下发扩展的核心契约。"""
    for tool in ("claude", "codex", "zcode", "qoder", "dsh", "pi"):
        assert tasks_api.schemas.TaskCreate(prompt="x", tool=tool).tool == tool


# ---------------- 列表 / 详情 ----------------


def test_detail_includes_output_tail(db, tmp_path):
    row = AgentTask(
        prompt="跑一下", tool="pi", status=task_runner.SUCCESS, timeout_seconds=600,
        output_path=str(tmp_path / "7.jsonl"),
    )
    (tmp_path / "7.jsonl").write_text('{"type":"agent_end"}\n噪声行\n', encoding="utf-8")
    db.add(row)
    db.commit()

    detail = tasks_api.task_detail(row.id, db=db)
    assert detail["output"].startswith('{"type":"agent_end"}')
    assert detail["output_truncated"] is False


def test_list_orders_newest_first(db):
    db.add_all(
        [
            AgentTask(prompt="旧", tool="pi", status=task_runner.SUCCESS, timeout_seconds=600),
            AgentTask(prompt="新", tool="pi", status=task_runner.SUCCESS, timeout_seconds=600),
        ]
    )
    db.commit()
    items = tasks_api.list_tasks(db=db)
    assert [i["prompt"] for i in items] == ["新", "旧"]


# ---------------- 取消 / 删除 ----------------


def test_cancel_pending_task(db):
    row = AgentTask(prompt="排队中", tool="pi", status=task_runner.PENDING, timeout_seconds=600)
    db.add(row)
    db.commit()
    result = tasks_api.cancel_task(row.id, db=db)
    assert result["cancelled"] is True
    assert db.get(AgentTask, row.id).status == task_runner.CANCELLED


def test_cancel_finished_task_is_noop(db):
    row = AgentTask(prompt="完成", tool="pi", status=task_runner.SUCCESS, timeout_seconds=600)
    db.add(row)
    db.commit()
    result = tasks_api.cancel_task(row.id, db=db)
    assert result["cancelled"] is False


def test_delete_refuses_running_and_removes_output_file(db, tmp_path):
    out = tmp_path / "9.md"
    out.write_text("产出", encoding="utf-8")
    running = AgentTask(prompt="跑着", tool="dsh", status=task_runner.RUNNING, timeout_seconds=600)
    done = AgentTask(
        prompt="完了", tool="dsh", status=task_runner.SUCCESS, timeout_seconds=600,
        output_path=str(out), finished_at=datetime.now(),
    )
    db.add_all([running, done])
    db.commit()

    with pytest.raises(HTTPException) as exc:
        tasks_api.delete_task(running.id, db=db)
    assert exc.value.status_code == 409

    assert tasks_api.delete_task(done.id, db=db) == {"deleted": done.id}
    assert not out.exists()
    assert db.get(AgentTask, done.id) is None
