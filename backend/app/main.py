"""FastAPI 应用入口（含静态文件托管）。"""
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import models  # noqa: F401  # 注册模型建表
from .api.v1 import (configs, desktop, feedback, fs, llama, mcp, providers,
                     skills, switch, tasks, update)
from .database import Base, engine, ensure_schema
from .logging_config import setup_logging
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
    app.include_router(fs.router, prefix="/api/v1", tags=["fs"])
    app.include_router(desktop.router, prefix="/api/v1", tags=["desktop"])

    # 前端静态资源：开发/桌面运行时由后端统一托管
    frontend_dir = _frontend_dir()
    if frontend_dir.exists():
        app.mount("/", _FrontendStatic(directory=str(frontend_dir), html=True), name="frontend")

    return app


app = create_app()
