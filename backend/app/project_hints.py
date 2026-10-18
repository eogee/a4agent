"""从各 AI 工具留下的历史记录里反查用户真实用过的项目目录。

项目视图的收录条件是「<目录>/.<tool>/skills 存在」，而这类目录可能藏在任意
深度（本机实测有 `C:\\ProgramMine\\mlj\\mlj-wechat` 这种三层路径）。让用户逐个
点选根目录，等于把「找到项目」的工作转嫁给用户；但这些工具本来就记录了用户
打开过哪些目录，读它们成本是毫秒级、零磁盘遍历。

三条来源，全部独立 try/except——任一工具没装、格式变了、文件损坏，都不影响
其余来源：

- claude-code：``~/.claude.json`` 的 ``projects`` 键（每次会话的 cwd 都记在里面）
  + ``~/.claude/history.jsonl`` 每行的 ``project`` 字段
- codex：``~/.codex/sessions/**/*.jsonl`` 首行的 ``payload.cwd``（只读首行，
  不解析正文，避免把整个会话读进内存）
- VS Code 系编辑器：``%APPDATA%/<编辑器>/User/globalStorage/storage.json`` 的
  ``profileAssociations.workspaces``（值是 ``file:///c%3A/...`` URI）；
  读不到时退回同目录 ``state.vscdb`` 的 ``history.recentlyOpenedPathsList``。
  用通配符发现而不是写死编辑器名单，Trae、CodeBuddy 这类分支自动覆盖。

这里只负责「把线索收集干净」，是否算项目、是否与全局根重叠，由 skill_manager
判定（它才掌握 TOOLS 与全局 skill 根）。

结果落盘缓存到 ``get_data_dir()/project_hints.json``，默认 10 分钟内复用。
"""
import glob
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .database import get_data_dir

logger = logging.getLogger(__name__)

CACHE_FILENAME = "project_hints.json"
CACHE_TTL_SECONDS = 600
# 设成 0 关闭线索发现（测试用：否则会读到开发机真实项目，污染发现结果）
ENV_DISABLE = "A4AGENT_PROJECT_HINTS"
# 单个来源最多读取的会话文件数：按 mtime 取最新的，老会话的项目基本不会再用
CODEX_MAX_SESSIONS = 200
# history.jsonl 可能很长，只读尾部若干行即可覆盖近期项目
HISTORY_MAX_LINES = 4000

# 编辑器目录名 → 界面显示名（没命中的用目录名本身）
EDITOR_LABELS = {
    "Code": "VS Code",
    "Code - Insiders": "VS Code Insiders",
    "Trae CN": "Trae",
    "Trae": "Trae",
    "CodeBuddy CN": "CodeBuddy",
    "CodeBuddy": "CodeBuddy",
    "VSCodium": "VSCodium",
}


def enabled() -> bool:
    """线索发现开关。默认开启；A4AGENT_PROJECT_HINTS=0/false 时关闭。"""
    return os.environ.get(ENV_DISABLE, "1").strip().lower() not in ("0", "false", "no", "off", "")


# ---------------- 路径归一化 ----------------


def _normalize(path_text) -> str | None:
    """线索里的路径统一成绝对路径：剥引号、补盘符反斜杠、把 / 归一成 \\。

    实测同一台机器上同一目录会以 ``C:\\x`` / ``C:/x`` / ``c:\\x`` /
    ``file:///c%3A/x`` 四种写法出现，不归一就会在项目视图里重复成四条。
    """
    text = str(path_text or "").strip().strip('"').strip("'").strip()
    if not text:
        return None
    if re.fullmatch(r"[A-Za-z]:", text):  # 只给了盘符：C: 在 Windows 上是「当前目录」
        text += "\\"
    if os.name == "nt" and re.match(r"^[a-z]:", text):
        text = text[0].upper() + text[1:]  # 盘符统一大写：c:\x 与 C:\x 是同一处
    try:
        return os.path.abspath(text)
    except (OSError, ValueError):
        return None


def _key(path_text: str) -> str:
    return os.path.normcase(os.path.abspath(path_text))


def _uri_to_path(uri) -> str | None:
    """``file:///c%3A/ProgramMine/x`` → ``c:\\ProgramMine\\x``；非 file URI 返回 None。"""
    try:
        parsed = urlparse(str(uri))
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    text = unquote(parsed.path or "")
    if not text:
        return None
    if os.name == "nt":
        if len(text) > 2 and text[0] == "/" and text[2] == ":":
            text = text[1:]
        text = text.replace("/", "\\")
    return text


# ---------------- 各来源提取器 ----------------


def _add(found: dict, path_text, source: str) -> None:
    norm = _normalize(path_text)
    if not norm:
        return
    item = found.setdefault(_key(norm), {"path": norm, "sources": []})
    if source not in item["sources"]:
        item["sources"].append(source)


