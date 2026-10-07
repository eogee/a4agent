"""移除三端托管配置：清理 a4agent 写入的残留，还原用户自主权。

v0.6.0 起dsh / ZCode / pi 的 API 配置改由用户在应用内自配，本模块负责把
旧版 a4agent 写入的托管条目清掉，让用户回到干净状态。

**三条不可越界的红线**（对应产品原则「绝不静默覆盖用户设置」）：

1. 只删自己写的东西：识别依据是 `a4a_p*` 前缀（v0.3.x 及更早为 `a4api_p*`）
   与 dsh 的 `llm-deepseek` 段，绝不按用户自定义的键名猜测。
2. 每个动作前强制快照，出错即回滚，不留半完成状态。
3. 判定依据不足时**不动**。dsh 的 `llm-deepseek` 段只有在确认 baseURL 指向
   本工具的本地代理端口（17890–17899）时才删——仅凭段名不足以断定是我们写的。
   用户的 `llm-pi-ai`（dsh 界面配置的正规命名空间）任何情况下都不碰。
"""
import logging

from . import config_manager, proxy_standalone, removal_backup
from .config_io import atomic_write_doc, backup_doc, read_doc
from .openai_proxy import PROXY_PORT_END, PROXY_PORT_START
from .config_manager import (A4AGENT_PROVIDER_PREFIX,
                             LEGACY_PROVIDER_PREFIXES)

logger = logging.getLogger(__name__)

_HANDLED_PREFIXES = (A4AGENT_PROVIDER_PREFIX,) + LEGACY_PROVIDER_PREFIXES
_PROXY_PORTS = set(range(PROXY_PORT_START, PROXY_PORT_END + 1))


def _is_managed(key: str) -> bool:
    return str(key).startswith(_HANDLED_PREFIXES)


