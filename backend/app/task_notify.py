"""任务终态提醒：桌面回调 + Windows 任务栏闪烁（P0 零新增依赖）。

窗口开着时的 layer 提示由前端轮询状态差值得到自己弹；本模块只负责把事件
广播给桌面壳（窗口隐藏时闪任务栏），以及 P1 接托盘/系统通知时的挂载点。
"""
import ctypes
import logging

logger = logging.getLogger(__name__)

_listeners: list = []

_FLASHW_ALL = 0x00000003
_FLASHW_TIMERNOFG = 0x0000000C


def subscribe(listener) -> None:
    """注册回调：listener(event: dict)。桌面壳启动时调用一次。"""
    if listener not in _listeners:
        _listeners.append(listener)


def unsubscribe(listener) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def notify(event: dict) -> None:
    """把任务终态广播给订阅者；单个订阅者异常不影响其它订阅者与任务落库。"""
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - 提醒失败不能拖累任务流程
            logger.exception("任务提醒回调失败")


def flash_taskbar(hwnd) -> bool:
    """任务栏按钮闪烁直到窗口重新获得焦点；非 Windows 或无句柄时静默跳过。"""
    if not hwnd:
        return False
    try:
        import sys

        if sys.platform != "win32":
            return False
        from ctypes import wintypes

        class _FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        info = _FLASHWINFO(
            ctypes.sizeof(_FLASHWINFO), hwnd, _FLASHW_ALL | _FLASHW_TIMERNOFG, 5, 0
        )
        return bool(ctypes.windll.user32.FlashWindowEx(ctypes.byref(info)))
    except (OSError, AttributeError):
        logger.debug("任务栏闪烁不可用", exc_info=True)
        return False
