"""配对请求系统级 toast 监听（B39）。

被控端收到配对请求时，设备页前端只在页面打开时才轮询 /pair/incoming——
页面不开就没人知道有请求进来。本模块在 backend 常驻线程里每 ~1.5 秒读
一次本机 node 的 /v1/pair/session（§23.3 免鉴权回环只读端点，GET 幂等），
会话状态从非 pending 转为 pending 的边沿发系统 toast 提醒用户去设备页确认。

容错口径：任何异常只记日志，绝不影响配对流程；toast 不可用（非 Windows/
系统禁用通知）时静默跳过。状态记忆在 node 未运行/查询失败时不推进，
恢复后不误报。
"""
import logging
import threading

from .. import win_toast
from . import node_client, node_proc

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 1.5        # 轮询节奏（比设备页前端 2s 略快，提醒更及时）

TOAST_TITLE = "a4agent 设备配对"
TOAST_REQUEST_MESSAGE = "有设备请求配对，请在设备互联页确认"


def _notify(title: str, message: str) -> bool:
    """系统 toast 直发（同步直发先例：api/v1/phone.py 的 test_desktop）。

    不可用或任何失败只记日志返回 False，绝不抛出——提醒链不能拖累配对链。
    """
    if not win_toast.AVAILABLE:
        logger.info("系统 toast 不可用（非 Windows 或通知被禁用），跳过配对提醒")
        return False
    try:
        return win_toast.show(title, message)
    except Exception:  # noqa: BLE001 - 提醒失败绝不影响配对流程
        logger.exception("配对提醒 toast 发送失败")
        return False


def _notify_pair_request() -> bool:
    """pending 边沿的提醒入口（测试经 monkeypatch 注入本函数）。"""
    return _notify(TOAST_TITLE, TOAST_REQUEST_MESSAGE)


class PairWatcher:
    """被控端配对请求 toast 监听线程（守护线程；幂等启动，一拍一查）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._last_state = None      # 最近一次读到的会话状态（None=尚未读到）

    # ---------- 生命周期 ----------

    def start(self):
        """幂等启动守护轮询线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="a4agent-pair-watch")
            self._thread.start()
            return self

    def stop(self):
        """请求停止轮询（线程在下拍自然退出）。"""
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.wait(POLL_INTERVAL_SECONDS):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 监听失败绝不影响配对流程
                logger.exception("配对请求监听单拍异常")

    # ---------- 单拍 ----------

    def _tick(self):
        guardian = node_proc.get_guardian()
        port = guardian.read_port()
        if guardian.state not in ("starting", "running") or not port:
            self._last_state = None     # node 不在：记忆清零，恢复后不误报
            return
        try:
            data, _fp = node_client.pair_request(
                "127.0.0.1", port, "GET", "/v1/pair/session",
                loopback_tofu=True)
        except node_client.NodeError as e:
            # 查询失败不推进状态记忆：node 瞬断恢复后 pending 仍会正常提醒
            logger.debug("配对会话查询失败：%s", e.message)
            return
        state = str(data.get("state") or "idle")
        last = self._last_state
        self._last_state = state
        if state == "pending" and last != "pending":
            logger.info("检测到配对请求（%s → pending），发送系统提醒", last)
            try:
                _notify_pair_request()
            except Exception:  # noqa: BLE001 - 提醒回调异常不影响下一拍
                logger.exception("配对提醒回调异常")


_watcher = None   # 惰性单例（应用启动时 start，一次即可）


def get_watcher() -> PairWatcher:
    global _watcher
    if _watcher is None:
        _watcher = PairWatcher()
    return _watcher


def start() -> PairWatcher:
    """应用启动入口：幂等拉起常驻监听线程。"""
    return get_watcher().start()
