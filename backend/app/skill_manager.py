"""Skill 管理：八端（Claude Code / Codex / dsh / zcode / pi / Qoder / OpenCode / WorkBuddy）
skill 发现、迁移、回收站。

各端均采用「<skill-name>/SKILL.md 目录 bundle + frontmatter（name/description）」
格式，因此迁移即目录复制。本模块职责：

- 路径解析：七端全局根（含 A4AGENT_*_SKILLS_PATH 环境变量覆盖）+ 可配置项目根
  列表下的项目级根；项目根列表持久化到 get_data_dir()/projects.json。

OpenCode 的两处特例（其余六端沿用既有口径）：
- **身份是目录名**：技能 ID 由路径派生、大小写敏感，frontmatter name 只是显示名。
  因此聚合键与冲突判定在 opencode 端按 dir_name（见 _scan_root / _aggregate /
  _trash_existing_conflicts），显示名仍取 frontmatter name。
- **兼容目录重叠**：OpenCode 除自有目录外还自动发现 ~/.claude/skills、
  ~/.agents/skills 及其项目级对应目录，而 .claude 是 Claude 端、.agents 是
  ZCode/Qoder 的跨工具目录。这类条目会被标注 opencode_visible=True，
  界面明示「OpenCode 已可见」，避免重复迁移与端数双计。
- 发现：扫描各根下的 skill bundle，以 frontmatter name 为唯一标识做聚合与
  重复标注（「已在 N 端存在」）；Codex 全局根的保留目录 .system/ 等点开头
  目录一律跳过，不视为用户 skill。
- 迁移：非破坏性复制（源端保留）；目标端已存在同名 skill 时先移入回收站再
  写入，不静默覆盖；每次迁移写入 SkillMigration 日志。
- 回收站：删除移入 get_data_dir()/skills_recycle/<name>.<ts>/，SkillTrash 表
  记录原位信息，30 天惰性过期清理，可恢复原位。
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import config_manager, disk_roots, project_hints
from .database import get_data_dir
from .env_compat import env_first

logger = logging.getLogger(__name__)

TOOLS = ("claude", "codex", "dsh", "zcode", "pi", "qoder", "opencode", "workbuddy")
TOOL_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "dsh": "DSH",
    "zcode": "ZCode",
    "pi": "Pi",
    "qoder": "Qoder",
    "opencode": "OpenCode",
    "workbuddy": "WorkBuddy",
}
SKILL_FILE = "SKILL.md"
TRASH_DIR_NAME = "skills_recycle"
TRASH_KEEP_DAYS = 30
PROJECTS_FILENAME = "projects.json"
# 历史预置值：早期版本把它写死成开发机布局，所有用户装完都只扫这一个目录。
# 现在只用于识别「这份配置是当年预置的、从没被用户改过」，不再作为默认值。
LEGACY_DEFAULT_ROOT = r"C:\ProgramMine"
# 网络盘/UNC 上单次枚举的等待上限：映射盘掉线时 stat 能挂几十秒，
# 而发现流程跑在请求线程里，不设上限就是整页白屏
REMOTE_SCAN_TIMEOUT = 3.0


# ---------------- 路径解析 ----------------


def claude_skills_root() -> Path:
    """Claude Code 全局 skill 根，可用环境变量 A4AGENT_CLAUDE_SKILLS_PATH 覆盖。"""
    override = env_first("A4AGENT_CLAUDE_SKILLS_PATH", "A4API_CLAUDE_SKILLS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "skills"


def codex_skills_root() -> Path:
    """Codex 全局 skill 根，可用环境变量 A4AGENT_CODEX_SKILLS_PATH 覆盖。"""
    override = env_first("A4AGENT_CODEX_SKILLS_PATH", "A4API_CODEX_SKILLS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".codex" / "skills"


def dsh_skills_root() -> Path:
    """dsh 全局 skill 根，可用环境变量 A4AGENT_DSH_SKILLS_PATH 覆盖。"""
    override = env_first("A4AGENT_DSH_SKILLS_PATH", "A4API_DSH_SKILLS_PATH")
    if override:
        return Path(override)
    return config_manager.dsh_home() / "skills"


def zcode_skills_root() -> Path:
    """zcode 全局 skill 根，可用环境变量 A4AGENT_ZCODE_SKILLS_PATH 覆盖。

    zcode 官方发现路径：~/.zcode/skills（另有跨工具兼容目录 ~/.agents/skills，
    本工具统一托管到 .zcode 前缀，与其他端保持一致）。
    """
    override = env_first("A4AGENT_ZCODE_SKILLS_PATH", "A4API_ZCODE_SKILLS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".zcode" / "skills"


def pi_skills_root() -> Path:
    """pi 全局 skill 根，可用环境变量 A4AGENT_PI_SKILLS_PATH 覆盖。

    pi 的发现路径是 agent 目录下的 skills（默认 ~/.pi/agent/skills，而不是
    ~/.pi/skills），项目级仍是通用的 <项目>/.pi/skills。
    """
    return config_manager.pi_skills_root()


def qoder_skills_root() -> Path:
    """Qoder 全局 skill 根，可用环境变量 A4AGENT_QODER_SKILLS_PATH 覆盖。

    Qoder 同时扫描跨工具目录 ~/.agents/skills，但本工具统一托管到
    ~/.qoder/skills（它优先级更高，且与其它端的 .{tool}/skills 约定一致，
    项目级 <项目>/.qoder/skills 因此无需任何特判）。
    """
    return config_manager.qoder_skills_root()


def opencode_skills_root() -> Path:
    """OpenCode 全局 skill 根，可用环境变量 A4AGENT_OPENCODE_SKILLS_PATH 覆盖。

    官方发现路径是 ~/.config/opencode/skills（不是 ~/.opencode/skills）——
    OpenCode 的用户级数据统一在 ~/.config/opencode 下，与其它端「~/.<tool>/」
    的约定不同。项目级则是 <项目>/.opencode/skills，正好命中 project_skill_roots
    的 .{tool}/skills 通用公式，无需特判。

    本工具只托管自有目录，不碰 ~/.claude/skills、~/.agents/skills 等兼容目录
    （见 opencode_compat_roots）。
    """
    override = env_first("A4AGENT_OPENCODE_SKILLS_PATH", "A4API_OPENCODE_SKILLS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "opencode" / "skills"


def opencode_compat_roots(project_path: Path | None = None) -> list[Path]:
    """OpenCode 除自有目录外还会自动发现的兼容 skill 根。

    官方把 ~/.claude/skills、~/.agents/skills 与项目级对应目录列为兼容来源。
    这些目录里的 skill 在 OpenCode 端天然可用，但物理归属仍是 Claude /
    ZCode / Qoder 端——a4agent 据此标注「OpenCode 已可见」，让界面能说清
    「不必再迁一次」，而不是把它算成第八份副本。
    """
    if project_path is not None:
        return [project_path / ".claude" / "skills",
                project_path / ".agents" / "skills"]
    return [claude_skills_root(), Path.home() / ".agents" / "skills"]


def workbuddy_skills_root() -> Path:
    """WorkBuddy 全局 skill 根，可用环境变量 A4AGENT_WORKBUDDY_SKILLS_PATH 覆盖。

    WorkBuddy 的用户级技能目录是 ~/.workbuddy/skills，项目级 <工作区>/.workbuddy/skills，
    正好命中 project_skill_roots 的 .{tool}/skills 通用公式，无需特判。
    """
    override = env_first("A4AGENT_WORKBUDDY_SKILLS_PATH", "A4API_WORKBUDDY_SKILLS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".workbuddy" / "skills"


def global_skill_roots() -> dict:
    """八端全局 skill 根映射 {tool: Path}。"""
    return {
        "claude": claude_skills_root(),
        "codex": codex_skills_root(),
        "dsh": dsh_skills_root(),
        "zcode": zcode_skills_root(),
        "pi": pi_skills_root(),
        "qoder": qoder_skills_root(),
        "opencode": opencode_skills_root(),
        "workbuddy": workbuddy_skills_root(),
    }


# ---------------- 项目根列表（projects.json） ----------------


def _projects_file() -> Path:
    return get_data_dir() / PROJECTS_FILENAME


def _root_key(path) -> str:
    """路径比较键：abspath + normcase。

    abspath 顺带把 `/` 归一成 `\\`，不这样做时 `C:/x`、`C:\\x`、`c:\\x` 会被当成
    三个不同的根：扫三遍、弹窗里重复三行、去重全部失效（实测同一台机器上这几种
    写法都出现过）。刻意不用 resolve：它要真实访问文件系统，Win11 24H2 会对
    「不受信任装入点」抛 WinError 448（原因同 _normkey）。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def normalize_root_text(text) -> str:
    """用户给的根路径 → 规范绝对路径。

    剥掉首尾引号与空白（从资源管理器「复制路径」常带引号），并把只写了盘符的
    `D:` 补成 `D:\\`——Windows 上 `D:` 表示「D 盘的当前目录」，语义完全不是
    用户想要的。
    """
    value = str(text or "").strip().strip('"').strip("'").strip()
    if not value:
        return ""
    if re.fullmatch(r"[A-Za-z]:", value):
        value += "\\"
    if os.name == "nt" and re.match(r"^[a-z]:", value):
        value = value[0].upper() + value[1:]  # 盘符统一大写，界面上不出现 c:\ 这种写法
    try:
        return os.path.abspath(value)
    except (OSError, ValueError):
        return ""


