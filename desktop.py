"""桌面入口：pywebview 加载 FastAPI 服务。

用法：
    python desktop.py
"""
import os
import socket
import sys
import threading

# 允许从项目根目录导入 backend 包（pyinstaller 打包时也依赖此路径策略）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if "--proxy-stop" in sys.argv:
    # 卸载/升级时停止后台翻译代理。必须在此处短路，
    # 避免拉入 backend.app.main 触发 FastAPI 初始化（建库、seed 等副作用）。
    from backend.app.proxy_standalone import stop_proxy

    stop_proxy()
    sys.exit(0)

if "--apply-update" in sys.argv:
    # 自更新应用阶段（由 updater.apply 以 DETACHED_PROCESS 独立拉起）：
    # 等待主实例退出并释放 AppMutex，然后启动安装器并立即退出。
    # 必须短路在 import webview / FastAPI / acquire() 之前，仅用 stdlib。
    import ctypes
    import subprocess
    import time
    from ctypes import wintypes

    _MUTEX = "Local\\A4AgentDesktopApp"
    _ERROR_ALREADY_EXISTS = 183

    def _wait_mutex_released(timeout_s: float = 20) -> bool:
        """轮询等待主实例释放 AppMutex；拿到所有权立即释放给安装器。

        与 singleton.acquire 同一道安全通道：use_last_error=True +
        ctypes.get_last_error()，误读 LastError 会把「主实例还在」判成
        「已退出」，提前拉起安装器撞正在被占用的 _internal。
        """
        if sys.platform != "win32":
            time.sleep(2)  # 非 Windows 开发兜底
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            handle = kernel32.CreateMutexW(None, False, _MUTEX)
            if not handle:  # 创建失败（如权限）→ 重试，不当作已就绪
                time.sleep(0.2)
                continue
            if ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)  # 立即释放，让安装器能创建互斥体
                return True
            kernel32.CloseHandle(handle)
            time.sleep(0.2)
        return False

    idx = sys.argv.index("--apply-update")
    installer = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
    if not installer:
        sys.exit(2)
    # 主实例未在超时内释放 AppMutex（如异常卡死）→ 放弃应用，避免 Inno 覆盖失败/残留
    if not _wait_mutex_released():
        sys.exit(3)
    # 用 Popen 启动可见安装向导后立即退出：不 run 等待，否则本进程会一直占用
    # _internal 的 DLL 句柄，Inno 升级时 DelTree(_internal) 将残留旧文件。
    subprocess.Popen([installer], close_fds=True)
    time.sleep(1)
    os._exit(0)

if "--toast-wake" in sys.argv:
    # 原生 toast 点击唤醒：横幅 activationType="protocol" + launch="a4agent://wake"，
    # 协议处理器（见 win_toast_native.ensure_aumid 注册）指向本命令，系统在用户
    # 点击横幅时拉起新进程。必须短路在重型 import 之前；唤醒常驻实例主窗口，
    # 失败静默退出（通知点击不能报错打扰）。
    import json as _json
    import pathlib as _pathlib
    import urllib.request as _urllib

    try:
        override = os.environ.get("A4AGENT_DATA_DIR") or os.environ.get("A4API_DATA_DIR")
        if override:
            base = _pathlib.Path(override)
        elif getattr(sys, "frozen", False):
            base = _pathlib.Path(os.environ.get("APPDATA", str(_pathlib.Path.home()))) / "a4agent"
        else:
            base = _pathlib.Path(__file__).resolve().parent / "backend" / "database"
        port = int(_json.loads((base / "desktop.json").read_text(encoding="utf-8"))["port"])
        req = _urllib.Request(
            f"http://127.0.0.1:{port}/api/v1/desktop/wake", method="POST",
            data=b"{}", headers={"Content-Type": "application/json"})
        _urllib.urlopen(req, timeout=3).read()
    except Exception:  # noqa: BLE001 - 唤醒失败静默
        pass
    sys.exit(0)

if len(sys.argv) >= 2 and sys.argv[1] == "hook":
    # Hook 子命令：终端宿主（Claude Code / ZCode 等）以管道重定向调用本进程，
    # 读 stdin 载荷、stdout 回写决策。必须短路在重型 import 之前——hook 是
    # AI 每次事件都调用的热路径，启动延迟直接挂在用户每次工具调用上。
    from backend.app.hooks.dispatch import run_hook_cli

    sys.exit(run_hook_cli(sys.argv[2:]))

import webview  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.singleton import acquire  # noqa: E402
from backend.app.desktop_recovery import (  # noqa: E402
    clear_instance,
    recover_or_restart,
    wake_running_instance,
    write_instance,
)


_WINDOW = None
# pywebview 的 Window 没有 visible 属性，可见性由关窗/唤回事件自己记
_STATE = {"quitting": False, "visible": True, "tray": None}


