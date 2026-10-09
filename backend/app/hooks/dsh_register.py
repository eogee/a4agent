"""DSH Cordis 插件的安装 / 卸载 / 状态：让 dsh 把通知能力挂进各 profile。

与另外五端的机制完全不同：

  claude/qoder/workbuddy  往宿主 settings.json 写外部命令，宿主回调
                          `a4agent hook`，靠子进程 + stdin JSON 通信；
  codex                   往 config.toml 追加 notify 命令；
  zcode                   hooks.events + enabled 开关；
  dsh                     **没有外部 hook 协议**——插件必须跑在 dsh 进程内，
                          只能把本包内置的 Cordis 插件挂进
                          ~/.dsh/profiles/*/cordis.patch.yml，让 dsh 启动时加载。

因此这里的注册不是「写一行配置」，而是两步：
  1. 把 resources/dsh-hook/ 复制到 ~/.dsh/a4agent-hook/（用户可见、路径稳定，
     便于排查；插件需要以 dsh 的 node 环境 import，不能从 PyInstaller 的
     _MEIPASS 临时目录加载——那个目录在 a4agent 退出后就没了）；
  2. 在每个 profile 的 cordis.patch.yml 追加 insert 块（marker 包裹，可幂等
     替换与清理）。

写入用相对路径（a4phone 已验证 dsh 按相对路径解析插件），因此 profile 目录
可以整体搬移而不失效。
"""
import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DSH_MARKER_START = "# ===== a4agent dsh-hook (auto-generated) ====="
DSH_MARKER_END = "# ===== end a4agent dsh-hook ====="

# 插件安装目录（放 ~/.dsh 下，与 dsh 自身的 skills/storages 同级）
PLUGIN_DIR_NAME = "a4agent-hook"
# 插件源目录：开发态在仓库 resources 下，打包后在 _MEIPASS/resources 下
PLUGIN_SRC_NAME = "dsh-hook"


def dsh_home() -> Path:
    return Path.home() / ".dsh"


def profiles_dir() -> Path:
    return dsh_home() / "profiles"


def installed_plugin_dir() -> Path:
    return dsh_home() / PLUGIN_DIR_NAME


def plugin_source_dir() -> Path | None:
    """内置插件源目录；源码缺失（裁剪打包等）时返回 None。"""
    import sys

    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ".")) / "resources" / PLUGIN_SRC_NAME
    else:
        base = Path(__file__).resolve().parents[3] / "resources" / PLUGIN_SRC_NAME
    return base if (base / "index.js").is_file() else None


def _block(rel_name: str) -> str:
    """cordis.patch.yml 的挂载块。rel_name 是相对 profile 目录的插件路径，
    统一用 posix 分隔符（Windows 上 dsh 也能解析）。"""
    return (f"{DSH_MARKER_START}\n"
            f"- insert:\n"
            f"    - id: a4agent-dsh-hook\n"
            f'      name: "{rel_name}"\n'
            f"{DSH_MARKER_END}")


def discover_profiles(root: Path | None = None) -> list[Path]:
    """发现所有 DSH profile：profiles 目录下含 cordis.yml 的子目录。

    cordis.yml 是 profile 的身份文件；node_modules 等非 profile 目录据此排除。
    ~/.dsh/profiles 不存在即 DSH 未安装，返回空列表。
    """
    root = root or profiles_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return []  # DSH 未安装
    out: list[Path] = []
    for entry in entries:
        if not entry.is_dir() or entry.name == "node_modules":
            continue
        if (entry / "cordis.yml").is_file():
            out.append(entry)
    return sorted(out, key=lambda p: p.name)


def _strip_blocks(content: str) -> str:
    """移除本工具写入的挂载块。

    同时清理旧版手动挂载（无 marker 但 id 为 a4agent-dsh-hook 的顶层 insert
    块），否则卸载后残留、重复注册会叠加两份。
    """
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == DSH_MARKER_START:
            i += 1
            while i < len(lines) and lines[i].strip() != DSH_MARKER_END:
                i += 1
            i += 1  # 跳过 marker 尾行
            while i < len(lines) and lines[i].strip() == "":
                i += 1  # 吞掉块后尾随空行
            continue
        # 顶层 insert 块（含 id: a4agent-dsh-hook 的旧版手写挂载）
        if line.startswith("- insert:"):
            block = [line]
            j = i + 1
            while j < len(lines) and lines[j][:1].isspace():
                block.append(lines[j])
                j += 1
            if "a4agent-dsh-hook" in "\n".join(block):
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                i = j
                continue
            out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _rel_plugin_path(profile_dir: Path) -> str:
    """profile 目录到插件 index.js 的相对路径（posix 分隔符）。"""
    target = installed_plugin_dir() / "index.js"
    return Path(os.path.relpath(target, profile_dir)).as_posix()


