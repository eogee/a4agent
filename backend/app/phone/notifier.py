"""任务终态 → 手机推送的桥接：订阅 task_notify 广播，异步派发 ntfy 推送。

task_notify.notify 在任务工作线程里同步调用监听器，这里只做轻量过滤后
把网络 I/O 丢进独立守护线程，ntfy 慢/挂不拖累后续任务的接续。
"""
import logging
import threading

from .. import task_engines, task_notify
from . import config, ntfy

logger = logging.getLogger(__name__)

# 通知类型词（统一样式第 2 行）；cancelled 默认不推手机，开关见 config.DEFAULT_EVENTS
_STATUS_META = {
    "success": "已完成",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已取消",
}

_ERROR_MAX = 200
# 「AI 最后输出」推送截断，与 hooks.transcript.MAX_LENGTH 同限
_LAST_OUTPUT_MAX = 1000

_registered = False
_register_lock = threading.Lock()


def format_notice(topic: str, type_word: str, app: str, last_output: str = "",
                  include_last_output: bool = False) -> tuple[str, str]:
    """统一通知样式（用户定稿）：标题=话题名称，正文=类型\\n应用名称。

    两条链（hook / 任务）的桌面横幅与手机推送全部走这里，样式只有这一份；
    原生 toast 的来源区（logo + a4agent）由系统按 AUMID 自带，不用拼。
    include_last_output 只给手机推送开：追加「AI 最后输出」段，桌面横幅
    短版不带——1000 字会把系统 toast 撑爆。
    """
    title = (topic or "").strip() or "a4agent 通知"
    message = f"{type_word}\n{app}"
    last = (last_output or "").strip()
    if include_last_output and last:
        message += (f"\n\nAI 最后输出：\n{last[:_LAST_OUTPUT_MAX]}"
                    + ("..." if len(last) > _LAST_OUTPUT_MAX else ""))
    return title, message


def compose(event: dict, include_last_output: bool = False) -> tuple[str, str]:
    """任务链事件 → 统一样式。纯函数便于测试。

    下发任务第三行带任务编号（用户决议保留）：任务 #7 · Claude Code；
    OpenCode 会话事件没有编号，第三行就是 OpenCode。
    """
    status = str(event.get("status") or "")
    type_word = _STATUS_META.get(status, status or "通知")
    label = task_engines.ENGINE_LABELS.get(event.get("tool", ""), event.get("tool", ""))
    if event.get("session_id"):
        app = label
    else:
        app = f"任务 #{event.get('id')} · {label}"
    title = str(event.get("prompt") or "").strip() or "a4agent 通知"
    message = f"{type_word}\n{app}"
    error = (event.get("error") or "").strip()
    if error and status != "success":
        message += f"\n{error[:_ERROR_MAX]}" + ("..." if len(error) > _ERROR_MAX else "")
    last = str(event.get("last_output") or "").strip()
    if include_last_output and last:
        message += (f"\n\nAI 最后输出：\n{last[:_LAST_OUTPUT_MAX]}"
                    + ("..." if len(last) > _LAST_OUTPUT_MAX else ""))
    return title, message


def _deliver(cfg: dict, event: dict) -> None:
    title, message = compose(event, include_last_output=True)
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
