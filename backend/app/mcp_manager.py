"""MCP 六端（Claude Code / Codex / dsh / zcode / pi / Qoder）server 发现、迁移、回收站。

与 skill_manager 同构的管理骨架，差异在 MCP 配置是「配置文件内的子结构」
而非独立目录，因此：

- 每端一个 adapter：claude 读写 ~/.claude.json 顶层 mcpServers 与项目
  .mcp.json；codex 读写 config.toml 的 [mcp_servers.*] 子表；dsh 读写
  profiles/<profile>/cordis.patch.yml 中 @deepseek-ai/dsh-mcp-client 插件的
  insert 条目；zcode 读写 ~/.zcode/cli/config.json 与 <repo>/.zcode/config.json
  的嵌套 mcp.servers（官方 schema 严格，只写规范键）；pi 读写
  ~/.pi/agent/mcp.json 与 <repo>/.pi/mcp.json 顶层 mcpServers（与 claude
  同构，额外有 exposure/toolExposure/enabled/timeout/auth 等自有键，需原样
  保留）；Qoder 读写 ~/.qoder/mcp.json 顶层 mcpServers（与 claude 同构，
  额外有 disabled/timeout/authType 等自有键，需原样保留），项目级不托管——
  Qoder 的项目级 MCP 与 Claude Code 共用同一个 <repo>/.mcp.json，由 claude
  端一次写入即两端同时生效。
- 归一 schema（迁移中枢）：{ name, transport, command, args, env, url,
  headers, cwd, ... }，transport ∈ {stdio, sse, http}。
- 传输能力矩阵：claude 支持 stdio/sse/http；codex 仅 stdio；dsh 支持
  stdio 与 streamable-http（归一 http）；zcode 支持 stdio/sse/http；
  pi 支持 stdio 与 streamable-http，且明确拒绝 sse；
  Qoder 支持 stdio/sse/http（streamable-http 归一为 http）。
  不可转换的组合整对失败并留日志，不静默降级。
- 安全：discover / content 响应中 env / headers 一律脱敏（只回键名）；
  迁移时从源配置文件直读明文写目标文件，API 永不回传明文；回收站快照中
  env / headers 用 DPAPI 加密落盘，恢复时解密。
- 写入前自动备份目标配置文件（滚动保留最近 5 份），dsh cordis.patch.yml
  重写会丢注释，以备份兜底可追溯。
"""
import json
import logging
import os
import shutil
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import tomli_w
import yaml

from . import config_manager, crypto
from .database import get_data_dir
from .skill_manager import load_project_roots, project_dirs

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

logger = logging.getLogger(__name__)

TOOLS = ("claude", "codex", "dsh", "zcode", "pi", "qoder")
TOOL_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "dsh": "dsh",
    "zcode": "ZCode",
    "pi": "pi",
    "qoder": "Qoder",
}
TRASH_DIR_NAME = "mcp_recycle"
TRASH_KEEP_DAYS = 30
BACKUP_KEEP = 5

DSH_MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
DSH_MCP_ENTRY_PREFIX = "mcp-"
# pi 的 server 名约束：仅字母数字与 _ -（含中文/点号的名字会被 pi 判为非法条目）
PI_SERVER_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)

# 传输能力矩阵：目标端可接收的 transport 集合
TRANSPORT_CAPABILITY = {
    "claude": {"stdio", "sse", "http"},
    "codex": {"stdio"},
    "dsh": {"stdio", "http"},  # dsh 的 http 写回为 streamable-http
    "zcode": {"stdio", "sse", "http"},  # 官方 schema 明确支持三种传输
    "pi": {"stdio", "http"},  # pi 明确拒绝 legacy SSE，只认 stdio / streamable HTTP
    "qoder": {"stdio", "sse", "http"},  # 与 claude 同构，三种传输都认
}

# dsh 项目级不支持（cordis 配置为全局 profile 层）；zcode 项目级支持
# （<repo>/.zcode/config.json → mcp.servers）；pi 项目级支持
# （<repo>/.pi/mcp.json，且仅在该项目被信任时生效）；Qoder 只托管全局
# ~/.qoder/mcp.json —— 它的「项目级」读的就是 Claude Code 的
# <repo>/.mcp.json，同一文件被两端各管一次会互相踩写，故项目级归 claude 端
DASH_SCOPE_CAPABILITY = {
    "claude": ("global", "project"),
    "codex": ("global", "project"),
    "dsh": ("global",),
    "zcode": ("global", "project"),
    "pi": ("global", "project"),
    "qoder": ("global",),
}

# ---------------- MCP 简介知识库 ----------------
# 卡片介绍的取数优先级：用户自定义 > 配置自带 description > 内置简介库
# （离线、即时）> npm registry 自动查询（npx 类 server，结果缓存一天）。
# 内置库覆盖常见 MCP server 的规范化名（server 名 / 包名尾段等）。

_NPM_QUERY_TIMEOUT_SECS = 2.5
_NPM_CACHE_FILENAME = "mcp_knowledge.json"
_NPM_CACHE_TTL_DAYS = 1
_PER_DISCOVER_NPM_LIMIT = 3  # 单次 discover 最多联网补查个数，防首次进入卡顿

_BUILTIN_MCP_DESCRIPTIONS = {
    "github": "GitHub 仓库、Issue、Pull Request 与代码审查管理",
    "github-mcp-server": "GitHub 仓库、Issue、Pull Request 与代码审查管理",
    "server-github": "GitHub 仓库、Issue、Pull Request 与代码审查管理",
    "gitlab": "GitLab 项目、Issue、Merge Request 与 CI 流水线管理",
    "git": "本地 Git 仓库操作：提交、分支、状态与日志查询",
    "playwright": "浏览器自动化：网页导航、截图、DOM 交互与断言",
    "puppeteer": "Chrome/Chromium 浏览器自动化：页面控制、截图与抓取",
    "chrome-devtools": "Chrome DevTools 协议：浏览器调试、DOM/网络/性能检查",
    "browser": "浏览器自动化与页面交互工具",
    "open-websearch": "网络搜索集成：访问搜索引擎并返回实时结果",
    "web-search": "网络搜索：查询网页并返回结果",
    "search": "统一搜索：多引擎网页搜索工具",
    "brave-search": "Brave Search API：实时网页搜索",
    "duckduckgo": "DuckDuckGo 搜索与即时答",
    "firecrawl": "网页抓取与爬取：转 Markdown、结构提取、站点地图",
    "fetch": "抓取 URL 内容并转成便于读取的 Markdown",
    "http": "HTTP 请求工具：支持 GET/POST 等并返回结构化结果",
    "context7": "最新库文档检索：为依赖注入上下文",
    "sequential-thinking": "思维链推理：分步推进复杂问题分析",
    "memory": "知识持久化记忆：跨会话保存自定义条目",
    "server-memory": "知识持久化记忆：跨会话保存自定义条目",
    "filesystem": "本地文件系统访问：读、写、移动与搜索文件",
    "server-filesystem": "本地文件系统访问：读、写、移动与搜索文件",
    "postgres": "PostgreSQL 查询：只读 SQL 执行与 schema 浏览",
    "postgresql": "PostgreSQL 查询：只读 SQL 执行与 schema 浏览",
    "sqlite": "SQLite 数据库查询与执行 SQL",
    "mysql": "MySQL 数据库查询与 SQL 执行",
    "mongodb": "MongoDB 数据查询与操作",
    "redis": "Redis 键值操作：读写与命令执行",
    "slack": "Slack 消息收发：读频道、发消息、查用户",
    "notion": "Notion 页面与数据库管理",
    "google-maps": "Google Maps 查询：地点、路线与地理编码",
    "docker": "Docker 容器与镜像管理",
    "kubernetes": "Kubernetes 集群资源管理",
    "vscode": "VS Code 控制：打开文件、执行命令与编辑器交互",
    "desktop-commander": "桌面进程与命令行工具控制",
    "time": "当前时间与日期查询",
    "everything": "本地全盘文件搜索（Everything）",
    "sentinel": "基于 WebSearch/SearXNG 的搜索接口",
}


