"""循环任务接口测试：定义增改、启停约束、立即执行、运行历史、日志与删除级联。

不打真机：预检与 submit 全部桩化（真实行为在 test_loop_scheduler /
test_tasks_api 覆盖），端点函数直接调用，与 test_tasks_api 同风格。
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import skill_manager, task_precheck, task_runner
from backend.app.api.v1 import loops as loops_api
from backend.app.api.v1.skill_snapshot import skill_snapshot
from backend.app.database import Base
from backend.app.models import AgentTask, Loop

GOOD_PRECHECK = {"ok": True, "reason": "", "steps": [], "ms": 12}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'loops.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    monkeypatch.setattr(task_precheck, "run", lambda *a, **kw: dict(GOOD_PRECHECK))
    monkeypatch.setattr(task_runner, "submit", lambda task_id: None)
    yield session
    session.close()


def _body(**kw):
    base = dict(
        name="每日巡检",
        tool="opencode",  # HTTP 引擎：绕开配置方案依赖，专注循环语义
        task_content="检查昨日报错并汇总",
        workdir="",
        schedule_type="interval",
        schedule_config={"minutes": 30},
        timeout_seconds=1800,
    )
    base.update(kw)
    return loops_api.schemas.LoopCreate(**base)


def _make_loop(db, **kw):
    loop = Loop(
        name=kw.pop("name", "巡检"),
        tool=kw.pop("tool", "opencode"),
        task_content=kw.pop("task_content", "汇总报错"),
        workdir=kw.pop("workdir", ""),
        schedule_type=kw.pop("schedule_type", "interval"),
        schedule_config=kw.pop("schedule_config", '{"minutes": 30}'),
        timeout_seconds=kw.pop("timeout_seconds", 1800),
        enabled=kw.pop("enabled", True),
        status=kw.pop("status", "active"),
        next_run_at=kw.pop("next_run_at", datetime.now() + timedelta(minutes=30)),
        created_at=kw.pop("created_at", datetime.now()),
        **kw,
    )
    db.add(loop)
    db.commit()
    db.refresh(loop)
    return loop


# ---------------- 新建 / 列表 ----------------

def test_create_computes_next_run_and_describes(db):
    out = loops_api.create_loop(_body(), db=db)
    assert out["schedule_config"] == {"minutes": 30}
    assert out["status"] == "active"
    assert out["next_run_at"]  # 已排期
    assert out["tool_label"] == "OpenCode"
    assert out["running"] is False

    listing = loops_api.list_loops(db=db)
    assert listing["scheduler"]["scan_interval"] > 0
    assert [l["name"] for l in listing["loops"]] == ["每日巡检"]


def test_create_rejects_bad_schedule(db):
    with pytest.raises(HTTPException) as exc:
        loops_api.create_loop(_body(schedule_type="daily", schedule_config={"time": "9点"}), db=db)
    assert exc.value.status_code == 422
    assert "HH:MM" in exc.value.detail


def test_create_rejects_relative_workdir(db):
    with pytest.raises(HTTPException) as exc:
        loops_api.create_loop(_body(workdir="relative/path"), db=db)
    assert exc.value.status_code == 422
    assert "绝对路径" in exc.value.detail


def test_create_accepts_disabled_as_paused(db):
    out = loops_api.create_loop(_body(enabled=False), db=db)
    assert out["enabled"] is False
    assert out["status"] == "paused"


# ---------------- 编辑 ----------------

def test_update_reschedules_when_schedule_changes(db):
    loop = _make_loop(db)
    out = loops_api.update_loop(
        loop.id, _body(name="改名", schedule_type="daily", schedule_config={"time": "09:00"}), db=db)
    assert out["schedule_config"] == {"time": "09:00"}
    row = db.get(Loop, loop.id)
    assert row.next_run_at is not None and row.next_run_at.hour == 9


def test_update_rename_does_not_revive_completed_once(db):
    """已完成的一次性任务只是改名：不被唤醒，也不重新排期。"""
    loop = _make_loop(db, schedule_type="once",
                      schedule_config='{"run_at": "2026-10-20 09:00"}',
                      status="completed", next_run_at=None)
    out = loops_api.update_loop(loop.id, _body(name="只改名", schedule_type="once",
                                               schedule_config={"run_at": "2026-10-20 09:00"}), db=db)
    assert out["status"] == "completed"
    assert out["next_run_at"] == ""


def test_update_new_time_revives_completed_once(db):
    loop = _make_loop(db, schedule_type="once",
                      schedule_config='{"run_at": "2026-10-20 09:00"}',
                      status="completed", next_run_at=None)
    out = loops_api.update_loop(loop.id, _body(name="改时间", schedule_type="once",
                                               schedule_config={"run_at": "2026-12-01 08:30"}), db=db)
    assert out["status"] == "active"
    assert out["next_run_at"] == "2026-12-01 08:30:00"


# ---------------- 启停 ----------------

def test_disable_then_enable(db):
    loop = _make_loop(db)
    out = loops_api.disable_loop(loop.id, db=db)
    assert out["status"] == "paused"
    out = loops_api.enable_loop(loop.id, db=db)
    assert out["status"] == "active"


def test_enable_completed_once_is_rejected(db):
    loop = _make_loop(db, schedule_type="once", schedule_config='{"run_at": "2026-10-20 09:00"}',
                      status="completed", next_run_at=None)
    with pytest.raises(HTTPException) as exc:
        loops_api.enable_loop(loop.id, db=db)
    assert exc.value.status_code == 409


# ---------------- 立即执行 ----------------

def test_run_now_queues_manual_task_without_touching_schedule(db, monkeypatch):
    loop = _make_loop(db)
    submitted = []
    monkeypatch.setattr(task_runner, "submit", lambda task_id: submitted.append(task_id))
    before = loop.next_run_at

    out = loops_api.run_now(loop.id, db=db)

    assert submitted == [out["id"]]
    assert out["trigger_type"] == "manual"
    assert out["status"] == task_runner.PENDING
    assert out["prompt"] == "汇总报错"
    db.refresh(loop)
    assert loop.next_run_at == before  # 手动执行不动自动计划


def test_run_now_blocked_by_precheck(db, monkeypatch):
    loop = _make_loop(db)
    monkeypatch.setattr(task_precheck, "run",
                        lambda *a, **kw: {"ok": False, "reason": "CLI 未登录", "steps": [], "ms": 1})
    with pytest.raises(HTTPException) as exc:
        loops_api.run_now(loop.id, db=db)
    assert exc.value.status_code == 422
    assert "CLI 未登录" in exc.value.detail
    assert db.query(AgentTask).count() == 0


def test_run_now_refuses_when_previous_run_active(db):
    loop = _make_loop(db)
    db.add(AgentTask(prompt="上一轮", tool="opencode", status=task_runner.RUNNING,
                     loop_id=loop.id, timeout_seconds=600))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        loops_api.run_now(loop.id, db=db)
    assert exc.value.status_code == 409


# ---------------- 运行历史 / 日志 / 取消 ----------------

def test_runs_history_and_log_endpoints(db, tmp_path):
    loop = _make_loop(db)
    log = tmp_path / "12.md"
    log.write_text("第一行\n" + "噪声\n" * 100 + "最后一行", encoding="utf-8")
    run = AgentTask(prompt="汇总报错", tool="opencode", status=task_runner.SUCCESS,
                    loop_id=loop.id, trigger_type="auto", timeout_seconds=600,
                    output_path=str(log), finished_at=datetime.now())
    db.add(run)
    db.commit()

    history = loops_api.loop_runs(loop.id, db=db)
    assert history["total"] == 1
    assert history["runs"][0]["id"] == run.id

    tail = loops_api.run_log(run.id, db=db)
    assert "最后一行" in tail["output"]

    full = loops_api.run_log_file(run.id, db=db)
    assert full["truncated"] is False
    assert full["output"].startswith("第一行")
    assert full["size"] == log.stat().st_size


def test_log_endpoints_reject_non_loop_task(db):
    manual = AgentTask(prompt="手动任务", tool="opencode", status=task_runner.SUCCESS,
                       timeout_seconds=600)
    db.add(manual)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        loops_api.run_log(manual.id, db=db)
    assert exc.value.status_code == 404


def test_cancel_run(db):
    loop = _make_loop(db)
    run = AgentTask(prompt="排队中", tool="opencode", status=task_runner.PENDING,
                    loop_id=loop.id, timeout_seconds=600)
    db.add(run)
    db.commit()
    result = loops_api.cancel_run(run.id, db=db)
    assert result["cancelled"] is True


# ---------------- 删除级联 ----------------

def test_delete_cascades_runs_and_log_files(db, tmp_path):
    loop = _make_loop(db)
    log = tmp_path / "3.md"
    log.write_text("日志", encoding="utf-8")
    run = AgentTask(prompt="旧运行", tool="opencode", status=task_runner.SUCCESS,
                    loop_id=loop.id, timeout_seconds=600, output_path=str(log))
    db.add(run)
    db.commit()

    out = loops_api.delete_loop(loop.id, db=db)

    assert out == {"deleted": loop.id, "runs_deleted": 1}
    assert db.get(Loop, loop.id) is None
    assert db.query(AgentTask).count() == 0
    assert not log.exists()


def test_delete_refuses_when_run_active(db):
    loop = _make_loop(db)
    db.add(AgentTask(prompt="跑着", tool="opencode", status=task_runner.RUNNING,
                     loop_id=loop.id, timeout_seconds=600))
    db.commit()
    with pytest.raises(HTTPException) as exc:
        loops_api.delete_loop(loop.id, db=db)
    assert exc.value.status_code == 409
    assert db.get(Loop, loop.id) is not None


# ---------------- 技能：校验 / 落库 / 回显（方案 §10，批 4） ----------------


def _skill_root(tmp_path, monkeypatch):
    """把「已知 skill 根」指到一个临时目录（别扫开发机），返回该根。"""
    root = tmp_path / "skills" / "claude"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skill_manager, "all_known_skill_roots", lambda: [root])
    return root


def _make_skill(root, dir_name="pdf", name="pdf"):
    skill_dir = root / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 处理 PDF\n---\n# 正文\n", encoding="utf-8"
    )
    return skill_dir


def _skill_body(**kw):
    src = kw.pop("src")
    base = dict(
        name="带技能巡检", tool="opencode", task_content="用技能汇总",
        workdir="", schedule_type="interval", schedule_config={"minutes": 30},
        skills=[loops_api.schemas.SkillRef(
            name="pdf", source_tool="claude", source_scope="global",
            source_path=str(src),
        )],
    )
    base.update(kw)
    return loops_api.schemas.LoopCreate(**base)


def test_create_loop_stores_and_echoes_skills(db, tmp_path, monkeypatch):
    root = _skill_root(tmp_path, monkeypatch)
    src = _make_skill(root)

    out = loops_api.create_loop(_skill_body(src=src), db=db)

    row = db.get(Loop, out["id"])
    assert json.loads(row.skills) == [{
        "name": "pdf", "dir_name": "pdf", "source_tool": "claude",
        "source_scope": "global", "source_project": "", "source_path": str(src),
    }]
    # GET /loops 回显同一份（LoopOut.skills 只保留 §4.2 的字段）
    listing = loops_api.list_loops(db=db)
    echoed = [l for l in listing["loops"] if l["id"] == out["id"]][0]
    assert echoed["skills"] == [{
        "name": "pdf", "source_tool": "claude", "source_scope": "global",
        "source_project": "", "source_path": str(src),
    }]


def test_update_loop_replaces_skills(db, tmp_path, monkeypatch):
    root = _skill_root(tmp_path, monkeypatch)
    src = _make_skill(root)
    loop = _make_loop(db)
    loops_api.update_loop(loop.id, _skill_body(src=src, name="补技能"), db=db)
    db.refresh(loop)
    assert json.loads(loop.skills)[0]["name"] == "pdf"
    # 传空 = 清空已选技能（替换式更新）
    loops_api.update_loop(loop.id, _body(name="清技能"), db=db)
    db.refresh(loop)
    assert loop.skills == ""


def test_create_loop_rejects_skill_path_outside_known_roots(db, tmp_path, monkeypatch):
    _skill_root(tmp_path, monkeypatch)
    outside = _make_skill(tmp_path / "elsewhere")
    with pytest.raises(HTTPException) as exc:
        loops_api.create_loop(_skill_body(src=outside), db=db)
    assert exc.value.status_code == 422
    assert "不在允许" in exc.value.detail
    assert db.query(Loop).count() == 0


def test_create_loop_rejects_too_many_skills():
    """数量上限 20 由 schema 拦（与 TaskCreate 同值，方案 §4.2）。"""
    refs = [{"name": f"s{i}"} for i in range(21)]
    with pytest.raises(Exception):
        loops_api.schemas.LoopCreate(
            name="x", tool="claude", task_content="x",
            schedule_type="interval", schedule_config={"minutes": 30}, skills=refs)


def test_skill_snapshot_helper_rejects_over_limit():
    """路由层助手同样守住 20（直呼不经 pydantic 的场景兜底）。"""
    refs = [loops_api.schemas.SkillRef(name=f"s{i}") for i in range(21)]
    with pytest.raises(HTTPException) as exc:
        skill_snapshot(refs)
    assert exc.value.status_code == 422
    assert "20" in exc.value.detail


def test_run_now_copies_skills_into_manual_task(db, tmp_path, monkeypatch):
    root = _skill_root(tmp_path, monkeypatch)
    src = _make_skill(root)
    out = loops_api.create_loop(_skill_body(src=src), db=db)
    loop = db.get(Loop, out["id"])

    task = loops_api.run_now(loop.id, db=db)

    # task_payload 回原始快照（HTTP 层由 TaskOut 剥掉 dir_name）；列值与 loop.skills 逐字相等
    assert task["skills"] == json.loads(loop.skills)
    assert db.get(AgentTask, task["id"]).skills == loop.skills


def test_create_loop_without_skills_keeps_column_empty(db):
    out = loops_api.create_loop(_body(), db=db)
    assert db.get(Loop, out["id"]).skills == ""
    assert out["skills"] == []
