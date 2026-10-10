"""无头任务接口：引擎探测 / 下发 / 列表 / 详情 / 取消 / 删除。

下发（POST）在返回前同步做完预检：通过则建 pending 行并交后台线程执行；
不通过不入队也不落列表行，直接回 422 + 自然语言原因，由前端弹窗前置提示。
"""
import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import config_manager, crud, schemas, task_engines, task_precheck, task_runner
from ...crypto import decrypt_text
from ...database import get_db
from ...models import AgentTask

router = APIRouter(prefix="/tasks")


@router.get("/engines")
def engines(refresh: bool = False, db: Session = Depends(get_db)):
    active = crud.get_active_config(db)
    engines = task_engines.probe_all(refresh)
    for item in engines:
        # 下发前展示「本次将使用的模型」，让工具自身的配置分叉（如 ZCode 的
        # IDE 与 CLI 不同步）对用户可见；读不到的端（dsh/qoder）为空串
        item["current_model"] = config_manager.read_current_model(item["tool"])
    return {
        "engines": engines,
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
    # HTTP 引擎（OpenCode）自带自己的模型与凭据体系（用户在 OpenCode 里配），
    # 不走 a4agent 的配置方案：要求用户先切换一个生效方案，对它毫无意义，
    # 只会把「OpenCode 已经能跑」这种状态卡在一句无关的提示上。
    needs_local_config = body.tool not in task_engines.HTTP_ENGINES
    if needs_local_config:
        if active is None or active.provider is None:
            raise HTTPException(409, "还没有生效的配置方案，请先在「配置方案」页切换一次")
        api_key = decrypt_text(active.api_key_encrypted)
        if not api_key:
            # 与切换接口同一道闸：空 Key 会让连通预检得到误导性的「密钥无效」
            raise HTTPException(409, "该配置方案的 API Key 解密失败或为空，请到「配置方案」页重新保存一次 Key")
    else:
        api_key = ""
    result = task_precheck.run(db, body.tool, active.provider if needs_local_config else None,
                               api_key, active.model if active else "")
    if not result["ok"]:
        # 预检失败不入队也不落列表行：422 的 detail 由前端弹窗原样展示，
        # 保持一句话可执行的原因
        raise HTTPException(422, f"预检未通过：{result['reason']}")

    task = AgentTask(
        prompt=prompt,
        tool=body.tool,
        working_dir=working_dir,
        timeout_seconds=body.timeout_seconds or task_runner.DEFAULT_TIMEOUT_SECONDS,
        status=task_runner.PENDING,
        config_id=active.id if active else None,
        model=(active.model if active else "") or "",
        precheck_result=json.dumps(result, ensure_ascii=False),
        precheck_ms=result["ms"],
        error_summary="",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

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