def _from_claude(found: dict) -> None:
    home = Path.home()
    cfg = home / ".claude.json"
    if cfg.is_file():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8-sig"))
            for path_text in (data.get("projects") or {}):
                _add(found, path_text, "claude-code")
        except (OSError, ValueError) as e:
            logger.debug("读取 %s 失败：%s", cfg, e)

    history = home / ".claude" / "history.jsonl"
    if history.is_file():
        try:
            with history.open(encoding="utf-8", errors="replace") as fh:
                for idx, line in enumerate(fh):
                    if idx >= HISTORY_MAX_LINES:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    _add(found, row.get("project") or row.get("cwd"), "claude-code")
        except OSError as e:
            logger.debug("读取 %s 失败：%s", history, e)


def _from_codex(found: dict) -> None:
    pattern = str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl")
    try:
        files = glob.glob(pattern, recursive=True)
    except OSError:
        return
    if not files:
        return

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0

    for path in sorted(files, key=mtime, reverse=True)[:CODEX_MAX_SESSIONS]:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
            row = json.loads(first)
        except (OSError, ValueError):
            continue
        payload = row.get("payload") if isinstance(row, dict) else None
        if isinstance(payload, dict):
            _add(found, payload.get("cwd"), "codex")


def _editor_storage_files() -> list[tuple[str, Path]]:
    """枚举各编辑器的 globalStorage 目录，返回 [(显示名, storage.json 路径)]。"""
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        bases.append(Path(appdata))
    bases.append(Path.home() / "Library" / "Application Support")  # macOS
    bases.append(Path.home() / ".config")  # Linux

    seen: set[str] = set()
    result: list[tuple[str, Path]] = []
    for base in bases:
        try:
            candidates = list(base.glob("*/User/globalStorage/storage.json"))
        except OSError:
            continue
        for path in candidates:
            editor_dir = path.parent.parent.parent.name
            key = _key(str(path))
            if key in seen:
                continue
            seen.add(key)
            result.append((EDITOR_LABELS.get(editor_dir, editor_dir), path))
    return result


def _from_editors(found: dict) -> None:
    for label, storage in _editor_storage_files():
        read = False
        try:
            data = json.loads(storage.read_text(encoding="utf-8-sig"))
            for uri in ((data.get("profileAssociations") or {}).get("workspaces") or {}):
                _add(found, _uri_to_path(uri), label)
                read = True
        except (OSError, ValueError) as e:
            logger.debug("读取 %s 失败：%s", storage, e)

        if read:
            continue
        # storage.json 里没有工作区记录时，退回同一份 globalStorage 的 SQLite
        _from_editor_db(found, label, storage.parent / "state.vscdb")


def _from_editor_db(found: dict, label: str, db_path: Path) -> None:
    if not db_path.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return
    try:
        row = con.execute(
            "select value from ItemTable where key = 'history.recentlyOpenedPathsList'"
        ).fetchone()
        if not row or row[0] is None:
            return
        payload = row[0]
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", "replace")
        for entry in (json.loads(payload).get("entries") or []):
            if isinstance(entry, dict):
                _add(found, _uri_to_path(entry.get("folderUri") or entry.get("workspace")), label)
    except (sqlite3.Error, ValueError) as e:
        logger.debug("读取 %s 失败：%s", db_path, e)
    finally:
        con.close()


# ---------------- 缓存与对外接口 ----------------


def _cache_file() -> Path:
    return get_data_dir() / CACHE_FILENAME


def _read_cache() -> dict | None:
    path = _cache_file()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return None
    try:
        scanned = datetime.fromisoformat(str(data.get("scanned_at")))
    except ValueError:
        return None
    if (datetime.now() - scanned).total_seconds() > CACHE_TTL_SECONDS:
        return None
    return data


def _write_cache(projects: list[dict]) -> None:
    try:
        _cache_file().write_text(
            json.dumps(
                {"scanned_at": datetime.now().isoformat(timespec="seconds"), "projects": projects},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("写入线索缓存失败：%s", e)


def collect(force: bool = False) -> list[dict]:
    """返回 [{"path": 绝对路径, "sources": [来源名, ...]}]，按路径去重。

    force=True 跳过缓存重新扫描；关闭开关时返回空列表。任何单个来源抛异常都
    只丢它自己，不影响其余来源。
    """
    if not enabled():
        return []
    if not force:
        cached = _read_cache()
        if cached is not None:
            return cached["projects"]

    found: dict[str, dict] = {}
    for extractor in (_from_claude, _from_codex, _from_editors):
        try:
            extractor(found)
        except Exception as e:  # noqa: BLE001 - 单来源异常不能拖垮整体
            logger.warning("项目线索来源 %s 解析失败（已跳过）：%s", extractor.__name__, e)

    projects = sorted(found.values(), key=lambda x: x["path"].lower())
    _write_cache(projects)
    return projects


def summary(force: bool = False) -> dict:
    """给界面的汇总：总数、各来源命中数、扫描时间。"""
    projects = collect(force=force)
    counts: dict[str, int] = {}
    for item in projects:
        for source in item["sources"]:
            counts[source] = counts.get(source, 0) + 1
    cached = _read_cache() or {}
    return {
        "enabled": enabled(),
        "candidate_count": len(projects),
        "sources": [
            {"name": name, "count": counts[name]}
            for name in sorted(counts, key=lambda n: (-counts[n], n.lower()))
        ],
        "scanned_at": cached.get("scanned_at", ""),
    }
