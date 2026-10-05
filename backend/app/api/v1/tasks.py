"""无头任务接口：引擎探测 / 下发 / 列表 / 详情 / 取消 / 删除。

下发（POST）在返回前同步做完预检：通过则建 pending 行并交后台线程执行，
不通过则建 precheck_failed 行留痕并回 422 + 自然语言原因。
"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import crud, schemas, task_engines, task_precheck, task_runner
from ...crypto import decrypt_text
from ...database import get_db
from ...models import AgentTask

router = APIRouter(prefix="/tasks")


@router.get("/engines")
def engines(refresh: bool = False, db: Session = Depends(get_db)):
    active = crud.get_active_config(db)
    return {
        "engines": task_engines.probe_all(refresh),
        "concurrency": task_runner.max_workers(),
        "default_timeout": task_runner.DEFAULT_TIMEOUT_SECONDS,
        "running": task_runner.running_count(),
        "active_config": None
        if active is None
        else {"id": active.id, "name": active.name, "model": active.model,
              "provider": active.provider.name if active.provider else ""},
    }


@router.post("", response_model=schemas.TaskOut, status_code=201)
def create_task(body: schemas.TaskCreate, db: Session = Depends(get_db)):
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(422, "任务描述不能为空")
    working_dir = (body.working_dir or "").strip()
    if working_dir:
        folder = Path(working_dir).expanduser()
        # 相对路径会落到 a4agent 自己的进程目录（打包后是安装目录），引擎在那里
        # 读到不该读的项目配置；目录选完后也可能被删，都要在入队前拦住
        if not folder.is_absolute():
            raise HTTPException(422, f"工作目录需要绝对路径：{working_dir}")
        if not folder.is_dir():
            raise HTTPException(422, f"工作目录不存在或不是文件夹：{working_dir}")
        working_dir = str(folder)
    active = crud.get_active_config(db)
    if active is None or active.provider is None:
        raise HTTPException(409, "还没有生效的配置方案，请先在「配置方案」页切换一次")

    api_key = decrypt_text(active.api_key_encrypted)
    if not api_key:
        # 与切换接口同一道闸：空 Key 会让连通预检得到误导性的「密钥无效」
        raise HTTPException(409, "该配置方案的 API Key 解密失败或为空，请到「配置方案」页重新保存一次 Key")
    result = task_precheck.run(db, body.tool, active.provider, api_key, active.model)

    task = AgentTask(
        prompt=prompt,
        tool=body.tool,
        working_dir=working_dir,
        timeout_seconds=body.timeout_seconds or task_runner.DEFAULT_TIMEOUT_SECONDS,
        status=task_runner.PENDING if result["ok"] else task_runner.PRECHECK_FAILED,
        config_id=active.id,
        model=active.model or "",
        precheck_result=json.dumps(result, ensure_ascii=False),
        precheck_ms=result["ms"],
        error_summary="" if result["ok"] else result["reason"],
    )
    if not result["ok"]:
        task.finished_at = datetime.now()
    db.add(task)
    db.commit()
    db.refresh(task)

    if not result["ok"]:
        # 422 的 detail 会被前端原样展示，保持一句话可执行的原因
        raise HTTPException(422, f"预检未通过：{result['reason']}")

    task_runner.submit(task.id)
    return task_runner.task_payload(task)


@router.get("", response_model=list[schemas.TaskOut])
def list_tasks(limit: int = 50, db: Session = Depends(get_db)):
    rows = (
        db.query(AgentTask)
        .order_by(AgentTask.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return [task_runner.task_payload(row) for row in rows]


@router.get("/{task_id}", response_model=schemas.TaskDetail)
def task_detail(task_id: int, db: Session = Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task_runner.task_payload(task, include_output=True)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        return task_runner.cancel(task_id, db)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in (task_runner.PENDING, task_runner.RUNNING):
        raise HTTPException(409, "任务仍在队列中或执行中，请先取消再删除")
    if task.output_path:
        try:
            Path(task.output_path).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(task)
    db.commit()
    return {"deleted": task_id}
