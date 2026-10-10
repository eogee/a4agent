"""Windows 系统通知横幅：Shell_NotifyIconW 气泡通知，零第三方依赖。

Win10/11 会把气泡自动渲染为原生 toast。实现要点：
- 消息泵跑在守护线程：气泡事件（超时/用户点击）只有窗口过程能收到，
  show() 调用方（任务提醒回调）不能被阻塞；
- 横幅仍在展示时再来一条，用 NIM_MODIFY 原位改写，不叠托盘图标；
- 用户点击横幅触发 on_click 回调（唤回主窗口）；
- 任何失败（非 win32、组策略禁用通知等）静默记日志返回 False，
  绝不影响任务流程——与 task_notify 的容错约定一致。
"""
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AVAILABLE = sys.platform == "win32"

_lock = threading.Lock()
# 共享状态，_lock 保护；on_click 由调用方注入（任意可调用对象）
_state: dict = {"icon_up": False, "on_click": None}

if AVAILABLE:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _shell32 = ctypes.windll.shell32
    _kernel32 = ctypes.windll.kernel32

    # DefWindowProcW 不声明原型时 ctypes 按 c_int 传参，64 位下 LPARAM 超范围
    # 会 OverflowError；回调里每个未处理消息都会触发
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = ctypes.c_longlong

    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_INFO = 0x1, 0x2, 0x10
    NIIF_RESPECT_QUIET_TIME = 0x80  # 勿扰/专注时段静默，不吵用户
    # 注：NIIF_USER（正文展示大图标）实测过——本机 Win11 对 hBalloonIcon 指定
    # 文件加载图标会整体拒绝 NIM_ADD，且大图标观感差，按用户决议不展示
    NIN_BALLOONHIDE = 0x403
    NIN_BALLOONTIMEOUT = 0x404
    NIN_BALLOONUSERCLICK = 0x405
    WM_TRAY_CALLBACK = 0x8000 + 0x11  # WM_APP 段自定义：托盘气泡事件
    WM_TOAST_QUIT = 0x8000 + 0x12  # 泵线程收尾（图标已被外部删掉等）
    WM_TIMER = 0x0113
    IDI_APPLICATION = 32512
    IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
    _TOAST_TIMER_ID = 1
    _TOAST_FALLBACK_MS = 20000  # 个别环境不回气泡事件时的兜底收尾

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD), ("data2", wintypes.WORD),
            ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),  # 旧 uTimeout，现代系统忽略
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM,
    )

    _nid = _NOTIFYICONDATAW()
    _nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
    _nid.uID = 1
    _nid.uCallbackMessage = WM_TRAY_CALLBACK
    _nid.uVersion = 10000
    _nid.dwInfoFlags = NIIF_RESPECT_QUIET_TIME
    _nid.szTip = "a4agent"

    _WNDCLASSW_FIELDS = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", ctypes.c_wchar_p),
    ]

    _pump_thread: threading.Thread | None = None
    _pump_ready = threading.Event()
    # wndproc 回调必须常驻引用，被 GC 后系统回调进野指针直接崩进程；
    # WNDPROC 是 WINFUNCTYPE 实例不能当类型注解，Any 即可
    _wndproc_ref: Any = None
    _class_registered = False

    def _load_icon():
        # 共享系统图标必须走 LoadIconW：LoadImageW 对 NULL 实例的
        # IDI_APPLICATION 实测返回 0（GetLastError 1813），null 图标会让
        # 带 NIF_ICON 的 NIM_ADD 被托盘直接拒绝
        try:
            return _user32.LoadIconW(None, IDI_APPLICATION)
        except Exception:  # noqa: BLE001 - 图标拿不到不影响通知
            return None

    def _app_icon_path() -> str:
        """打包后 logo.ico 在 _MEIPASS/resources（spec/build 均整目录带上）。"""
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", "."))
        else:
            base = Path(__file__).resolve().parent.parent.parent
        return str(base / "resources" / "logo.ico")

    def _load_app_icon():
        """应用 logo 大图标：横幅正文左侧展示（NIIF_USER + hBalloonIcon）。"""
        try:
            path = _app_icon_path()
            if not Path(path).is_file():
                return None
            return _user32.LoadImageW(
                None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
        except Exception:  # noqa: BLE001 - 图标拿不到回退系统图标
            return None

    def _pump_hwnd():
        """泵线程创建隐藏窗口并进入消息循环，把窗口句柄交给调用方。"""
        global _class_registered
        hinstance = _kernel32.GetModuleHandleW(None)
        if not _class_registered:
            class_w = type("WNDCLASSW", (ctypes.Structure,),
                           {"_fields_": _WNDCLASSW_FIELDS})
            wc_struct = class_w()
            wc_struct.lpfnWndProc = _wndproc_ref
            wc_struct.lpszClassName = "a4agent-toast"
            wc_struct.hInstance = hinstance
            _user32.RegisterClassW(ctypes.byref(wc_struct))
            _class_registered = True
        hwnd = _user32.CreateWindowExW(
            0, "a4agent-toast", None, 0, 0, 0, 0, 0,
            None, None, hinstance, None,
        )
        return hwnd

    def _finish_pump(hwnd):
        """气泡收场：删托盘图标、停兜底定时器、退出消息循环。"""
        with _lock:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(_nid))
            _state["icon_up"] = False
            _state["on_click"] = None
        _user32.KillTimer(hwnd, _TOAST_TIMER_ID)
        _user32.PostQuitMessage(0)

    def _make_wndproc():
        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_TRAY_CALLBACK:
                if lparam == NIN_BALLOONUSERCLICK:
                    with _lock:
                        on_click = _state["on_click"]
                        _state["on_click"] = None
                    if on_click:
                        try:
                            on_click()
                        except Exception:  # noqa: BLE001 - 点击回调失败不影响收场
                            logger.exception("通知横幅点击回调失败")
                    _finish_pump(hwnd)
                elif lparam in (NIN_BALLOONHIDE, NIN_BALLOONTIMEOUT):
                    _finish_pump(hwnd)
                return 0
            if msg == WM_TIMER and wparam == _TOAST_TIMER_ID:
                _finish_pump(hwnd)
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        return WNDPROC(wndproc)

    def _pump():
        """消息泵主体：窗口存活期间接收气泡事件，收场后线程自然结束。"""
        global _pump_thread
        try:
            hwnd = _pump_hwnd()
            if not hwnd:
                logger.warning("通知横幅隐藏窗口创建失败")
                return
            with _lock:
                _nid.hWnd = hwnd
                # hIcon 只影响横幅展示期的托盘槽位小图标（用 logo 比通用图标体面），
                # 通知正文不带大图标（用户决议：原生纯文字样式）
                _nid.hIcon = _load_app_icon() or _load_icon()
            _pump_ready.set()
            msg = wintypes.MSG()
            # GetMessage 返回 0（WM_QUIT）或 -1（错误）都结束循环
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:  # noqa: BLE001 - 通知横幅整体是尽力而为
            logger.exception("通知横幅消息泵异常")
        finally:
            _pump_ready.clear()
            with _lock:
                _pump_thread = None


