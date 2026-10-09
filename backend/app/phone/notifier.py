"""任务终态 → 手机推送的桥接：订阅 task_notify 广播，异步派发 ntfy 推送。

task_notify.notify 在任务工作线程里同步调用监听器，这里只做轻量过滤后
把网络 I/O 丢进独立守护线程，ntfy 慢/挂不拖累后续任务的接续。
"""
import logging
import threading

from .. import task_engines, task_notify
from . import config, ntfy

logger = logging.getLogger(__name__)

# (标题状态词, 推送图标)；cancelled 默认不推，开关见 config.DEFAULT_EVENTS
_STATUS_META = {
    "success": ("已完成", "✅"),
    "failed": ("失败", "❌"),
    "timeout": ("超时", "⏱"),
    "cancelled": ("已取消", "🚫"),
}

_registered = False
_register_lock = threading.Lock()


def compose(event: dict) -> tuple[str, str]:
    """事件 → (标题, 正文)。纯函数便于测试。"""
    task_id = event.get("id")
    status = event.get("status", "")
    meta = _STATUS_META.get(status)
    status_word, icon = meta if meta else (status or "结束", "🔔")
    label = task_engines.ENGINE_LABELS.get(event.get("tool", ""), event.get("tool", ""))
    title = f"任务 #{task_id} {status_word}"
    message = f"{icon} [{label}] {event.get('prompt', '')}"
    error = (event.get("error") or "").strip()
    if error and status != "success":
        message += f"\n{error}"
    return title, message


def _deliver(cfg: dict, event: dict) -> None:
    title, message = compose(event)
    ok, reason = ntfy.publish(cfg, title, message)
    if not ok:
        logger.warning("任务 #%s 手机通知未送达：%s", event.get("id"), reason)


def _on_event(event: dict) -> None:
    try:
        cfg = config.load()
        if not cfg.get("enabled"):
            return
        if not cfg.get("events", {}).get(event.get("status")):
            return
        threading.Thread(target=_deliver, args=(cfg, event), daemon=True,
                         name="a4agent-phone-push").start()
    except Exception:  # noqa: BLE001 - 提醒失败不能拖累任务流程
        logger.exception("手机通知派发异常")


def register() -> None:
    """向 task_notify 订阅任务终态；create_app 启动时调用，幂等。"""
    global _registered
    with _register_lock:
        if _registered:
            return
        task_notify.subscribe(_on_event)
        _registered = True