def _npm_pkg_from(command, args):
    """从 npx 类命令解析包名：command 为 npx/npx.cmd 时取 args 中第一个非选项词。"""
    if str(command or "").strip().lower() not in ("npx", "npx.cmd"):
        return None
    for a in (args or []):
        s = str(a)
        if s.startswith("-"):
            continue
        return s
    return None


def _kb_candidates(name, command, args):
    """生成简介查询候选键集合（server 名 / 命令 / 包名及其去前后缀变体）。"""
    cands: set[str] = set()

    def add(s):
        s = str(s or "").strip().lower()
        if s:
            cands.add(s)

    add(name)
    add(command)
    pkg = _npm_pkg_from(command, args)
    add(pkg)
    if pkg:
        add(pkg.rsplit("/", 1)[-1])
    for base in {str(name or "").strip().lower(), str(command or "").strip().lower(), str(pkg or "").strip().lower()}:
        if not base:
            continue
        for pref in ("mcp-", "mcp-server-", "server-", "mcp_"):
            if base.startswith(pref):
                add(base[len(pref):])
        for suf in ("-server", "_server", "-mcp"):
            if base.endswith(suf):
                add(base[: -len(suf)])
    return cands


def _npm_knowledge_cache() -> dict:
    path = get_data_dir() / _NPM_CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_npm_knowledge_cache(data: dict) -> None:
    path = get_data_dir() / _NPM_CACHE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def fetch_npm_description(pkg: str) -> str | None:
    """查询 npm registry 的包简介（缓存一天）；网络失败静默返回 None。"""
    cache = _npm_knowledge_cache()
    entry = cache.get(pkg)
    if isinstance(entry, dict) and entry.get("desc"):
        try:
            ts = datetime.fromisoformat(entry.get("ts", ""))
            if datetime.now() - ts < timedelta(days=_NPM_CACHE_TTL_DAYS):
                return entry["desc"]
        except (ValueError, TypeError):
            pass
    desc = None
    try:
        with urllib.request.urlopen(
            f"https://registry.npmjs.org/{pkg}", timeout=_NPM_QUERY_TIMEOUT_SECS
        ) as resp:
            meta = json.loads(resp.read(65536).decode("utf-8", "replace"))
        desc = str(meta.get("description") or "").strip() or None
    except Exception:
        desc = None
    if desc:
        cache[pkg] = {"desc": desc, "ts": datetime.now().isoformat()}
        _save_npm_knowledge_cache(cache)
    return desc


def auto_description(name, command, args, npm_budget=None) -> tuple:
    """自动获取 server 简介：内置库优先，其次 npm registry。

    返回 (text, source)，source ∈ {"builtin", "npm", ""}。
    npm_budget 为共享配额 dict（{"left": n}），控制单次 discover 联网补查数。
    """
    for cand in _kb_candidates(name, command, args):
        if cand in _BUILTIN_MCP_DESCRIPTIONS:
            return _BUILTIN_MCP_DESCRIPTIONS[cand], "builtin"
    pkg = _npm_pkg_from(command, args)
    if pkg and (npm_budget is None or npm_budget.get("left", 0) > 0):
        if isinstance(npm_budget, dict) and npm_budget.get("left", 0) > 0:
            npm_budget["left"] -= 1
        desc = fetch_npm_description(pkg)
        if desc:
            return desc, "npm"
    return "", ""

MASK = "••••••"


# ---------------- 路径解析 ----------------


def claude_mcp_path() -> Path:
    """Claude Code 用户级 MCP 配置：~/.claude.json 顶层 mcpServers。"""
    override = os.environ.get("A4AGENT_CLAUDE_MCP_PATH")
    if override:
        return Path(override)
    return Path.home() / ".claude.json"


def claude_project_mcp_path(project_root: Path) -> Path:
    """Claude Code 项目级 MCP 配置：<项目>/.mcp.json。"""
    return project_root / ".mcp.json"


def codex_mcp_path() -> Path:
    """Codex 全局配置：~/.codex/config.toml（复用 config_manager 的覆盖变量）。"""
    return config_manager.codex_settings_path()


def codex_project_mcp_path(project_root: Path) -> Path:
    """Codex 项目级配置：<项目>/.codex/config.toml。"""
    return project_root / ".codex" / "config.toml"


def dsh_mcp_profile() -> str:
    return os.environ.get("A4AGENT_DSH_MCP_PROFILE") or "web"


def dsh_mcp_patch_path(profile: str | None = None) -> Path:
    """dsh 的 MCP server 挂在 profile 层 cordis.patch.yml 的 insert 条目里。"""
    override = os.environ.get("A4AGENT_DSH_MCP_PATCH_PATH")
    if override:
        return Path(override)
    return config_manager.dsh_home() / "profiles" / (profile or dsh_mcp_profile()) / "cordis.patch.yml"


def zcode_mcp_path() -> Path:
    """zcode 用户级 MCP 配置：~/.zcode/cli/config.json 的嵌套 mcp.servers。

    复用 config_manager 的 A4AGENT_ZCODE_CLI_CONFIG_PATH 覆盖变量。
    """
    return config_manager.zcode_cli_config_path()


def zcode_project_mcp_path(project_root: Path) -> Path:
    """zcode 项目级 MCP 配置：<项目>/.zcode/config.json 的嵌套 mcp.servers。"""
    return project_root / ".zcode" / "config.json"


def pi_mcp_path() -> Path:
    """pi 用户级 MCP 配置：~/.pi/agent/mcp.json 顶层 mcpServers。

    复用 config_manager 的 A4AGENT_PI_AGENT_DIR / PI_CODING_AGENT_DIR 覆盖，
    另有 A4AGENT_PI_MCP_PATH 可单独指向别处（测试与非标准安装用）。
    """
    override = os.environ.get("A4AGENT_PI_MCP_PATH")
    if override:
        return Path(override)
    return config_manager.pi_agent_dir() / "mcp.json"


def pi_project_mcp_path(project_root: Path) -> Path:
    """pi 项目级 MCP 配置：<项目>/.pi/mcp.json（项目被信任时生效）。"""
    return project_root / ".pi" / "mcp.json"


def qoder_mcp_path() -> Path:
    """Qoder 用户级 MCP 配置：~/.qoder/mcp.json 顶层 mcpServers。

    复用 config_manager 的 A4AGENT_QODER_HOME 目录探测，另有
    A4AGENT_QODER_MCP_PATH 可单独指向别处（测试用）。
    """
    override = os.environ.get("A4AGENT_QODER_MCP_PATH")
    if override:
        return Path(override)
    return config_manager.qoder_home() / "mcp.json"


