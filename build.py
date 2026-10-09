"""PyInstaller 打包 + Inno Setup 安装包脚本。

用法：
    uv run python build.py                # 文件夹版（onedir），默认
    uv run python build.py --installer    # 文件夹版 + 编译 Inno Setup 安装包
    uv run python build.py --onefile      # 单 exe（逃生舱/临时分发）
    uv run python build.py --installer --iscc C:\\path\\to\\ISCC.exe
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

HIDDEN_IMPORTS = [
    # uvicorn 动态加载的模块
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # pywebview / pythonnet 运行时
    "clr_loader",
    "webview",
    "webview.platforms.winforms",
]

DEFAULT_VERSION = "0.1.0"


def _app_version(override: str | None) -> str:
    """版本号单一来源：解析 pyproject.toml [project].version，失败回退默认值。"""
    if override:
        return override
    try:
        m = re.search(
            r'version\s*=\s*"([\d.]+)"',
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        )
        if m:
            return m.group(1)
    except OSError:
        pass
    return DEFAULT_VERSION


def _find_iscc(explicit: str | None) -> Path | None:
    """定位 ISCC.exe：--iscc > 环境变量 ISCC > 常见安装路径 > PATH。

    winget 安装 Inno Setup 不进 PATH，且作用域不同落盘位置不同
    （用户级 %LOCALAPPDATA%\\Programs\\Inno Setup 6，机器级 Program Files (x86)），
    因此必须多路径探测。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("ISCC")
    if env:
        candidates.append(Path(env))
    candidates += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Inno Setup 6" / "ISCC.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c
    from shutil import which

    w = which("ISCC")
    return Path(w) if w else None


