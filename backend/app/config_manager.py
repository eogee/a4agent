"""Claude Code settings.json 读写、备份、原子写入。"""
import copy
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import tomli_w
import yaml

from .config_io import atomic_write_doc, backup_doc, backup_many, read_doc, resolve_path
from .database import get_data_dir
from .env_compat import env_first

CONFIG_FILENAME = "settings.json"
CODEX_CONFIG_FILENAME = "config.toml"
DEFAULT_BACKUP_KEEP = 5
A4AGENT_PROVIDER_PREFIX = "a4a_p"
# v0.3.x 及更早写入用户配置的托管前缀：切换清理托管条目时需一并移除，
# 否则老用户升级后配置里会残留孤儿条目、越积越多
LEGACY_PROVIDER_PREFIXES = ("a4api_p",)

# dsh（DeepSeek Harness）相关常量
DSH_HOME_ENV = "DSH_HOME"
DSH_SETTINGS_FILENAME = "settings.yaml"
DSH_CREDENTIALS_FILENAME = ".credentials.yaml"
DSH_LLM_NS = "llm-deepseek"
DSH_MODEL_NS = "agent-default-model"
DSH_PROVIDER_ROUTE = "deepseek-official"
DSH_API_KEY_REF = "DEEPSEEK_API_KEY"
# dsh llm-deepseek 适配器自身的 maxTokens 默认值。
DSH_ADAPTER_DEFAULT_MAX_TOKENS = 256000
# dsh 适配器默认 max_tokens 为 256000，远超多数上游（如智谱、Console Go）的
# 131072 输出上限，会把请求直接打回 INVALID_REQUEST；切换时写一个兼容的安全值兜底。
DSH_DEFAULT_MAX_TOKENS = 131072

# zcode（智谱 Agentic 开发环境）相关常量
# 环境变量取 (新名, 旧名) 二元组：读取走 env_first，旧 A4API_* 名向后兼容
ZCODE_HOME_ENV = ("A4AGENT_ZCODE_HOME", "A4API_ZCODE_HOME")
ZCODE_CLI_CONFIG_ENV = ("A4AGENT_ZCODE_CLI_CONFIG_PATH", "A4API_ZCODE_CLI_CONFIG_PATH")
ZCODE_V2_CONFIG_ENV = ("A4AGENT_ZCODE_V2_CONFIG_PATH", "A4API_ZCODE_V2_CONFIG_PATH")
ZCODE_CLI_CONFIG_REL = "cli/config.json"  # CLI/用户配置文件（hooks/plugins/provider/model）
ZCODE_V2_CONFIG_REL = "v2/config.json"  # 桌面端 provider/models 配置

# pi（本地 pi coding agent）相关常量
# pi 的全部配置都是明文 JSON：models.json（provider + 模型目录）、
# settings.json（defaultProvider/defaultModel）。原生讲 anthropic-messages /
# openai-completions / openai-responses 三种协议，因此与其它端一样直连上游。
PI_AGENT_DIR_ENV = ("A4AGENT_PI_AGENT_DIR", "PI_CODING_AGENT_DIR")
PI_MODELS_CONFIG_ENV = "A4AGENT_PI_MODELS_PATH"
PI_SETTINGS_CONFIG_ENV = "A4AGENT_PI_SETTINGS_PATH"
PI_MODELS_FILENAME = "models.json"
PI_SETTINGS_FILENAME = "settings.json"
# provider.api_type（+ native_responses）→ pi 的 models.json `api` 取值
PI_API_ANTHROPIC = "anthropic-messages"
PI_API_OPENAI = "openai-completions"
PI_API_OPENAI_RESPONSES = "openai-responses"

# Qoder（AI IDE）相关常量：只托管 skill 与 MCP，配置目录名随发行版而变
QODER_HOME_ENV = ("A4AGENT_QODER_HOME",)
QODER_HOME_DIRNAMES = (".qoder", ".qoder-cn")
QODER_SKILLS_CONFIG_ENV = ("A4AGENT_QODER_SKILLS_PATH",)


def settings_path() -> Path:
    return resolve_path(
        ("A4AGENT_SETTINGS_PATH", "A4API_SETTINGS_PATH"),
        lambda: Path.home() / ".claude" / CONFIG_FILENAME,
    )


