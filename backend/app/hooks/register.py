"""会话 Hook 注册/卸载：把 a4agent 的 hook 命令写进各宿主配置文件。

覆盖五端（DSH 走进程内插件，殿后）：
  Claude Code  ~/.claude/settings.json      hooks.{Stop,PreToolUse,PermissionRequest}
  Qoder        ~/.qoder/settings.json       同构 Claude，命令带 qoder 标识
  WorkBuddy    ~/.workbuddy/settings.json   同构 Claude，命令带 workbuddy 标识
  ZCode        ~/.zcode/cli/config.json     事件挂在 hooks.events 下，需 hooks.enabled=true
  Codex        ~/.codex/config.toml         文本追加（marker 块；TOML 重写会毁用户注释）

幂等：已注册时不重复写。归属判定用「命令含 a4agent 且含 ' hook'」，
不会误删用户自己的 a4p（a4phone）或其它 hook 命令。
"""
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# 命令归属标识：a4agent 的 hook 命令形如 "C:\...\a4agent.exe" hook [agent]
OWN_COMMAND_PATTERN = re.compile(r"a4agent[^\"']*\s+hook\b|\ba4agent\b.*\bhook\b", re.IGNORECASE)

CODEX_MARKER_START = "# >>> a4agent hooks >>>"
CODEX_MARKER_END = "# <<< a4agent hooks <<<"

ENGINES = ("claude", "codex", "zcode", "qoder", "workbuddy")


def default_hook_command(agent: str | None = None) -> str:
    """默认 hook 命令（带引号防路径空格）。

    打包态 sys.executable 即 a4agent.exe，子命令直接跟在后面；
    开发态 sys.executable 是 python.exe，必须显式给出 desktop.py 脚本路径——
    否则 `python.exe hook workbuddy` 会被解释成「运行名为 hook 的脚本」，
    宿主侧报 can't open file，hook 静默失效（实测坑）。
    """
    suffix = f" {agent}" if agent else ""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" hook{suffix}'
    entry = Path(__file__).resolve().parents[3] / "desktop.py"
    return f'"{sys.executable}" "{entry}" hook{suffix}'


def _is_own_command(command: str) -> bool:
    return bool(command) and "hook" in command and "a4agent" in command.lower()


def _settings_home() -> Path:
    return Path.home()


# ---------------- Claude / Qoder / WorkBuddy（settings.json 同构） ----------------

_EVENT_BLOCKS = {
    "Stop": [{"matcher": "*", "hooks": [{}]}],
    "PreToolUse": [{"matcher": "AskUserQuestion", "hooks": [{}]}],
    "PermissionRequest": [{"matcher": "*", "hooks": [{}]}],
}


def _blocks_with_command(blocks_template: list, hook_command: str) -> list:
    import copy

    blocks = copy.deepcopy(blocks_template)
    for b in blocks:
        for h in b["hooks"]:
            h.update({"type": "command", "command": hook_command})
    return blocks


def register_json_hooks(settings_path: Path, hook_command: str) -> bool:
    settings: dict = {}
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):
        settings = {}
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    settings["hooks"] = hooks

    changed = False
    for event, blocks in _EVENT_BLOCKS.items():
        raw = hooks.get(event)
        event_list = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        # 先剔除本工具的历史命令再按当前命令重建（而非「已存在就跳过」）：
        # 早期版本注册过写错的命令（如缺脚本路径的 python.exe hook），
        # 幂等跳过会让错误配置永久留在宿主里，只能靠重新安装修复。
        kept = [b for b in event_list
                if not (isinstance(b, dict)
                        and any(_is_own_command(h.get("command", ""))
                                for h in (b.get("hooks") or []) if isinstance(h, dict)))]
        new_list = kept + _blocks_with_command(blocks, hook_command)
        if new_list != event_list:
            changed = True
        if new_list:
            hooks[event] = new_list
        else:
            hooks.pop(event, None)

    if not changed:
        return False
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def unregister_json_hooks(settings_path: Path) -> bool:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, raw in list(hooks.items()):
        if not isinstance(raw, list):
            continue
        kept = []
        for b in raw:
            if isinstance(b, dict) and any(_is_own_command(h.get("command", ""))
                                           for h in (b.get("hooks") or [])
                                           if isinstance(h, dict)):
                changed = True
                continue
            kept.append(b)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
            changed = changed or event in ("Stop", "PreToolUse", "PermissionRequest")
    if not changed:
        return False
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _json_hooks_registered(settings_path: Path) -> bool:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hooks = settings.get("hooks") if isinstance(settings, dict) else {}
    if not isinstance(hooks, dict):
        return False
    for raw in hooks.values():
        blocks = raw if isinstance(raw, list) else [raw]
        for b in blocks:
            if isinstance(b, dict) and any(_is_own_command(h.get("command", ""))
                                           for h in (b.get("hooks") or [])
                                           if isinstance(h, dict)):
                return True
    return False


