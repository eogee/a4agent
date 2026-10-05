"""本机目录浏览接口：只列子目录名，供「选择文件夹」弹窗逐级点选。

桌面壳有系统原生对话框，但用浏览器打开 127.0.0.1 时 JS 拿不到本机绝对路径
（浏览器故意隐藏），只能由同机的后端来列。服务只监听 127.0.0.1，接口也只
返回目录名——不读文件、不返回文件内容。
"""
import ctypes
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/fs")

# 单个目录列出的上限：node_modules 这类目录动辄上万项，全返回会拖死弹窗
MAX_DIRS = 800


def drive_roots() -> list:
    """Windows 盘符根目录，非 Windows 返回空。

    用 GetLogicalDrives 的位掩码而不是逐个 isdir：断开的网络盘会让 isdir 卡住。
    """
    if os.name != "nt":
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:  # noqa: BLE001 - 取不到盘符就只走路径浏览
        return []
    return [f"{chr(ord('A') + i)}:\\" for i in range(26) if (mask >> i) & 1]


@router.get("/dirs")
def list_dirs(path: str = ""):
    """列 path 下的子目录。path 留空 = 「我的电脑」（Windows 回盘符）。"""
    home = str(Path.home())
    drives = drive_roots()
    target = (path or "").strip()
    if not target:
        if drives:
            return {"path": "", "parent": None, "home": home,
                    "drives": drives, "dirs": [], "truncated": False}
        target = "/"   # 非 Windows 没有盘符层，直接从根目录开始列

    folder = Path(target).expanduser()
    if not folder.is_absolute():
        raise HTTPException(400, "需要绝对路径")
    try:
        folder = folder.resolve()
        if not folder.is_dir():
            raise HTTPException(404, f"目录不存在：{folder}")
        names = []
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        names.append(entry.name)
                except OSError:   # 悬空符号链接等单项读不到，跳过而不是整页失败
                    continue
    except PermissionError:
        raise HTTPException(403, f"没有权限访问：{folder}")

    names.sort(key=str.lower)
    at_root = folder.parent == folder
    return {
        "path": str(folder),
        "parent": ("" if drives else None) if at_root else str(folder.parent),
        "home": home,
        "drives": drives,
        "dirs": [{"name": n, "path": str(folder / n)} for n in names[:MAX_DIRS]],
        "truncated": len(names) > MAX_DIRS,
    }
