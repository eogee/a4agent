"""循环任务接口：定义增删改查 / 启停 / 立即执行 / 运行历史 / 日志 / 取消。

运行记录复用任务表（AgentTask.loop_id），日志与取消直接走 task_runner 的
既有能力；本文件只管「定义」与「手动触发」。端点草案见
docs/循环管理-需求分析.md §8。
"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import crud, loop_schedule, loop_scheduler, schemas, task_engines, task_precheck, task_runner
from ...crypto import decrypt_text
from ...database import get_db
from ...models import AgentTask, Loop
from .skill_snapshot import skill_snapshot

router = APIRouter(prefix="/loops")

# 「打开完整日志」一次性回传的上限：日志可能几 MB，全部塞进 JSON 会把
# 浏览器憋住；超限只给头部并告知，完整文件仍在磁盘上
FULL_LOG_MAX_BYTES = 4 * 1024 * 1024


def _fmt(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def _config_dict(loop: Loop) -> dict:
    raw = loop.schedule_config or "{}"
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _get_loop(db: Session, loop_id: int) -> Loop:
    loop = db.get(Loop, loop_id)
    if loop is None:
        raise HTTPException(404, "循环任务不存在")
    return loop


def _get_run(db: Session, run_id: int) -> AgentTask:
    task = db.get(AgentTask, run_id)
    if task is None or task.loop_id is None:
        raise HTTPException(404, "运行记录不存在或不属于循环任务")
    return task


def _has_active_run(db: Session, loop_id: int) -> bool:
    return (db.query(AgentTask)
            .filter(AgentTask.loop_id == loop_id,
                    AgentTask.status.in_((task_runner.PENDING, task_runner.RUNNING)))
            .first()) is not None


def _check_workdir(raw: str) -> str:
    """工作目录校验与手动下发同规则：绝对路径且存在。"""
    working_dir = (raw or "").strip()
    if not working_dir:
        return ""
    folder = Path(working_dir).expanduser()
    if not folder.is_absolute():
        raise HTTPException(422, f"工作目录需要绝对路径：{working_dir}")
    if not folder.is_dir():
        raise HTTPException(422, f"工作目录不存在或不是文件夹：{working_dir}")
    return str(folder)


def _parse_schedule(schedule_type: str, raw) -> dict:
    try:
        return loop_schedule.parse_config(schedule_type, raw)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


def _parse_skills(raw: str) -> list:
    """Loop.skills 快照 JSON → list[SkillRef]；空串/坏数据一律按「没选技能」。

    回显只保留 SkillRef 契约五字段（§4.2），快照里的 dir_name 等内部字段不外带。
    """
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {
            "name": item.get("name") or "",
            "source_tool": item.get("source_tool") or "",
            "source_scope": item.get("source_scope") or "",
            "source_project": item.get("source_project") or "",
            "source_path": item.get("source_path") or "",
        }
        for item in data if isinstance(item, dict) and item.get("name")
    ]


def loop_payload(loop: Loop, db: Session) -> dict:
    """列表/详情共用的序列化：是否有进行中的运行、最近一次运行。

    计划描述不在后端拼：唯一消费者是前端卡片，而它要按语言分别措辞
    （见 frontend/js/loops.js 的 scheduleLine），这里只给结构化字段。
    """
    config = _config_dict(loop)
    last = (db.query(AgentTask)
            .filter(AgentTask.loop_id == loop.id)
            .order_by(AgentTask.id.desc())
            .first())
    return {
        "id": loop.id,
        "name": loop.name,
        "tool": loop.tool,
        "tool_label": task_engines.ENGINE_LABELS.get(loop.tool, loop.tool),
        "task_content": loop.task_content,
        "workdir": loop.workdir or "",
        "schedule_type": loop.schedule_type,
        "schedule_config": config,
        "timeout_seconds": loop.timeout_seconds,
        "catch_up": bool(loop.catch_up),
        "enabled": bool(loop.enabled),
        "status": loop.status,
        "next_run_at": _fmt(loop.next_run_at),
        "last_run_at": _fmt(loop.last_run_at),
        "running": _has_active_run(db, loop.id),
        "last_run": None if last is None else task_runner.task_payload(last),
        "skills": _parse_skills(loop.skills),
    }


@router.get("")
def list_loops(db: Session = Depends(get_db)):
    loops = db.query(Loop).all()
    # 启用的排前面，再按下次运行时间近的优先；无下次运行（已完成/一次性）垫底
    loops.sort(key=lambda l: (not l.enabled, l.next_run_at is None,
                              l.next_run_at or datetime.max))
    return {
        "loops": [loop_payload(loop, db) for loop in loops],
        "scheduler": {
            "running": loop_scheduler.running(),
            "scan_interval": loop_scheduler.SCAN_INTERVAL,
            "concurrency": task_runner.max_workers(),
        },
    }


@router.post("", response_model=schemas.LoopOut, status_code=201)
def create_loop(body: schemas.LoopCreate, db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "名称不能为空")
    workdir = _check_workdir(body.workdir)
    config = _parse_schedule(body.schedule_type, body.schedule_config)
    # 技能校验与任务下发同一口径（workdir 用循环自己的工作目录），失败 422 不建行
    skills = skill_snapshot(body.skills, workdir=workdir)
    loop = Loop(
        name=name,
        tool=body.tool,
        task_content=(body.task_content or "").strip(),
        workdir=workdir,
        schedule_type=body.schedule_type,
        schedule_config=json.dumps(config, ensure_ascii=False),
        timeout_seconds=body.timeout_seconds,
        catch_up=body.catch_up,
        enabled=body.enabled,
        status="active" if body.enabled else "paused",
        next_run_at=loop_schedule.next_run(body.schedule_type, config, datetime.now()),
        skills=json.dumps(skills, ensure_ascii=False) if skills else "",
    )
    db.add(loop)
    db.commit()
    db.refresh(loop)
    return loop_payload(loop, db)


@router.put("/{loop_id}", response_model=schemas.LoopOut)
def update_loop(loop_id: int, body: schemas.LoopUpdate, db: Session = Depends(get_db)):
    loop = _get_loop(db, loop_id)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "名称不能为空")
    workdir = _check_workdir(body.workdir)
    config = _parse_schedule(body.schedule_type, body.schedule_config)
    # 技能校验与任务下发同一口径；整个替换式更新（传空 = 清空已选技能）
    skills = skill_snapshot(body.skills, workdir=workdir)

    schedule_changed = (loop.schedule_type != body.schedule_type
                        or _config_dict(loop) != config)
    loop.name = name
    loop.tool = body.tool
    loop.task_content = (body.task_content or "").strip()
    loop.workdir = workdir
    loop.schedule_type = body.schedule_type
    loop.schedule_config = json.dumps(config, ensure_ascii=False)
    loop.timeout_seconds = body.timeout_seconds
    loop.catch_up = body.catch_up
    loop.enabled = body.enabled
    loop.skills = json.dumps(skills, ensure_ascii=False) if skills else ""
    # 改计划 = 重新排期；已完成的一次性任务改时间即「再跑一次」。
    # 只是改名/改内容时不动 next_run_at，已完成的一次性任务也不会被改名唤醒。
    if schedule_changed:
        loop.next_run_at = loop_schedule.next_run(body.schedule_type, config, datetime.now())
        if loop.status == "completed":
            loop.status = "active" if body.enabled else "paused"
    if loop.next_run_at is None and loop.status != "completed":
        loop.next_run_at = loop_schedule.next_run(body.schedule_type, config, datetime.now())
    if loop.status != "completed":
        loop.status = "active" if body.enabled else "paused"
    db.commit()
    db.refresh(loop)
    return loop_payload(loop, db)


@router.delete("/{loop_id}")
def delete_loop(loop_id: int, db: Session = Depends(get_db)):
    loop = _get_loop(db, loop_id)
    if _has_active_run(db, loop_id):
        raise HTTPException(409, "该循环还有运行中或排队中的任务，请先取消再删除")
    runs = (db.query(AgentTask)
            .filter(AgentTask.loop_id == loop_id)
            .all())
    for run in runs:  # 级联删运行记录，日志文件一并收走
        if run.output_path:
            try:
                Path(run.output_path).unlink(missing_ok=True)
            except OSError:
                pass
        db.delete(run)
    db.delete(loop)
    db.commit()
    return {"deleted": loop_id, "runs_deleted": len(runs)}


@router.post("/{loop_id}/enable", response_model=schemas.LoopOut)
def enable_loop(loop_id: int, db: Session = Depends(get_db)):
    loop = _get_loop(db, loop_id)
    if loop.status == "completed":
        raise HTTPException(409, "一次性任务已完成，不能重新启用；请编辑运行时间或新建")
    loop.enabled = True
    loop.status = "active"
    if loop.next_run_at is None:  # 理论上不会发生，兜底重新排期
        loop.next_run_at = loop_schedule.next_run(
            loop.schedule_type, _config_dict(loop), datetime.now())
    db.commit()
    db.refresh(loop)
    return loop_payload(loop, db)


@router.post("/{loop_id}/disable", response_model=schemas.LoopOut)
def disable_loop(loop_id: int, db: Session = Depends(get_db)):
    loop = _get_loop(db, loop_id)
    loop.enabled = False
    loop.status = "paused"
    db.commit()
    db.refresh(loop)
    return loop_payload(loop, db)


@router.post("/{loop_id}/run")
def run_now(loop_id: int, db: Session = Depends(get_db)):
    """立即执行：完整预检（含连通）后插一条手动触发的运行记录。

    不改自动计划（next_run_at / last_run_at 都不动），这次运行只进历史。
    """
    loop = _get_loop(db, loop_id)
    if loop.status == "completed":
        raise HTTPException(409, "一次性任务已完成；请编辑运行时间或新建")
    if _has_active_run(db, loop.id):
        raise HTTPException(409, "上一轮运行尚未结束，请等它结束再手动执行")

    active = crud.get_active_config(db)
    needs_local_config = loop.tool not in task_engines.HTTP_ENGINES
    api_key = ""
    if needs_local_config:
        if active is None or active.provider is None:
            raise HTTPException(409, "还没有生效的配置方案，请先在「配置方案」页切换一次")
        api_key = decrypt_text(active.api_key_encrypted)
        if not api_key:
            raise HTTPException(409,
                                "该配置方案的 API Key 解密失败或为空，请到「配置方案」页重新保存一次 Key")
    result = task_precheck.run(
        db, loop.tool, active.provider if needs_local_config else None,
        api_key, active.model if active else "")
    if not result["ok"]:
        raise HTTPException(422, f"预检未通过：{result['reason']}")

    task = AgentTask(
        prompt=loop.task_content,
        tool=loop.tool,
        working_dir=loop.workdir or "",
        timeout_seconds=loop.timeout_seconds or task_runner.DEFAULT_TIMEOUT_SECONDS,
        status=task_runner.PENDING,
        loop_id=loop.id,
        trigger_type="manual",
        config_id=active.id if active else None,
        model=(active.model if active else "") or "",
        precheck_result=json.dumps(result, ensure_ascii=False),
        precheck_ms=result["ms"],
        error_summary="",
        skills=loop.skills or "",  # 手动执行与调度触发同口径：快照原样复制
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    task_runner.submit(task.id)
    return task_runner.task_payload(task)


@router.get("/{loop_id}/runs")
def loop_runs(loop_id: int, limit: int = 50, offset: int = 0,
              db: Session = Depends(get_db)):
    _get_loop(db, loop_id)
    query = (db.query(AgentTask)
             .filter(AgentTask.loop_id == loop_id)
             .order_by(AgentTask.id.desc()))
    total = query.count()
    rows = query.offset(max(0, offset)).limit(max(1, min(limit, 200))).all()
    return {"total": total, "runs": [task_runner.task_payload(row) for row in rows]}


# ---- 运行级端点（日志/取消）：路径以 runs 开头，与 /{loop_id} 不冲突 ----

@router.get("/runs/{run_id}/log")
def run_log(run_id: int, db: Session = Depends(get_db)):
    """日志尾部摘要（与任务详情同一份实现：全文在磁盘，这里给尾部）。"""
    task = _get_run(db, run_id)
    return task_runner.task_payload(task, include_output=True)


@router.get("/runs/{run_id}/log/file")
def run_log_file(run_id: int, db: Session = Depends(get_db)):
    """完整日志内容：「打开完整日志」用；超限截断并告知，磁盘文件不受影响。"""
    task = _get_run(db, run_id)
    path = task.output_path or ""
    if not path or not Path(path).is_file():
        return {"path": path, "size": 0, "truncated": False, "output": ""}
    size = Path(path).stat().st_size
    truncated = size > FULL_LOG_MAX_BYTES
    with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
        text = fh.read(FULL_LOG_MAX_BYTES)
    return {"path": path, "size": size, "truncated": truncated, "output": text}


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, db: Session = Depends(get_db)):
    task = _get_run(db, run_id)
    try:
        return task_runner.cancel(task.id, db)
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
