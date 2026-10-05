"""无头引擎适配层：探测、命令矩阵与产出解析。

P0 只接 pi 与 dsh（计划 §4.2）：pi 的无头契约最完整（`-p` + `--mode json` 的
JSONL 事件流），dsh 的 `--profile headless` 为官方示例确认的一次性任务入口。
zcode / claude / codex 待 P1 确认 CLI 入口后再进这张表。
"""
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENGINES = ("pi", "dsh")

ENGINE_LABELS = {"pi": "pi", "dsh": "dsh"}

# npm 全局壳在 Windows 上是 .cmd，argv 直接给命令名会 FileNotFoundError，
# 因此一律先 shutil.which() 解析绝对路径（解析不到即视为未安装）。
_SHIM = {"pi": "pi", "dsh": "dsh"}
_VERSION_ARGS = {"pi": ("--version",), "dsh": ("-V",)}
# pi 的 stopReason 到人话的映射：这两类都算任务失败
_PI_STOP_MESSAGES = {
    "error": "引擎执行出错（见产出）",
    "aborted": "任务被中断（见产出）",
}

_PROBE_CACHE_SECONDS = 300


def resolve_command(tool: str) -> str | None:
    """引擎可执行文件的绝对路径；未安装返回 None。"""
    if tool not in _SHIM:
        raise ValueError(f"未知的无头引擎：{tool}")
    return shutil.which(_SHIM[tool])


_probe_cache: dict = {}


def _pick_error_line(lines: list) -> str:
    """严格挑选「明确是错误」的一行，用于判定崩溃与否。

    优先取 `Cannot find module` / `Error:` 这类说明性行——`node:internal/...` 只是
    栈头，对用户没有信息量。没有特征词就返回空串（不能拿首行凑数：探测健康的
    引擎时，首行是版本号）。
    """
    for marker in ("Cannot find module", "Error:"):
        for text in lines:
            if marker in text:
                return text[:200]
    for text in lines:
        if text.startswith("node:") or text.startswith("throw "):
            return text[:200]
    return ""


def _failure_summary(lines: list) -> str:
    """失败摘要用：先找明确错误行，找不到就回退首行非空文本。

    dsh 的失败是一行 `dsh: AUTH: upstream HTTP 403: …`，没有任何英文特征词，
    只报「退出码 1」等于没说原因。
    """
    return _pick_error_line(lines) or (lines[0][:200] if lines else "")


def _crash_line(done) -> str:
    """认出 node 崩溃横幅：npm 壳指向不存在的文件时常以退出码 0 + 这类输出收场。"""
    lines = [
        text.strip()
        for stream in (done.stderr, done.stdout)
        for text in (stream or "").strip().splitlines()
        if text.strip()
    ]
    return _pick_error_line(lines)


def probe(tool: str, refresh: bool = False) -> dict:
    """探测单个引擎：安装状态、路径与版本（结果缓存 5 分钟）。"""
    now = time.time()
    cached = _probe_cache.get(tool)
    if cached and not refresh and now - cached[0] < _PROBE_CACHE_SECONDS:
        return cached[1]

    command = resolve_command(tool)
    info = {
        "tool": tool,
        "label": ENGINE_LABELS[tool],
        "installed": command is not None,
        "command": command,
        "version": "",
        "error": "",
    }
    if command is None:
        info["error"] = f"未探测到 {ENGINE_LABELS[tool]} 命令，请确认已安装并在 PATH 中"
    else:
        try:
            done = subprocess.run(
                [command, *_VERSION_ARGS[tool]],
                capture_output=True,
                text=True,
                timeout=20,
                encoding="utf-8",
                errors="replace",
                creationflags=_no_window_flags(),
            )
            lines = (done.stdout or "").strip().splitlines()
            info["version"] = lines[0].strip()[:80] if lines else ""
            crash = _crash_line(done)
            if done.returncode != 0 or crash:
                info["installed"] = False
                # npm 壳可能指向不存在的文件而仍以 0 退出并打印 node 崩溃栈
                # （本机 pi.cmd 就是这种坏状态），只报退出码会让人无从下手
                detail = crash or (lines[0].strip() if lines else f"退出码 {done.returncode}")
                info["error"] = f"{ENGINE_LABELS[tool]} 无法执行：{detail[:160]}"
        except (OSError, subprocess.SubprocessError) as e:
            info["installed"] = False
            info["error"] = f"{ENGINE_LABELS[tool]} 无法执行：{e}"

    _probe_cache[tool] = (now, info)
    return info


def probe_all(refresh: bool = False) -> list:
    """全部 P0 引擎的探测结果，供前端下拉与预检复用。"""
    return [probe(tool, refresh=refresh) for tool in ENGINES]


DSH_HEADLESS_PROFILE = "headless"


def dsh_profile_patch_path() -> Path:
    """headless profile 自己的补丁层文件（外部工具会往这里插 hook）。"""
    from . import config_manager

    return config_manager.dsh_home() / "profiles" / DSH_HEADLESS_PROFILE / "cordis.patch.yml"


def foreign_plugin_ids(profile_patch_path: Path | None = None) -> list:
    """profile 里以相对路径插入的外部插件 id 列表。

    dsh 自带插件按包名解析，只有外部工具（如 a4phone 的 dsh-hook）才会写成
    `../../...`。这类 hook 常依赖 host 侧服务（`workspaceRegistry` 等），而
    headless profile 不提供，dsh 会在启动阶段直接判定
    「plugin tree failed to load / entry did not activate」并退出 1。
    """
    import yaml

    path = profile_patch_path or dsh_profile_patch_path()
    if not path.exists():
        return []
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return []
    ids = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        for item in entry.get("insert") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if name.startswith("./") or name.startswith("../") or "/../" in name or "..\\" in name:
                pid = str(item.get("id") or "").strip()
                if pid:
                    ids.append(pid)
    return ids


