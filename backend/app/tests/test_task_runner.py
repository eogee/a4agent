"""无头任务执行器测试：用真实子进程当引擎，覆盖状态机、超时、取消与回收。"""
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import (skill_manager, task_engines, task_notify, task_runner,
                         task_skills)
from backend.app.database import Base
from backend.app.models import AgentTask


@pytest.fixture(autouse=True)
def _reset_runner_globals():
    """任务 id 在各测试的独立库里都从 1 重新计数，而 _cancelled / _shutdown_reason
    是模块级单例——前一个文件的取消测试留下的残留会把这里同 id 的任务误判为取消。"""
    task_runner._cancelled.clear()
    task_runner._shutdown_reason.clear()
    task_skills._skill_refs.clear()
    task_skills._skill_kinds.clear()
    yield
    task_runner._cancelled.clear()
    task_runner._shutdown_reason.clear()
    task_skills._skill_refs.clear()
    task_skills._skill_kinds.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'runner.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)
    monkeypatch.setattr(task_runner, "SessionLocal", maker)
    # task_skills 在 finally 里用独立 session 回收：不换掉就会摸到开发机真实库
    monkeypatch.setattr(task_skills, "SessionLocal", maker)
    events = []
    monkeypatch.setattr(task_notify, "notify", lambda event: events.append(event))
    yield SimpleNamespace(maker=maker, events=events, tmp=tmp_path)
    maker().close()


def _fake_engine(monkeypatch, code):
    """把 build_argv 换成执行 Python 片段，stdout 即引擎产出。

    签名必须与 task_engines.build_argv 一致（tool, prompt, working_dir=None）：
    _run_cli 现在会把工作目录传下去，少了这个形参会直接 TypeError。
    """
    monkeypatch.setattr(task_engines, "resolve_command", lambda tool: sys.executable)
    monkeypatch.setattr(
        task_engines, "build_argv",
        lambda tool, prompt, working_dir=None: [sys.executable, "-c", code],
    )


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
    assert payload["tool_label"] == "Pi"
    assert payload["status"] == task_runner.SUCCESS
    assert payload["duration_seconds"] == 0
    assert "agent_end" in payload["output"]
    assert payload["output_truncated"] is False
    db.close()


# ---------------- §6.3：working_dir 与 Popen 的 cwd 同值 ----------------


def _spy_popen(monkeypatch, captured):
    """包住真正的 Popen，记下 cwd（引擎仍要真跑，状态机才走得完）。"""
    real_popen = task_runner.subprocess.Popen

    def spy(argv, **kw):
        captured["argv"] = argv
        captured["cwd"] = kw.get("cwd")
        return real_popen(argv, **kw)

    monkeypatch.setattr(task_runner.subprocess, "Popen", spy)


def _spy_build_argv(monkeypatch, code, captured):
    def fake_build(tool, prompt, working_dir=None):
        captured["working_dir"] = working_dir
        captured["prompt"] = prompt
        return [sys.executable, "-c", code]

    monkeypatch.setattr(task_engines, "build_argv", fake_build)


def test_build_argv_receives_same_dir_as_popen_cwd(env, monkeypatch):
    """钉死 §6.3 约束 1：传给 build_argv 的目录必须与 Popen(cwd=...) 同值。"""
    workdir = env.tmp / "proj"
    workdir.mkdir()
    captured = {}
    _spy_build_argv(monkeypatch, _pi_jsonl("完成"), captured)
    _spy_popen(monkeypatch, captured)
    db = env.maker()
    task = _new_task(db, working_dir=str(workdir))

    task_runner._execute(task.id)

    assert captured["working_dir"] == str(workdir)
    assert captured["cwd"] == str(workdir)
    db.close()


def test_empty_working_dir_passes_none_and_runs_in_home(env, monkeypatch):
    """§6.3 约束 2：没选工作目录时不把 home 塞进 argv，但进程仍跑在 home。"""
    captured = {}
    _spy_build_argv(monkeypatch, _pi_jsonl("完成"), captured)
    _spy_popen(monkeypatch, captured)
    db = env.maker()
    task = _new_task(db, working_dir="")

    task_runner._execute(task.id)

    assert captured["working_dir"] is None
    assert captured["cwd"] == str(Path.home())
    db.close()


# ---------------- §7：注入挂点 / 回收点 ----------------


def _make_skill(parent: Path, dir_name: str = "pdf", name: str = "pdf") -> Path:
    skill_dir = parent / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 测试用技能\n---\n# 正文\n", encoding="utf-8"
    )
    return skill_dir


