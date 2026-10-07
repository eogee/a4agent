"""移除托管配置的用户操作接口。

两个端点：
- GET  /removal/status  预览：还需要清理吗？会动哪些文件？
- POST /removal/cleanup 执行：先永久快照，再逐端清理，返回逐项结果

设计原则：任何写入用户文件的操作都先给出可预览的报告，用户看清再执行；
执行时全程有快照兜底，且快照失败即中止。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import removal
from ...database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/removal")


@router.get("/status")
def status():
    """预览：是否还有托管残留，以及迁移指引文档的位置。"""
    return {
        "needs_cleanup": removal.needs_cleanup(),
        "guide": "docs/迁移对照表-三端API配置.md",
    }


@router.post("/cleanup")
def cleanup(dry_run: bool = True, db: Session = Depends(get_db)):
    """执行清理。

    默认 dry_run=True——先看报告再确认，这是涉及用户文件的操作，
    不该在用户不知情时直接改。传 dry_run=false 才真正写盘。
    """
    try:
        report = removal.cleanup_all(dry_run=dry_run)
    except Exception as e:
        logger.exception("托管配置清理失败")
        raise HTTPException(500, f"清理失败：{e}")

    if report["errors"]:
        # 有错误时不静默吞掉——用户需要知道哪一步没成功
        return {"ok": False, "dry_run": dry_run, **report}
    return {"ok": True, "dry_run": dry_run, **report}