def backup_dir() -> Path:
    d = get_data_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def target_list(targets) -> list:
    """规范化配置方案的应用目标列表（仅 claude / codex）。

    v0.5.1 起不再代管dsh / ZCode / pi 的 API 配置：这三端应用自身都自带完整
    的供应商配置界面，由用户自行配置更可靠（应用界面上的能力开关、上下文
    窗口等状态外部写入时无从得知）。技能与 MCP 托管不受影响。
    旧数据里残留的这三个值在读入时被静默丢弃。
    """
    result = []
    for t in (targets or "claude").split(","):
        t = (t or "").strip()
        if t in ("claude", "codex") and t not in result:
            result.append(t)
    return result or ["claude"]


def read_settings() -> dict:
    """读取 Claude Code settings.json；不存在或损坏时返回空字典。"""
    return read_doc(settings_path(), "json_sig")


def backup_settings() -> Path | None:
    """修改前备份 settings.json（滚动保留最近 N 份）。"""
    return backup_doc(settings_path(), "settings.{stamp}{ext}.bak")


def atomic_write_settings(data: dict) -> None:
    """原子写入 Claude Code settings.json。"""
    atomic_write_doc(settings_path(), data, "json_sig")


def build_settings(
    existing: dict | None,
    provider,
    api_key: str,
    model: str,
    proxy: dict | None = None,
) -> dict:
    """基于现有 settings.json 生成切换后的内容（合并式，保护用户已有配置）。

    anthropic：写 ANTHROPIC_* 环境变量（官方 Claude Code 原生支持）。
    openai：官方 Claude Code 不识别 OPENAI_* 配置，需经由本地翻译代理
    （openai_proxy）把 Anthropic 请求转成 OpenAI 格式；这里把
    ANTHROPIC_BASE_URL 指向本地代理并写入代理鉴权 token。

    只覆盖本工具托管的键（env 中的 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL、
    model、alwaysThinkingEnabled），其余顶层键（hooks、permissions、
    mcpServers、其他 env 变量等）原样保留，避免切换时抹掉用户已有配置。
    """
    if provider.api_type == "openai":
        if not proxy or not proxy.get("base_url") or not proxy.get("token"):
            raise ValueError("OpenAI 类型服务商需要先启动本地翻译代理")
        env = {
            "ANTHROPIC_AUTH_TOKEN": proxy["token"],
            "ANTHROPIC_BASE_URL": proxy["base_url"],
        }
    else:
        env = {
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_BASE_URL": provider.api_base,
        }

    data = dict(existing or {})
    current_env = data.get("env")
    if not isinstance(current_env, dict):
        current_env = {}
    data["env"] = {**current_env, **env}
    data["model"] = model
    data["alwaysThinkingEnabled"] = False
    return data


# ---------------- Codex（~/.codex/config.toml） ----------------


def codex_settings_path() -> Path:
    """Codex 全局配置文件路径，可用 A4AGENT_CODEX_CONFIG_PATH 覆盖。"""
    return resolve_path(
        ("A4AGENT_CODEX_CONFIG_PATH", "A4API_CODEX_CONFIG_PATH"),
        lambda: Path.home() / ".codex" / CODEX_CONFIG_FILENAME,
    )


def read_codex_settings() -> dict:
    """读取 Codex config.toml；文件不存在或损坏时返回空字典。"""
    return read_doc(codex_settings_path(), "toml")


def backup_codex_settings() -> Path | None:
    """修改前备份 config.toml（滚动保留最近 N 份）。"""
    return backup_doc(codex_settings_path(), "codex.config.{stamp}.toml.bak")


def atomic_write_codex_settings(data: dict) -> None:
    """原子写入 config.toml。"""
    atomic_write_doc(codex_settings_path(), data, "toml")