def write_plugin_suppress_overlay(ids: list, dest: Path) -> Path:
    """生成 `--patch` 覆盖层：只禁用上面那批外部插件，不碰 profile 文件本身。"""
    import yaml

    dest.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(
        [{"id": pid, "disabled": True} for pid in ids],
        allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    header = (
        "# a4agent 自动生成：无头任务下发 dsh 时屏蔽外部插件 hook\n"
        "# （headless profile 缺少它们依赖的 host 服务，不屏蔽则 dsh 启动即失败）\n"
        "# 手改无效：每次下发前会重新生成。要恢复请在 profile 的 cordis.patch.yml 里调整。\n"
    )
    dest.write_text(header + body, encoding="utf-8")
    return dest


def _dsh_headless_overlay() -> Path | None:
    """有需要屏蔽的外部插件时返回覆盖层路径，否则 None。"""
    ids = foreign_plugin_ids()
    if not ids:
        return None
    from .database import get_data_dir

    logger.warning("dsh headless：屏蔽 profile 中的外部插件 %s（缺 host 服务会导致启动失败）", ids)
    return write_plugin_suppress_overlay(ids, get_data_dir() / "task_runtime" / "dsh-headless-suppress.yml")


def build_argv(tool: str, prompt: str) -> list:
    """无头执行命令（计划 §4.2 命令矩阵）。"""
    command = resolve_command(tool)
    if command is None:
        raise ValueError(f"未探测到 {ENGINE_LABELS.get(tool, tool)} 命令，无法下发任务")
    if tool == "pi":
        # --no-session：一次性任务不落会话；JSONL 事件流便于进度与用量解析
        return [command, "-p", prompt, "--mode", "json", "--no-session"]
    if tool == "dsh":
        # headless profile：答完一个任务打印结果即退出（dsh --help 官方示例）
        argv = [command, "--profile", DSH_HEADLESS_PROFILE]
        overlay = _dsh_headless_overlay()
        if overlay is not None:
            argv += ["--patch", str(overlay)]
        return argv + [prompt]
    raise ValueError(f"未知的无头引擎：{tool}")


def parse_output(tool: str, stdout: str, exit_code: int) -> dict:
    """把引擎产出归一为 {ok, text, error, usage}。"""
    if tool == "pi":
        return _parse_pi(stdout, exit_code)
    if tool == "dsh":
        return _parse_flat(stdout, exit_code)
    raise ValueError(f"未知的无头引擎：{tool}")


def _no_window_flags() -> int:
    """桌面应用绝不能弹黑窗（与 proxy_standalone 同一套做法）。"""
    import os
    import subprocess as sp

    if os.name != "nt":
        return 0
    return getattr(sp, "CREATE_NO_WINDOW", 0)


def _parse_pi(stdout: str, exit_code: int) -> dict:
    """pi 的 JSONL：取 assistant 文本为产出，成败以「最后一条 assistant 消息」为准。

    对齐 pi 自己的判定（print-mode.ts：stopReason ∈ error/aborted 即失败并 exit 1）。
    两条坑：
    - pi 自带 auto_retry（实测 3 次、退避递增），失败信号只出现在事件流末尾，
      json 模式下进程退出码可能仍是 0，所以不能只看 exit_code；
    - 重试环里每条消息都带 errorMessage，某次成功后不会再清空——只有最后一条
      assistant 消息的错误才算数，否则成功的任务会被误判为失败。
    """
    texts: list = []
    last: dict = {}
    usage = {"input": 0, "output": 0, "totalTokens": 0, "cost": 0.0}

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") != "message_end":
            continue
        message = event.get("message") or {}
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for part in message.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    texts.append(text)
        u = message.get("usage") or {}
        usage["input"] += int(u.get("input") or 0)
        usage["output"] += int(u.get("output") or 0)
        usage["totalTokens"] += int(u.get("totalTokens") or 0)
        cost = u.get("cost") or {}
        usage["cost"] += float(cost.get("total") or 0.0)
        last = message

    text = "\n\n".join(texts).strip()
    stop = str(last.get("stopReason") or "")
    error = ""
    if not last:
        error = "引擎无文本产出" if exit_code == 0 else f"引擎退出码 {exit_code}"
    elif stop in ("error", "aborted"):
        friendly = _PI_STOP_MESSAGES.get(stop, "引擎执行出错（见产出）")
        error = str(last.get("errorMessage") or friendly)
    elif not text:
        error = "引擎无文本产出"
    if exit_code != 0 and not error:
        error = f"引擎退出码 {exit_code}"

    return {"ok": not error, "text": text, "error": error, "usage": usage}


def _parse_flat(stdout: str, exit_code: int) -> dict:
    """DSH 等直出型引擎：整体 JSON 则抽字段，否则按纯文本收。"""
    raw = (stdout or "").strip()
    text = raw
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("response", "result", "text", "content", "output"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
    ok = exit_code == 0 and bool(text)
    error = ""
    if not ok:
        if exit_code == 0:
            error = "引擎无产出"
        else:
            # 只报「退出码 1」等于什么都没说：崩溃原因就在同一次输出里
            # （stderr 已合并进产出文件），取其中最能说明问题的一行进摘要
            detail = _failure_summary([t.strip() for t in raw.splitlines() if t.strip()])
            error = f"引擎退出码 {exit_code}" + (f"：{detail}" if detail else "")
    return {"ok": ok, "text": text, "error": error, "usage": {}}
