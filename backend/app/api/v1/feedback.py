"""应用内反馈接口：Bug 报告 / 功能需求，截图随行，直接邮件送达开发者。

参考 EoListen 的反馈机制（multipart 提交 + 截图入库 + 邮件通知 + 防刷
频控）。本应用无服务器与后台管理页：反馈先落本地库（feedback /
feedback_images），随后把内容与截图以邮件附件送达 eogee@qq.com（配置见
mail_secrets.py）；邮件失败不影响入库，emailed 标志随响应返回。
"""
import asyncio
import os
import platform as _platform
import sys
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import mail, schemas
from ...database import get_db
from ...models import Feedback, FeedbackImage
from ...version import current_version

router = APIRouter(prefix="/feedback")

CONTENT_MAX_CHARS = 2000
CONTACT_MAX_CHARS = 200
FEEDBACK_MAX_IMAGES = 10
FEEDBACK_IMAGE_MAX_BYTES = 1 * 1024 * 1024
ALLOWED_IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp")
DAILY_LIMIT = 20  # 本地单用户防手滑连点（语义同 EoListen 的每日限量）
LOG_TAIL_MAX_LINES = 200


def log_path() -> Path:
    """与 logging_config 一致的应用日志位置；新位置尚无日志时回退改名前的旧路径。"""
    p = Path.home() / ".a4agent" / "logs" / "a4agent.log"
    if p.exists():
        return p
    legacy = Path.home() / ".a4api" / "logs" / "a4api.log"
    return legacy if legacy.exists() else p


def _log_tail(lines: int) -> str:
    p = log_path()
    if not p.exists():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _env_lines() -> list[str]:
    """环境信息（对应 README「提交 Issue 要求」的环境项），服务端采集保证真实。"""
    return [
        f"- a4agent 版本：v{current_version()}",
        f"- 操作系统：{_platform.platform()}",
        f"- 运行形态：{'桌面安装版' if getattr(sys, 'frozen', False) else '开发调试'}",
    ]


@router.get("/context")
def feedback_context():
    """反馈弹窗用环境快照：日志路径/存在性决定「附带日志」勾选是否可用。"""
    p = log_path()
    return {
        "version": current_version(),
        "platform": _platform.platform(),
        "python": _platform.python_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "log_path": str(p),
        "log_exists": p.exists(),
    }


@router.post("", response_model=schemas.FeedbackSubmitted)
async def create_feedback(
    kind: str = Form(...),
    content: str = Form(...),
    contact: str = Form(""),
    include_log: str = Form("0"),
    images: list[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    """提交反馈：内容必填、截图选填（≤10 张 × ≤1MB）；入库后尽力发信。"""
    content = (content or "").strip()
    contact = (contact or "").strip()
    if kind not in ("bug", "feature"):
        raise HTTPException(400, "反馈类型必须是 bug 或 feature")
    if not content:
        raise HTTPException(400, "请填写问题描述")
    if len(content) > CONTENT_MAX_CHARS:
        raise HTTPException(400, f"问题描述不能超过 {CONTENT_MAX_CHARS} 字")
    if len(contact) > CONTACT_MAX_CHARS:
        raise HTTPException(400, f"联系方式不能超过 {CONTACT_MAX_CHARS} 字")

    files = [f for f in (images or []) if f and f.filename]
    if len(files) > FEEDBACK_MAX_IMAGES:
        raise HTTPException(400, f"截图最多 {FEEDBACK_MAX_IMAGES} 张")

    blobs: list[tuple[str, str, bytes]] = []
    for f in files:
        mime = (f.content_type or "").split(";")[0]
        if mime not in ALLOWED_IMAGE_MIMES:
            raise HTTPException(400, f"截图 {f.filename} 不是支持的图片格式")
        # 先用文件对象自身的偏移量拦截超大文件再读字节：Starlette 把超过阈值的
        # 上传落盘缓冲，无条件 read() 会把整个文件拉进内存（攻击面是 OOM）
        f.file.seek(0, os.SEEK_END)
        size = f.file.tell()
        f.file.seek(0)
        if size > FEEDBACK_IMAGE_MAX_BYTES:
            raise HTTPException(400, f"截图 {f.filename} 超过 1MB")
        data = await f.read()
        blobs.append((f.filename or "image.png", f.content_type or "image/png", data))

    # 防刷频控（同 EoListen：每日限量）；本地单用户，按全量表计数即可
    today_count = db.execute(
        select(func.count()).select_from(Feedback).where(
            func.date(Feedback.created_at) == date.today())
    ).scalar_one()
    if today_count >= DAILY_LIMIT:
        raise HTTPException(429, "今日反馈已达上限，请明日再试")

    include_log = include_log in ("1", "true", "True")
    log_excerpt = _log_tail(120) if include_log else ""
    env_lines = _env_lines()

    fb = Feedback(
        kind=kind,
        content=content,
        contact=contact,
        env_info="\n".join(env_lines),
        log_excerpt=log_excerpt,
        image_count=len(blobs),
    )
    db.add(fb)
    db.flush()
    for seq, (name, mime, data) in enumerate(blobs, start=1):
        db.add(FeedbackImage(feedback_id=fb.id, seq=seq, filename=name, mime=mime, data=data))
    db.commit()
    fb_id = fb.id

    # 邮件同步发送（线程内等待，至多一个 SMTP 超时），成功与否回写 emailed
    emailed = await asyncio.to_thread(
        mail.send_feedback_mail, fb_id, kind, content, contact,
        env_lines, log_excerpt, blobs)
    with Session(bind=db.get_bind()) as s:
        row = s.get(Feedback, fb_id)
        if row:
            row.emailed = emailed
            s.commit()
    return schemas.FeedbackSubmitted(id=fb_id, emailed=emailed)