def build_codex_settings(
    existing: dict, provider, api_key: str, model: str, proxy: dict | None = None
) -> dict:
    """基于现有 config.toml 生成新配置：

    更新顶层 model / model_provider，并用 a4agent 托管的服务商条目
    （[model_providers.a4a_p*]）整体替换本工具旧条目（含改名前的 a4api_p* 遗留），
    其余配置原样保留。
    Codex CLI（0.146+）使用 OpenAI Responses 协议，wire_api 固定为 responses。
    传入 proxy 时把 base_url 指向本地翻译代理、token 换成代理鉴权 token，
    用于智谱等只提供 Chat Completions、不支持原生 /responses 的上游。
    """
    data = dict(existing)
    providers = dict(data.get("model_providers") or {})
    for key in [k for k in providers if str(k).startswith((A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES)]:
        providers.pop(key, None)
    provider_key = f"{A4AGENT_PROVIDER_PREFIX}{provider.id}"
    if proxy and proxy.get("base_url") and proxy.get("token"):
        providers[provider_key] = {
            "name": provider.name,
            "base_url": proxy["base_url"],
            "wire_api": "responses",
            "experimental_bearer_token": proxy["token"],
        }
    else:
        providers[provider_key] = {
            "name": provider.name,
            "base_url": provider.api_base,
            "wire_api": "responses",
            "experimental_bearer_token": api_key,
        }
    data["model_providers"] = providers
    data["model"] = model
    data["model_provider"] = provider_key
    return data


# ---------------- Codex 模型目录（model_catalog_json） ----------------


def codex_catalog_path(existing: dict | None = None) -> Path:
    """Codex 自定义模型目录路径，可用环境变量 A4AGENT_CODEX_CATALOG_PATH 覆盖。"""
    override = env_first("A4AGENT_CODEX_CATALOG_PATH", "A4API_CODEX_CATALOG_PATH")
    if override:
        return Path(override)
    catalog = (existing or {}).get("model_catalog_json")
    if catalog:
        return Path(str(catalog).strip('"'))
    return Path.home() / ".codex" / "models.json"


_MODEL_CATALOG_TEMPLATE = {
    "slug": "",
    "prefer_websockets": False,
    "support_verbosity": True,
    "default_verbosity": "low",
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text",
    "input_modalities": ["text"],
    "supports_image_detail_original": False,
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_parallel_tool_calls": True,
    "tool_mode": None,
    "multi_agent_version": "v2",
    "use_responses_lite": False,
    "include_skills_usage_instructions": False,
    "auto_review_model_override": None,
    "context_window": 200000,
    "max_context_window": 200000,
    "effective_context_window_percent": 95,
    "auto_compact_token_limit": None,
    "comp_hash": "3000",
    "reasoning_summary_format": "experimental",
    "default_reasoning_summary": "none",
    "default_reasoning_level": "high",
    "supported_reasoning_levels": [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {"effort": "high", "description": "Extra high reasoning depth for complex problems"},
        {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
    ],
    "shell_type": "shell_command",
    "visibility": "list",
    "minimal_client_version": "0.144.0",
    "supported_in_api": True,
    "priority": 1,
}


def ensure_model_in_catalog(model: str, existing: dict | None = None) -> dict:
    """确保模型出现在 model_catalog_json 中，供 Codex 解析自定义模型能力。

    已存在则直接返回；不存在时优先复制目录中已有条目（如 deepseek-v4-flash）
    的完整结构，仅替换标识字段，避免因缺字段导致 Codex 解析失败。
    """
    path = codex_catalog_path(existing)
    data: dict = {"models": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {"models": []}
    models = data.setdefault("models", [])
    existing_entry = next(
        (m for m in models if isinstance(m, dict) and m.get("slug") == model),
        None,
    )
    if existing_entry is not None and (
        "model_messages" in existing_entry or "base_instructions" in existing_entry
    ):
        return data
    if existing_entry is not None:
        # 已有简略条目时移除，按完整模板重建，避免 Codex 无法解析模型元数据
        models.remove(existing_entry)

    template = next((copy.deepcopy(m) for m in models if isinstance(m, dict)), None)
    if template is None:
        # 目标目录为空时，优先从默认用户目录复制完整条目结构（如 deepseek-v4-flash），
        # 避免简略模板缺字段导致 Codex 无法解析模型元数据。
        try:
            default_path = codex_catalog_path()
            if default_path != path and default_path.exists():
                default_data = json.loads(default_path.read_text(encoding="utf-8-sig"))
                template = next(
                    (
                        copy.deepcopy(m)
                        for m in default_data.get("models", [])
                        if isinstance(m, dict)
                    ),
                    None,
                )
        except (OSError, ValueError):
            template = None
    template = template or copy.deepcopy(_MODEL_CATALOG_TEMPLATE)
    entry = template
    entry["slug"] = model
    entry["display_name"] = model
    entry["description"] = f"{model} via a4agent local proxy"
    if "context_window" in entry:
        entry["context_window"] = 200000
    if "max_context_window" in entry:
        entry["max_context_window"] = 200000
    if "effective_context_window_percent" in entry:
        entry["effective_context_window_percent"] = 95
    models.append(entry)

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".models.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
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
    return data


# ---------------- dsh（DeepSeek Harness，~/.dsh） ----------------
#
# dsh 的全局用户文档是 settings.yaml（按 namespace 分段的 YAML），凭证单独存
# 在 .credentials.yaml。llm-deepseek 插件把连接配置放在 `llm-deepseek` 段
# （baseURL / apiKeyEnv），默认模型放在 `agent-default-model` 段
# （provider / model）。dsh 只注册 `deepseek-official` 一个 provider 路由，
# 原生走 OpenAI chat/completions：dsh 目标只支持 openai 类型服务商、直连上游、
# 无需本地翻译代理；配置文件被 watcher 热加载，切换后新会话即生效、免重启。


def dsh_home() -> Path:
    """dsh 数据目录：优先 $DSH_HOME，否则 ~/.dsh。"""
    override = os.environ.get(DSH_HOME_ENV)
    return Path(override) if override else Path.home() / ".dsh"


def dsh_settings_path() -> Path:
    """dsh 全局设置文档路径，可用 A4AGENT_DSH_SETTINGS_PATH 覆盖。"""
    return resolve_path(
        ("A4AGENT_DSH_SETTINGS_PATH", "A4API_DSH_SETTINGS_PATH"),
        lambda: dsh_home() / DSH_SETTINGS_FILENAME,
    )


def dsh_settings_candidates() -> list:
    """dsh 设置文档的所有可能落点（存在与否都返回）。

    主路径是 `settings.yaml`，但实测发现部分安装环境下该文件名为
    `settings.yaml.imported`（来源未能确证——dsh 主体包、dsh-tui 与本工具
    都未使用该名）。两种落点都纳入处理范围，避免残留断链。
    读取一律以主路径为准，此列表只用于清理与诊断。
    """
    primary = dsh_settings_path()
    return [primary, Path(f"{primary}.imported")]


def dsh_credentials_path() -> Path:
    """dsh 凭证文档路径，可用 A4AGENT_DSH_CREDENTIALS_PATH 覆盖。"""
    return resolve_path(
        ("A4AGENT_DSH_CREDENTIALS_PATH", "A4API_DSH_CREDENTIALS_PATH"),
        lambda: dsh_home() / DSH_CREDENTIALS_FILENAME,
    )


def read_dsh_settings() -> dict:
    """读取 dsh settings.yaml。"""
    return read_doc(dsh_settings_path(), "yaml")


def read_dsh_credentials() -> dict:
    """读取 dsh .credentials.yaml。"""
    return read_doc(dsh_credentials_path(), "yaml")


def backup_dsh_settings() -> Path | None:
    """修改前备份 settings.yaml。"""
    return backup_doc(dsh_settings_path(), "dsh.settings.{stamp}.yaml.bak")


def backup_dsh_credentials() -> Path | None:
    """修改前备份 .credentials.yaml。"""
    return backup_doc(dsh_credentials_path(), "dsh.credentials.{stamp}.yaml.bak")


def atomic_write_dsh_settings(data: dict) -> None:
    """原子写入 dsh settings.yaml。"""
    atomic_write_doc(dsh_settings_path(), data, "yaml")


def atomic_write_dsh_credentials(data: dict) -> None:
    """原子写入 dsh .credentials.yaml。"""
    atomic_write_doc(dsh_credentials_path(), data, "yaml")


def build_dsh_settings(
    existing: dict | None, provider, model: str, max_tokens: int | None = None,
    proxy: dict | None = None,
) -> dict:
    """基于现有 settings.yaml 生成切换后的内容（合并式，保留 ui-onboarding 等其它段）。

    dsh 只注册 `deepseek-official` 一个 provider 路由，原生走 OpenAI
    chat/completions：baseURL 指向本地翻译代理的 `/chat/completions` 透传
    端点（代理负责归一上游 tool_calls 分片中的 null 字段，规避 dsh 适配器
    把工具名/ID 覆盖为空的问题），apiKeyEnv 固定为 DEEPSEEK_API_KEY 并显式
    写入，key 本体由 build_dsh_credentials() 落到 .credentials.yaml。

    max_tokens：a4agent 里为该配置显式填写的单次输出上限，优先于一切既有值；
    留空（None）时保留用户已手动设置的 maxTokens，都没有则用安全默认。
    """
    data = dict(existing or {})
    llm = dict(data.get(DSH_LLM_NS) or {})
    # 经本地翻译代理透传（dsh 始终需要本地代理），代理未就绪时防御性直连。
    proxy_base = (proxy or {}).get("base_url")
    llm["baseURL"] = (
        str(proxy_base).rstrip("/")
        if proxy_base
        else str(provider.api_base).rstrip("/")
    )
    llm["apiKeyEnv"] = DSH_API_KEY_REF
    # 输出上限：显式填写 > 既有手动值 > 安全默认。
    # dsh 适配器默认 256000 会超出多数上游（如智谱）131072 的上限而被打回
    # INVALID_REQUEST，故没有显式值时绝不能放行 dsh 的默认值；既有值恰好等于
    # 适配器默认 256000 时视为历史遗留（并非用户手动选择），同样回落安全默认，
    # 避免旧值跟随切换带到限制更严的上游（如实测 Console Go 限 [1,131072]）。
    if max_tokens is not None:
        llm["maxTokens"] = max_tokens
    else:
        existing_tokens = llm.get("maxTokens")
        llm["maxTokens"] = (
            existing_tokens
            if existing_tokens and existing_tokens != DSH_ADAPTER_DEFAULT_MAX_TOKENS
            else DSH_DEFAULT_MAX_TOKENS
        )
    data[DSH_LLM_NS] = llm

    model_ns = dict(data.get(DSH_MODEL_NS) or {})
    model_ns["provider"] = DSH_PROVIDER_ROUTE
    model_ns["model"] = model
    data[DSH_MODEL_NS] = model_ns
    return data


def build_dsh_credentials(existing: dict | None, api_key: str, proxy_token: str | None = None) -> dict:
    """在 .credentials.yaml 的 refs 下写入 DEEPSEEK_API_KEY，保留其它凭证键。

    dsh 经本地翻译代理连接时，写入的应是代理鉴权 token（真实上游 key 由
    代理持有）；proxy_token 缺省时写真实 key（防御性直连场景）。

    dsh 要求 version-1 布局：顶层仅允许 version / refs / records 三个键，
    凭证全部嵌在 refs 下，任何未知顶层键都会让 dsh 拒绝启动。因此输出固定
    为 {"version": 1, "refs": {...}}（records 存在时原样保留）；输入若还是
    旧版扁平布局（凭证散在顶层），先并入 refs 一并迁移。
    """
    creds = dict(existing or {})
    refs = creds.get("refs")
    refs = dict(refs) if isinstance(refs, dict) else {}
    reserved = ("version", "refs", "records")
    for key, value in creds.items():
        if key in reserved or key in refs:
            continue
        if isinstance(value, str) and value:
            refs[key] = value
    refs[DSH_API_KEY_REF] = proxy_token if proxy_token else api_key
    out: dict = {"version": 1, "refs": refs}
    records = creds.get("records")
    if isinstance(records, dict):
        out["records"] = records
    return out


def read_dsh_selection() -> tuple[str | None, str | None]:
    """读取 dsh 当前生效的默认模型与 provider（agent-default-model 段）。"""
    section = read_dsh_settings().get(DSH_MODEL_NS)
    if not isinstance(section, dict):
        return None, None
    return section.get("model"), section.get("provider")


# ---------------- zcode（~/.zcode） ----------------
#
# zcode 原生支持「anthropic」与「openai-compatible」两种 provider kind，直连上游、
# 无需本地翻译代理。配置分两份：
#   - ~/.zcode/cli/config.json：CLI 用户配置，provider 条目 + 顶层 model
#     （格式为 "<provider_id>/<model>"，据此选定当前生效的 provider）；
#   - ~/.zcode/v2/config.json：桌面端 provider/models 配置，含各模型的
#     reasoning / limit / modalities 元数据。
# 切换时两份都写（合并式，保留 hooks / 其它 provider 等既有键），provider 条目
# 以 a4a_p<id> 命名托管，便于切换时整体替换而不污染用户手工添加的条目。


def zcode_home() -> Path:
    """zcode 数据目录：优先 A4AGENT_ZCODE_HOME（旧 A4API_ZCODE_HOME 兼容），否则 ~/.zcode。"""
    override = env_first(*ZCODE_HOME_ENV)
    return Path(override) if override else Path.home() / ".zcode"


def zcode_cli_config_path() -> Path:
    """zcode CLI 用户配置文件路径，可用环境变量 A4AGENT_ZCODE_CLI_CONFIG_PATH 覆盖。"""
    return resolve_path(ZCODE_CLI_CONFIG_ENV, lambda: zcode_home() / ZCODE_CLI_CONFIG_REL)


def zcode_v2_config_path() -> Path:
    """zcode 桌面端 provider 配置路径，可用环境变量 A4AGENT_ZCODE_V2_CONFIG_PATH 覆盖。"""
    return resolve_path(ZCODE_V2_CONFIG_ENV, lambda: zcode_home() / ZCODE_V2_CONFIG_REL)


def read_zcode_cli_config() -> dict:
    """读取 zcode cli/config.json。"""
    return read_doc(zcode_cli_config_path(), "json_sig")


def read_zcode_v2_config() -> dict:
    """读取 zcode v2/config.json。"""
    return read_doc(zcode_v2_config_path(), "json_sig")


def backup_zcode_configs() -> dict:
    """修改前备份 zcode 两份配置，返回 {cli, v2} 各自的备份路径（无原文件时为 None）。"""
    return backup_many(
        [
            ("cli", zcode_cli_config_path(), "zcode.cli.{stamp}.json.bak"),
            ("v2", zcode_v2_config_path(), "zcode.v2.{stamp}.json.bak"),
        ]
    )


def atomic_write_zcode_cli_config(data: dict) -> None:
    """原子写入 zcode cli/config.json。"""
    atomic_write_doc(zcode_cli_config_path(), data, "json_sig")


def atomic_write_zcode_v2_config(data: dict) -> None:
    """原子写入 zcode v2/config.json。"""
    atomic_write_doc(zcode_v2_config_path(), data, "json_sig")


def _zcode_model_entry(v2_existing: dict | None, model: str) -> dict:
    """为目标模型生成 v2/config.json 中的 models 条目。

    优先复制已有任意 provider 下同名模型的完整元数据结构（能力描述一致），
    否则用保守默认（reasoning 可选 / context 200k / output 128k / 纯文本）。
    """
    providers = (v2_existing or {}).get("provider")
    if isinstance(providers, dict):
        for p in providers.values():
            if not isinstance(p, dict):
                continue
            models = p.get("models")
            if isinstance(models, dict) and model in models and isinstance(models[model], dict):
                return copy.deepcopy(models[model])
    return {
        "reasoning": {
            "enabled": True,
            "variants": ["off", "high", "max"],
            "defaultVariant": "max",
        },
        "limit": {"context": 200000, "output": 128000},
        "modalities": {"input": ["text"], "output": ["text"]},
        "zcode": {"modalitiesConfigured": True},
    }


def build_zcode_settings(
    cli_existing: dict | None,
    v2_existing: dict | None,
    provider,
    api_key: str,
    model: str,
) -> tuple[dict, dict]:
    """基于现有两份 zcode 配置生成切换后的内容，返回 (cli_config, v2_config)。

    zcode 原生支持 anthropic 与 openai-compatible 两种 provider kind，直连
    上游、无需本地翻译代理：api_type=anthropic → kind=anthropic，
    api_type=openai → kind=openai-compatible。provider 条目以 a4a_p<id>
    命名托管（切换时整体替换本工具旧条目，保留用户手工添加的其它 provider），
    顶层字段（hooks / plugins / 其它键）原样保留。
    """
    kind = "anthropic" if provider.api_type == "anthropic" else "openai-compatible"
    provider_key = f"{A4AGENT_PROVIDER_PREFIX}{provider.id}"
    entry = {
        "name": provider.name,
        "kind": kind,
        "options": {
            "apiKey": api_key,
            "baseURL": provider.api_base,
            "apiKeyRequired": True,
        },
    }

    cli = dict(cli_existing or {})
    cli_providers = dict(cli.get("provider") or {})
    for key in [k for k in cli_providers if str(k).startswith((A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES)]:
        cli_providers.pop(key, None)
    cli_providers[provider_key] = {
        **entry,
        "options": {**entry["options"], "apiKey": api_key},
    }
    cli["provider"] = cli_providers
    cli["model"] = f"{provider_key}/{model}"

    v2 = dict(v2_existing or {})
    v2_providers = dict(v2.get("provider") or {})
    for key in [k for k in v2_providers if str(k).startswith((A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES)]:
        v2_providers.pop(key, None)
    v2_providers[provider_key] = {
        **entry,
        "source": "custom",
        "models": {model: _zcode_model_entry(v2_existing, model)},
    }
    v2["provider"] = v2_providers
    return cli, v2


def read_zcode_selection() -> tuple[str | None, str | None]:
    """读取 zcode 当前生效的 provider 与 model（cli/config.json 顶层 model）。

    zcode 的 model 格式为 "<provider_id>/<model>"，这里拆开返回。
    """
    model = read_zcode_cli_config().get("model")
    if not model or "/" not in str(model):
        return None, str(model) if model else None
    provider_id, _, model_name = str(model).partition("/")
    return model_name, provider_id


# ---------------- pi（本地 pi coding agent）配置读写 ----------------
# pi 的用户配置全是明文 JSON，agent 目录默认 ~/.pi/agent。切换写两份：
# models.json 的 providers.<id>（pi 支持从 models.json 取 apiKey）与
# settings.json 的 defaultProvider / defaultModel。
# auth.json 是 pi 自己的凭证库（可能存着 oauth 登录态，且只有它内部的
# read-modify-write 路径保证并发安全），本工具不写入，避免覆盖用户凭证。


def pi_agent_dir() -> Path:
    """pi 的 agent 目录：A4AGENT_PI_AGENT_DIR（兼容 pi 自身的 PI_CODING_AGENT_DIR），否则 ~/.pi/agent。"""
    return resolve_path(PI_AGENT_DIR_ENV, lambda: Path.home() / ".pi" / "agent")


def pi_skills_root() -> Path:
    """pi 全局 skill 根，可用环境变量 A4AGENT_PI_SKILLS_PATH 覆盖。"""
    return resolve_path(("A4AGENT_PI_SKILLS_PATH",), lambda: pi_agent_dir() / "skills")


def pi_models_config_path() -> Path:
    """pi 的 provider / 模型目录配置路径，可用 A4AGENT_PI_MODELS_PATH 覆盖。"""
    return resolve_path((PI_MODELS_CONFIG_ENV,), lambda: pi_agent_dir() / PI_MODELS_FILENAME)


def pi_settings_path() -> Path:
    """pi 的会话默认配置路径，可用 A4AGENT_PI_SETTINGS_PATH 覆盖。"""
    return resolve_path((PI_SETTINGS_CONFIG_ENV,), lambda: pi_agent_dir() / PI_SETTINGS_FILENAME)


def read_pi_models_config() -> dict:
    """读取 pi models.json。"""
    return read_doc(pi_models_config_path(), "json_sig")


def read_pi_settings() -> dict:
    """读取 pi settings.json。"""
    return read_doc(pi_settings_path(), "json_sig")


def backup_pi_configs() -> dict:
    """修改前备份 pi 两份配置，返回 {models, settings} 各自备份路径（无原文件时 None）。"""
    return backup_many(
        [
            ("models", pi_models_config_path(), "pi.models.{stamp}.json.bak"),
            ("settings", pi_settings_path(), "pi.settings.{stamp}.json.bak"),
        ]
    )


def atomic_write_pi_models_config(data: dict) -> None:
    """原子写入 pi models.json。"""
    atomic_write_doc(pi_models_config_path(), data, "json_sig")


def atomic_write_pi_settings(data: dict) -> None:
    """原子写入 pi settings.json。"""
    atomic_write_doc(pi_settings_path(), data, "json_sig")


def pi_api_kind(provider) -> str:
    """服务商协议 → pi models.json 的 `api` 取值（pi 三种协议原生支持，直连免代理）。"""
    if provider.api_type == "anthropic":
        return PI_API_ANTHROPIC
    if provider.native_responses:
        return PI_API_OPENAI_RESPONSES
    return PI_API_OPENAI


def _pi_model_entry(models_config: dict | None, model: str, context_window=None) -> dict:
    """目标模型在 pi 里的条目：复用已有同名模型的完整定义，否则只写 id 交 pi 兜默认值。

    pi 的 schema 里 contextWindow / cost 等字段均可选，但缺省 contextWindow
    按 128000 计——接本地 llama-server 时用户把 `-c` 调到 256k，pi 仍会按 128k
    管理上下文，故传入 context_window 时补写该字段。已有条目只在「本工具托管的
    provider」或原本就缺 contextWindow 时更新，避免改写用户在 pi 里手工调过的值。
    """
    found = None
    from_managed = False
    providers = (models_config or {}).get("providers")
    if isinstance(providers, dict):
        for pid, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            for item in entry.get("models") or []:
                if isinstance(item, dict) and item.get("id") == model:
                    found = copy.deepcopy(item)
                    from_managed = str(pid).startswith(
                        (A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES
                    )
                    break
            if found is not None:
                break
    out = found if found is not None else {"id": model}
    if context_window:
        if found is None or from_managed or not out.get("contextWindow"):
            out["contextWindow"] = int(context_window)
    return out


def build_pi_settings(
    models_existing: dict | None,
    settings_existing: dict | None,
    provider,
    api_key: str,
    model: str,
    context_window: int | None = None,
) -> tuple[dict, dict]:
    """基于 pi 现有两份配置生成切换后的内容，返回 (models_config, settings)。

    provider 条目以 a4a_p<id> 托管（整体替换本工具旧条目，保留用户手工添加的
    provider 与 models.json 其它顶层键）；settings 只改 defaultProvider /
    defaultModel，其它键（theme / enabledSkills 等）原样保留。
    context_window 由调用方在服务商是本地 llama-server 时给出（pi 侧的窗口
    需与服务实际 `-c` 对齐）。
    """
    provider_key = f"{A4AGENT_PROVIDER_PREFIX}{provider.id}"
    entry = {
        "name": provider.name,
        "baseUrl": provider.api_base,
        "api": pi_api_kind(provider),
        "apiKey": api_key,
        "models": [_pi_model_entry(models_existing, model, context_window)],
    }

    models_config = dict(models_existing or {})
    providers = dict(models_config.get("providers") or {})
    for key in [k for k in providers if str(k).startswith((A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES)]:
        providers.pop(key, None)
    providers[provider_key] = entry
    models_config["providers"] = providers

    settings = dict(settings_existing or {})
    settings["defaultProvider"] = provider_key
    settings["defaultModel"] = model
    return models_config, settings


def read_pi_selection() -> tuple[str | None, str | None]:
    """读取 pi 当前生效的 model 与 provider（settings.json）。"""
    settings = read_pi_settings()
    model = settings.get("defaultModel")
    provider_id = settings.get("defaultProvider")
    return (str(model) if model else None), (str(provider_id) if provider_id else None)


# ---------------- Qoder（AI IDE）配置读写 ----------------
# Qoder 只托管 Skill 与 MCP 两类配置文件，**不参与 API 服务商切换**：
# 它的 BYOK / external-provider 数据全部落在 ~/.qoder/.models/<uid>/ 下，
# 且内容经 Qoder 自带的 WASM 原生模块按机器码加密（catalog-v6 / customs /
# external-providers/catalog-v12-*），算法专有且随版本变化，无法像其它端那样
# 原子写明文配置。因此 target_list 里刻意不含 qoder。


def qoder_home() -> Path:
    """Qoder 配置目录：优先 A4AGENT_QODER_HOME，其次取实际存在的发行版目录。

    目录名随发行版变化（国际版 .qoder / 国内版 .qoder-cn），故按候选名探测。
    这也是全文件唯一保留手写环境变量解析的地方——候选目录探测无法表达成
    resolve_path 的「一串环境变量名 + 单个默认值」。
    """
    override = env_first(*QODER_HOME_ENV)
    if override:
        return Path(override)
    for name in QODER_HOME_DIRNAMES:
        candidate = Path.home() / name
        if candidate.is_dir():
            return candidate
    return Path.home() / QODER_HOME_DIRNAMES[0]


def qoder_skills_root() -> Path:
    """Qoder 全局 skill 根（~/.qoder/skills），可用 A4AGENT_QODER_SKILLS_PATH 覆盖。"""
    return resolve_path(QODER_SKILLS_CONFIG_ENV, lambda: qoder_home() / "skills")
