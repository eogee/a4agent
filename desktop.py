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
        """轮询等待主实例释放 AppMutex；拿到所有权立即释放给安装器。"""
        if sys.platform != "win32":
            time.sleep(2)  # 非 Windows 开发兜底
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            handle = kernel32.CreateMutexW(None, False, _MUTEX)
            if not handle:  # 创建失败（如权限）→ 重试，不当作已就绪
                time.sleep(0.2)
                continue
            if kernel32.GetLastError() != _ERROR_ALREADY_EXISTS:
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

if len(sys.argv) >= 2 and sys.argv[1] == "hook":
    # Hook 子命令：终端宿主（Claude Code / ZCode 等）以管道重定向调用本进程，
    # 读 stdin 载荷、stdout 回写决策。必须短路在重型 import 之前——hook 是
    # AI 每次事件都调用的热路径，启动延迟直接挂在用户每次工具调用上。
    from backend.app.hooks.dispatch import run_hook_cli

    sys.exit(run_hook_cli(sys.argv[2:]))

import webview  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.singleton import acquire  # noqa: E402


_WINDOW = None
# pywebview 的 Window 没有 visible 属性，可见性由关窗/唤回事件自己记
_STATE = {"quitting": False, "visible": True}


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


def instance_file() -> "os.PathLike":
    """记录运行实例的端口，供二次启动唤回隐藏窗口。"""
    from pathlib import Path

    from backend.app.database import get_data_dir

    return Path(get_data_dir()) / "desktop.json"


def write_instance(port: int) -> None:
    import json

    try:
        instance_file().write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def clear_instance() -> None:
    try:
        instance_file().unlink(missing_ok=True)
    except OSError:
        pass


def wake_running_instance() -> bool:
    """已有实例在跑（可能是隐藏常驻）时，唤回它的窗口；成功返回 True。"""
    import json
    import urllib.request
    from pathlib import Path

    path = Path(instance_file())
    if not path.exists():
        return False
    try:
        port = int(json.loads(path.read_text(encoding="utf-8")).get("port"))
    except (OSError, ValueError):
        return False
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/desktop/wake", method="POST", data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return bool(body.get("restored"))
    except Exception:
        return False


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
        # 唤不到（老实例即将退出等）才提示已在运行。
        if wake_running_instance():
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

    webview.start()
    # 窗口关闭后先回收本地推理引擎（llama-server 子进程）与无头任务子进程，
    # 再强制退出，避免后台线程/子进程挂住进程
    try:
        from backend.app import task_runner

        task_runner.shutdown_all("应用退出")
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
        if _STATE["visible"]:
            return
        # Window 没有 visible 属性，可见性由 on_closing / wake 自己记
        task_notify.flash_taskbar(_hwnd_of(win))
        title, message = compose(event)
        win_toast.show(title, message, on_click=lambda: _wake(win))

    task_notify.subscribe(on_event)


if __name__ == "__main__":
    main()