class DesktopApi:
    """暴露给前端 JS 的原生对话框能力（window.pywebview.api.*）。

    仅在 pywebview 桌面环境存在；纯浏览器打开时前端自动退回手动输入路径。
    """

    def select_folder(self):
        """弹出系统文件夹选择对话框，返回所选路径；取消返回 None。"""
        import webview as _webview

        win = _webview.windows[0] if _webview.windows else None
        if win is None:
            return None
        result = win.create_file_dialog(_webview.FileDialog.FOLDER)
        return result[0] if result else None

    def select_file(self):
        """弹出系统文件选择对话框（单选），返回所选路径；取消返回 None。"""
        import webview as _webview

        win = _webview.windows[0] if _webview.windows else None
        if win is None:
            return None
        try:
            result = win.create_file_dialog(
                _webview.FileDialog.OPEN, allow_multiple=False,
                file_types=("所有文件 (*.*)", "所有文件 (*.*)"),
            )
        except Exception:
            result = win.create_file_dialog(_webview.FileDialog.OPEN)
        return result[0] if result else None

    def running_tasks(self):
        """当前在跑/排队的无头任务数：前端据此决定关窗确认与退出按钮是否可用。"""
        from backend.app import task_runner

        return {"running": task_runner.outstanding_count()}

    def quit_app(self):
        """应用内「退出」：放行关窗策略并关闭窗口，由 main() 统一回收后台资源。"""
        _STATE["quitting"] = True
        if _WINDOW is not None:
            try:
                _WINDOW.destroy()
            except Exception:  # noqa: BLE001 - 兜底：销毁失败则关闭
                try:
                    _WINDOW.close()
                except Exception:
                    pass
        return {"ok": True}


def find_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(port: int) -> None:
    import uvicorn

    # 不用 uvicorn 默认日志配置：它对 uvicorn.error 设 propagate=False，
    # 窗口化打包下 stderr 丢弃，接口 500 的堆栈会完全不可见。
    # log_config=None 让错误沿 root logger 落到 ~/.a4agent/logs/a4agent.log。
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", log_config=None)


def _hwnd_of(win) -> int:
    """尽力取得原生窗口句柄（任务栏闪烁用），拿不到返回 0。

    pywebview 6 在 Windows 上把 WinForms 窗体挂在 Window.native，句柄是
    .NET IntPtr（.Handle.ToInt64()）；Window 本身没有 hwnd/handle 属性可拿。
    """
    native = getattr(win, "native", None)
    if native is not None:
        try:
            return int(native.Handle.ToInt64())
        except (AttributeError, TypeError, ValueError):
            pass
    for attr in ("hwnd", "handle"):
        try:
            value = getattr(win, attr, None)
            if value:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _bring_to_front(win) -> None:
    """把窗口交给系统置前；句柄探测不到时保持已显示的状态即可。"""
    hwnd = _hwnd_of(win)
    if not hwnd or sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.SetForegroundWindow(hwnd)
    except (OSError, AttributeError):
        pass


def _apply_closing_policy(win, state) -> None:
    """关窗不等于退出：拦截关闭并隐藏窗口，真正退出走应用内「退出」按钮。

    pywebview 的 closing 事件只在「handler 返回值严格为 False」时取消关闭，
    且触发时按 Event.set 的调用约定给的是零参调用（winforms 不传任何参数）——
    handler 必须是零参并显式 return False，否则窗口会照常销毁、任务全被带走。
    不在 closing 回调里弹原生对话框——该回调运行在窗口消息线程上，
    阻塞式原生对话框有死锁风险。
    """

    def on_closing():
        if state["quitting"]:
            return None  # 应用内「退出」：放行，走正常回收流程
        state["visible"] = False
        try:
            win.hide()
        except Exception:  # noqa: BLE001 - 隐藏失败也不能让窗口把任务带下去
            pass
        return False  # 取消关闭：窗口转后台，任务继续跑

    win.events.closing += on_closing  # Event 没有 listen 方法，订阅只能用 +=