def default_project_roots() -> list[str]:
    """默认扫描范围：本机所有可见盘符（含网络盘）。"""
    return disk_roots.default_project_roots()


def _read_projects_file() -> dict:
    path = _projects_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_projects(roots: list[str] | None = None, ignored: list[str] | None = None) -> None:
    """写回 projects.json；未指定的那一半沿用文件里的原值（只改根不能丢忽略列表）。"""
    current = _read_projects_file()
    payload = {
        "roots": list(roots) if roots is not None else list(current.get("roots") or []),
        "ignored": list(ignored) if ignored is not None else list(current.get("ignored") or []),
    }
    _projects_file().parent.mkdir(parents=True, exist_ok=True)
    _projects_file().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_project_roots() -> list[str]:
    """读取项目根目录列表；无文件或损坏时返回默认值。去重保序、路径归一。"""
    raw = _read_projects_file().get("roots")
    roots: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            normalized = normalize_root_text(item)
            if normalized:
                roots.append(normalized)

    result: list[str] = []
    seen: set[str] = set()
    for text in roots:
        key = _root_key(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)

    if not result:
        return default_project_roots()
    # 存量兼容：老版本的预置值是写死的开发机目录。若配置里恰好只有它、而它在本机
    # 根本不存在，说明这份文件从没被用户真正配置过，换成新预置（存在则尊重原样）。
    if len(result) == 1 and _root_key(result[0]) == _root_key(LEGACY_DEFAULT_ROOT):
        try:
            exists = Path(result[0]).is_dir()
        except OSError:
            exists = False
        if not exists:
            return default_project_roots()
    return result


def save_project_roots(roots: list) -> list[str]:
    """校验并保存项目根目录列表，返回规范化后的列表。非法项直接拒绝。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in roots or []:
        text = normalize_root_text(item)
        if not text:
            continue
        p = Path(text)
        if not p.is_absolute():
            raise ValueError(f"项目根必须是绝对路径：{text}")
        try:
            is_dir = p.is_dir()
        except OSError as e:
            raise ValueError(f"项目根无法访问：{text}（{e}）") from e
        if not is_dir:
            raise ValueError(f"项目根不存在或不是目录：{text}")
        key = _root_key(p)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(str(p))
    if not cleaned:
        raise ValueError("至少需要一个项目根目录")
    _write_projects(roots=cleaned)
    return cleaned


def load_ignored_projects() -> list[str]:
    """被用户忽略的自动发现项目（从历史线索里挖出来、但不想看到的目录）。"""
    raw = _read_projects_file().get("ignored")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        normalized = normalize_root_text(item)
        if not normalized:
            continue
        key = _root_key(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def save_ignored_projects(paths: list) -> list[str]:
    """保存忽略列表。只做绝对路径校验，不要求目录仍存在（盘拔了不该丢配置）。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in paths or []:
        text = normalize_root_text(item)
        if not text:
            continue
        if not Path(text).is_absolute():
            raise ValueError(f"忽略项必须是绝对路径：{text}")
        key = _root_key(text)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    _write_projects(ignored=cleaned)
    return cleaned


