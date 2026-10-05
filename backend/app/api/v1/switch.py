"""切换与状态接口。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import config_manager, crud, proxy_standalone, schemas, version as app_version
from ...crypto import decrypt_text
from ...database import get_db
from ...llama import runtime as llama_runtime
from ...process import is_claude_running, restart_claude

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/status", response_model=schemas.StatusOut)
def get_status(db: Session = Depends(get_db)):
    active = crud.get_active_config(db)
    current = config_manager.read_settings()
    codex = config_manager.read_codex_settings()
    dsh_model, dsh_provider = config_manager.read_dsh_selection()
    zcode_model, zcode_provider = config_manager.read_zcode_selection()
    pi_model, pi_provider = config_manager.read_pi_selection()
    return schemas.StatusOut(
        version=app_version.current_version(),
        active_config=active,
        settings_file_exists=config_manager.settings_path().exists(),
        current_model=current.get("model"),
        codex_file_exists=config_manager.codex_settings_path().exists(),
        current_codex_model=codex.get("model"),
        current_codex_provider=codex.get("model_provider"),
        dsh_file_exists=config_manager.dsh_settings_path().exists(),
        current_dsh_model=dsh_model,
        current_dsh_provider=dsh_provider,
        zcode_file_exists=config_manager.zcode_cli_config_path().exists(),
        current_zcode_model=zcode_model,
        current_zcode_provider=zcode_provider,
        pi_file_exists=config_manager.pi_models_config_path().exists(),
        current_pi_model=pi_model,
        current_pi_provider=pi_provider,
    )


@router.get("/proxy/status")
def proxy_status():
    """查询本地翻译代理是否在运行。"""
    return proxy_standalone.is_proxy_running()


@router.post("/proxy/stop")
def proxy_stop():
    """停止本地翻译代理进程。"""
    result = proxy_standalone.stop_proxy()
    if not result.get("stopped"):
        raise HTTPException(409, result.get("detail", "代理未在运行"))
    return result


@router.post("/switch/{config_id}", response_model=schemas.SwitchResult)
def switch_config(config_id: int, body: schemas.SwitchRequest, db: Session = Depends(get_db)):
    config = crud.get_config(db, config_id)
    if not config:
        raise HTTPException(404, "配置方案不存在")
    if config.provider is None:
        raise HTTPException(409, "该配置关联的服务商已被删除，请先编辑或删除此配置方案")

    backup_path = None
    codex_backup_path = None
    dsh_backup_path = None
    zcode_backup_path = None
    pi_backup_path = None
    try:
        api_key = decrypt_text(config.api_key_encrypted)
        if not api_key:
            raise ValueError("API Key 解密失败")
        targets = config_manager.target_list(config.targets)
        # 目标含 Codex / dsh 但服务商非 OpenAI 兼容时，在标记生效 / 写任何文件之前干净失败，
        # 避免出现“Claude 配置已写入但整体报错”的半生效状态
        if ("codex" in targets or "dsh" in targets) and config.provider.api_type != "openai":
            need = "、".join(
                name
                for name, key in (("Codex", "codex"), ("dsh", "dsh"))
                if key in targets
            )
            raise ValueError(
                f"{need} 需要 OpenAI 兼容接口，"
                f"请为「{config.name}」选择 OpenAI 兼容的服务商"
            )
        proxy: dict | None = None
        # 先标记生效并提交，独立翻译代理进程才能从数据库找到当前配置
        crud.set_active(db, config)
        if "claude" in targets:
            backup_path = config_manager.backup_settings()
            # 读现有 settings.json 传入合并，保留 hooks / permissions 等用户已有键
            existing = config_manager.read_settings()
            if config.provider.api_type == "openai":
                # 独立代理进程：应用退出后仍然存活，Claude Code 可继续使用
                proxy = proxy_standalone.ensure_proxy_running()
                settings = config_manager.build_settings(
                    existing, config.provider, api_key, config.model, proxy=proxy
                )
            else:
                settings = config_manager.build_settings(
                    existing, config.provider, api_key, config.model
                )
            config_manager.atomic_write_settings(settings)
        if "codex" in targets:
            codex_backup_path = config_manager.backup_codex_settings()
            existing = config_manager.read_codex_settings()
            # 上游原生支持 Responses（如 DeepSeek）时直连上游，无需本地代理；
            # 仅提供 Chat Completions 的上游（如智谱）才经本地翻译代理转发。
            proxy = None
            if not config.provider.native_responses:
                proxy = proxy_standalone.ensure_proxy_running()
            codex_settings = config_manager.build_codex_settings(
                existing, config.provider, api_key, config.model, proxy=proxy
            )
            config_manager.atomic_write_codex_settings(codex_settings)
            config_manager.ensure_model_in_catalog(config.model, existing)
        if "dsh" in targets:
            # dsh 原生走 OpenAI chat/completions，但统一经本地翻译代理的
            # /chat/completions 透传端点连接上游：代理会把上游流式分片中
            # tool_calls 的 null 字段归一为省略键，规避 dsh-llm-deepseek
            # 适配器把工具名/ID 覆盖为空导致 `unknown tool ""` 的问题。
            # 配置与凭证均被 watcher 热加载，切换后新会话即生效、免重启。
            dsh_backup_path = config_manager.backup_dsh_settings()
            config_manager.backup_dsh_credentials()
            dsh_existing = config_manager.read_dsh_settings()
            dsh_proxy = proxy_standalone.ensure_proxy_running()
            dsh_settings = config_manager.build_dsh_settings(
                dsh_existing, config.provider, config.model, config.max_tokens,
                proxy=dsh_proxy,
            )
            config_manager.atomic_write_dsh_settings(dsh_settings)
            creds = config_manager.build_dsh_credentials(
                config_manager.read_dsh_credentials(), api_key,
                proxy_token=dsh_proxy.get("token"),
            )
            config_manager.atomic_write_dsh_credentials(creds)
        if "zcode" in targets:
            # zcode 原生支持 anthropic / openai-compatible 两种 provider kind，
            # 直连上游、无需本地翻译代理；CLI 与桌面端两份配置都写（provider
            # 条目托管为 a4a_p<id>，model 格式 "<provider_id>/<model>"）。
            zcode_backup_path = config_manager.backup_zcode_configs().get("cli")
            cli_existing = config_manager.read_zcode_cli_config()
            v2_existing = config_manager.read_zcode_v2_config()
            zcode_cli, zcode_v2 = config_manager.build_zcode_settings(
                cli_existing, v2_existing, config.provider, api_key, config.model
            )
            config_manager.atomic_write_zcode_cli_config(zcode_cli)
            config_manager.atomic_write_zcode_v2_config(zcode_v2)
        if "pi" in targets:
            # pi 原生支持 anthropic-messages / openai-completions / openai-responses
            # 三种协议，直连上游、无需本地翻译代理：托管 provider 条目（a4a_p<id>）
            # 写 models.json，生效的 provider/model 写 settings.json。auth.json 是
            # pi 自己的凭证库（可能存 oauth 登录态），不参与切换。
            pi_backup_path = config_manager.backup_pi_configs().get("models")
            # 服务商是本机 llama-server 时，把服务实际的 -c 窗口同步给 pi：
            # pi 的模型条目缺省按 128000 计，不同步会让放大后的窗口被提前截断。
            pi_models, pi_settings = config_manager.build_pi_settings(
                config_manager.read_pi_models_config(),
                config_manager.read_pi_settings(),
                config.provider,
                api_key,
                config.model,
                context_window=llama_runtime.runtime().served_context_for(
                    config.provider.api_base, config.model
                ),
            )
            config_manager.atomic_write_pi_models_config(pi_models)
            config_manager.atomic_write_pi_settings(pi_settings)
        detail = "切换成功"
        if "codex" in targets:
            detail += "，Codex 配置已写入"
        if "dsh" in targets:
            detail += "，dsh 配置已写入"
        if "zcode" in targets:
            detail += "，ZCode 配置已写入"
        if "pi" in targets:
            detail += "，pi 配置已写入"
        crud.add_log(db, config_id, "success", detail)
    except Exception as e:
        logger.exception("切换配置「%s」失败", config.name)
        crud.add_log(db, config_id, "failed", str(e))
        raise HTTPException(500, f"切换失败：{e}")

    process_info = None
    restarted = False
    if body.restart and "claude" in targets:
        process_info = restart_claude()
        restarted = True
    elif "claude" in targets and not is_claude_running():
        process_info = {"killed": 0, "started": False, "detail": "未发现运行中的 Claude Code 进程"}

    message = "切换成功"
    if "codex" in targets:
        message += "；Codex 配置已写入（" + ("原生直连" if config.provider.native_responses else "经本地代理") + "），重启 Codex 后生效"
    if "dsh" in targets:
        message += "；dsh 配置已写入（经本地代理，热加载生效）"
    if "zcode" in targets:
        message += "；ZCode 配置已写入（直连上游）"
    if "pi" in targets:
        message += "；pi 配置已写入（直连上游），下次启动 pi 会话生效"
    return schemas.SwitchResult(
        success=True,
        message=message,
        backup_path=str(backup_path) if backup_path else None,
        codex_backup_path=str(codex_backup_path) if codex_backup_path else None,
        dsh_backup_path=str(dsh_backup_path) if dsh_backup_path else None,
        zcode_backup_path=str(zcode_backup_path) if zcode_backup_path else None,
        pi_backup_path=str(pi_backup_path) if pi_backup_path else None,
        restart=restarted,
        process_info=process_info,
    )
