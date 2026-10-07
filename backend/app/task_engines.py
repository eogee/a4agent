"""无头引擎适配层：探测、命令矩阵与产出解析。

覆盖六个支持无头调用的目标（v0.6.0 起从P0 的 pi/dsh 扩展到全量）：

| 端| 命令 | 产出契约 |
|---|---|---|
| claude | `-p --output-format json` | JSON 单对象，产出在 `result` |
| codex  | `exec --json --ephemeral` | JSONL，取 `item.completed` 里 `type: agent_message` |
| zcode  | `-p --json --mode yolo` | JSON 单对象，产出在 `text` |
| qoder  | `qodercli -p -o json` | JSON 单对象 |
| dsh    | `--profile headless` | 纯文本一行 |
| pi     | `-p --mode json --no-session` | JSONL，取 `message_end` |

**配置来源一律为用户自己在应用内配好的配置**，本工具不再写入这些应用
的API 配置（dsh / ZCode / pi 自带完整供应商界面，外部代管反而易错）。
因此预检只做「能不能真跑」的真实连通验证，不比对任何托管条目。

每个引擎的可用性完全由用户自己的配置决定：本工具只负责把「能不能用」
这件事测出来并说清楚，不负责替用户配置。
"""
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENGINES = ("claude", "codex", "zcode", "qoder", "dsh", "pi")

ENGINE_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "zcode": "ZCode",
    "qoder": "Qoder",
    "dsh": "dsh",
    "pi": "pi",
}

# 各端去界面的配置位置，预检第二步据此判断「用户是否已配好」。
# 只收录明文可读的配置文件；读不到的（如 Qoder 的加密模型目录）返回 None，
# 由预检如实说明「无法校验具体服务商」，而不是假装通过。
CONFIG_HINTS = {
    "claude": "设置 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN",
    "codex": "在 ~/.codex/config.toml 配置 model_provider",
    "zcode": "设置 → 模型供应商 → 添加供应商",
    "qoder": "设置 → 模型 → 添加模型（需登录或 PAT）",
    "dsh": "Settings → Models → Add a custom provider",
    "pi": "在 ~/.pi/agent/models.json 配置 provider",
}

# npm 全局壳在 Windows 上是 .cmd，argv 直接给命令名会FileNotFoundError，
# 因此一律先 shutil.which() 解析绝对路径（解析不到即视为未安装）。
# Qoder 的命令名官方文档写 qodercli、博客写 qoder，两者都探测。
_SHIM = {
    "claude": ("claude",),
    "codex": ("codex",),
    "zcode": ("zcode",),
    "qoder": ("qodercli", "qoder"),
    "dsh": ("dsh",),
    "pi": ("pi",),
}
_VERSION_ARGS = {"claude": ("--version",), "codex": ("--version",),
                 "zcode": ("--version",), "qoder": ("--version",),
                 "dsh": ("-V",), "pi": ("--version",)}
# pi 的 stopReason 到人话的映射：这两类都算任务失败
_PI_STOP_MESSAGES = {
    "error": "引擎执行出错（见产出）",
    "aborted": "任务被中断（见产出）",
}
# 无头模式下无人值守，必须预授权工具，否则引擎会卡在审批或直接拒绝。
# 这三个都有官方文档承诺的 yolo / bypass 开关。
_YOLO_FLAG = {
    "claude": ["--permission-mode", "bypassPermissions"],
    "codex": ["--sandbox", "danger-full-access"],
    "zcode": ["--mode", "yolo"],
    "qoder": ["--yolo"],
    "dsh": [],
    "pi": ["--yolo"],
}

_PROBE_CACHE_SECONDS = 300


def resolve_command(tool: str) -> str | None:
    """引擎可执行文件的绝对路径；未安装返回 None。"""
    names = _SHIM.get(tool)
    if names is None:
        raise ValueError(f"未知的无头引擎：{tool}")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


_probe_cache: dict = {}


def _pick_error_line(lines: list) -> str:
    """严格挑选「明确是错误」的一行，用于判定崩溃与否。

    优先取 `Cannot find module` / `Error:` 这类说明性行——`node:internal/...` 只是
    栈头，对用户没有信息量。没有特征词就返回空串（不能拿首行凑数：探测健康的
    引擎时，首行是版本号）。
    """
    for marker in ("Cannot find module", "Error:", "error:"):
        for text in lines:
            if marker in text:
                return text[:200]
    for text in lines:
        if text.startswith("node:") or text.startswith("throw "):
            return text[:200]
    return ""