# ---------------- ZCode（hooks.events + enabled） ----------------
# ZCode 的事件挂在 hooks.events.<Event> 下，比 Claude/Qoder/WorkBuddy 的
# hooks.<Event> 多一层，因此状态检测与卸载都不能复用上面那套平铺实现——
# 否则永远匹配不到命令（表现为「点注册没反应」，且每次点击都重复追加）。

def _zcode_config_path() -> Path:
    return _settings_home() / ".zcode" / "cli" / "config.json"


def _zcode_read(path: Path) -> dict:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def _zcode_own_commands(config: dict) -> list[tuple[str, list]]:
    """列出 hooks.events 下所有属于 a4agent 的事件块，返回 [(event, blocks)]。"""
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return []
    events = hooks.get("events")
    if not isinstance(events, dict):
        return []
    found = []
    for event, raw in events.items():
        blocks = raw if isinstance(raw, list) else [raw]
        if any(_is_own_command(h.get("command", ""))
               for b in blocks if isinstance(b, dict)
               for h in (b.get("hooks") or []) if isinstance(h, dict)):
            found.append((event, blocks))
    return found


def register_zcode(hook_command: str) -> bool:
    path = _zcode_config_path()
    config = _zcode_read(path)
    hooks = config.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    # 记录 enabled 变化：Hook 已配但 runner 被禁用时仍需写入（避免误判无需保存）
    changed = hooks.get("enabled") is not True
    hooks["enabled"] = True
    events = hooks.get("events")
    events = events if isinstance(events, dict) else {}

    for event, blocks in _EVENT_BLOCKS.items():
        raw = events.get(event)
        event_list = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
        # 先剔除本工具的历史残留，再按新命令重建，保证同一事件只有一条
        # a4agent 规则（重复点「注册」收敛而非追加）
        kept = [b for b in event_list
                if not (isinstance(b, dict)
                        and any(_is_own_command(h.get("command", ""))
                                for h in (b.get("hooks") or []) if isinstance(h, dict)))]
        new_list = kept + _blocks_with_command(blocks, hook_command)
        if new_list != event_list:
            changed = True
        if new_list:
            events[event] = new_list
        else:
            events.pop(event, None)
    hooks["events"] = events
    config["hooks"] = hooks
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def unregister_zcode() -> bool:
    path = _zcode_config_path()
    config = _zcode_read(path)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("events"), dict):
        return False
    events = hooks["events"]
    changed = False
    for event, blocks in _zcode_own_commands(config):
        kept = [b for b in blocks
                if not (isinstance(b, dict)
                        and any(_is_own_command(h.get("command", ""))
                                for h in (b.get("hooks") or []) if isinstance(h, dict)))]
        if len(kept) == len(blocks):
            continue
        changed = True
        if kept:
            events[event] = kept
        else:
            events.pop(event, None)
    if not changed:
        return False
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _zcode_registered() -> bool:
    return bool(_zcode_own_commands(_zcode_read(_zcode_config_path())))


# ---------------- Codex（config.toml 文本追加，marker 块） ----------------

def _codex_config_path() -> Path:
    return _settings_home() / ".codex" / "config.toml"


def _codex_defs(hook_command: str) -> str:
    # TOML 用单引号字面量字符串：Windows 路径的反斜杠无需转义
    cmd = hook_command.replace("'", "''")
    return f"""[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = '{cmd}'

[[hooks.PreToolUse]]
matcher = "request_user_input"
[[hooks.PreToolUse.hooks]]
type = "command"
command = '{cmd}'

[[hooks.PermissionRequest]]
[[hooks.PermissionRequest.hooks]]
type = "command"
command = '{cmd}'"""


def _codex_marker_block(content: str) -> "str | None":
    """截出已有的 marker 块（含首尾标记行），没有则返回 None。"""
    start = content.find(CODEX_MARKER_START)
    end = content.find(CODEX_MARKER_END)
    if start == -1 or end == -1 or end < start:
        return None
    return content[start:end + len(CODEX_MARKER_END)]