def _safe_is_dir(path: Path) -> bool:
    """is_dir 的容错版：Windows 11 24H2 会对「不受信任装入点」（如部分 junction）
    抛 WinError 448 且为间歇性，pathlib 对该错误码不归为可忽略，会直接上抛。
    单个异常目录不应炸掉整个发现/枚举流程，这里吞掉并记日志。"""
    try:
        return path.is_dir()
    except OSError as e:
        logger.warning("无法访问 %s：%s（已跳过）", path, e)
        return False


def _run_guarded(fn, timeout: float, default):
    """在守护线程里跑 fn，超时返回 default。

    只用于网络盘/UNC 路径：映射盘掉线时 stat 会一直挂着，HTTP 请求线程不能陪它
    一起挂。用 daemon 线程而不是线程池——线程池的 worker 默认非守护，解释器退出
    时会 join，卡死的 worker 会把整个进程退出也拖住。
    """
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except Exception as e:  # noqa: BLE001 - 兜底，超时/异常都降级为空结果
            box["error"] = e

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.warning("扫描超时（>%.1fs），已跳过：%s", timeout, getattr(fn, "__name__", fn))
        return default
    if "error" in box:
        logger.warning("扫描失败（已跳过）：%s", box["error"])
        return default
    return box.get("value", default)


def _list_root_children(root: Path) -> list[Path]:
    """列某个项目根下的一级子目录（跳过点开头目录）。"""

    def work() -> list[Path]:
        try:
            if not root.is_dir():
                return []
            children = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError as e:
            logger.warning("无法读取项目根 %s：%s（已跳过）", root, e)
            return []
        return [c for c in children if not c.name.startswith(".") and _safe_is_dir(c)]

    if disk_roots.is_remote(root):
        return _run_guarded(work, REMOTE_SCAN_TIMEOUT, [])
    return work()


def _has_skill_dir(path: Path) -> bool:
    """该目录下是否存在任意一个 .<tool>/skills。

    用于预筛历史线索：线索里绝大多数是普通仓库（本机实测 163 个候选只有 9 个
    含 skill），先筛掉能避免把它们一股脑塞进项目列表、迁移目标与归属校验。
    """
    for tool in TOOLS:
        if _safe_is_dir(path / f".{tool}" / "skills"):
            return True
    return False


def _shadows_global_scope(path: Path) -> bool:
    """该目录是否与全局 skill 根重叠——重叠就不能当项目。

    主目录是最典型的例子：project_skill_roots(主目录) 拼出的 ~/.claude/skills
    与全局根是**同一个目录**。两边都收，同一批 skill 会同时显示成「全局」和
    「某项目里的」，端数与重复判断全被污染。全局根本身或它下面的目录同理。
    """
    try:
        if path.parent == path:  # 盘根本身不是项目
            return True
    except OSError:
        return True
    key = _root_key(path)
    for root in global_skill_roots().values():
        root_key = _root_key(root)
        if key == root_key or key.startswith(root_key + os.sep):
            return True
        if root_key.startswith(key + os.sep):
            return True
    return False


def _disambiguate_names(items: list[dict]) -> list[dict]:
    """同名不同路径的项目加父目录后缀消歧。

    迁移目标、一键适配都是按项目**名字**定位目录的（_root_for）。默认扫描范围
    扩到所有盘符、再加上自动发现之后，两个盘下同名的 mlj 会同时出现——不消歧
    就会出现「点了 A 的项目，写进了 B 的目录」。
    """
    counts: dict[str, int] = {}
    for item in items:
        counts[item["project"]] = counts.get(item["project"], 0) + 1
    for item in items:
        if counts[item["project"]] > 1:
            parent = str(Path(item["root"]).parent)
            item["project"] = f'{item["project"]} ({parent})'
    return items


def project_dirs(roots: list[str] | None = None) -> list[dict]:
    """枚举项目：扫描范围下的一级子目录 + 历史线索反查到的精确目录。

    两条来源缺一不可：
    - 扫描范围只能覆盖「一级子目录」，用户的项目藏在几层之下时根本看不到；
    - 历史线索来自各 AI 工具的会话记录，深度任意、零手动操作，但不是每个项目
      都被工具打开过，扫描范围兜住这部分。

    返回 [{"project", "root", "source", "sources"}]。source ∈ root|hints|both，
    sources 是人可读的来源明细（如 "自动发现：Claude Code"）。
    """
    found: dict[str, dict] = {}

    def add(path: Path, source_text: str, kind: str) -> None:
        key = _root_key(path)
        item = found.get(key)
        if item is None:
            item = {"project": path.name or str(path), "root": path,
                    "source": "root", "sources": [], "_kinds": set()}
            found[key] = item
        if source_text not in item["sources"]:
            item["sources"].append(source_text)
        item["_kinds"].add(kind)

    ignored = {_root_key(p) for p in load_ignored_projects()}

    for root_text in roots if roots is not None else load_project_roots():
        root = Path(root_text)
        for child in _list_root_children(root):
            if _root_key(child) in ignored:
                continue
            add(child, f"扫描范围：{root_text}", "root")

    # 线索发现关闭（或全部失败）时为空，此时行为与旧版完全一致
    for hint in project_hints.collect():
        path = Path(hint["path"])
        if _root_key(path) in ignored:
            continue
        if _shadows_global_scope(path) or not _safe_is_dir(path):
            continue
        if not _has_skill_dir(path):
            continue
        for label in hint.get("sources") or []:
            add(path, f"自动发现：{label}", "hints")

    items = []
    for item in found.values():
        kinds = item.pop("_kinds")
        item["source"] = "both" if len(kinds) > 1 else ("hints" if "hints" in kinds else "root")
        items.append(item)
    return _disambiguate_names(items)


def _scan_project(proj_root: Path, project_name: str) -> list[dict]:
    """扫一个项目下八端的项目级 skill 根；远程项目加超时。"""

    def work() -> list[dict]:
        entries: list[dict] = []
        for tool, root in project_skill_roots(proj_root).items():
            entries.extend(_scan_root(root, tool, "project", project_name))
        return entries

    if disk_roots.is_remote(proj_root):
        return _run_guarded(work, REMOTE_SCAN_TIMEOUT, [])
    return work()


