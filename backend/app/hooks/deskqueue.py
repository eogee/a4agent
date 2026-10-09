"""桌面弹窗请求队列：hook 进程只写请求文件，a4agent 常驻进程周期消费代发。

为什么不直接弹：ZCode 的执行端口会在 hook 命令退出时杀掉整棵进程树，
hook 里 fire-and-forget 的气泡来不及渲染（a4phone 实测教训）。队列文件
不依赖进程存活，a4agent 常驻进程每 2 秒扫描、弹后删除；停运期间积压超过
TTL 的请求直接丢弃，避免恢复后批量轰炸。
"""
import json
import logging
import os
import tempfile
import time
from pathlib import Path

from ..database import get_data_dir

logger = logging.getLogger(__name__)

TTL_SECONDS = 60


def queue_dir() -> Path:
    return get_data_dir() / "notify-queue"


def queue_notify(title: str, message: str) -> bool:
    """原子写一条桌面通知请求；失败只影响这一次弹窗。"""
    try:
        d = queue_dir()
        d.mkdir(parents=True, exist_ok=True)
        name = f"notify-{os.getpid()}-{int(time.time() * 1000)}.json"
        file = d / name
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-notify-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"title": title, "message": message, "ts": int(time.time() * 1000)}, f)
            os.replace(tmp, file)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return True
    except OSError:
        logger.warning("桌面通知请求写入失败", exc_info=True)
        return False


def _safe_unlink(file: Path) -> None:
    """删除队列文件，失败只记日志不抛。

    `missing_ok=True` 只吞 FileNotFoundError，但本机存在 safe-delete shim
    会把 unlink 改走回收站操作，该路径失败时抛的是 OSError（0x80070002等），
    不会被 missing_ok 捕获。一旦抛出就会中断 process_queue 的整轮循环，
    后续通知文件全部卡在目录里弹不出来。
    """
    try:
        file.unlink(missing_ok=True)
    except OSError:
        logger.warning("桌面通知请求文件删除失败：%s", file.name, exc_info=True)


def process_queue(show, ttl_seconds: int = TTL_SECONDS) -> int:
    """消费队列：未过期请求逐条调 show(title, message) 后删除。

    show 可注入以便测试；show 与文件删除的异常都不阻断后续条目。
    """
    d = queue_dir()
    try:
        entries = list(d.iterdir())
    except OSError:
        return 0  # 目录不存在：无事可做
    processed = 0
    now_ms = int(time.time() * 1000)
    for file in entries:
        if not file.name.endswith(".json") or not file.is_file():
            continue
        try:
            req = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _safe_unlink(file)  # 损坏文件直接丢弃
            continue
        if not isinstance(req, dict) or not isinstance(req.get("title"), str) \
                or not isinstance(req.get("message"), str):
            _safe_unlink(file)
            continue
        if now_ms - int(req.get("ts") or 0) > ttl_seconds * 1000:
            _safe_unlink(file)  # 过期积压：丢弃防轰炸
            continue
        try:
            show(req["title"], req["message"])
            processed += 1
        except Exception:  # noqa: BLE001 - 单条弹窗失败不阻断其余条目
            logger.warning("桌面通知弹出失败：%s", file.name, exc_info=True)
        finally:
            _safe_unlink(file)
    return processed
