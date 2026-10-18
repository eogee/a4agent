"""盘符探测：默认扫描范围与「选择文件夹」弹窗共用的底层来源。

放在业务层而不是 api 层，是因为 skill_manager 的默认项目根也要用它；api 反向
从这里导入即可，避免业务层依赖 api 包。

两份口径刻意分开：
- drive_roots()：本机**所有可见**盘符（含网络盘、光驱），供目录浏览弹窗逐级点选；
- default_project_roots()：默认扫描范围，同样取所有可见盘符——用户可能把项目
  放在任意盘。跨盘（尤其断开的网络映射盘）访问会挂住几秒到几十秒，因此
  skill_manager 对远程盘的单次枚举加了超时，见 is_remote() 的调用方。
"""
import ctypes
import os
from pathlib import Path

# GetDriveType 返回值（winbase.h）
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6


def drive_roots() -> list[str]:
    """本机所有可见盘符，形如 ["C:\\", "D:\\"]；非 Windows 返回空列表。

    用 GetLogicalDrives 的位掩码而不是逐个 isdir：断开的网络盘会让 isdir 卡住。
    """
    if os.name != "nt":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001 - 取不到盘符就只走路径浏览
        return []
    return [f"{chr(ord('A') + i)}:\\" for i in range(26) if (mask >> i) & 1]


def drive_type(root: str) -> int:
    """盘符类型（DRIVE_* 常量）；探测失败返回 0（DRIVE_UNKNOWN）。"""
    if os.name != "nt":
        return DRIVE_UNKNOWN
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(str(root)))
    except Exception:  # noqa: BLE001
        return DRIVE_UNKNOWN


def is_remote(path) -> bool:
    """路径是否落在网络盘或 UNC 共享上。

    这类路径的每次 stat/iterdir 都可能挂几十秒（映射盘掉线时尤其明显），
    而发现流程跑在请求线程里，一挂就是整页白屏——调用方必须据此加超时。
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(text)[0]
    if not drive:
        return False
    return drive_type(drive + "\\") == DRIVE_REMOTE


def default_project_roots() -> list[str]:
    """默认扫描范围：本机所有可见盘符。

    空光驱这类「有盘符但没有介质」的会被排除——留着它会让用户在根目录弹窗里
    一按保存就被后端校验拒绝（那条路径 is_dir() 为 False）。远程盘不做探测：
    断线的映射盘上 isdir 能挂几十秒，交给发现流程的超时兜。

    探测不到任何可用盘符时退回用户主目录，保证列表非空——save_project_roots
    拒绝空列表，默认值不能给它一个空集。
    """
    usable: list[str] = []
    for root in drive_roots():
        if is_remote(root):
            usable.append(root)
            continue
        try:
            if os.path.isdir(root):
                usable.append(root)
        except OSError:
            continue
    return usable or [str(Path.home())]