def main() -> None:
    global _WINDOW

    if "--proxy" in sys.argv:
        from backend.app.proxy_standalone import main as proxy_main

        proxy_main()
        return

    if not acquire():
        # 已有实例可能是「关窗后隐藏常驻」的状态：先尝试唤回它的窗口，
        # 唤不到再走兜底（清障重启），绝不只弹一个提示把用户晾死
        if wake_running_instance():
            return
        if "--recover" in sys.argv:
            # 已经是兜底重启出来的实例仍抢不到锁：不再互拉，给出可操作指引
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "a4agent 已在运行中，但窗口无法唤回。\n"
                "请在任务管理器中结束 a4agent 进程后重新启动。",
                "提示", 0x40,
            )
            return
        if recover_or_restart():
            return
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "a4agent 已在运行中。", "提示", 0x40)
        return

    port = find_free_port()
    t = threading.Thread(target=start_server, args=(port,), daemon=True)
    t.start()
    write_instance(port)

    win = webview.create_window(
        "a4agent",
        f"http://127.0.0.1:{port}",
        width=1000,
        height=720,
        min_size=(800, 560),
        js_api=DesktopApi(),
        # 关窗策略由 events.closing 接管（隐藏常驻），不用后端自带的确认框
        confirm_close=False,
    )
    _WINDOW = win
    _apply_closing_policy(win, _STATE)
    _register_desktop_hooks(win)
    _subscribe_task_notifications(win)
    _start_tray(win)

    webview.start()
    # 窗口关闭后先回收托盘、本地推理引擎（llama-server 子进程）与无头任务
    # 子进程，再强制退出，避免后台线程/子进程挂住进程
    tray = _STATE.get("tray")
    if tray is not None:
        try:
            tray.stop()
        except Exception:  # noqa: BLE001 - 托盘收尾失败不影响退出
            pass
    try:
        from backend.app import task_runner

        task_runner.shutdown_all("应用退出")
    except Exception:
        pass
    try:
        from backend.app import opencode_listen

        opencode_listen.stop()
    except Exception:
        pass
    try:
        from backend.app.llama import runtime as llama_runtime

        llama_runtime.shutdown()
    except Exception:
        pass
    clear_instance()
    os._exit(0)


def _wake(win) -> None:
    """显示并置前主窗口；唤回接口与系统通知横幅点击共用。"""
    try:
        win.show()
    except Exception:  # noqa: BLE001 - show 不可用时退回 restore
        pass
    try:
        win.restore()
    except Exception:
        pass
    _STATE["visible"] = True
    _bring_to_front(win)


def _register_desktop_hooks(win) -> None:
    """把「唤回窗口」交给 /api/v1/desktop/wake（二次启动时调用）。"""
    from backend.app.api.v1 import desktop as desktop_api

    desktop_api.set_wake_hook(lambda: _wake(win))


def _subscribe_task_notifications(win) -> None:
    """任务到终态时：窗口隐藏则闪任务栏 + 弹系统通知横幅（点击唤回窗口），
    窗口可见时不弹系统横幅——页面自己有应用内提示，避免双重打扰。"""
    from backend.app import task_notify, win_toast
    from backend.app.phone.notifier import compose

    def on_event(event: dict) -> None:
        visible = _STATE["visible"]
        # 下发任务窗口可见时不弹（页面自己有应用内提示，避免双重打扰）；
        # OpenCode 会话终局发生在应用之外、页面内没有任何提示，与六端同权总要弹
        if visible and not event.get("session_id"):
            return
        if not visible:
            task_notify.flash_taskbar(_hwnd_of(win))
        title, message = compose(event)
        win_toast.show(title, message, on_click=lambda: _wake(win))

    task_notify.subscribe(on_event)


def _logo_path() -> "str | None":
    """托盘图标素材：打包后取 _MEIPASS/resources，开发态取仓库 resources。"""
    from pathlib import Path

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "."))
    else:
        base = Path(__file__).resolve().parent
    for name in ("logo.png", "logo.ico"):
        candidate = base / "resources" / name
        if candidate.exists():
            return str(candidate)
    return None


def _quit_from_tray(win) -> None:
    """托盘「退出」：放行关窗策略后投递 WM_CLOSE，走 UI 线程消息泵正常收尾。

    不直接 destroy()——WinForms 控件操作跨线程无保证，PostMessage 让关闭
    在窗口自己的消息循环里发生，与点标题栏 X 等价（quitting 已放行）。
    """
    _STATE["quitting"] = True
    hwnd = _hwnd_of(win)
    if hwnd and sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return
    try:
        win.destroy()
    except Exception:  # noqa: BLE001 - 兜底：销毁失败则关闭
        try:
            win.close()
        except Exception:
            pass


def _start_tray(win) -> None:
    """常驻托盘图标：单击/双击唤回主窗口，右键菜单可显式退出。

    「关窗即隐藏」的设计里，托盘是隐藏实例唯一常驻的可视入口——唤醒链路
    （desktop.json）断裂时用户仍能从托盘把窗口找回来。回调跑在托盘线程：
    show/restore 跨线程调用已由 wake 接口实证可行；退出走 PostMessage。
    """
    try:
        import pystray
    except ImportError:
        return  # 依赖缺失不阻塞主流程（老环境兼容）
    from PIL import Image

    icon_path = _logo_path()
    if not icon_path:
        return
    try:
        with Image.open(icon_path) as im:
            image = im.convert("RGBA")
    except Exception:  # noqa: BLE001 - 图标加载失败仅损失托盘，不挡主窗口
        return
    menu = pystray.Menu(
        pystray.MenuItem("显示主窗口", lambda icon, item: _wake(win), default=True),
        pystray.MenuItem("退出", lambda icon, item: _quit_from_tray(win)),
    )
    icon = pystray.Icon("a4agent", image, "a4agent", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    _STATE["tray"] = icon


if __name__ == "__main__":
    main()