def project_skill_roots(project_path: Path) -> dict:
    """某项目的三端项目级 skill 根映射 {tool: Path}。"""
    return {tool: project_path / f".{tool}" / "skills" for tool in TOOLS}


def all_known_skill_roots() -> list[Path]:
    """全部已知 skill 根（全局 + 各项目级），用于路径归属校验。"""
    roots = list(global_skill_roots().values())
    for proj in project_dirs():
        roots.extend(project_skill_roots(proj["root"]).values())
    return roots


# ---------------- frontmatter 解析与 skill 识别 ----------------


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 的 YAML frontmatter，返回 ({...}, 正文)。无 frontmatter 返回 ({}, 原文)。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    block = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, body
    return (data if isinstance(data, dict) else {}), body


# pi 对 skill 的校验比其它端严格：name 只认小写字母/数字/连字符（≤64，不得
# 以连字符首尾或含连续连字符），description 必填且 ≤1024，不合规的条目 pi 直接
# 不加载。迁到 pi 后按 pi 的规则复查一次，否则界面「迁移成功」但 pi 里找不到。
PI_SKILL_NAME_MAX = 64
PI_SKILL_DESC_MAX = 1024


def pi_skill_notice(skill_dir: Path) -> str:
    """返回该 skill 在 pi 端不会被加载的原因（多条以 ； 分隔）；合规时返回空串。"""
    md = skill_dir / SKILL_FILE
    if not md.exists():
        return ""
    try:
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8-sig"))
    except OSError:
        return ""
    problems: list[str] = []
    name = str(meta.get("name") or skill_dir.name)
    if not re.fullmatch(r"[a-z0-9-]+", name):
        problems.append(f"pi 只认小写字母、数字与连字符的技能名，「{name}」不合规")
    elif len(name) > PI_SKILL_NAME_MAX:
        problems.append(f"技能名超过 pi 上限 {PI_SKILL_NAME_MAX} 字符（{len(name)}）")
    elif name.startswith("-") or name.endswith("-") or "--" in name:
        problems.append(f"技能名「{name}」不得以连字符开头/结尾或含连续连字符")
    desc = meta.get("description")
    if not (isinstance(desc, str) and desc.strip()):
        problems.append("pi 要求 frontmatter 有非空 description")
    elif len(desc) > PI_SKILL_DESC_MAX:
        problems.append(f"description 超过 pi 上限 {PI_SKILL_DESC_MAX} 字符（{len(desc)}）")
    return "；".join(problems)


def opencode_skill_notice(skill_dir: Path) -> str:
    """返回该 skill 在 OpenCode 端的使用提示（多条以 ； 分隔）；无提示返回空串。

    OpenCode V2 的判定口径与 pi 相反：它**不强制**任何命名规范，因此这里没有
    pi 那种「不合规直接不加载」的硬拦截，只有两类真会影响实际使用的情况值得
    提前告知：

    - 缺 description：该 skill 不会被广告给模型（等于装了没生效）
    - autoinvoke 关闭：技能仍注册，但不会出现在模型可用列表里

    另外提示 ID 由目录名派生：frontmatter name 与目录名不一致时，用户在
    @skill-id 里要用的其实是目录名。
    """
    md = skill_dir / SKILL_FILE
    if not md.exists():
        return ""
    try:
        meta, _ = parse_frontmatter(md.read_text(encoding="utf-8-sig"))
    except OSError:
        return ""
    problems: list[str] = []
    name = str(meta.get("name") or "").strip()
    desc = meta.get("description")
    if not (isinstance(desc, str) and desc.strip()):
        problems.append("OpenCode 要求 frontmatter 有非空 description，否则不会推荐给模型")
    if name and name != skill_dir.name:
        problems.append(
            f"OpenCode 的技能 ID 取目录名「{skill_dir.name}」，"
            f"frontmatter name「{name}」只作显示名"
        )
    if meta.get("disable-model-invocation") is True:
        problems.append("该技能已设 disable-model-invocation，OpenCode 不会自动调用它")
    auto = (meta.get("metadata") or {})
    if isinstance(auto, dict) and auto.get("opencode/autoinvoke") is False:
        problems.append("该技能已设 metadata.opencode/autoinvoke: false，OpenCode 不会自动调用它")
    return "；".join(problems)


def read_skill(skill_dir: Path, full: bool = False) -> dict | None:
    """读取 skill bundle 元数据；缺 SKILL.md 或目录不可读时返回 None。

    full=True 时附带 frontmatter 原始 dict、正文与全文，供内容预览使用。
    """
    md = skill_dir / SKILL_FILE
    try:
        if not md.is_file():
            return None
    except OSError as e:
        logger.warning("无法访问 %s：%s（已跳过）", md, e)
        return None
    try:
        # utf-8-sig 兼容带 BOM 的文件（Windows 编辑器常见）
        raw_text = md.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        logger.warning("读取 %s 失败：%s", md, e)
        return None
    meta, body = parse_frontmatter(raw_text)
    info = {
        "dir_name": skill_dir.name,
        "path": str(skill_dir),
        "name": str(meta.get("name") or "").strip() or skill_dir.name,
        "description": str(meta.get("description") or "").strip(),
    }
    if full:
        info["frontmatter"] = meta
        info["body"] = body
        info["raw"] = raw_text
    return info


def _identity_key(info: dict, tool: str) -> str:
    """同一上下文内的聚合键（仅供分组结果显示用）：绝大多数端是 frontmatter
    name，OpenCode 是目录名。

    真正的跨端同一性判定在 _aggregate：那里用「name 或 dir_name 任一命中」，
    因为 OpenCode 的身份是 ID（目录名）而其余端是显示名，两边都得认。
    """
    return info["dir_name"] if tool == "opencode" else info["name"]