def _looks_like_error_line(text: str) -> bool:
    """裸错误行识别：常见于引擎把HTTP 错误直接打进stderr 的情形。

    只认这些强信号（HTTP 状态码 / 认证失败 / 模型不存在），不做泛化关键词匹配——
    否则「0 errors」「no error」这类正常产出会被误当成失败原因。
    """
    lowered = text.lower()
    if any(sig in lowered for sig in (
        "unauthorized", "forbidden", "not found", "invalid",
        "timeout", "timed out", "rate limit", "quota",
        "api key", "authentication", "permission denied",
    )):
        return True
    # HTTP xxx / status=xxx / 401 这类形式
    digits = text.split()
    return any(t.isdigit() and 400 <= int(t) < 600 for t in digits)


def _failure_summary(lines: list) -> str:
    """失败摘要用：先找明确错误行，找不到就回退首行非空文本。

    dsh 的失败是一行 `dsh: AUTH: upstream HTTP 403: …`，没有任何英文特征词，
    只报「退出码 1」等于没说原因。
    """
    return _pick_error_line(lines) or next(
        (t for t in lines if _looks_like_error_line(t)), ""
    ) or (lines[0][:200] if lines else "")


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


def build_argv(tool: str, prompt: str, working_dir: str | None = None) -> list:
    """无头执行命令矩阵。

    每条命令都满足三个硬性要求：结构化输出（便于解析产出）、一次性会话
    （不留状态）、权限预授权（无人在场审批，不预授权会卡死或被拒）。
    """
    command = resolve_command(tool)
    if command is None:
        names = "/".join(_SHIM.get(tool, (tool,)))
        raise ValueError(
            f"未探测到 {ENGINE_LABELS.get(tool, tool)} 命令（试过 {names}），"
            "请确认已安装并在 PATH 中"
        )
    argv = [command]

    if tool == "claude":
        # --output-format json 返回单个 JSON 对象，产出在 result 字段；
        # --permission-mode bypassPermissions 让工具调用免审批（无人值守前提）
        argv += ["-p", prompt, "--output-format", "json"]
    elif tool == "codex":
        # exec 是官方非交互入口：进度走 stderr，只有最终消息进 stdout；
        # --json 给 JSONL 事件流；--ephemeral 不落盘会话
        argv += ["exec", "--json", "--ephemeral"]
        if working_dir:
            argv += ["-C", working_dir]
        argv += list(_YOLO_FLAG[tool])
        argv.append(prompt)
        return argv
    elif tool == "zcode":
        # -p 一次性调用，--json 给结构化输出（text / toolCalls / usage / stopReason）；
        # --mode yolo 免审批（无头模式下的默认要求）
        argv += ["-p", prompt, "--json"]
    elif tool == "qoder":
        # qodercli -p 打印即退出，-o json 给结构化输出
        argv += ["-p", prompt, "-o", "json"]
        if working_dir:
            argv += ["-w", working_dir]
    elif tool == "dsh":
        # headless profile：答完一个任务打印结果即退出（dsh --help 官方示例）
        argv += ["--profile", DSH_HEADLESS_PROFILE]
        overlay = _dsh_headless_overlay()
        if overlay is not None:
            argv += ["--patch", str(overlay)]
        argv.append(prompt)
        return argv
    elif tool == "pi":
        # --no-session：一次性任务不落会话；JSONL 事件流便于进度与用量解析
        argv += ["-p", prompt, "--mode", "json", "--no-session"]

    argv += list(_YOLO_FLAG[tool])
    if working_dir and tool == "claude":
        # claude 没有 -C，用 --add-dir 授权工作目录（bypassPermissions 下的必要条件）
        argv += ["--add-dir", working_dir]
    return argv


def parse_output(tool: str, stdout: str, exit_code: int) -> dict:
    """把引擎产出归一为 {ok, text, error, usage}。"""
    if tool == "claude":
        return _parse_json_object(stdout, exit_code, ("result", "text"),
                                  usage_from=_usage_claude)
    if tool == "qoder":
        return _parse_json_object(stdout, exit_code, ("result", "response", "text", "output"))
    if tool == "zcode":
        return _parse_json_object(stdout, exit_code, ("text", "response", "result"),
                                  usage_from=_usage_zcode, stop_key="stopReason")
    if tool == "codex":
        return _parse_codex_jsonl(stdout, exit_code)
    if tool == "pi":
        return _parse_pi(stdout, exit_code)
    if tool == "dsh":
        return _parse_flat(stdout, exit_code)
    raise ValueError(f"未知的无头引擎：{tool}")


