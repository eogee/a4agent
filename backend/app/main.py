"""FastAPI 应用入口（含静态文件托管）。"""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  # 注册模型建表
from .api.v1 import (configs, desktop, feedback, fs, llama, mcp, opencode,
                     phone, providers, removal, skills, switch, tasks, update)
from .database import Base, engine, ensure_schema
from .logging_config import setup_logging
from .phone import notifier
from .seed import seed_providers
from .version import current_version

setup_logging()


def _frontend_dir() -> Path:
    """定位前端目录：打包后用 sys._MEIPASS，开发时用项目根。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", ".")) / "frontend"
    return Path(__file__).resolve().parent.parent.parent / "frontend"


class _FrontendStatic(StaticFiles):
    """前端资源每次回源校验：缺 Cache-Control 时浏览器按 Last-Modified 启发式长期缓存，
    升级后会继续显示旧界面；no-cache 配合 ETag 命中 304，开销可忽略。"""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    app = FastAPI(title="a4agent", version=current_version())

    # 本地工具只允许本机页面访问 API，避免任意网站读取/操作配置
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "x-api-key"],
    )

    Base.metadata.create_all(bind=engine)
    ensure_schema()
    seed_providers()

    app.include_router(providers.router, prefix="/api/v1", tags=["providers"])
    app.include_router(configs.router, prefix="/api/v1", tags=["configs"])
    app.include_router(switch.router, prefix="/api/v1", tags=["switch"])
    app.include_router(skills.router, prefix="/api/v1", tags=["skills"])
    app.include_router(mcp.router, prefix="/api/v1", tags=["mcp"])
    app.include_router(update.router, prefix="/api/v1", tags=["update"])
    app.include_router(feedback.router, prefix="/api/v1", tags=["feedback"])
    app.include_router(llama.router, prefix="/api/v1", tags=["llama"])
    app.include_router(tasks.router, prefix="/api/v1", tags=["tasks"])
    app.include_router(removal.router, prefix="/api/v1", tags=["removal"])
    app.include_router(fs.router, prefix="/api/v1", tags=["fs"])
    app.include_router(desktop.router, prefix="/api/v1", tags=["desktop"])
    app.include_router(phone.router, prefix="/api/v1", tags=["phone"])
    app.include_router(opencode.router, prefix="/api/v1", tags=["opencode"])

    # 任务终态 → 手机推送（enabled 关闭时监听器内部直接跳过）
    notifier.register()
    _start_notify_queue_consumer()
    _start_opencode_events()

    # 前端静态资源：开发/桌面运行时由后端统一托管
    frontend_dir = _frontend_dir()
    if frontend_dir.exists():
        app.mount("/", _FrontendStatic(directory=str(frontend_dir), html=True), name="frontend")

    return app


_OC_EVENT_STREAM = None


def _start_opencode_events() -> None:
    """订阅 OpenCode 事件流（仅在用户启用接入时）。

    用途是**把 OpenCode 侧发生的会话终局也推给用户**，而不只是 a4agent 自己
    下发的任务：用户可能正在 OpenCode 的网页端里干别的活，那边的会话跑完、
    失败或卡在权限审批上，都值得用同一套桌面横幅 + 手机推送告知。

    订阅失败（服务没起 / 未启用 / 鉴权不对）一律只记日志：通知是尽力而为，
    不能因此拖住应用启动。
    """
    import logging
    import threading

    from .api.v1 import opencode as opencode_api
    from . import opencode_client, task_notify

    global _OC_EVENT_STREAM
    logger = logging.getLogger(__name__)

    def _run() -> None:
        cfg = opencode_api.load()
        if not cfg.get("enabled"):
            return
        base, password = opencode_client.resolve_connection(cfg)
        if not password:
            logger.info("OpenCode 未启用：无服务密码，跳过事件订阅")
            return

        def _on_event(payload: dict) -> None:
            typ = str(payload.get("type") or "")
            props = payload.get("properties") if isinstance(payload.get("properties"), dict) else {}
            session_id = str((props or {}).get("sessionID") or payload.get("sessionID") or "")
            if not session_id:
                return
            status = _OC_STATUS_BY_EVENT.get(typ)
            if status is None:
                return
            info = {}
            try:
                info = opencode_client.get_session(session_id, base, password)
            except opencode_client.OpenCodeError:
                pass
            task_notify.notify(
                {
                    "id": f"oc:{session_id[-8:]}",
                    "tool": "opencode",
                    "status": status,
                    "error": str(info.get("error") or ""),
                    "prompt": (str(info.get("title") or "OpenCode 会话"))[:80],
                    "session_id": session_id,
                }
            )

        try:
            stream = opencode_client.EventStream(base, password, _on_event)
            stream.start()
            _set_stream(stream)
            logger.info("已订阅 OpenCode 事件流：%s", base)
        except Exception:  # noqa: BLE001 - 通知链路失败不影响应用
            logger.exception("OpenCode 事件订阅启动失败")

    threading.Thread(target=_run, daemon=True, name="a4agent-oc-events-boot").start()


def _set_stream(stream) -> None:
    global _OC_EVENT_STREAM
    _OC_EVENT_STREAM = stream


def stop_opencode_events() -> None:
    """应用退出时停掉事件订阅（幂等）。"""
    if _OC_EVENT_STREAM is not None:
        try:
            _OC_EVENT_STREAM.stop()
        except Exception:  # noqa: BLE001
            pass


# OpenCode 终局事件 → a4agent 的终态词。实测（docs/OpenCode-实测记录.md）成功
# 走 session.execution.succeeded，失败走 session.step.failed + execution.failed。
_OC_STATUS_BY_EVENT = {
    "session.execution.succeeded": "success",
    "session.execution.failed": "failed",
    "session.execution.interrupted": "cancelled",
}


def _start_notify_queue_consumer() -> None:
    """桌面弹窗请求队列消费：会话 hook 只写请求文件（ZCode 会在 hook 退出时
    杀进程树，hook 内弹窗来不及渲染），由本常驻进程周期代发。"""
    import logging
    import threading
    import time

    from .hooks import deskqueue
    from .win_toast import show as toast_show

    def _loop() -> None:
        while True:
            try:
                deskqueue.process_queue(toast_show)
            except Exception:  # noqa: BLE001 - 队列消费失败不影响主服务
                logging.getLogger(__name__).exception("桌面通知队列消费异常")
            time.sleep(2)

    threading.Thread(target=_loop, daemon=True, name="a4agent-notify-queue").start()


app = create_app()