def _scan_root(root: Path, tool: str, scope: str, project: str | None) -> list[dict]:
    """扫描一个 skill 根，返回其中合法 bundle 列表（跳过点开头目录如 Codex 的 .system/）。"""
    skills: list[dict] = []
    if not root.is_dir():
        return skills
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return skills
    for child in children:
        if not _safe_is_dir(child) or child.name.startswith("."):
            continue
        info = read_skill(child)
        if info is None:
            continue
        info["tool"] = tool
        info["scope"] = scope
        info["project"] = project
        info["key"] = _identity_key(info, tool)
        info["opencode_visible"] = False
        skills.append(info)
    return skills


def _mark_opencode_visible(entries: list[dict], roots: list[Path]) -> None:
    """标注落在 OpenCode 兼容目录里的条目（就地改写，标注理由）。

    只标不搬：这些目录物理上属于 Claude / ZCode / Qoder 端，OpenCode 只是顺带
    能看见。标注让界面能说清「这技能 OpenCode 已经能用」，从而不必再迁一份，
    也避免把它误算成 OpenCode 端已托管。
    """
    keys = {_normkey(r): r for r in roots}
    if not keys:
        return
    for entry in entries:
        parent = _normkey(Path(entry["path"]).parent)
        match = keys.get(parent)
        if match is not None:
            entry["opencode_visible"] = True
            entry["opencode_via"] = str(match)