def _isolate_skill_roots(monkeypatch, tmp_path) -> Path:
    """全局 skill 根与项目发现全部指向临时目录（别去扫开发机）。"""
    global_root = tmp_path / "global"
    global_root.mkdir(exist_ok=True)
    monkeypatch.setattr(
        skill_manager, "global_skill_roots",
        lambda: {tool: global_root / tool for tool in skill_manager.TOOLS},
    )
    monkeypatch.setattr(skill_manager, "project_dirs", lambda roots=None: [])
    monkeypatch.setattr(
        skill_manager, "all_known_skill_roots",
        lambda: [global_root / tool for tool in skill_manager.TOOLS],
    )
    return global_root


def _skill_ref(path: Path, name: str = "pdf", tool: str = "pi") -> dict:
    return {
        "name": name,
        "dir_name": path.name,
        "source_tool": tool,
        "source_scope": "global",
        "source_project": "",
        "source_path": str(path),
    }


def test_mount_failure_fails_task_without_spawning(env, monkeypatch):
    """注入失败 → FAILED + 原因含技能名，且**不拉起进程**（方案 §7.1）。"""
    spawned = []

    def forbid_popen(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("技能挂载失败时不允许拉起引擎进程")

    monkeypatch.setattr(task_runner.subprocess, "Popen", forbid_popen)

    def boom(task, db):
        raise task_skills.SkillMountError("技能「ghost」已不存在，请重新选择")

    monkeypatch.setattr(task_skills, "mount", boom)
    db = env.maker()
    task = _new_task(db, working_dir=str(env.tmp))

    task_runner._execute(task.id)

    db.expire_all()
    row = db.get(AgentTask, task.id)
    assert row.status == task_runner.FAILED
    assert row.error_summary.startswith("技能挂载失败：")
    assert "ghost" in row.error_summary
    assert row.finished_at is not None
    assert spawned == []
    assert env.events == []  # 没跑就没有终态通知
    db.close()


def test_skill_mounted_during_run_and_reclaimed_after(env, monkeypatch):
    """真建真删：运行期间 `<workdir>/.pi/skills/pdf` 存在，结束即消失且 skill_links 清空。"""
    global_root = _isolate_skill_roots(monkeypatch, env.tmp)
    src = _make_skill(global_root / "pi")
    workdir = env.tmp / "proj"
    workdir.mkdir()
    dest = workdir / ".pi" / "skills" / "pdf"

    captured = {}
    _spy_build_argv(monkeypatch, _pi_jsonl("完成"), captured)
    real_popen = task_runner.subprocess.Popen

    def spy(argv, **kw):
        # 进程一旦拉起就再也补不进去（附录 C4.1），这里就是「注入早于 Popen」的证据
        captured["exists_when_spawned"] = dest.is_dir()
        return real_popen(argv, **kw)

    monkeypatch.setattr(task_runner.subprocess, "Popen", spy)

    db = env.maker()
    task = _new_task(db, prompt="干活", working_dir=str(workdir))
    task.skills = json.dumps([_skill_ref(src)], ensure_ascii=False)
    db.commit()

    task_runner._execute(task.id)

    assert captured["exists_when_spawned"] is True
    # 前缀只进 argv，不写回原文（§7.2）
    assert captured["prompt"].startswith("本次可用的技能：pdf")
    assert captured["prompt"].endswith("干活")
    db.expire_all()
    row = db.get(AgentTask, task.id)
    assert row.prompt == "干活"
    assert row.status == task_runner.SUCCESS
    assert not dest.exists()  # 回收：链接消失
    assert not (workdir / ".pi" / "skills").exists()  # 自建的空根也清掉
    assert (row.skill_links or "") == ""  # 回收依据清空
    assert json.loads(row.skills)[0]["name"] == "pdf"  # 快照保留（复跑要用）
    db.close()


def test_no_workdir_uses_degraded_prefix(env, monkeypatch):
    """无工作目录不注入，前缀降级成「建议使用」（裁定 3 / 方案 §7.2）。"""
    global_root = _isolate_skill_roots(monkeypatch, env.tmp)
    src = _make_skill(global_root / "pi")
    captured = {}
    _spy_build_argv(monkeypatch, _pi_jsonl("完成"), captured)
    db = env.maker()
    task = _new_task(db, prompt="干活", working_dir="")
    task.skills = json.dumps([_skill_ref(src)], ensure_ascii=False)
    db.commit()

    task_runner._execute(task.id)

    assert captured["prompt"].startswith("本次建议使用的技能：pdf")
    assert "未注入到工作目录" in captured["prompt"]
    assert not (env.tmp / ".pi").exists()
    db.close()