def deploy_plugin() -> tuple[bool, str]:
    """把内置插件复制到 ~/.dsh/a4agent-hook/；返回 (是否发生变更, 说明)。"""
    src = plugin_source_dir()
    if src is None:
        return False, "内置插件文件缺失，无法安装"
    dst = installed_plugin_dir()
    try:
        dst.mkdir(parents=True, exist_ok=True)
        changed = False
        for item in src.iterdir():
            if not item.is_file():
                continue
            target = dst / item.name
            try:
                same = target.is_file() and target.read_bytes() == item.read_bytes()
            except OSError:
                same = False
            if not same:
                shutil.copy2(item, target)
                changed = True
        return changed, f"插件已部署到 {dst}"
    except OSError as exc:
        return False, f"插件部署失败：{exc}"


def _mount(profile_dir: Path) -> bool:
    """把挂载块写入单个 profile 的 cordis.patch.yml（幂等）。"""
    patch = profile_dir / "cordis.patch.yml"
    try:
        content = patch.read_text(encoding="utf-8")
    except OSError:
        content = ""

    block = _block(_rel_plugin_path(profile_dir))
    if DSH_MARKER_START in content and block in content:
        return False  # 已是最新

    # 默认模板的占位符 `[]` 本身就是完整的 YAML 文档；原样保留再追加条目，
    # 文件会变成两个无 `---` 分隔的文档，dsh 启动解析时报
    # "end of the stream or a document separator is expected"。
    base = re.sub(r"(?m)^[ \t]*\[][ \t]*\r?$", "", _strip_blocks(content))
    base = re.sub(r"\n{3,}", "\n\n", base).rstrip()
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text((base + "\n\n" if base else "") + block + "\n", encoding="utf-8")
    return True


def register_dsh() -> dict:
    """部署插件并挂载到全部 profile。返回 {registered, changed, detail}。"""
    profiles = discover_profiles()
    if not profiles:
        return {"registered": False, "changed": False,
                "detail": "未检测到 DSH 环境（~/.dsh/profiles 下没有 profile）"}

    deployed, detail = deploy_plugin()
    if not installed_plugin_dir().is_dir():
        return {"registered": False, "changed": False, "detail": detail}

    mounted = [p.name for p in profiles if _mount(p)]
    changed = deployed or bool(mounted)
    if mounted:
        return {"registered": True, "changed": True,
                "detail": f"已挂载 cordis 插件：{'、'.join(mounted)}（共 {len(profiles)} 个 profile）"}
    return {"registered": True, "changed": False, "detail": "此前已注册"}


def unregister_dsh() -> dict:
    """移除全部 profile 里的挂载块，并删除插件目录。"""
    profiles = discover_profiles()
    cleaned: list[str] = []
    for profile_dir in profiles:
        patch = profile_dir / "cordis.patch.yml"
        try:
            content = patch.read_text(encoding="utf-8")
        except OSError:
            continue
        stripped = re.sub(r"\n{3,}", "\n\n", _strip_blocks(content)).strip()
        if stripped == content.strip():
            continue  # 该 profile 没有本工具的挂载
        # 清理后若只剩注释，回到「只有注释」的可接受形态即可
        patch.write_text(stripped + "\n", encoding="utf-8")
        cleaned.append(profile_dir.name)

    if not cleaned:
        return {"unregistered": False, "detail": "此前已移除"}

    try:
        shutil.rmtree(installed_plugin_dir())
    except OSError:
        pass  # 目录残留不影响功能，下次注册会覆盖
    return {"unregistered": True, "detail": f"已移除：{'、'.join(cleaned)}"}


def registered() -> tuple[bool, str]:
    """注册状态 + 展示用路径：已挂载的 profile 数 / DSH 配置目录。"""
    profiles = discover_profiles()
    if not profiles:
        return False, str(dsh_home())
    mounted = 0
    for profile_dir in profiles:
        try:
            content = (profile_dir / "cordis.patch.yml").read_text(encoding="utf-8")
        except OSError:
            continue
        if "a4agent-dsh-hook" in content:
            mounted += 1
    if mounted:
        return True, f"{profiles_dir()}（{mounted}/{len(profiles)} 个 profile）"
    return False, str(profiles_dir())