def _no_window_flags() -> int:
    """桌面应用绝不能弹黑窗（与 proxy_standalone 同一套做法）。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _empty_usage() -> dict:
    return {"input": 0, "output": 0, "totalTokens": 0, "cost": 0.0}


def _parse_json_object(
    stdout: str,
    exit_code: int,
    text_keys: tuple[str, ...],
    usage_from=None,
    stop_key: str | None = None,
) -> dict:
    """解析「单个 JSON 对象」型产出（claude / zcode / qoder）。

    三端的产出都是 stdout 上的一个 JSON 对象，只是文本字段名不同
    （claude 用 result，zcode 用 text，qoder 用 result/response）。统一按
    text_keys 顺序取第一个非空字符串，并把 is_error / stopReason 视为失败信号
    ——退出码为 0 但引擎自报失败的情况必须能识别出来。
    """
    raw = (stdout or "").strip()
    usage = _empty_usage()
    error = ""
    text = ""

    parsed = _load_json_safe(raw)
    if isinstance(parsed, dict):
        for key in text_keys:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if usage_from is not None:
            usage = usage_from(parsed)
        if parsed.get("is_error") is True:
            error = str(parsed.get("result") or parsed.get("error") or "引擎自报执行失败")
        elif stop_key and str(parsed.get(stop_key) or "") in ("error", "aborted"):
            error = str(parsed.get("errorMessage") or "引擎执行出错（见产出）")
    else:
        # 输出不是 JSON（如模型返回了裸文本或 CLI 打了别的日志）：按纯文本收
        text = raw

    if not error:
        if exit_code != 0:
            detail = _failure_summary([t.strip() for t in raw.splitlines() if t.strip()])
            error = f"引擎退出码 {exit_code}" + (f"：{detail}" if detail else "")
        elif not text:
            error = "引擎无文本产出"
    return {"ok": not error, "text": text, "error": error, "usage": usage}


def _load_json_safe(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _usage_claude(parsed: dict) -> dict:
    u = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    usage = _empty_usage()
    usage["input"] = int(u.get("input_tokens") or 0)
    usage["output"] = int(u.get("output_tokens") or 0)
    usage["totalTokens"] = usage["input"] + usage["output"]
    try:
        usage["cost"] = float(parsed.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        usage["cost"] = 0.0
    return usage


def _usage_zcode(parsed: dict) -> dict:
    u = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    usage = _empty_usage()
    usage["input"] = int(u.get("inputTokens") or 0)
    usage["output"] = int(u.get("outputTokens") or 0)
    usage["totalTokens"] = int(u.get("totalTokens") or (usage["input"] + usage["output"]))
    return usage


def _parse_codex_jsonl(stdout: str, exit_code: int) -> dict:
    """解析 codex exec --json 的 JSONL 事件流。

    产出在 `{"type":"item.completed","item":{"type":"agent_message","text":...}}`
    事件里；进度类事件（thread.started / turn.started / command_execution）
    一律忽略。用量取最后一条 turn.completed 的 usage。
    """
    texts: list = []
    usage = _empty_usage()
    failed = ""

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        event = _load_json_safe(line)
        if not isinstance(event, dict):
            continue
        etype = str(event.get("type") or "")
        if etype == "item.completed":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    texts.append(text)
        elif etype == "turn.completed":
            u = event.get("usage") or {}
            if isinstance(u, dict):
                usage["input"] = int(u.get("input_tokens") or 0)
                usage["output"] = int(u.get("output_tokens") or 0)
                usage["totalTokens"] = usage["input"] + usage["output"]
        elif etype in ("turn.failed", "error"):
            failed = failed or str(event.get("message") or event.get("error") or "引擎执行失败")

    text = "\n\n".join(texts).strip()
    error = failed
    if not error:
        if exit_code != 0:
            detail = _failure_summary([t.strip() for t in (stdout or "").splitlines() if t.strip()])
            error = f"引擎退出码 {exit_code}" + (f"：{detail}" if detail else "")
        elif not text:
            error = "引擎无文本产出"
    return {"ok": not error, "text": text, "error": error, "usage": usage}


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
    if stop in ("error", "aborted") and last:
        friendly = _PI_STOP_MESSAGES.get(stop, "引擎执行出错（见产出）")
        error = str(last.get("errorMessage") or friendly)
    elif text:
        pass  # 有产出且无错误信号，交给下面按退出码兜底
    elif exit_code != 0:
        # 退出码非 0 但没解析到结构化事件（引擎崩在启动阶段、或错误只打在
        # stderr）：从原始输出捞最能说明问题的一行，否则用户只看到「退出码 1」
        detail = _failure_summary([t.strip() for t in (stdout or "").splitlines() if t.strip()])
        error = f"引擎退出码 {exit_code}" + (f"：{detail}" if detail else "")
    elif not last:
        error = "引擎无文本产出"
    elif not text:
        error = "引擎无文本产出"
    if exit_code != 0 and not error:
        detail = _failure_summary([t.strip() for t in (stdout or "").splitlines() if t.strip()])
        error = f"引擎退出码 {exit_code}" + (f"：{detail}" if detail else "")

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
