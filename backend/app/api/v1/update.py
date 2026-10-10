"""自更新接口。"""
import os

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from ... import updater

router = APIRouter()


class VersionBody(BaseModel):
    version: str


@router.get("/update/check")
def update_check(force: bool = False):
    """检查更新。force=1 时忽略缓存强制重新拉取清单。"""
    return updater.check(force=force)


@router.post("/update/download")
def update_download(body: VersionBody):
    result = updater.start_download(body.version)
    if result.get("error"):
        raise HTTPException(409, result["error"])
    return result


@router.get("/update/progress")
def update_progress():
    return updater.progress()


@router.post("/update/cancel")
def update_cancel():
    return updater.cancel_download()


@router.post("/update/ignore")
def update_ignore(body: VersionBody):
    updater.ignore_version(body.version)
    return {"ok": True}


def _exit_after_apply() -> None:
    """后台任务：响应 flush 后先做退出清理，再退出整个 GUI 进程释放 AppMutex。

    清理与 desktop.main 的退出路径同源：回收无头任务子进程与 llama-server。
    直接 os._exit 会把它们留成孤儿——llama-server 继续占着 8080，更新完成后
    重启必报「端口已被占用」。
    """
    try:
        from ... import task_runner

        task_runner.shutdown_all("应用更新")
    except Exception:
        pass
    try:
        from ... import opencode_listen

        opencode_listen.stop()
    except Exception:
        pass
    try:
        from ...llama import runtime as llama_runtime

        llama_runtime.shutdown()
    except Exception:
        pass
    try:
        from ...database import get_data_dir

        (get_data_dir() / "desktop.json").unlink(missing_ok=True)
    except OSError:
        pass
    os._exit(0)


@router.post("/update/apply")
def update_apply(background: BackgroundTasks):
    try:
        result = updater.apply()
    except Exception as e:
        raise HTTPException(409, str(e))
    background.add_task(_exit_after_apply)
    return result