def _aggregate(entries: list[dict]) -> list[dict]:
    """同一上下文内按身份键聚合，标注端数、是否重复与 OpenCode 兼容目录可见性。

    跨端合并用「name 或 dir_name 任一命中」，与 _find_source 的定位口径一致。
    这一条对 OpenCode 尤其重要：它的身份是 dir_name，若只按 name 合并，
    一个「目录名 gamma、显示名 Shared」的 OpenCode 条目会与「frontmatter name
    为 Shared」的 Claude 条目分家——用户看到的是同一个技能在两端各一张卡，
    迁移与一键适配也随之失准。
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    # 归一别名表：把同一实体的 name / dir_name 映射到同一个组键
    alias: dict[str, str] = {}

    def resolve(token: str) -> str | None:
        return alias.get(token.lower())

    for entry in entries:
        key = entry.get("key") or entry["name"]
        # OpenCode 条目只以 dir_name 入索引：它的 name 是显示标签而非身份，
        # 若也参与别名，两个「显示名相同、ID 不同」的技能会被错并成一份
        # （在 OpenCode 里它们是两个各自独立加载的技能）。
        tokens = {entry["dir_name"]} if entry.get("tool") == "opencode" \
            else {key, entry["name"], entry["dir_name"]}
        found = {resolve(t) for t in tokens if resolve(t)}
        group_key = found.pop() if found else key
        for t in tokens:
            alias[t.lower()] = group_key
        if group_key not in groups:
            groups[group_key] = []
            order.append(group_key)
        groups[group_key].append(entry)
    result = []
    for key in sorted(order, key=str.lower):
        copies = groups[key]
        ends = sorted({c["tool"] for c in copies}, key=TOOLS.index)
        first = copies[0]
        visible = next((c for c in copies if c.get("opencode_visible")), None)
        result.append(
            {
                "name": key,
                "display_name": first["name"],
                "description": first["description"],
                "ends": ends,
                "end_count": len(ends),
                "duplicate": len(copies) > 1,
                "opencode_visible": visible is not None,
                "opencode_via": (visible or {}).get("opencode_via", ""),
                "copies": copies,
            }
        )
    return result


def discover() -> dict:
    """全量发现：全局上下文按身份键聚合；每个项目上下文各自聚合。"""
    global_entries: list[dict] = []
    roots = global_skill_roots()
    for tool in TOOLS:
        global_entries.extend(_scan_root(roots[tool], tool, "global", None))
    _mark_opencode_visible(global_entries, opencode_compat_roots())

    projects_out = []
    for proj in project_dirs():
        entries = _scan_project(proj["root"], proj["project"])
        if not entries:
            continue  # 只收录含至少一个 skill 的项目
        _mark_opencode_visible(entries, opencode_compat_roots(proj["root"]))
        projects_out.append(
            {
                "project": proj["project"],
                "root": str(proj["root"]),
                "source": proj.get("source", "root"),
                "sources": proj.get("sources", []),
                "skills": _aggregate(entries),
            }
        )

    return {
        "global": _aggregate(global_entries),
        "projects": projects_out,
        "roots": {tool: str(path) for tool, path in roots.items()},
        "project_roots": load_project_roots(),
        "project_hints": project_hints.summary(),
    }


def project_sources(refresh: bool = False) -> dict:
    """项目来源汇总：扫描范围、默认值、忽略项、自动发现的候选与命中。"""
    hints = project_hints.summary(force=refresh)
    return {
        "roots": load_project_roots(),
        "defaults": default_project_roots(),
        "ignored": load_ignored_projects(),
        "hints": hints,
        "projects": [
            {
                "project": p["project"],
                "root": str(p["root"]),
                "source": p.get("source", "root"),
                "sources": p.get("sources", []),
                "has_skills": _has_skill_dir(p["root"]),
            }
            for p in project_dirs()
        ],
    }


# ---------------- 路径归属校验（删除 / 打开 / 预览的安全前提） ----------------


def _normkey(path) -> str:
    """路径比较键：abspath 纯字符串归一化（normcase 处理大小写与斜杠）。

    刻意不用 Path.resolve()：
    - resolve 会把 junction/符号链接解析到真实目标，与发现流程（iterdir 视角，
      junction 就是一个普通子目录）口径不一致，会让链接进来的 skill 被误判
      「不在任何已知 skill 存放区」；
    - resolve 需要真实访问文件系统，Windows 11 24H2 对「不受信任装入点」会
      间歇性抛 WinError 448，让整条校验链路未捕获 500。
    abspath 两者皆无：不跟随 reparse point，也不触碰文件系统。
    """
    return os.path.normcase(os.path.abspath(str(path)))


def locate_skill(path_text: str, require_skill_md: bool = True) -> Path:
    """校验给定路径位于某个已知 skill 根之下（且是其中的直接子目录）。

    校验失败抛 ValueError；成功返回归一化后的目录路径。归属比较使用
    _normkey（不解析 junction、不访问文件系统），与发现流程同视角。
    """
    if not path_text or not str(path_text).strip():
        raise ValueError("缺少 skill 路径")
    try:
        target = Path(os.path.abspath(str(path_text)))
    except (OSError, ValueError) as e:
        raise ValueError(f"无效的 skill 路径：{e}") from e
    try:
        if not target.is_dir():
            raise ValueError(f"skill 目录不存在：{target}")
    except OSError as e:
        raise ValueError(f"skill 目录无法访问：{e}") from e
    known = [_normkey(r) for r in all_known_skill_roots()]
    parent = _normkey(target.parent)
    if parent not in known:
        raise ValueError("该路径不在任何已知 skill 存放区中，拒绝操作")
    if require_skill_md:
        try:
            has_md = (target / SKILL_FILE).is_file()
        except OSError as e:
            raise ValueError(f"skill 目录无法访问：{e}") from e
        if not has_md:
            raise ValueError(f"该目录不是合法的 skill bundle（缺少 {SKILL_FILE}）")
    return target


def skill_location(target: Path) -> dict:
    """推断 skill 目录的位置属性（scope/tool/project）。与 locate_skill 同用
    _normkey 比较，junction skill 的位置按链接所在端计（删除/回收站只动链接）。"""
    parent = _normkey(target.parent)
    for tool in TOOLS:
        root = global_skill_roots()[tool]
        if _normkey(root) == parent:
            return {"scope": "global", "tool": tool, "project": None}
    for proj in project_dirs():
        for tool in TOOLS:
            root = project_skill_roots(proj["root"])[tool]
            if _normkey(root) == parent:
                return {"scope": "project", "tool": tool, "project": proj["project"]}
    return {"scope": "unknown", "tool": "", "project": None}


def _root_for(scope: str, tool: str, project: str | None) -> Path:
    if scope == "global":
        roots = global_skill_roots()
        if tool not in roots:
            raise ValueError(f"未知工具：{tool}")
        return roots[tool]
    if scope == "project":
        for proj in project_dirs():
            if proj["project"] == project:
                return project_skill_roots(proj["root"])[tool]
        raise ValueError(f"未找到项目：{project}")
    raise ValueError(f"未知 scope：{scope}")


def custom_project_skills_root(path_text: str | None, tool: str) -> Path:
    """自选项目文件夹的目标 skill 根：文件夹必须已存在（绝对路径），
    其下 .{tool}/skills 缺失没关系——迁移时 mkdir(parents=True) 自动创建。"""
    if tool not in TOOLS:
        raise ValueError(f"未知工具：{tool}")
    p = Path(path_text or "")
    if not p.is_absolute():
        raise ValueError(f"项目文件夹必须是绝对路径：{path_text}")
    if not _safe_is_dir(p):
        raise ValueError(f"项目文件夹不存在或不是目录：{path_text}")
    return p / f".{tool}" / "skills"


# ---------------- 回收站 ----------------


def trash_dir() -> Path:
    d = get_data_dir() / TRASH_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def purge_expired(db) -> int:
    """惰性清理超过 30 天的回收站条目，返回本次清理条数。"""
    from . import models

    deadline = datetime.now() - timedelta(days=TRASH_KEEP_DAYS)
    expired = db.query(models.SkillTrash).filter(models.SkillTrash.trash_time <= deadline).all()
    purged = 0
    for item in expired:
        p = Path(item.trash_path)
        if p.exists():
            try:
                shutil.rmtree(p)
            except OSError as e:
                logger.warning("清理回收站目录失败 %s：%s", p, e)
                continue  # 清理失败时保留记录，下次再试
        db.delete(item)
        purged += 1
    if purged:
        db.commit()
        logger.info("回收站清理了 %s 条过期条目", purged)
    return purged


def delete_to_trash(db, path_text: str) -> dict:
    """把 skill 目录移入回收站，返回操作结果（含顺带清理的过期条数）。"""
    from . import models

    target = locate_skill(path_text)
    location = skill_location(target)
    purged = purge_expired(db)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = trash_dir() / f"{target.name}.{ts}"
    n = 1
    while dest.exists():
        dest = trash_dir() / f"{target.name}.{ts}_{n}"
        n += 1
    try:
        shutil.move(str(target), str(dest))
    except OSError as e:
        raise ValueError(f"移动到回收站失败：{e}") from e

    meta = read_skill(dest) or {}
    row = models.SkillTrash(
        skill_name=meta.get("name") or target.name,
        dir_name=target.name,
        tool=location["tool"],
        scope=location["scope"],
        project=location["project"],
        original_path=str(target),
        trash_path=str(dest),
        trash_time=datetime.now(),
    )
    db.add(row)
    db.commit()
    logger.info("skill「%s」已移入回收站（原位置 %s）", row.skill_name, target)
    return {"deleted": True, "name": row.skill_name, "purged_expired": purged}


def serialize_trash(item) -> dict:
    days_left = max(
        0,
        TRASH_KEEP_DAYS - (datetime.now() - item.trash_time).days,
    )
    return {
        "id": item.id,
        "name": item.skill_name,
        "dir_name": item.dir_name,
        "tool": item.tool,
        "scope": item.scope,
        "project": item.project,
        "original_path": item.original_path,
        "trash_time": item.trash_time.isoformat(sep=" ", timespec="seconds"),
        "days_left": days_left,
    }


def list_trash(db) -> dict:
    """回收站列表（顺带执行过期清理），purged_expired 为本次清理条数。"""
    from . import models

    purged = purge_expired(db)
    rows = (
        db.query(models.SkillTrash)
        .order_by(models.SkillTrash.trash_time.desc())
        .all()
    )
    return {"items": [serialize_trash(r) for r in rows], "purged_expired": purged}


def restore_from_trash(db, trash_id: int) -> dict:
    """从回收站恢复到原位置；原位置被占用时报错，不做覆盖。"""
    from . import models

    item = db.query(models.SkillTrash).filter(models.SkillTrash.id == trash_id).first()
    if not item:
        raise ValueError("回收站中不存在该条目")
    src = Path(item.trash_path)
    if not src.exists():
        db.delete(item)
        db.commit()
        raise ValueError("回收站目录已丢失，条目已被移除")
    dest = Path(item.original_path)
    if dest.exists():
        raise ValueError(f"原位置已存在同名目录，无法恢复：{dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dest))
    except OSError as e:
        raise ValueError(f"恢复失败：{e}") from e
    db.delete(item)
    db.commit()
    logger.info("skill「%s」已恢复到 %s", item.skill_name, dest)
    return {"restored": True, "name": item.skill_name, "path": str(dest)}


def delete_permanent(db, trash_id: int) -> dict:
    """彻底删除回收站中的单个条目。"""
    from . import models

    item = db.query(models.SkillTrash).filter(models.SkillTrash.id == trash_id).first()
    if not item:
        raise ValueError("回收站中不存在该条目")
    src = Path(item.trash_path)
    if src.exists():
        try:
            shutil.rmtree(src)
        except OSError as e:
            raise ValueError(f"彻底删除失败：{e}") from e
    db.delete(item)
    db.commit()
    return {"deleted_permanently": True, "name": item.skill_name}


# ---------------- 迁移（复制语义） ----------------


def _find_source(descriptor: dict) -> tuple[Path, dict]:
    """按 (scope, tool, project, name/dir_name) 定位源 skill 目录。"""
    scope = descriptor.get("scope")
    tool = descriptor.get("tool")
    project = descriptor.get("project")
    name = str(descriptor.get("name") or "").strip()
    if not name:
        raise ValueError("迁移源缺少 skill 名称")
    if scope == "global":
        entries = _scan_root(_root_for("global", tool, None), tool, "global", None)
    elif scope == "project" and descriptor.get("project_root"):
        # 源端同样支持自选项目文件夹（与目标端对称）
        entries = _scan_root(
            custom_project_skills_root(descriptor["project_root"], tool),
            tool, "project", None,
        )
    elif scope == "project":
        found = None
        for proj in project_dirs():
            if proj["project"] == project:
                found = proj
                break
        if found is None:
            raise ValueError(f"未找到项目：{project}")
        entries = _scan_root(
            project_skill_roots(found["root"])[tool], tool, "project", project
        )
    else:
        raise ValueError(f"未知 scope：{scope}")

    lowered = name.lower()
    for entry in entries:
        if entry["name"].lower() == lowered or entry["dir_name"].lower() == lowered:
            return Path(entry["path"]), entry
    where = "全局" if scope == "global" else f"项目「{project}」"
    raise ValueError(f"在{where}{TOOL_LABELS.get(tool, tool)}端未找到 skill：{name}")


def _ignore_links(directory, names):
    """copytree 的 ignore 回调：跳过符号链接与 Windows junction（reparse point）。

    copytree 默认整树解引用：环状链接会递归到爆栈，junction 会被展开成实体
    副本（可能巨大且重复）。技能包不应含跨目录引用，跳过比展开安全。
    """
    skip = set()
    for n in names:
        p = os.path.join(directory, n)
        if os.path.islink(p):
            skip.add(n)
        elif os.name == "nt" and _is_reparse_point(p):
            skip.add(n)
    return skip


def _is_reparse_point(path) -> bool:
    """Windows junction / 挂载点检测（os.path.islink 不认 junction）。"""
    try:
        import ctypes

        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:  # noqa: BLE001 - 探测失败按普通条目处理
        return False


def _trash_existing_conflicts(
    db,
    dest_root: Path,
    incoming_name: str,
    incoming_dir: str,
    tool: str | None = None,
) -> int:
    """目标根下已存在的同名 skill 先移入回收站，返回处理条数。

    判定口径跟目标端对齐：OpenCode 的技能身份是目录名，只有目录名相同才算同一
    个技能。若沿用「目录名或 frontmatter name 任一命中」，会把一个仅显示名相同、
    ID 实际不同的既有技能误删——那是在替用户销毁一个能正常工作的条目。
    """
    moved = 0
    dir_only = tool == "opencode"
    lowered_name = incoming_name.lower()
    lowered_dir = incoming_dir.lower()
    if not dest_root.is_dir():
        return 0
    for child in sorted(dest_root.iterdir(), key=lambda p: p.name.lower()):
        if not _safe_is_dir(child) or child.name.startswith("."):
            # 点开头目录与发现层（_scan_root 等）同口径不可见：迁移暂存目录
            # （.<name>.staging）内含同名 SKILL.md，不跳过会被当成冲突回收掉
            continue
        meta = read_skill(child)
        same_dir = child.name.lower() == lowered_dir
        same_name = bool(meta) and meta["name"].lower() == lowered_name
        if not (same_dir or (same_name and not dir_only)):
            continue
        location = skill_location(child)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = trash_dir() / f"{child.name}.{ts}"
        n = 1
        while dest.exists():
            dest = trash_dir() / f"{child.name}.{ts}_{n}"
            n += 1
        shutil.move(str(child), str(dest))
        from . import models

        db.add(
            models.SkillTrash(
                skill_name=(meta or {}).get("name") or child.name,
                dir_name=child.name,
                tool=location["tool"],
                scope=location["scope"],
                project=location["project"],
                original_path=str(child),
                trash_path=str(dest),
                trash_time=datetime.now(),
            )
        )
        moved += 1
    if moved:
        db.commit()
    return moved


def migrate(db, sources: list[dict], targets: list[dict]) -> dict:
    """一键迁移：sources × targets 逐对复制（非破坏性，源端保留）。

    每对迁移写入一条 SkillMigration 日志；返回汇总结果供前端提示。
    """
    from . import models

    if not sources:
        raise ValueError("请选择要迁移的 skill")
    if not targets:
        raise ValueError("请选择迁移目标")

    resolved_sources = []
    for s in sources:
        path, entry = _find_source(s)
        resolved_sources.append((path, entry))

    # 目标合法性预校验：未知工具 / 项目 / scope / 自选路径 在任何复制发生前整体失败
    for t in targets:
        if t.get("project_root"):
            custom_project_skills_root(t.get("project_root"), t.get("tool"))
        else:
            _root_for(t.get("scope"), t.get("tool"), t.get("project"))

    results = []
    migrated = skipped = conflicts = failed = 0
    seen_pairs: set[tuple[str, str]] = set()

    for s_idx, (src, entry) in enumerate(resolved_sources):
        s_desc = sources[s_idx]
        for t in targets:
            t_tool = t.get("tool")
            if t_tool not in TOOLS:
                raise ValueError(f"未知工具：{t_tool}")
            t_root_text = (t.get("project_root") or "").strip()
            custom = bool(t_root_text)
            if custom:
                t_scope = "project"
                t_project = t_root_text[:200]
                dest_key = ("project", t_tool, str(Path(t_root_text)))
                target_label = f"项目「{t_root_text}」· {TOOL_LABELS[t_tool]}"
            else:
                t_scope = t.get("scope")
                t_project = t.get("project")
                if t_scope not in ("global", "project"):
                    raise ValueError(f"未知 scope：{t_scope}")
                dest_key = (
                    ("global", t_tool, "")
                    if t_scope == "global"
                    else ("project", t_tool, t_project or "")
                )
                target_label = (
                    f"全局 · {TOOL_LABELS[t_tool]}"
                    if t_scope == "global"
                    else f"项目「{t_project}」· {TOOL_LABELS[t_tool]}"
                )

            pair_key = (str(src), "|".join(dest_key))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # 目标位置与源完全一致时跳过（自选路径按物理目录比较）
            same_place = (
                (s_desc.get("scope") == t_scope)
                and (s_desc.get("tool") == t_tool)
                and ((s_desc.get("project") or "") == dest_key[2])
            )
            if custom and src.parent == Path(dest_key[2]) / f".{t_tool}" / "skills":
                same_place = True
            if same_place:
                skipped += 1
                results.append(
                    {
                        "source": entry["name"],
                        "target": target_label,
                        "status": "skipped",
                        "detail": "源与目标相同",
                    }
                )
                continue

            base = {
                "skill_name": entry["name"],
                "source_tool": s_desc.get("tool"),
                "source_scope": s_desc.get("scope"),
                "source_project": s_desc.get("project"),
                "source_path": str(src),
                "target_tool": t_tool,
                "target_scope": t_scope,
                "target_project": t_project,
            }
            try:
                if custom:
                    dest_root = Path(dest_key[2]) / f".{t_tool}" / "skills"
                else:
                    dest_root = _root_for(t_scope, t_tool, t_project)
                dest_root.mkdir(parents=True, exist_ok=True)
                dest = dest_root / src.name
                # 先复制到暂存目录、回收冲突后再换入：直接 copytree 到 dest 的
                # 顺序下，复制一旦失败旧版已进回收站而新版只有半截，两头落空
                staging = dest_root / f".{src.name}.staging"
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                shutil.copytree(src, staging, ignore=_ignore_links)
                try:
                    trashed = _trash_existing_conflicts(
                        db, dest_root, entry["name"], src.name, tool=t_tool
                    )
                    if dest.exists():
                        raise IOError(f"目标位置仍被占用：{dest}")
                    staging.rename(dest)
                except Exception:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
                conflicts += trashed
                migrated += 1
                detail = f"已复制到 {dest}"
                if trashed:
                    detail += f"；目标端旧版 {trashed} 份已移入回收站"
                if t_tool == "pi":
                    notice = pi_skill_notice(dest)
                    if notice:
                        detail += f"；⚠ pi 不会加载该 skill：{notice}"
                elif t_tool == "opencode":
                    notice = opencode_skill_notice(dest)
                    if notice:
                        detail += f"；⚠ OpenCode 端注意：{notice}"
                db.add(models.SkillMigration(**base, status="success", detail=detail))
                results.append(
                    {
                        "source": entry["name"],
                        "target": target_label,
                        "status": "success",
                        "detail": detail,
                    }
                )
                logger.info("skill「%s」迁移成功：%s", entry["name"], detail)
            except Exception as e:  # 单对失败不影响其余迁移，日志留痕
                failed += 1
                db.add(
                    models.SkillMigration(**base, status="failed", detail=str(e))
                )
                results.append(
                    {
                        "source": entry["name"],
                        "target": target_label,
                        "status": "failed",
                        "detail": str(e),
                    }
                )
                logger.exception("skill「%s」迁移失败", entry["name"])

    db.commit()
    return {
        "migrated": migrated,
        "skipped": skipped,
        "conflicts_trashed": conflicts,
        "failed": failed,
        "results": results,
    }


def list_migrations(db, limit: int = 200) -> list[dict]:
    """最近的迁移日志（新→旧）。"""
    from . import models

    rows = (
        db.query(models.SkillMigration)
        .order_by(models.SkillMigration.migrate_time.desc(), models.SkillMigration.id.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )

    def fmt(row) -> dict:
        def place(scope, project):
            if scope == "global":
                return "全局"
            return f"项目「{project or '?'}」"

        return {
            "id": row.id,
            "skill_name": row.skill_name,
            "source": f"{place(row.source_scope, row.source_project)} · {TOOL_LABELS.get(row.source_tool, row.source_tool)}",
            "target": f"{place(row.target_scope, row.target_project)} · {TOOL_LABELS.get(row.target_tool, row.target_tool)}",
            "status": row.status,
            "detail": row.detail,
            "migrate_time": row.migrate_time.isoformat(sep=" ", timespec="seconds"),
        }

    return [fmt(r) for r in rows]


# ---------------- 打开 / 预览 ----------------


def open_in_explorer(path_text: str) -> dict:
    """在系统资源管理器中打开 skill 目录。"""
    target = locate_skill(path_text)
    try:
        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606  # Windows 资源管理器
        elif os.uname().sysname == "Darwin":  # pragma: no cover
            subprocess.Popen(["open", str(target)])
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(target)])
    except OSError as e:
        raise ValueError(f"打开资源管理器失败：{e}") from e
    return {"opened": True, "path": str(target)}


def read_content(path_text: str) -> dict:
    """读取 SKILL.md 内容供前端预览渲染。"""
    target = locate_skill(path_text)
    info = read_skill(target, full=True)
    if info is None:
        raise ValueError(f"该目录不是合法的 skill bundle（缺少 {SKILL_FILE}）")
    return {
        "name": info["name"],
        "description": info["description"],
        "dir_name": info["dir_name"],
        "path": str(target),
        "frontmatter": info["frontmatter"],
        "body": info["body"],
        "raw": info["raw"],
    }
