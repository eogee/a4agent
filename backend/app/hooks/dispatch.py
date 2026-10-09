"""Hook 分发器：按 hook_event_name 路由到三类处理器。

对应 a4phone 的 hook.mjs。返回 hook 输出 dict 或 None（None = 不干预，
走终端默认流程）。桌面提醒在分发层统一经队列代发——ZCode 会在 hook 命令
退出时杀掉整棵进程树，hook 内直接弹窗来不及渲染。
"""
import logging

from ..phone import config as phone_config
from . import deskqueue, handlers

logger = logging.getLogger(__name__)

AGENT_NAMES = {"codex": "Codex", "zcode": "ZCode", "qoder": "Qoder",
               "workbuddy": "WorkBuddy"}

# WorkBuddy 宿主 60s 硬超时，超时脚本被直接终止：外出模式的作答等待收窄到
# 45s，留出写回与传输余量，否则手机端已作答而会话无反应（静默失效）
WORKBUDDY_HOOK_TIMEOUT_S = 60
WORKBUDDY_SAFE_WAIT_S = WORKBUDDY_HOOK_TIMEOUT_S - 15


def agent_name(agent: str) -> str:
    return AGENT_NAMES.get(agent, "Claude Code")


def resolve_wait_seconds(agent: str, hook_cfg: dict) -> int:
    configured = int(hook_cfg.get("timeout", 60))
    if agent != "workbuddy":
        return configured
    return max(15, min(configured, WORKBUDDY_SAFE_WAIT_S))


def dispatch(input: dict, agent: str, cfg: dict | None = None) -> dict | None:
    cfg = cfg or phone_config.load()
    hook_cfg = cfg.get("hook", {})
    event = input.get("hook_event_name")
    is_out = hook_cfg.get("mode") == "out"
    name = agent_name(agent)
    wait_seconds = resolve_wait_seconds(agent, hook_cfg)
    tool_name = input.get("tool_name") or ""
    # 桌面横幅总开关关时不入队；手机推送不受影响，两条通道独立
    desktop_on = cfg.get("desktop", True)

    if event == "Stop":
        handlers.handle_stop(input, name, cfg, desktop_on=desktop_on)
        return None

    if event == "PreToolUse":
        # Claude Code 的提问工具叫 AskUserQuestion；Codex 的叫 request_user_input
        is_ask = tool_name == "AskUserQuestion" or (agent == "codex" and tool_name == "request_user_input")
        if is_ask:
            if desktop_on:
                deskqueue.queue_notify(name, "有提问需要处理")
            if not is_out:
                return None  # 终端优先：不阻塞
            return handlers.handle_ask_user_question(input, name, agent, wait_seconds, cfg)

    if event == "PermissionRequest":
        # ZCode 对一次提问同时触发 PreToolUse 和 PermissionRequest：提问提醒已由
        # PreToolUse 分支负责，这里跳过弹窗避免重复通知与误导性文案
        if tool_name != "AskUserQuestion" and desktop_on:
            deskqueue.queue_notify(name, "有权限请求需要处理")
        if not is_out:
            return None  # 终端优先：不阻塞
        return handlers.handle_permission_request(input, name, wait_seconds, cfg)

    return None


def run_hook_cli(args: list) -> int:
    """`a4agent.exe hook [agent]` 入口：stdin 读载荷，stdout 写决策。"""
    import json
    import sys

    try:
        raw = sys.stdin.read()
        input_payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return 0  # 载荷不可解析 = 不干预
    if not isinstance(input_payload, dict):
        return 0
    agent = args[0] if args and args[0] in AGENT_NAMES else "claude"
    try:
        result = dispatch(input_payload, agent)
    except Exception:  # noqa: BLE001 - hook 任何异常都不能卡宿主流程
        logger.exception("hook 处理异常")
        return 0
    if result:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0
