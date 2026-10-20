"""桌面实例记录与唤醒兜底（仅 stdlib，供 desktop.py 壳层与单测复用）。

desktop.json 是「二次启动唤回隐藏窗口」链路的唯一事实源，但会被异常实例
覆盖成死记录（实测事故：活实例 port 59950 的记录被一个已死实例覆盖成
18900），也可能缺失。本模块负责：读记录、pid 验明正身、唤醒，以及唤醒
失败后的清障重启兜底——保证双击 exe 必有下文：窗口回来，或新实例接管，
或明确告知需要任务管理器，不再有「提示已在运行却无处可去」的死胡同。
"""
import os
import sys

from .database import get_data_dir


def instance_file() -> "os.PathLike":
    """记录运行实例的端口，供二次启动唤回隐藏窗口。"""
    from pathlib import Path

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


def read_instance_record() -> "dict | None":
    """读 desktop.json 的 {port, pid}；缺失/损坏/非对象一律返回 None。"""
    import json
    from pathlib import Path

    try:
        record = json.loads(Path(instance_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def pid_image_name(pid: int) -> str:
    """进程映像完整路径；pid 不存在或无权查询返回空串。仅 Windows 有效。"""
    if sys.platform != "win32" or pid <= 0:
        return ""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        kernel32.CloseHandle(handle)


def pid_is_a4agent(pid: int) -> bool:
    """pid 是否属于与当前入口同名的进程（frozen 看 exe 名，开发看解释器名）。

    记录里的 pid 可能已被系统复用给别的程序，杀之前必须验明正身。
    """
    image = pid_image_name(pid)
    if not image:
        return False
    from pathlib import Path

    return Path(image).name.lower() == Path(sys.executable).name.lower()


def wake_running_instance() -> bool:
    """已有实例在跑（可能是隐藏常驻）时，唤回它的窗口；成功返回 True。"""
    import json
    import urllib.request
    from pathlib import Path

    path = Path(instance_file())
    if not path.exists():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        port = int(record.get("port"))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    # pid 预检：记录指向的进程已死/非同名 → 记录陈旧，直接失败交给上层
    # 兜底，省掉对死端口 3s 的超时等待；无 pid 的老记录跳过预检保持兼容
    pid = int(record.get("pid") or 0)
    if pid and not pid_is_a4agent(pid):
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


def confirm_kill(pid: int, *, stale_record: bool) -> bool:
    """杀残留实例前征询用户；返回是否获得确认。"""
    if sys.platform != "win32":
        return False
    import ctypes

    if stale_record:
        text = (
            "检测到后台残留的 a4agent 进程，其窗口已无法唤回。\n\n"
            f"是否结束该进程（PID {pid}）并重新启动？进行中的任务会被终止。"
        )
    else:
        text = (
            "a4agent 正在后台运行，但窗口唤回失败（进程可能已僵死）。\n\n"
            f"是否结束该进程（PID {pid}）并重新启动？进行中的任务会被终止。"
        )
    return ctypes.windll.user32.MessageBoxW(None, text, "提示", 0x40 | 0x04) == 6  # MB_YESNO


def confirm_kill_stale_by_name() -> bool:
    """按映像名清残余前征询用户（拿不到具体 pid，提示里不带编号）。"""
    if sys.platform != "win32":
        return False
    import ctypes

    text = (
        "检测到后台残留的 a4agent 进程（其窗口已无法唤回，实例记录已失效）。\n\n"
        "是否结束它并重新启动？进行中的任务会被终止。"
    )
    return ctypes.windll.user32.MessageBoxW(None, text, "提示", 0x40 | 0x04) == 6


def kill_process(pid: int) -> bool:
    """强制结束进程树；返回 taskkill 是否报成功。"""
    import subprocess

    result = subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
    )
    return result.returncode == 0


def wait_gone(pid: int, timeout_s: float = 5) -> bool:
    """轮询等待进程退出（互斥体随进程释放）；超时返回 False。"""
    import time

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_image_name(pid):
            return True
        time.sleep(0.2)
    return False


def relaunch_detached() -> None:
    """以 detached 方式重启自己（清障后接管），当前进程随后退出。

    --recover 标记给重启出来的实例：若它仍抢不到锁（理论边缘态），由壳层
    给出终局提示而不是再次互拉，杜绝无限重启循环。
    """
    import subprocess

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--recover"]
        cwd = os.path.dirname(sys.executable)
    else:
        cmd = [sys.executable, os.path.abspath(sys.argv[0]), "--recover"]
        cwd = os.path.dirname(os.path.abspath(sys.argv[0]))
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, creationflags=flags, close_fds=True, cwd=cwd)


def recover_or_restart() -> bool:
    """唤醒失败后的兜底：要么清障重启接管，要么交回调用方弹提示。

    返回 True 表示已处理（用户将看到新实例的窗口）；False 表示未处理或
    用户拒绝，调用方退回「已在运行中」提示。
    """
    import time

    record = read_instance_record()
    pid = int(record.get("pid") or 0) if record else 0
    if pid and pid != os.getpid() and pid_is_a4agent(pid):
        # 真有同名实例活着但窗口唤不回（WebView2 挂死等）：交给用户决定
        if not confirm_kill(pid, stale_record=False):
            return False
        if not kill_process(pid):
            return False
        wait_gone(pid)
        clear_instance()
        relaunch_detached()
        return True

    # 记录陈旧（pid 已死）或无记录：刚启动的主实例可能还没来得及写记录，
    # 先短重试一次唤回，避免双击竞态误杀健康实例
    time.sleep(0.6)
    if wake_running_instance():
        return True

    if sys.platform == "win32" and getattr(sys, "frozen", False):
        # 互斥体被无记录的残留实例占着（实测事故形态）：先探测同名残余
        # （self 除外），征询后再清。仅 frozen 生效——开发态按 python.exe
        # 名匹配会误伤无关进程，不可走这条路
        import subprocess
        from pathlib import Path

        exe_name = Path(sys.executable).name
        probe = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}"],
            capture_output=True,
        )
        listed = probe.stdout.decode(errors="replace").splitlines()
        has_residual = any(exe_name.lower() in line.lower() for line in listed)
        if has_residual:
            if not confirm_kill_stale_by_name():
                return False
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", exe_name,
                 "/FI", f"PID ne {os.getpid()}"],
                capture_output=True,
            )
            time.sleep(0.5)  # 等互斥体随进程释放
        clear_instance()
        relaunch_detached()
        return True

    clear_instance()
    relaunch_detached()
    return True