def mcp_config_file(scope: str, tool: str, project: str | None) -> Path:
    """(scope, tool, project) → 配置文件路径；未知组合抛 ValueError。"""
    if scope == "global":
        if tool == "claude":
            return claude_mcp_path()
        if tool == "codex":
            return codex_mcp_path()
        if tool == "dsh":
            return dsh_mcp_patch_path()
        if tool == "zcode":
            return zcode_mcp_path()
        if tool == "pi":
            return pi_mcp_path()
        if tool == "qoder":
            return qoder_mcp_path()
    if scope == "project":
        if not project:
            raise ValueError("项目级位置缺少项目名")
        root = None
        for proj in project_dirs():
            if proj["project"] == project:
                root = proj["root"]
                break
        if root is None:
            raise ValueError(f"未找到项目：{project}")
        if tool == "claude":
            return claude_project_mcp_path(root)
        if tool == "codex":
            return codex_project_mcp_path(root)
        if tool == "zcode":
            return zcode_project_mcp_path(root)
        if tool == "pi":
            return pi_project_mcp_path(root)
        raise ValueError(f"{TOOL_LABELS[tool]} 端不支持项目级 MCP 配置")
    raise ValueError(f"未知 scope：{scope}")


def sanitize_name(name: str) -> str:
    """dsh 条目 id / 文件名安全化：仅保留 [A-Za-z0-9_-]，空则兜底。"""
    s = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)
    return s.strip("-") or "mcp-server"


# ---------------- 归一化 / 渲染 ----------------

# 每条归一 server：{name, transport, command, args, env, url, headers, cwd,
#                   tool, scope, project, path, extra}