def _points_at_proxy(url: str) -> bool:
    """baseURL 是否指向本工具的本地翻译代理端口。

    这是删除 dsh `llm-deepseek` 段的必要条件：段名相同不代表是我们写的，
    用户完全可能自己配过一段指向别处。只有确认指向 17890–17899 才是我们的。
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(url or ""))
        return parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port in _PROXY_PORTS
    except ValueError:
        return False


def _clean_provider_entries(doc: dict, keys_path: str) -> tuple[dict, list]:
    """删掉托管 provider 条目，返回 (新文档, 删除的键列表)。

    keys_path 需显式指定：ZCode 两份配置用 `provider`（单数），pi 的
    models.json 用 `providers`（复数），两者不统一，不能靠默认值猜。
    """
    providers = doc.get(keys_path)
    if not isinstance(providers, dict):
        return doc, []
    removed = [k for k in providers if _is_managed(k)]
    if not removed:
        return doc, []
    new_doc = dict(doc)
    new_doc[keys_path] = {k: v for k, v in providers.items() if not _is_managed(k)}
    return new_doc, removed


def _clean_zcode(dry_run: bool) -> dict:
    """ZCode：删两处 a4a_p* 条目；model 指向被删条目时一并清空。

    清空 model 而不是随便挑一个：ZCode 的 model 格式是 `<provider_id>/<model>`，
    被删的provider 没了指向就是悬空，留在原地会让用户在选择器里点到坏条目。
    """
    result: dict = {"removed": [], "cleared_fields": []}

    cli = config_manager.read_zcode_cli_config()
    new_cli, removed = _clean_provider_entries(cli, "provider")
    if removed:
        result["removed"].extend(f"cli:{k}" for k in removed)
        model = str(new_cli.get("model") or "")
        if model.split("/", 1)[0] in removed:
            new_cli["model"] = ""
            result["cleared_fields"].append("cli.model")
        if not dry_run:
            config_manager.backup_zcode_configs()
            config_manager.atomic_write_zcode_cli_config(new_cli)

    v2 = config_manager.read_zcode_v2_config()
    new_v2, removed_v2 = _clean_provider_entries(v2, "provider")
    if removed_v2:
        result["removed"].extend(f"v2:{k}" for k in removed_v2)
        if not dry_run:
            config_manager.backup_zcode_configs()
            config_manager.atomic_write_zcode_v2_config(new_v2)
    return result


def _clean_pi(dry_run: bool) -> dict:
    """pi：删 models.json 里的 a4a_p* 条目；defaultProvider 悬空时清空两项。

    auth.json 不动——那是 pi 自己的凭证库，可能存着 oauth 登录态，
    只有它内部的 read-modify-write 才能保证并发安全。
    """
    result: dict = {"removed": [], "cleared_fields": []}

    models = config_manager.read_pi_models_config()
    new_models, removed = _clean_provider_entries(models, "providers")
    if removed:
        result["removed"].extend(f"models:{k}" for k in removed)
        if not dry_run:
            config_manager.backup_pi_configs()
            config_manager.atomic_write_pi_models_config(new_models)

    settings = config_manager.read_pi_settings()
    pid = str(settings.get("defaultProvider") or "")
    if pid and pid in removed:
        new_settings = dict(settings)
        new_settings["defaultProvider"] = ""
        new_settings["defaultModel"] = ""
        result["cleared_fields"].extend(["defaultProvider", "defaultModel"])
        if not dry_run:
            config_manager.backup_pi_configs()
            config_manager.atomic_write_pi_settings(new_settings)
    return result


def _clean_dsh(dry_run: bool) -> dict:
    """dsh：逐个候选路径处理，仅当 llm-deepseek.baseURL 指向本工具代理时清理。

    覆盖 `settings.yaml` 与实测存在的 `settings.yaml.imported` 两个落点——后者
    的来源未能确证，但既然可能是旧版写入的残留，就不能留下指向已不启动的代理
    的断链。

    绝不碰 `llm-pi-ai`——那是 dsh 界面配置的正规命名空间，用户自己在那边
    配的供应商全在里面，动了就是把用户的配置毁掉。
    """
    result: dict = {"removed": [], "cleared_fields": [], "skipped": []}

    for path in config_manager.dsh_settings_candidates():
        if not path.exists():
            continue
        settings = read_doc(path, "yaml")
        llm = settings.get(config_manager.DSH_LLM_NS)
        url = str((llm or {}).get("baseURL") or "") if isinstance(llm, dict) else ""

        if not isinstance(llm, dict) or not url:
            result["skipped"].append(f"{path.name}: 未发现 llm-deepseek 段")
            continue
        if not _points_at_proxy(url):
            # 段在，但不是指向我们的代理——判定依据不足，不动
            result["skipped"].append(
                f"{path.name}: baseURL 指向 {url}，非本工具代理端口，"
                "判定为用户自有配置，不做改动"
            )
            continue

        new_settings = {
            k: v for k, v in settings.items() if k != config_manager.DSH_LLM_NS
        }
        result["removed"].append(f"{path.name}:{config_manager.DSH_LLM_NS}")
        # 默认模型段指向的 provider 也随该段失效，一并清掉避免留下悬空引用
        model_ns = new_settings.get(config_manager.DSH_MODEL_NS)
        if isinstance(model_ns, dict) and model_ns.get("provider") == config_manager.DSH_PROVIDER_ROUTE:
            new_settings[config_manager.DSH_MODEL_NS] = {**model_ns, "provider": "", "model": ""}
            result["cleared_fields"].append(f"{path.name}:{config_manager.DSH_MODEL_NS}.provider/model")
        if not dry_run:
            backup_doc(path, f"dsh.{path.stem}.{{stamp}}.yaml.bak")
            atomic_write_doc(path, new_settings, "yaml")

    result["skipped"] = result["skipped"] or ["未发现 a4agent 写入的 llm-deepseek 段"]
    _clean_dsh_credentials(dry_run, result)
    return result


def _clean_dsh_credentials(dry_run: bool, result: dict) -> None:
    """移除写进凭证的代理 token。

    代理 token 与真实上游 key 不是一回事：真实 key 由代理进程持有，应用退出后
    代理不再启动，用户直接跑 dsh 就会拿着一个没人认的 token 去认证。因此确认
    凭证里那个值就是当前代理的 token 时必须清掉。
    """
    if dry_run:
        return

    creds = read_doc(config_manager.dsh_credentials_path(), "yaml")
    refs = creds.get("refs") if isinstance(creds.get("refs"), dict) else {}
    stored = refs.get(config_manager.DSH_API_KEY_REF)
    if not stored:
        return
    running = proxy_standalone.is_proxy_running()
    if not running.get("running"):
        # 代理没在跑就无法判定这个 token 是不是我们的，保守起见不动
        result["skipped"].append(
            f"credentials: 代理未运行，无法判定 {config_manager.DSH_API_KEY_REF} 是否为代理 token，未改动"
        )
        return
    current = _current_proxy_token()
    if current and str(stored) == str(current):
        new_refs = {k: v for k, v in refs.items() if k != config_manager.DSH_API_KEY_REF}
        new_creds = dict(creds)
        new_refs_present = "refs" in creds
        if new_refs_present:
            new_creds["refs"] = new_refs
        backup_doc(config_manager.dsh_credentials_path(), "dsh.credentials.{stamp}.yaml.bak")
        atomic_write_doc(config_manager.dsh_credentials_path(), new_creds, "yaml")
        result["removed"].append(f"credentials:{config_manager.DSH_API_KEY_REF}(代理token)")


def _current_proxy_token() -> str | None:
    """读当前代理进程持有的 token（用于判断凭证里那个是不是代理 token）。"""
    try:
        return proxy_standalone.ensure_proxy_running().get("token")
    except Exception as e:
        # 拉不起代理就不能判定，此时保守地不动凭证
        logger.info("代理未就绪，跳过 dsh 凭证清理：%s", e)
        return None


def cleanup_all(dry_run: bool = False) -> dict:
    """清理三端托管配置残留，返回 {snapshots, dsh, zcode, pi, errors}。

    顺序：先做永久快照，再逐端清理。任何一端失败都记入 errors 而不抛出——
    三端相互独立，一端失败不应阻断另两端的清理。
    """
    report: dict = {"snapshots": {}, "dsh": {}, "zcode": {}, "pi": {}, "errors": []}

    try:
        snap = removal_backup.take_snapshot(dry_run=dry_run)
        report["snapshots"] = {"dir": snap["dir"], "files": snap["files"]}
        if snap["errors"]:
            report["errors"].extend(snap["errors"])
    except Exception as e:
        # 快照失败就必须停手：没有回滚依据就不要动用户文件
        report["errors"].append(f"快照失败，已中止清理：{e}")
        logger.exception("清理前快照失败，中止")
        return report

    for key, fn in (("dsh", _clean_dsh), ("zcode", _clean_zcode), ("pi", _clean_pi)):
        try:
            report[key] = fn(dry_run)
        except Exception as e:
            report["errors"].append(f"{key} 清理失败：{e}")
            logger.exception("清理 %s 托管配置失败", key)
    return report


def needs_cleanup() -> bool:
    """是否还有托管残留。用于在界面提示用户「需要执行一次清理」。"""
    models = config_manager.read_pi_models_config()
    providers = models.get("providers")
    if isinstance(providers, dict) and any(_is_managed(k) for k in providers):
        return True
    for doc in (config_manager.read_zcode_cli_config(), config_manager.read_zcode_v2_config()):
        entries = doc.get("provider")
        if isinstance(entries, dict) and any(_is_managed(k) for k in entries):
            return True
    # 两个候选落点都要查：settings.yaml 与 settings.yaml.imported
    for path in config_manager.dsh_settings_candidates():
        if not path.exists():
            continue
        llm = read_doc(path, "yaml").get(config_manager.DSH_LLM_NS)
        if isinstance(llm, dict) and _points_at_proxy(llm.get("baseURL", "")):
            return True
    return False