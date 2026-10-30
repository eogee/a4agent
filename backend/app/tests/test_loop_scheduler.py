"""循环调度器测试：到期投递、防重入、一次性收尾、启动补算。

用独立内存/临时库，并把 submit 换成假函数——真实投递会拉起 CLI 子进程。
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import loop_scheduler as sched
from backend.app.models import AgentTask, Base, Loop

NOW = datetime(2026, 10, 25, 12, 0)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def submitted():
    ids: list[int] = []
    return ids, ids.append


@pytest.fixture
def precheck_ok():
    """默认让本地快检通过（真实预检会探测 CLI 与配置方案，测试不碰）。"""
    return lambda loop, db: (True, "")


def _make_loop(db, **kwargs):
    loop = Loop(
        name=kwargs.pop("name", "每日巡检"),
        tool=kwargs.pop("tool", "claude"),
        task_content=kwargs.pop("task_content", "检查昨日报错并汇总"),
        workdir=kwargs.pop("workdir", r"C:\work\demo"),
        schedule_type=kwargs.pop("schedule_type", "interval"),
        schedule_config=kwargs.pop("schedule_config", '{"minutes": 30}'),
        timeout_seconds=kwargs.pop("timeout_seconds", 1800),
        enabled=kwargs.pop("enabled", True),
        status=kwargs.pop("status", "active"),
        next_run_at=kwargs.pop("next_run_at", NOW - timedelta(minutes=1)),
        created_at=kwargs.pop("created_at", NOW - timedelta(hours=1)),
        **kwargs,
    )
    db.add(loop)
    db.commit()
    return loop


def test_tick_fires_due_loop_and_advances(db, submitted, precheck_ok):
    ids, submit = submitted
    loop = _make_loop(db)

    counts = sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 1, "skipped": 0}
    task = db.query(AgentTask).filter(AgentTask.loop_id == loop.id).one()
    assert task.status == "pending"
    assert task.trigger_type == "auto"
    assert task.prompt == "检查昨日报错并汇总"
    assert task.timeout_seconds == 1800
    assert ids == [task.id]  # 先落库再投递，worker 才读得到
    db.refresh(loop)
    assert loop.last_run_at == NOW
    assert loop.next_run_at == NOW + timedelta(minutes=30)


def test_tick_skips_when_previous_run_running(db, submitted, precheck_ok):
    """防重入：上一轮没结束时只记 skipped，不排队、不叠加。"""
    ids, submit = submitted
    loop = _make_loop(db)
    db.add(AgentTask(prompt="上一轮", tool="claude", status="running", loop_id=loop.id))
    db.commit()

    counts = sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 0, "skipped": 1}
    assert ids == []
    skipped = (db.query(AgentTask)
               .filter(AgentTask.loop_id == loop.id, AgentTask.status == "skipped").one())
    assert "上一轮" in skipped.error_summary
    db.refresh(loop)
    assert loop.next_run_at == NOW + timedelta(minutes=30)


def test_tick_ignores_future_disabled_and_completed(db, submitted, precheck_ok):
    _, submit = submitted
    _make_loop(db, next_run_at=NOW + timedelta(minutes=5))
    _make_loop(db, enabled=False)
    _make_loop(db, status="completed", next_run_at=None)

    assert sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok) == {"fired": 0, "skipped": 0}


def test_once_loop_completes_after_run(db, submitted, precheck_ok):
    ids, submit = submitted
    loop = _make_loop(db, schedule_type="once",
                      schedule_config='{"run_at": "2026-10-25 11:59"}',
                      next_run_at=NOW - timedelta(minutes=1))

    sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    db.refresh(loop)
    assert loop.status == "completed"
    assert loop.next_run_at is None
    assert len(ids) == 1


def test_startup_sweep_records_missed_without_catch_up(db, submitted, precheck_ok):
    ids, submit = submitted
    loop = _make_loop(db, catch_up=False, next_run_at=NOW - timedelta(hours=3))

    counts = sched.startup_sweep(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 0, "missed": 1}
    assert ids == []  # 没勾补跑就不拉进程
    missed = (db.query(AgentTask)
              .filter(AgentTask.loop_id == loop.id, AgentTask.status == "missed").one())
    assert "错过" in missed.error_summary
    db.refresh(loop)
    assert loop.next_run_at == NOW + timedelta(minutes=30)  # 不按错过次数叠加


def test_startup_sweep_runs_once_when_catch_up(db, submitted, precheck_ok):
    ids, submit = submitted
    loop = _make_loop(db, catch_up=True, schedule_type="daily",
                      schedule_config='{"time": "09:00"}',
                      next_run_at=NOW - timedelta(hours=3))

    counts = sched.startup_sweep(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 1, "missed": 0}
    assert len(ids) == 1
    db.refresh(loop)
    assert loop.next_run_at == datetime(2026, 10, 26, 9, 0)


def test_startup_sweep_ignores_not_due(db, submitted, precheck_ok):
    ids, submit = submitted
    _make_loop(db, next_run_at=NOW + timedelta(hours=1))
    assert sched.startup_sweep(now=NOW, db=db, submit=submit, precheck=precheck_ok) == {"fired": 0, "missed": 0}
    assert ids == []


def test_tick_records_precheck_failed_without_firing(db, submitted):
    """预检不过不拉进程：记一条 precheck_failed，计划照常推进。"""
    ids, submit = submitted
    loop = _make_loop(db)

    counts = sched.tick(now=NOW, db=db, submit=submit,
                        precheck=lambda l, d: (False, "还没有生效的配置方案"))

    assert counts == {"fired": 0, "skipped": 0, "precheck_failed": 1}
    assert ids == []
    run = (db.query(AgentTask)
           .filter(AgentTask.loop_id == loop.id, AgentTask.status == "precheck_failed").one())
    assert "预检未通过" in run.error_summary
    assert run.finished_at == NOW
    db.refresh(loop)
    assert loop.last_run_at is None  # 没真跑就不更新上次运行时间
    assert loop.next_run_at == NOW + timedelta(minutes=30)


def test_tick_commits_skipped_across_sessions(db, submitted):
    """回归：skipped 记录与 next_run_at 推进必须落库。

    tick() 自建 session 时跑完即 close，未 commit 的改动会被回滚——
    线上表现为每 20 秒重试同一轮、运行历史里永远看不到「已跳过」。
    同 session 内断言看不见这个问题，必须换一个 session 复查。
    """
    ids, submit = submitted
    engine = db.get_bind()
    loop = _make_loop(db)
    db.add(AgentTask(prompt="上一轮", tool="claude", status="running", loop_id=loop.id))
    db.commit()
    loop_id = loop.id  # 先取 id：session 关掉后实例即分离，再访问属性会炸

    counts = sched.tick(now=NOW, db=db, submit=submit,
                        precheck=lambda l, d: (True, ""))

    assert counts == {"fired": 0, "skipped": 1}
    db.close()
    fresh = sessionmaker(bind=engine)()
    try:
        skipped = (fresh.query(AgentTask)
                   .filter(AgentTask.loop_id == loop_id,
                           AgentTask.status == "skipped").one())
        assert "上一轮" in skipped.error_summary
        assert fresh.get(Loop, loop_id).next_run_at == NOW + timedelta(minutes=30)
    finally:
        fresh.close()


def test_tick_commits_precheck_failed_across_sessions(db, submitted):
    """预检失败的运行记录同样要落库（原因要能在历史里看见）。"""
    ids, submit = submitted
    engine = db.get_bind()
    loop = _make_loop(db)
    loop_id = loop.id

    counts = sched.tick(now=NOW, db=db, submit=submit,
                        precheck=lambda l, d: (False, "还没有生效的配置方案"))

    assert counts == {"fired": 0, "skipped": 0, "precheck_failed": 1}
    db.close()
    fresh = sessionmaker(bind=engine)()
    try:
        run = fresh.query(AgentTask).filter(AgentTask.loop_id == loop_id).one()
        assert run.status == "precheck_failed"
        assert "预检未通过" in run.error_summary
        assert fresh.get(Loop, loop_id).next_run_at == NOW + timedelta(minutes=30)
    finally:
        fresh.close()


# ---------------- 技能快照复制（方案 §10，批 4） ----------------

SKILLS = json.dumps([{"name": "pdf", "dir_name": "pdf", "source_tool": "claude",
                      "source_scope": "global", "source_project": "",
                      "source_path": r"C:\skills\claude\pdf"}], ensure_ascii=False)


def test_fire_copies_skills_into_task(db, submitted, precheck_ok):
    """fired 分支：loop.skills 原样复制进本次运行的任务行（快照语义）。"""
    ids, submit = submitted
    loop = _make_loop(db, skills=SKILLS)

    counts = sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 1, "skipped": 0}
    task = db.query(AgentTask).filter(AgentTask.loop_id == loop.id).one()
    assert json.loads(task.skills) == json.loads(loop.skills)  # 同一份快照
    assert task.skills == SKILLS  # 原样复制，不重算不重排


def test_skipped_record_carries_skills(db, submitted, precheck_ok):
    """skipped 标记行同样带快照（_fire 的建行分支全生效）。"""
    ids, submit = submitted
    loop = _make_loop(db, skills=SKILLS)
    db.add(AgentTask(prompt="上一轮", tool="claude", status="running", loop_id=loop.id))
    db.commit()

    counts = sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    assert counts == {"fired": 0, "skipped": 1}
    assert ids == []
    skipped = (db.query(AgentTask)
               .filter(AgentTask.loop_id == loop.id, AgentTask.status == "skipped").one())
    assert json.loads(skipped.skills) == json.loads(SKILLS)


def test_fire_without_skills_keeps_column_empty(db, submitted, precheck_ok):
    """零回归：不带技能的循环，任务行 skills 仍为空串。"""
    ids, submit = submitted
    loop = _make_loop(db)

    sched.tick(now=NOW, db=db, submit=submit, precheck=precheck_ok)

    task = db.query(AgentTask).filter(AgentTask.loop_id == loop.id).one()
    assert task.skills == ""