def show(title: str, message: str, on_click=None) -> bool:
    """发桌面通知横幅：原生 WinRT toast 优先（有声、来源区 a4agent+logo、
    无消息泵崩溃面），失败降级 legacy 气泡（本文件 _legacy_show）。"""
    if not AVAILABLE:
        return False
    try:
        from . import win_toast_native

        ok, detail = win_toast_native.show(title, message, on_click=on_click)
        if ok:
            return True
        logger.warning("原生 toast 未发出，降级 legacy 气泡：%s", detail)
    except Exception:  # noqa: BLE001 - 原生链路任何异常都走降级
        logger.exception("原生 toast 调用异常，降级 legacy 气泡")
    try:
        return _legacy_show(title, message, on_click)
    except Exception:  # noqa: BLE001 - 横幅失败不能拖累任务提醒链
        logger.exception("legacy 气泡横幅异常")
        return False


def _legacy_show(title: str, message: str, on_click=None) -> bool:
    """Shell_NotifyIconW 气泡横幅（legacy）：原生 toast 不可用时的降级路径。"""
    if not AVAILABLE:
        return False
    try:
        global _pump_thread, _wndproc_ref
        with _lock:
            if _pump_thread is None:
                _wndproc_ref = _make_wndproc()
                _pump_ready.clear()
                _pump_thread = threading.Thread(
                    target=_pump, daemon=True, name="a4agent-toast")
                _pump_thread.start()
        # 等待必须在锁外：泵线程要拿 _lock 写 _nid.hWnd 之后才会 set 事件，
        # 持锁等待会死锁到超时（首发横幅因此全部失败）
        if not _pump_ready.wait(timeout=3):
            logger.warning("通知横幅泵线程未就绪")
            return False

        with _lock:
            if not _nid.hWnd:
                return False
            _nid.szInfoTitle = (title or "")[:64]
            _nid.szInfo = (message or "")[:256]
            _nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_INFO
            _state["on_click"] = on_click
            action = NIM_MODIFY if _state["icon_up"] else NIM_ADD
            ok = bool(_shell32.Shell_NotifyIconW(action, ctypes.byref(_nid)))
            need_retry = not ok and action == NIM_ADD
        if need_retry:
            # 实测首发可能被托盘瞬时拒绝：锁外退避重试；勿扰标记个别系统不认
            for flags in (NIIF_RESPECT_QUIET_TIME, 0):
                time.sleep(0.3)
                with _lock:
                    _nid.dwInfoFlags = flags
                    ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(_nid)))
                    if ok:
                        _state["icon_up"] = True
                        _user32.SetTimer(_nid.hWnd, _TOAST_TIMER_ID,
                                         _TOAST_FALLBACK_MS, None)
                    _nid.dwInfoFlags = NIIF_RESPECT_QUIET_TIME
                if ok:
                    break
            if not ok:
                logger.warning("系统通知横幅发起失败（Shell_NotifyIcon 拒绝）")
            return ok
        with _lock:
            if ok:
                if action == NIM_ADD:
                    _state["icon_up"] = True
                _user32.SetTimer(_nid.hWnd, _TOAST_TIMER_ID, _TOAST_FALLBACK_MS, None)
            else:
                logger.warning("系统通知横幅改写失败")
        return ok
    except Exception:  # noqa: BLE001 - 横幅失败不能拖累任务提醒链
        logger.exception("系统通知横幅异常")
        return False