def normalize_claude(name: str, raw: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    typ = str(raw.get("type") or "stdio").lower()
    if typ not in ("stdio", "sse", "http"):
        typ = "stdio"
    server = {
        "name": name,
        "transport": typ,
        "command": raw.get("command"),
        "args": list(raw.get("args") or []),
        "env": dict(raw.get("env") or {}),
        "url": raw.get("url"),
        "headers": dict(raw.get("headers") or {}) if isinstance(raw.get("headers"), dict) else {},
        "cwd": None,
        "description": str(raw.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {},
    }
    return server


def normalize_codex(name: str, raw: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    extra_keys = ("command", "args", "env")
    server = {
        "name": name,
        "transport": "stdio",
        "command": raw.get("command"),
        "args": list(raw.get("args") or []),
        "env": dict(raw.get("env") or {}),
        "url": None,
        "headers": {},
        "cwd": None,
        "description": str(raw.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {k: v for k, v in raw.items() if k not in extra_keys},
    }
    return server


def normalize_dsh(entry: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    config = entry.get("config") or {}
    if not isinstance(config, dict):
        config = {}
    transport = str(config.get("transport") or "stdio").lower()
    if transport == "streamable-http":
        transport = "http"
    elif transport != "stdio":
        transport = "stdio"
    # 名称：dsh 条目 id 惯例为 mcp-<name>，优先以去掉前缀的 id 命名；
    # serverName 可能是泛化别名（如 id=mcp-open-websearch、serverName=web），
    # 直接用它会在管理界面认不出 server。id 缺失时回退 serverName。
    sid = str(entry.get("id") or "").strip()
    if sid.startswith("mcp-"):
        name = sid[len("mcp-"):]
    elif sid:
        name = sid
    else:
        name = str(config.get("serverName") or "").strip()
    name = name or sanitize_name(sid) or "mcp-server"
    server = {
        "name": name,
        "transport": transport,
        "command": config.get("command"),
        "args": list(config.get("args") or []),
        "env": dict(config.get("env") or {}),
        "url": config.get("url"),
        "headers": dict(config.get("headers") or {}) if isinstance(config.get("headers"), dict) else {},
        "cwd": config.get("cwd"),
        "description": str(config.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {},
    }
    return server


def _portable_command(command):
    """Windows 上把裸 npx/npm 归一带 .cmd，规避 spawn 失败；其它原样。"""
    if os.name != "nt" or not command:
        return command
    lowered = str(command).strip().lower()
    if lowered in ("npx", "npm"):
        return lowered + ".cmd"
    return command


def render_claude(server: dict) -> dict:
    out: dict = {}
    if server["transport"] == "stdio":
        out["type"] = "stdio"
        if server.get("command"):
            out["command"] = _portable_command(server["command"])
        if server.get("args"):
            out["args"] = list(server["args"])
        if server.get("env"):
            out["env"] = dict(server["env"])
    else:  # sse / http
        out["type"] = server["transport"]
        if server.get("url"):
            out["url"] = server["url"]
        if server.get("headers"):
            out["headers"] = dict(server["headers"])
    if server.get("description"):
        out["description"] = server["description"]
    return out


def render_codex(server: dict) -> dict:
    out: dict = {}
    if server.get("command"):
        out["command"] = _portable_command(server["command"])
    if server.get("args"):
        out["args"] = list(server["args"])
    if server.get("env"):
        out["env"] = dict(server["env"])
    if server.get("description"):
        out["description"] = server["description"]
    for k, v in (server.get("extra") or {}).items():
        out[k] = v
    return out


def render_dsh(server: dict) -> dict:
    config: dict = {"serverName": server["name"]}
    if server["transport"] == "http":
        config["transport"] = "streamable-http"
        if server.get("url"):
            config["url"] = server["url"]
        if server.get("headers"):
            config["headers"] = dict(server["headers"])
    else:
        config["transport"] = "stdio"
        if server.get("command"):
            config["command"] = _portable_command(server["command"])
        if server.get("args"):
            config["args"] = list(server["args"])
        if server.get("env"):
            config["env"] = dict(server["env"])
        if server.get("cwd"):
            config["cwd"] = server["cwd"]
    if server.get("description"):
        config["description"] = server["description"]
    return {
        "id": f"{DSH_MCP_ENTRY_PREFIX}{sanitize_name(server['name'])}",
        "name": DSH_MCP_CLIENT_PLUGIN,
        "config": config,
    }


def normalize_zcode(name: str, raw: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    """zcode mcp.servers 条目归一。

    zcode 的 type 可省略：有 command 推断 stdio、有 url 推断 http/sse；
    配置 schema 严格（未知键会让 server 被丢弃），额外键只保留
    enabled / timeoutMs（写回时白名单回填，不污染目标配置）。
    description 仅读取用于展示，写回时不输出（zcode 不认该键）。
    """
    typ = str(raw.get("type") or "").lower()
    if typ not in ("stdio", "sse", "http"):
        typ = "http" if raw.get("url") else "stdio"
    server = {
        "name": name,
        "transport": typ,
        "command": raw.get("command"),
        "args": list(raw.get("args") or []),
        "env": dict(raw.get("env") or {}),
        "url": raw.get("url"),
        "headers": dict(raw.get("headers") or {}) if isinstance(raw.get("headers"), dict) else {},
        "cwd": raw.get("cwd"),
        "description": str(raw.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {
            k: v for k, v in raw.items()
            if k in ("enabled", "timeoutMs") and v is not None
        },
    }
    return server


def render_zcode(server: dict) -> dict:
    """输出 zcode 规范字段（严格 schema：只写合法键，enabled/timeoutMs 回填）。"""
    out: dict = {}
    if server["transport"] == "stdio":
        out["type"] = "stdio"
        if server.get("command"):
            out["command"] = _portable_command(server["command"])
        if server.get("args"):
            out["args"] = list(server["args"])
        if server.get("cwd"):
            out["cwd"] = server["cwd"]
        if server.get("env"):
            out["env"] = dict(server["env"])
    else:  # sse / http
        out["type"] = server["transport"]
        if server.get("url"):
            out["url"] = server["url"]
        if server.get("headers"):
            out["headers"] = dict(server["headers"])
    for k, v in (server.get("extra") or {}).items():
        out[k] = v
    return out


# pi 的 mcp.json 与 claude 同构（顶层 mcpServers），额外有 pi 自有的键：
# 这些键决定工具如何暴露给模型，任何一次「装/删/迁移」都会整表重写，
# 因此必须无损读回并原样写回，否则会把用户调好的 exposure 配置抹平。
PI_SERVER_OWNED_KEYS = (
    "type",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "cwd",
    "description",
)


def normalize_pi(name: str, raw: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    """pi mcpServers 条目归一：streamable-http 记为 http，其余非规范键进 extra 原样保留。"""
    typ = str(raw.get("type") or "").lower()
    if typ == "streamable-http":
        typ = "http"
    if typ not in ("stdio", "sse", "http"):
        typ = "http" if raw.get("url") else "stdio"
    server = {
        "name": name,
        "transport": typ,
        "command": raw.get("command"),
        "args": list(raw.get("args") or []),
        "env": dict(raw.get("env") or {}),
        "url": raw.get("url"),
        "headers": dict(raw.get("headers") or {}) if isinstance(raw.get("headers"), dict) else {},
        "cwd": raw.get("cwd"),
        "description": str(raw.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {k: v for k, v in raw.items() if k not in PI_SERVER_OWNED_KEYS},
    }
    return server


def render_pi(server: dict) -> dict:
    """输出 pi 的 server 条目：stdio 写 command/args/env/cwd，HTTP 写 url/headers；
    传输靠 command / url 的存在与否被 pi 推断，不写 type（pi 对 streamable HTTP
    认 url 即可）。项目级文件不允许 auth（pi 会拒绝该条目），写项目时剔除。"""
    out: dict = dict(server.get("extra") or {})
    if server["transport"] == "stdio":
        if server.get("command"):
            out["command"] = _portable_command(server["command"])
        if server.get("args"):
            out["args"] = list(server["args"])
        if server.get("env"):
            out["env"] = dict(server["env"])
        if server.get("cwd"):
            out["cwd"] = server["cwd"]
    else:  # http（sse 已被能力矩阵挡在门外）
        if server.get("url"):
            out["url"] = server["url"]
        if server.get("headers"):
            out["headers"] = dict(server["headers"])
    if server.get("description"):
        out["description"] = server["description"]
    if server.get("scope") == "project" and "auth" in out:
        out.pop("auth", None)
        logger.warning("pi 项目级配置不支持 auth，写入「%s」时已剔除该键", server.get("name"))
    return out


def check_pi_server_name(name: str) -> None:
    """pi 只认字母/数字/_/- 的 server 名，其它字符（如中文、点号）会被 pi 判废。"""
    if not set(str(name)) <= PI_SERVER_NAME_CHARS:
        raise ValueError(
            f"pi 端 server 名只允许字母、数字、下划线与连字符，「{name}」无法写入；"
            "请先在源端改名，或在 pi 端手工安装"
        )


# Qoder 的 mcp.json 与 Claude Code 同构（顶层 mcpServers），自有键是
# disabled / timeout / authType / legacySseFallback / environment：
# disabled 是用户在 Qoder 界面里关掉某个 server 的开关，重写整表时必须
# 无损读回并原样写回，否则一迁移就把用户的停用状态清掉了。
QODER_SERVER_OWNED_KEYS = (
    "type",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "cwd",
    "description",
)


def normalize_qoder(name: str, raw: dict, path: Path, tool: str, scope: str, project: str | None) -> dict:
    """Qoder mcpServers 条目归一：streamable-http 记为 http，非规范键进 extra 原样保留。"""
    typ = str(raw.get("type") or "").lower().replace("streamablehttp", "streamable-http")
    if typ == "streamable-http":
        typ = "http"
    if typ not in ("stdio", "sse", "http"):
        typ = "http" if raw.get("url") else "stdio"
    server = {
        "name": name,
        "transport": typ,
        "command": raw.get("command"),
        "args": list(raw.get("args") or []),
        "env": dict(raw.get("env") or {}),
        "url": raw.get("url"),
        "headers": dict(raw.get("headers") or {}) if isinstance(raw.get("headers"), dict) else {},
        "cwd": raw.get("cwd"),
        "description": str(raw.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {k: v for k, v in raw.items() if k not in QODER_SERVER_OWNED_KEYS},
    }
    return server


def render_qoder(server: dict) -> dict:
    """输出 Qoder 的 server 条目：写规范 type（stdio/sse/http）+ 对应字段，
    并保留 normalize_qoder 收进 extra 的 Qoder 自有键。"""
    out: dict = dict(server.get("extra") or {})
    out["type"] = server["transport"]
    if server["transport"] == "stdio":
        if server.get("command"):
            out["command"] = _portable_command(server["command"])
        if server.get("args"):
            out["args"] = list(server["args"])
        if server.get("env"):
            out["env"] = dict(server["env"])
        if server.get("cwd"):
            out["cwd"] = server["cwd"]
    else:  # sse / http
        if server.get("url"):
            out["url"] = server["url"]
        if server.get("headers"):
            out["headers"] = dict(server["headers"])
    if server.get("description"):
        out["description"] = server["description"]
    return out


# ---------------- 读取 ----------------


def read_claude_servers(path: Path, tool: str = "claude", scope: str = "global", project: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    mcp = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    servers = []
    for name, raw in mcp.items():
        if not isinstance(raw, dict):
            continue
        try:
            servers.append(normalize_claude(str(name), raw, path, tool, scope, project))
        except Exception:
            continue
    return servers


def read_codex_servers(path: Path, tool: str = "codex", scope: str = "global", project: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        data = tomllib.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    mcp = data.get("mcp_servers") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    servers = []
    for name, raw_dict in mcp.items():
        if not isinstance(raw_dict, dict):
            continue
        try:
            servers.append(normalize_codex(str(name), raw_dict, path, tool, scope, project))
        except Exception:
            continue
    return servers


def _read_patch_entries(path: Path) -> list:
    """读取 cordis.patch.yml 顶层列表；空/损坏返回 []。"""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    return data if isinstance(data, list) else []


def read_dsh_servers(path: Path, tool: str = "dsh", scope: str = "global", project: str | None = None, profile: str | None = None) -> list[dict]:
    """cordis.patch.yml 中所有 name 为 dsh-mcp-client 的 insert 条目。"""
    # 仅项目级不被支持时使用给定路径，取值一致性由调用方保证
    servers = []
    entries = _read_patch_entries(path)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        insert = entry.get("insert")
        if not isinstance(insert, list):
            continue
        for item in insert:
            if not isinstance(item, dict) or item.get("name") != DSH_MCP_CLIENT_PLUGIN:
                continue
            try:
                servers.append(normalize_dsh(item, path, tool, scope, project))
            except Exception:
                continue
    return servers


def read_zcode_servers(path: Path, tool: str = "zcode", scope: str = "global", project: str | None = None) -> list[dict]:
    """读取 zcode 配置文件嵌套 mcp.servers 下的 server 列表。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    if not isinstance(data, dict):
        return []
    mcp = data.get("mcp")
    servers_map = mcp.get("servers") if isinstance(mcp, dict) else None
    if not isinstance(servers_map, dict):
        return []
    servers = []
    for name, raw in servers_map.items():
        if not isinstance(raw, dict):
            continue
        try:
            servers.append(normalize_zcode(str(name), raw, path, tool, scope, project))
        except Exception:
            continue
    return servers


def read_pi_servers(path: Path, tool: str = "pi", scope: str = "global", project: str | None = None) -> list[dict]:
    """读取 pi mcp.json 顶层 mcpServers（结构与 claude 一致，归一走 normalize_pi 以保留 pi 自有键）。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    mcp = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    servers = []
    for name, raw in mcp.items():
        if not isinstance(raw, dict):
            continue
        try:
            servers.append(normalize_pi(str(name), raw, path, tool, scope, project))
        except Exception:
            continue
    return servers


def read_qoder_servers(path: Path, tool: str = "qoder", scope: str = "global", project: str | None = None) -> list[dict]:
    """读取 ~/.qoder/mcp.json 顶层 mcpServers（与 claude 同构，归一走 normalize_qoder 以保留 disabled 等自有键）。"""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        logger.warning("读取 %s 失败：%s", path, e)
        return []
    mcp = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    servers = []
    for name, raw in mcp.items():
        if not isinstance(raw, dict):
            continue
        try:
            servers.append(normalize_qoder(str(name), raw, path, tool, scope, project))
        except Exception:
            continue
    return servers


def read_servers(scope: str, tool: str, project: str | None = None) -> list[dict]:
    """从指定位置读取归一 server 列表（文件不存在返回空）。"""
    path = mcp_config_file(scope, tool, project)
    if tool == "claude":
        return read_claude_servers(path, tool, scope, project)
    if tool == "codex":
        return read_codex_servers(path, tool, scope, project)
    if tool == "zcode":
        return read_zcode_servers(path, tool, scope, project)
    if tool == "pi":
        return read_pi_servers(path, tool, scope, project)
    if tool == "qoder":
        return read_qoder_servers(path, tool, scope, project)
    return read_dsh_servers(path, tool, scope, project)


# ---------------- 写入 ----------------


def _backup_config(path: Path) -> Path | None:
    """修改前备份配置文件，滚动保留最近 N 份；文件不存在返回 None。"""
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = config_manager.backup_dir() / f"mcp.{path.name}.{ts}.bak"
    shutil.copy2(path, dest)
    backups = sorted(config_manager.backup_dir().glob(f"mcp.{path.name}.*.bak"))
    for old in backups[:-BACKUP_KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _atomic_write(path: Path, content: str) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mcp.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _write_claude(path: Path, servers: list[dict]) -> None:
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as e:
            raise ValueError(f"读取 {path} 失败，已中止写入：{e}")
    # data 只增不删：除 mcpServers 外其它顶层键原样保留
    data["mcpServers"] = {s["name"]: render_claude(s) for s in servers}
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _write_codex(path: Path, servers: list[dict]) -> None:
    data: dict = {}
    if path.exists():
        try:
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            loaded = tomllib.loads(raw.decode("utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as e:
            raise ValueError(f"读取 {path} 失败，已中止写入：{e}")
    data["mcp_servers"] = {s["name"]: render_codex(s) for s in servers}
    _atomic_write(path, tomli_w.dumps(data))


def _write_dsh(path: Path, servers: list[dict]) -> None:
    """重写 cordis.patch.yml：保留非 dsh-mcp-client 条目，管理条目按 id 重建。"""
    entries = _read_patch_entries(path)
    kept: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        insert = entry.get("insert")
        if not isinstance(insert, list):
            kept.append(entry)
            continue
        if any(isinstance(i, dict) and i.get("name") == DSH_MCP_CLIENT_PLUGIN for i in insert):
            continue  # 丢掉旧的管理条目，下方按当前服务器列表重建
        kept.append(entry)
    managed = [render_dsh(s) for s in servers]
    if managed:
        kept.append({"insert": managed})
    _atomic_write(
        path,
        yaml.safe_dump(kept, allow_unicode=True, sort_keys=False, default_flow_style=False),
    )


def _write_zcode(path: Path, servers: list[dict]) -> None:
    """仅改 mcp.servers，其它顶层键（hooks / provider / model 等）原样保留。"""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as e:
            raise ValueError(f"读取 {path} 失败，已中止写入：{e}")
    mcp = dict(data.get("mcp") or {})
    mcp["servers"] = {s["name"]: render_zcode(s) for s in servers}
    data["mcp"] = mcp
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _write_pi(path: Path, servers: list[dict]) -> None:
    """重写 pi 的 mcp.json：只换 mcpServers，autoEnableCodemode 等顶层键原样保留。"""
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as e:
            raise ValueError(f"读取 {path} 失败，已中止写入：{e}")
    data["mcpServers"] = {s["name"]: render_pi(s) for s in servers}
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def _write_qoder(path: Path, servers: list[dict]) -> None:
    """重写 Qoder 的 mcp.json：只换 mcpServers，其余顶层键原样保留。

    Qoder 自身也在写这个文件（界面里的连接器开关会改 disabled），因此读取
    失败时必须中止，不能按空配置重建——那会把用户已启用的 server 全清掉。
    """
    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError) as e:
            raise ValueError(f"读取 {path} 失败，已中止写入：{e}")
    data["mcpServers"] = {s["name"]: render_qoder(s) for s in servers}
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_servers(scope: str, tool: str, project: str | None, servers: list[dict]) -> Path:
    """把归一 server 列表写回指定位置（先备份），返回配置文件路径。"""
    path = mcp_config_file(scope, tool, project)
    _backup_config(path)
    if tool == "claude":
        _write_claude(path, servers)
    elif tool == "codex":
        _write_codex(path, servers)
    elif tool == "zcode":
        _write_zcode(path, servers)
    elif tool == "pi":
        _write_pi(path, servers)
    elif tool == "qoder":
        _write_qoder(path, servers)
    elif tool == "dsh":
        _write_dsh(path, servers)
    else:
        # 未知端过去会静默落到 _write_dsh，把配置写进 dsh 的 patch 文件；
        # 加新端时若忘了接 adapter，必须在这里明确失败。
        raise ValueError(f"未知的 MCP 配置端：{tool}")
    return path


# ---------------- 发现 / 聚合 ----------------


def _mcp_descriptions_file() -> Path:
    """用户自定义 MCP 描述存储（按 server 名，跨端共享）。"""
    return get_data_dir() / "mcp_descriptions.json"


def load_mcp_descriptions() -> dict:
    """读取用户自定义描述表 {server_name: description}；无/损坏返回空。"""
    path = _mcp_descriptions_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_mcp_description(name: str, description: str) -> dict:
    """保存（或清空）单个 server 的自定义描述，返回最新描述表。"""
    data = load_mcp_descriptions()
    text = (description or "").strip()
    if text:
        data[name] = text
    else:
        data.pop(name, None)
    path = _mcp_descriptions_file()
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return data


def _aggregate(entries: list[dict], mask: bool = False, descriptions: dict | None = None) -> list[dict]:
    """按 name 聚合并标注端数/重复，与 skill 视图保持一致。

    mask=True 时 copies 中的 env/headers 值一律脱敏（仅回键名），
    用于 discover 响应——明文只在迁移内部从源文件直读，不经 API。

    descriptions：用户自定义描述表；group.description 按优先级取
    「自定义 > 配置自带 > 内置简介库 > npm 注册表」，description_source
    标识来源（custom/config/builtin/npm/empty），custom_description 供前端
    编辑回填，config_description 保留配置文件自带的描述。
    """
    custom = descriptions if isinstance(descriptions, dict) else {}
    npm_budget = {"left": _PER_DISCOVER_NPM_LIMIT}
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for entry in entries:
        name = entry["name"]
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(entry)
    result = []
    for name in sorted(order, key=str.lower):
        copies = groups[name]
        ends = sorted({c["tool"] for c in copies}, key=TOOLS.index)
        first = copies[0]
        config_desc = str(first.get("description") or "")
        if custom.get(name):
            text, source = custom[name], "custom"
        elif config_desc:
            text, source = config_desc, "config"
        else:
            text, source = auto_description(
                name, first.get("command"), first.get("args"), npm_budget
            )
            source = source or "empty"
        result.append(
            {
                "name": name,
                "transport": first["transport"],
                "end_count": len(ends),
                "duplicate": len(copies) > 1,
                "ends": ends,
                "description": text,
                "description_source": source,
                "custom_description": str(custom.get(name) or ""),
                "config_description": config_desc,
                "copies": [_mask_server(c) for c in copies] if mask else copies,
            }
        )
    return result


def _mask_server(server: dict) -> dict:
    """脱敏副本：env/headers 只回键名，值用掩码替换。"""
    out = dict(server)
    out["env"] = {k: MASK for k in server.get("env") or {}}
    out["headers"] = {k: MASK for k in server.get("headers") or {}}
    out["extra"] = {k: v for k, v in (server.get("extra") or {}).items() if k != "env"}
    return out


def discover() -> dict:
    """全量发现：全局六端 + 各项目（claude/codex/zcode/pi），按 name 聚合。"""
    custom = load_mcp_descriptions()
    global_entries: list[dict] = []
    for tool in TOOLS:
        if tool == "dsh":
            global_entries.extend(read_dsh_servers(dsh_mcp_patch_path(), tool, "global", None))
        else:
            global_entries.extend(read_servers("global", tool))

    projects_out = []
    for proj in project_dirs():
        entries: list[dict] = []
        for tool in ("claude", "codex", "zcode", "pi"):
            entries.extend(read_servers("project", tool, proj["project"]))
        if not entries:
            continue
        projects_out.append(
            {
                "project": proj["project"],
                "root": str(proj["root"]),
                "servers": _aggregate(entries, mask=True, descriptions=custom),
            }
        )

    return {
        "global": _aggregate(global_entries, mask=True, descriptions=custom),
        "projects": projects_out,
        "roots": {
            "claude": str(claude_mcp_path()),
            "codex": str(codex_mcp_path()),
            "dsh": str(dsh_mcp_patch_path()),
            "zcode": str(zcode_mcp_path()),
            "pi": str(pi_mcp_path()),
            "qoder": str(qoder_mcp_path()),
        },
        "project_roots": load_project_roots(),
        "capability": {tool: sorted(list(transports)) for tool, transports in TRANSPORT_CAPABILITY.items()},
    }


def server_content(scope: str, tool: str, project: str | None, name: str) -> dict:
    """返回单个 server 脱敏后的详情，供前端预览。"""
    lowered = str(name).lower()
    for entry in read_servers(scope, tool, project):
        if entry["name"].lower() == lowered:
            return _mask_server(entry)
    where = "全局" if scope == "global" else f"项目「{project}」"
    raise ValueError(f"在{where}{TOOL_LABELS[tool]}端未找到 MCP server：{name}")


# ---------------- 定位 ----------------


def _find_server(descriptor: dict) -> dict:
    """按 (scope, tool, project, name) 定位 server；找不到抛 ValueError。"""
    scope = descriptor.get("scope")
    tool = descriptor.get("tool")
    project = descriptor.get("project")
    name = str(descriptor.get("name") or "").strip()
    if not name:
        raise ValueError("缺少 MCP server 名称")
    lowered = name.lower()
    for entry in read_servers(scope, tool, project):
        if entry["name"].lower() == lowered:
            return entry
    where = "全局" if scope == "global" else f"项目「{project}」"
    raise ValueError(f"在{where}{TOOL_LABELS.get(tool, tool)}端未找到 MCP server：{name}")


# ---------------- 回收站 ----------------


def trash_dir() -> Path:
    d = get_data_dir() / TRASH_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def purge_expired(db) -> int:
    """惰性清理超过 30 天的回收站条目。"""
    from . import models

    deadline = datetime.now() - timedelta(days=TRASH_KEEP_DAYS)
    expired = db.query(models.McpTrash).filter(models.McpTrash.trash_time <= deadline).all()
    purged = 0
    for item in expired:
        p = Path(item.trash_path)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                logger.warning("清理回收站快照失败 %s：%s", p, e)
                continue
        db.delete(item)
        purged += 1
    if purged:
        db.commit()
        logger.info("MCP 回收站清理了 %s 条过期条目", purged)
    return purged


def _snapshot_payload(server: dict) -> dict:
    """env/headers 用 DPAPI 加密后落盘（恢复时解密）。"""
    return {
        "name": server["name"],
        "transport": server["transport"],
        "command": server.get("command"),
        "args": list(server.get("args") or []),
        "url": server.get("url"),
        "cwd": server.get("cwd"),
        "description": server.get("description") or "",
        "tool": server["tool"],
        "scope": server["scope"],
        "project": server.get("project"),
        "original_path": server.get("path"),
        "env_encrypted": {k: crypto.encrypt_text(str(v)) for k, v in (server.get("env") or {}).items()},
        "headers_encrypted": {k: crypto.encrypt_text(str(v)) for k, v in (server.get("headers") or {}).items()},
    }


def _decrypt_snapshot(payload: dict) -> dict:
    server = {
        "name": payload["name"],
        "transport": payload.get("transport", "stdio"),
        "command": payload.get("command"),
        "args": list(payload.get("args") or []),
        "env": {},
        "url": payload.get("url"),
        "headers": {},
        "cwd": payload.get("cwd"),
        "description": payload.get("description") or "",
        "tool": payload.get("tool", ""),
        "scope": payload.get("scope", "global"),
        "project": payload.get("project"),
        "path": payload.get("original_path", ""),
        "extra": {},
    }
    for k, v in (payload.get("env_encrypted") or {}).items():
        server["env"][k] = crypto.decrypt_text(str(v))
    for k, v in (payload.get("headers_encrypted") or {}).items():
        server["headers"][k] = crypto.decrypt_text(str(v))
    return server


def _write_snapshot(payload: dict) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = trash_dir() / f"{sanitize_name(payload['name'])}.{ts}.json"
    n = 1
    while dest.exists():
        dest = trash_dir() / f"{sanitize_name(payload['name'])}.{ts}_{n}.json"
        n += 1
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def delete_to_trash(db, descriptor: dict) -> dict:
    """删除某端某个 server（移除配置片段 → 快照进回收站）。"""
    from . import models

    server = _find_server(descriptor)
    scope, tool, project = server["scope"], server["tool"], server["project"]
    purged = purge_expired(db)
    remaining = [s for s in read_servers(scope, tool, project) if s["name"].lower() != server["name"].lower()]
    write_servers(scope, tool, project, remaining)

    payload = _snapshot_payload(server)
    dest = _write_snapshot(payload)
    db.add(
        models.McpTrash(
            server_name=server["name"],
            tool=tool,
            scope=scope,
            project=project,
            original_path=str(payload["original_path"]),
            trash_path=str(dest),
            trash_time=datetime.now(),
        )
    )
    db.commit()
    logger.info("MCP server「%s」已移入回收站（原位置 %s）", server["name"], payload["original_path"])
    return {"deleted": True, "name": server["name"], "purged_expired": purged}


def list_trash(db) -> dict:
    from . import models

    purged = purge_expired(db)
    rows = db.query(models.McpTrash).order_by(models.McpTrash.trash_time.desc()).all()
    items = []
    for item in rows:
        days_left = max(0, TRASH_KEEP_DAYS - (datetime.now() - item.trash_time).days)
        items.append(
            {
                "id": item.id,
                "name": item.server_name,
                "tool": item.tool,
                "scope": item.scope,
                "project": item.project,
                "original_path": item.original_path,
                "trash_time": item.trash_time.isoformat(sep=" ", timespec="seconds"),
                "days_left": days_left,
            }
        )
    return {"items": items, "purged_expired": purged}


def restore_from_trash(db, trash_id: int) -> dict:
    """从快照恢复：env 解密后写回原配置文件；原位置被占用时报错。"""
    from . import models

    item = db.query(models.McpTrash).filter(models.McpTrash.id == trash_id).first()
    if not item:
        raise ValueError("回收站中不存在该条目")
    src = Path(item.trash_path)
    if not src.exists():
        db.delete(item)
        db.commit()
        raise ValueError("回收站快照已丢失，条目已被移除")
    try:
        payload = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"回收站快照损坏：{e}")

    server = _decrypt_snapshot(payload)
    name = server["name"]
    scope, tool = server["scope"], server["tool"]
    project = server.get("project")
    existing = read_servers(scope, tool, project)
    if any(s["name"].lower() == name.lower() for s in existing):
        raise ValueError(f"原位置已存在同名 server：{name}，无法恢复")
    existing.append(server)
    write_servers(scope, tool, project, existing)
    db.delete(item)
    db.commit()
    logger.info("MCP server「%s」已恢复到 %s", name, server.get("path"))
    return {"restored": True, "name": name, "path": server.get("path", "")}


def delete_permanent(db, trash_id: int) -> dict:
    from . import models

    item = db.query(models.McpTrash).filter(models.McpTrash.id == trash_id).first()
    if not item:
        raise ValueError("回收站中不存在该条目")
    src = Path(item.trash_path)
    if src.exists():
        try:
            src.unlink()
        except OSError as e:
            raise ValueError(f"彻底删除失败：{e}")
    db.delete(item)
    db.commit()
    return {"deleted_permanently": True, "name": item.server_name}


# ---------------- 安装（新建 server） ----------------


def parse_mcp_json(text: str) -> list[dict]:
    """解析用户粘贴的 MCP server 配置片段（兼容多种来源）。

    兼容输入：
    - 顶层 mcpServers：`{"mcpServers": {"name": {...}}}`（直接复制 claude.json）
    - 顶层 server 字典：`{"amap-maps": {"command": "npx", ...}}`
    - 单 server 对象：无 name 时以键名兜底（各条目需是对象）
    transport 按 type 显式值或 command→stdio / url→http 推断。
    每条返回 create_server 可直接消费的字段 dict。
    """
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"JSON 解析失败：{e}")
    if not isinstance(data, dict):
        raise ValueError("配置片段必须是 JSON 对象")
    if isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    servers = []
    for key, raw in data.items():
        if not isinstance(raw, dict):
            continue  # 非对象条目（如说明字段）跳过
        typ = str(raw.get("type") or "").lower()
        if typ not in ("stdio", "sse", "http"):
            typ = "http" if raw.get("url") else "stdio"
        servers.append(
            {
                "name": str(key).strip(),
                "transport": typ,
                "command": str(raw.get("command") or "").strip() or None,
                "args": [str(a) for a in (raw.get("args") or []) if str(a).strip()],
                "env": {str(k): str(v) for k, v in (raw.get("env") or {}).items() if str(k).strip()},
                "url": str(raw.get("url") or "").strip() or None,
                "headers": {str(k): str(v) for k, v in (raw.get("headers") or {}).items() if str(k).strip()},
                "cwd": str(raw.get("cwd") or "").strip() or None,
                "description": str(raw.get("description") or "").strip(),
            }
        )
    if not servers:
        raise ValueError("未解析到任何 MCP server，请检查配置片段")
    return servers


def import_servers(scope: str, tool: str, project: str | None, parsed: list[dict]) -> dict:
    """批量安装（JSON 导入）多个 server：逐条独立安装，单条失败不中断。"""
    installed: list[dict] = []
    failed: list[dict] = []
    for s in parsed:
        name = s["name"]
        try:
            created = create_server(scope, tool, project, s)
            installed.append({"name": name, "transport": created["transport"]})
        except ValueError as e:
            failed.append({"name": name, "error": str(e)})
    return {"installed": installed, "failed": failed, "total": len(parsed)}

def create_server(scope: str, tool: str, project: str | None, fields: dict) -> dict:
    """向指定端新建（安装）一个 MCP server；同名已存在时报错。

    fields 为已校验的提交字段（name / transport / command / args / env /
    url / headers / cwd / description）。安装沿用各端 render 与原子写：
    先备份目标配置文件，只增不删既有 server，敏感字段写入配置（无可避免，
    与迁移一致），返回脱敏后的 server 详情。
    """
    if tool not in TOOLS:
        raise ValueError(f"未知工具：{tool}")
    if scope not in ("global", "project"):
        raise ValueError(f"未知 scope：{scope}")
    if scope == "project" and "project" not in DASH_SCOPE_CAPABILITY[tool]:
        raise ValueError(f"{TOOL_LABELS[tool]} 端不支持项目级 MCP 配置")
    path = mcp_config_file(scope, tool, project)  # 位置合法性由该函数兜底

    name = str(fields.get("name") or "").strip()
    if not name:
        raise ValueError("缺少 MCP server 名称")
    if tool == "pi":
        check_pi_server_name(name)
    transport = str(fields.get("transport") or "stdio").lower()
    if transport not in TRANSPORT_CAPABILITY[tool]:
        raise ValueError(f"{TOOL_LABELS[tool]} 端不支持 {transport} 传输")
    if transport == "stdio":
        if not str(fields.get("command") or "").strip():
            raise ValueError("stdio 传输需要填写启动命令（command）")
    else:  # http / sse
        if not str(fields.get("url") or "").strip():
            raise ValueError(f"{transport} 传输需要填写服务地址（url）")

    existing = read_servers(scope, tool, project)
    if any(s["name"].lower() == name.lower() for s in existing):
        raise ValueError(f"目标端已存在同名 server：{name}（可直接迁移，或先删除旧条目）")

    server = {
        "name": name,
        "transport": transport,
        "command": str(fields.get("command") or "").strip() or None,
        "args": [str(a) for a in (fields.get("args") or []) if str(a).strip()],
        "env": {str(k): str(v) for k, v in (fields.get("env") or {}).items() if str(k).strip()},
        "url": str(fields.get("url") or "").strip() or None,
        "headers": {str(k): str(v) for k, v in (fields.get("headers") or {}).items() if str(k).strip()},
        "cwd": str(fields.get("cwd") or "").strip() or None,
        "description": str(fields.get("description") or "").strip(),
        "tool": tool,
        "scope": scope,
        "project": project,
        "path": str(path),
        "extra": {},
    }
    existing.append(server)
    write_servers(scope, tool, project, existing)
    logger.info("已安装 MCP server「%s」到 %s · %s（%s）", name, TOOL_LABELS[tool], scope, transport)
    return _mask_server(server)


# ---------------- 迁移 ----------------


def migrate(db, sources: list[dict], targets: list[dict]) -> dict:
    """一键迁移：sources × targets 逐对写入目标配置文件（源端保留）。

    与 skill 的目录复制不同，这里是「配置片段写入」：目标端已有同名 server
    先快照进回收站再写入；传输能力不匹配的组合（如 sse → dsh）整对失败并
    留日志，不静默降级。
    """
    from . import models

    if not sources:
        raise ValueError("请选择要迁移的 MCP server")
    if not targets:
        raise ValueError("请选择迁移目标")

    resolved = []
    for s in sources:
        try:
            resolved.append((_find_server(s), s))
        except ValueError as e:
            raise ValueError(str(e))

    # 目标合法性预校验
    for t in targets:
        mcp_config_file(t.get("scope"), t.get("tool"), t.get("project"))

    results = []
    migrated = skipped = conflicts = failed = 0
    seen_pairs: set[tuple[str, str]] = set()

    for s_idx, (server, s_desc) in enumerate(resolved):
        s_name = server["name"]
        for t in targets:
            t_scope = t.get("scope")
            t_tool = t.get("tool")
            t_project = t.get("project")
            if t_tool not in TOOLS:
                raise ValueError(f"未知工具：{t_tool}")
            if t_scope not in ("global", "project"):
                raise ValueError(f"未知 scope：{t_scope}")
            if t_scope == "project" and "project" not in DASH_SCOPE_CAPABILITY.get(t_tool, ()):
                raise ValueError(f"{TOOL_LABELS[t_tool]} 端不支持项目级 MCP 配置")
            dest_key = ("global", t_tool, "") if t_scope == "global" else ("project", t_tool, t_project or "")
            target_label = (
                f"全局 · {TOOL_LABELS[t_tool]}"
                if t_scope == "global"
                else f"项目「{t_project}」· {TOOL_LABELS[t_tool]}"
            )

            pair_key = (s_name, server["path"], "|".join(dest_key))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # 源与目标完全一致时跳过
            if (
                (s_desc.get("scope") == t_scope)
                and (s_desc.get("tool") == t_tool)
                and ((s_desc.get("project") or "") == dest_key[2])
            ):
                skipped += 1
                results.append(
                    {
                        "source": s_name,
                        "target": target_label,
                        "status": "skipped",
                        "detail": "源与目标相同",
                    }
                )
                continue

            base = {
                "server_name": s_name,
                "transport": server["transport"],
                "source_tool": s_desc.get("tool"),
                "source_scope": s_desc.get("scope"),
                "source_project": s_desc.get("project"),
                "source_path": server["path"],
                "target_tool": t_tool,
                "target_scope": t_scope,
                "target_project": t_project,
            }
            try:
                if server["transport"] not in TRANSPORT_CAPABILITY[t_tool]:
                    raise ValueError(
                        f"{TOOL_LABELS[t_tool]} 端不支持 {server['transport']} 传输"
                        + (
                            "（dsh 仅支持 stdio / streamable-http）"
                            if t_tool == "dsh" and server["transport"] == "sse"
                            else "（pi 仅支持 stdio / streamable-http，不认 legacy SSE）"
                            if t_tool == "pi" and server["transport"] == "sse"
                            else ""
                        )
                    )
                if t_tool == "pi":
                    check_pi_server_name(s_name)
                # 目标端同名 server 先快照进回收站
                target_servers = read_servers(t_scope, t_tool, t_project)
                conflicts_in = [
                    s for s in target_servers if s["name"].lower() == s_name.lower()
                ]
                remaining = [
                    s for s in target_servers if s["name"].lower() != s_name.lower()
                ]
                for old in conflicts_in:
                    payload = _snapshot_payload(old)
                    dest = _write_snapshot(payload)
                    db.add(
                        models.McpTrash(
                            server_name=old["name"],
                            tool=t_tool,
                            scope=t_scope,
                            project=t_project,
                            original_path=str(old["path"]),
                            trash_path=str(dest),
                            trash_time=datetime.now(),
                        )
                    )
                remaining.append(server)
                path = write_servers(t_scope, t_tool, t_project, remaining)
                conflicts += len(conflicts_in)
                migrated += 1
                detail = f"已写入 {path}"
                if conflicts_in:
                    detail += f"；目标端旧版 {len(conflicts_in)} 份已移入回收站"
                db.add(models.McpMigration(**base, status="success", detail=detail))
                results.append(
                    {
                        "source": s_name,
                        "target": target_label,
                        "status": "success",
                        "detail": detail,
                    }
                )
                logger.info("MCP server「%s」迁移成功：%s", s_name, detail)
            except Exception as e:
                failed += 1
                db.add(models.McpMigration(**base, status="failed", detail=str(e)))
                results.append(
                    {
                        "source": s_name,
                        "target": target_label,
                        "status": "failed",
                        "detail": str(e),
                    }
                )
                logger.exception("MCP server「%s」迁移失败", s_name)

    db.commit()
    return {
        "migrated": migrated,
        "skipped": skipped,
        "conflicts_trashed": conflicts,
        "failed": failed,
        "results": results,
    }


def list_migrations(db, limit: int = 200) -> list[dict]:
    from . import models

    rows = (
        db.query(models.McpMigration)
        .order_by(models.McpMigration.migrate_time.desc(), models.McpMigration.id.desc())
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
            "server_name": row.server_name,
            "transport": row.transport,
            "source": f"{place(row.source_scope, row.source_project)} · {TOOL_LABELS.get(row.source_tool, row.source_tool)}",
            "target": f"{place(row.target_scope, row.target_project)} · {TOOL_LABELS.get(row.target_tool, row.target_tool)}",
            "status": row.status,
            "detail": row.detail,
            "migrate_time": row.migrate_time.isoformat(sep=" ", timespec="seconds"),
        }

    return [fmt(r) for r in rows]