def register_codex(hook_command: str) -> bool:
    path = _codex_config_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        content = ""

    existing = _codex_marker_block(content)
    if existing is not None:
        # 块在但命令可能已过期（如开发态补上 desktop.py 路径、python 换版本），
        # 逐字比对而非"见到 marker 就跳过"，否则旧命令会永久留在宿主里。
        want = f"{CODEX_MARKER_START}\n{_codex_defs(hook_command)}\n{CODEX_MARKER_END}"
        if existing == want:
            return False
        content = content.replace(existing, want, 1)
        path.write_text(content, encoding="utf-8")
        return True
    if re.search(r"a4agent.*\bhook\b", content):
        return False  # 已注册（含 Codex 规范化后的内联格式）

    defs = f"{CODEX_MARKER_START}\n{_codex_defs(hook_command)}\n{CODEX_MARKER_END}"
    features_match = re.search(r"^\[features\]\s*$", content, re.MULTILINE)
    if features_match:
        # 已有 [features] 表：仅当表内尚无 hooks 键时插入，避免重复键
        rest = content[features_match.start():]
        next_section = rest.find("\n[")
        table_body = rest if next_section == -1 else rest[:next_section]
        if not re.search(r"^\s*hooks\s*=", table_body, re.MULTILINE):
            insert_at = features_match.start() + len("[features]")
            content = content[:insert_at] + "\nhooks = true" + content[insert_at:]
        content = content.rstrip() + "\n\n" + defs + "\n"
    else:
        block = f"{CODEX_MARKER_START}\n[features]\nhooks = true\n\n{_codex_defs(hook_command)}\n{CODEX_MARKER_END}"
        content = (content.rstrip() + "\n\n" if content.strip() else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _strip_own_hook_blocks(content: str) -> str:
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\[\[hooks\.", line):
            block = [line]
            j = i + 1
            while j < len(lines) and lines[j].strip() != "":
                block.append(lines[j])
                j += 1
            if any("a4agent" in b and "hook" in b for b in block):
                while j < len(lines) and lines[j].strip() == "":
                    j += 1  # a4agent 的块：整块丢弃并吞掉尾随空行
                i = j
                continue
            out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def unregister_codex() -> bool:
    path = _codex_config_path()
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    original = content
    start = content.find(CODEX_MARKER_START)
    end = content.find(CODEX_MARKER_END)
    if start != -1 and end != -1:
        content = content[:start] + content[end + len(CODEX_MARKER_END):]
    content = _strip_own_hook_blocks(content)
    content = re.sub(r"\n{3,}", "\n\n", content).rstrip()
    if content == original.rstrip():
        return False
    path.write_text(content + "\n", encoding="utf-8")
    return True


def _codex_registered() -> bool:
    try:
        content = _codex_config_path().read_text(encoding="utf-8")
    except OSError:
        return False
    return CODEX_MARKER_START in content or bool(re.search(r"a4agent.*\bhook\b", content))


# ---------------- 引擎注册表 ----------------

def _engine_paths() -> dict:
    home = _settings_home()
    return {
        "claude": home / ".claude" / "settings.json",
        "qoder": home / ".qoder" / "settings.json",
        "workbuddy": home / ".workbuddy" / "settings.json",
    }


def register_engine(engine: str, hook_command: str | None = None) -> dict:
    """注册单引擎 hook；返回 {registered, detail}。"""
    if engine not in ENGINES:
        return {"registered": False, "detail": f"未知引擎：{engine}"}
    if engine == "codex":
        changed = register_codex(hook_command or default_hook_command("codex"))
        return {"registered": True, "changed": changed,
                "detail": "已注册" if changed else "此前已注册"}
    if engine == "zcode":
        changed = register_zcode(hook_command or default_hook_command("zcode"))
        return {"registered": True, "changed": changed,
                "detail": "已注册" if changed else "此前已注册"}
    path = _engine_paths()[engine]
    changed = register_json_hooks(path, hook_command or default_hook_command(
        None if engine == "claude" else engine))
    return {"registered": True, "changed": changed,
            "detail": "已注册" if changed else "此前已注册"}


def unregister_engine(engine: str) -> dict:
    if engine not in ENGINES:
        return {"unregistered": False, "detail": f"未知引擎：{engine}"}
    if engine == "codex":
        return {"unregistered": unregister_codex()}
    if engine == "zcode":
        return {"unregistered": unregister_zcode()}
    return {"unregistered": unregister_json_hooks(_engine_paths()[engine])}


def registration_status() -> dict:
    """五端注册状态 + 配置文件路径，供 GUI 展示。"""
    paths = _engine_paths()
    return {
        "claude": {"registered": _json_hooks_registered(paths["claude"]),
                   "path": str(paths["claude"])},
        "qoder": {"registered": _json_hooks_registered(paths["qoder"]),
                  "path": str(paths["qoder"])},
        "workbuddy": {"registered": _json_hooks_registered(paths["workbuddy"]),
                      "path": str(paths["workbuddy"])},
        "zcode": {"registered": _zcode_registered(), "path": str(_zcode_config_path())},
        "codex": {"registered": _codex_registered(), "path": str(_codex_config_path())},
    }