def _ensure_icon(resources: Path) -> Path | None:
    """从 resources/logo.png 生成多尺寸 logo.ico，作为 exe/安装包图标。

    png 更新或 ico 缺失时重新生成；无图标或缺少 Pillow 时返回 None，
    打包不因缺图标而失败。
    """
    png = resources / "logo.png"
    ico = resources / "logo.ico"
    if png.exists() and (not ico.exists() or png.stat().st_mtime > ico.stat().st_mtime):
        try:
            from PIL import Image
        except ImportError:
            print("提示：生成图标需要 Pillow（uv add --dev pillow），本次跳过图标生成")
            return None
        im = Image.open(png).convert("RGBA")
        w, h = im.size
        side = max(w, h)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(im, ((side - w) // 2, (side - h) // 2), im)
        # 先放大到 256 主图再保存：Pillow 会静默丢弃大于源图尺寸的目标尺寸
        master = canvas.resize((256, 256), Image.LANCZOS)
        master.save(
            ico,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        print(f"已生成图标：{ico}")
    return ico if ico.exists() else None


def _write_version_json(version: str) -> Path:
    """把运行时版本写入 build/version.json，随包内嵌（sys._MEIPASS/version.json）。

    运行时由 backend/app/version.py 读取，作为冻结态的版本单一来源。
    """
    build_dir = ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    p = build_dir / "version.json"
    p.write_text(json.dumps({"version": version}), encoding="utf-8")
    return p


def _write_version_file(version: str) -> Path:
    """生成 exe 版本信息资源（--version-file）。

    任务管理器、系统通知横幅等界面按 FileDescription 显示应用身份——没有这个
    资源时回退为可执行文件名，桌面通知横幅头部就无法稳定显示「a4agent」。
    """
    build_dir = ROOT / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    import re

    m = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    triple = m.groups() if m else ("0", "0", "0")
    p = build_dir / "version_info.txt"
    p.write_text(
        f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({triple[0]}, {triple[1]}, {triple[2]}, 0),
    prodvers=({triple[0]}, {triple[1]}, {triple[2]}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable('080404b0', [
        StringStruct('CompanyName', 'eogee'),
        StringStruct('FileDescription', 'a4agent'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', 'a4agent'),
        StringStruct('LegalCopyright', 'MIT License'),
        StringStruct('OriginalFilename', 'a4agent.exe'),
        StringStruct('ProductName', 'a4agent'),
        StringStruct('ProductVersion', '{version}'),
      ]),
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ],
)
""",
        encoding="utf-8",
    )
    return p


def _sync_changelog() -> None:
    """把 release_body.md 同步为 frontend/changelog.md，随包内嵌供「版本与更新」弹窗展示。

    发布流程每次重写 release_body.md；打包前同步一次，保证应用内展示的
    更新说明与本次发布一致。文件不存在只提示、不阻断打包。
    「校验」段（安装包 SHA256）仅属于发布页——exe 无法内嵌自身的哈希，
    同步时剥掉，应用内不展示占位/失效的校验值。
    """
    src = ROOT / "release_body.md"
    dst = ROOT / "frontend" / "changelog.md"
    if not src.exists():
        print("提示：release_body.md 不存在，跳过更新日志同步")
        return
    text = src.read_text(encoding="utf-8")
    cutoff = text.find("## 校验")
    if cutoff != -1:
        text = text[:cutoff].rstrip() + "\n"
    if dst.exists() and dst.read_text(encoding="utf-8") == text:
        return
    dst.write_text(text, encoding="utf-8")
    print(f"已同步更新日志：{dst}")


def _run_pyinstaller(onefile: bool, version: str) -> None:
    frontend = ROOT / "frontend"
    resources = ROOT / "resources"
    ico = _ensure_icon(resources)
    _sync_changelog()
    sep = os.pathsep
    version_json = _write_version_json(version)
    version_file = _write_version_file(version)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",  # 覆盖旧构建产物时不二次询问
        "--windowed",
        "--name", "a4agent",
        "--add-data", f"{frontend}{sep}frontend",
        "--add-data", f"{version_json}{sep}.",
        "--version-file", str(version_file),
    ]
    if onefile:
        cmd.append("--onefile")
    if ico is not None:
        cmd += ["--icon", str(ico)]
    if resources.exists() and any(resources.iterdir()):
        cmd += ["--add-data", f"{resources}{sep}resources"]
    for mod in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", mod]
    cmd.append(str(ROOT / "desktop.py"))

    print("执行命令：", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    if onefile:
        print("\n打包完成：dist/a4agent.exe")
    else:
        print("\n打包完成：dist/a4agent/a4agent.exe（文件夹版）")


def _run_installer(version: str, iscc: Path) -> None:
    print(f"\n编译 Inno Setup 安装包（版本 {version}）...")
    subprocess.run(
        [str(iscc), "installer.iss", f"/DMyAppVersion={version}"],
        cwd=str(ROOT),
        check=True,
    )
    print(f"\n安装包完成：dist/a4agent-setup-{version}.exe")


def main() -> None:
    parser = argparse.ArgumentParser(description="a4agent 打包脚本")
    parser.add_argument("--installer", action="store_true", help="同时编译 Inno Setup 安装包")
    parser.add_argument("--iscc", help="指定 ISCC.exe 路径（默认自动探测）")
    parser.add_argument("--onefile", action="store_true", help="单 exe 模式（默认 onedir）")
    parser.add_argument("--version", help="覆盖版本号（默认读取 pyproject.toml）")
    args = parser.parse_args()

    version = _app_version(args.version)
    _run_pyinstaller(args.onefile, version)

    if args.installer:
        iscc = _find_iscc(args.iscc)
        if iscc is None:
            print(
                "\n未找到 Inno Setup 编译器（ISCC.exe）。已生成 onedir 产物，安装包跳过。"
                "\n请先安装：winget install --id JRSoftware.InnoSetup -e --accept-source-agreements"
                "\n或使用 --iscc 指定路径。"
            )
            sys.exit(1)
        _run_installer(version, iscc)


if __name__ == "__main__":
    main()
