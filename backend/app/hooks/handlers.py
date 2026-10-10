"""三类 hook 事件的处理器：提问作答 / 权限审批 / 任务完成通知。

语义与 a4phone 逐条对齐（含实测坑）：
  提问   Claude 用 updatedInput.answers 注入；Codex 无注入能力，阻断工具调用
         并把答案写进 permissionDecisionReason 让模型直接采用
  审批   Approve/Deny/Always Approve；ExitPlanMode 用计划审批上限
  完成   桌面弹窗（经队列代发）+ 手机推送（含 AI 最后输出）+ 记录最近会话
"""
import json
import logging
import time
from pathlib import Path

from ..database import get_data_dir
from ..phone import ntfy
from ..phone.notifier import format_notice
from . import deskqueue, response
from .transcript import extract_first_prompt, resolve_last_output

logger = logging.getLogger(__name__)

MESSAGE_MAX_LENGTH = 1000


def _response_url(cfg: dict) -> str:
    return f"{cfg['server']}/{cfg['topic']}-response"


# ---------------- 提问作答 ----------------

def build_ask_output(agent: str, questions: list, answers: dict) -> dict:
    if agent == "codex":
        lines = [f"{i + 1}. {q} → {a}" for i, (q, a) in enumerate(answers.items())]
        reason = ("用户在手机端回答了以下提问，请直接采用这些答案继续当前任务，"
                  "不要再调用 request_user_input 工具：\n" + "\n".join(lines))
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }}
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {"questions": questions, "answers": answers},
    }}


def handle_ask_user_question(input: dict, agent_name: str, agent: str,
                             wait_seconds: int, cfg: dict) -> dict | None:
    questions = (input.get("tool_input") or {}).get("questions") or []
    if not cfg.get("topic") or not questions:
        return None
    answers: dict = {}
    for q in questions:
        options = q.get("options") or []
        if not options:
            continue
        request_id = response.new_request_id()
        # ntfy 最多 3 个按钮（超过 HTTP 400 推送失败）：>3 降级编号列表 + 文字作答
        use_actions = len(options) <= 3
        actions = None
        if use_actions:
            actions = [{
                "action": "http", "label": o.get("label", ""),
                "url": _response_url(cfg), "method": "POST", "clear": False,
                "body": json.dumps({"requestId": request_id, "answer": o.get("label", "")}),
            } for o in options]
        lines = [q.get("question", "")]
        lines += [f"{i + 1}. {o.get('label', '')}" for i, o in enumerate(options)]
        lines.append("")
        lines.append(f"（也可直接向话题 {cfg['topic']}-response 发送文字作答）"
                     if use_actions else
                     f"（选项较多，请回复编号如「3」，或向话题 {cfg['topic']}-response 发送文字作答）")
        lines.append(f"（如未订阅响应话题，可浏览器打开 {_response_url(cfg)} 直接回复）")
        sent, _ = ntfy.publish(cfg, f"{agent_name}: {q.get('header') or 'Question'}",
                               "\n".join(lines), actions=actions)
        if not sent:
            return None  # 推送失败 → 回退终端
        resp = response.wait_for_response(cfg, request_id, wait_seconds)
        if not isinstance(resp, dict) or not resp.get("answer"):
            return None  # 手机超时 → 回退终端
        answer: str = str(resp.get("answer") or "")
        if not use_actions:
            # 降级场景手机可能回编号（如「3」），映射回选项 label；自由文本原样作答
            try:
                idx = int(answer.strip())
                if 1 <= idx <= len(options):
                    answer = str(options[idx - 1].get("label") or answer)
            except (TypeError, ValueError):
                pass
        answers[q.get("question", "")] = answer
    return build_ask_output(agent, questions, answers)


# ---------------- 权限审批 ----------------

def _format_permission_message(tool_name: str, tool_input) -> str:
    tool_input = tool_input or {}
    if tool_name == "ExitPlanMode" and isinstance(tool_input.get("plan"), str):
        message = tool_input["plan"].strip() or "(empty plan)"
    elif tool_name == "Bash":
        message = tool_input.get("command") or json.dumps(tool_input, ensure_ascii=False)
    elif tool_name in ("Read", "Write", "Edit"):
        message = tool_input.get("file_path") or json.dumps(tool_input, ensure_ascii=False)
    else:
        message = json.dumps(tool_input, ensure_ascii=False)
    return message[:MESSAGE_MAX_LENGTH] + ("..." if len(message) > MESSAGE_MAX_LENGTH else "")


def handle_permission_request(input: dict, agent_name: str, wait_seconds: int,
                              cfg: dict) -> dict | None:
    if not cfg.get("topic"):
        return None
    # AskUserQuestion 的交互由提问处理器负责，这里直接放行，避免冗余的原始 JSON 推送
    if input.get("tool_name") == "AskUserQuestion":
        return {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }}

    tool_name = input.get("tool_name") or "Unknown"
    request_id = response.new_request_id()
    url = _response_url(cfg)

    def _action(label: str, body: dict) -> dict:
        return {"action": "http", "label": label, "url": url,
                "method": "POST", "clear": False, "body": json.dumps(body)}

    actions = [_action("Approve", {"requestId": request_id, "approved": True}),
               _action("Deny", {"requestId": request_id, "approved": False})]
    suggestions = input.get("permission_suggestions") or []
    if suggestions:
        actions.insert(1, _action("Always Approve", {
            "requestId": request_id, "approved": True, "alwaysAllow": True}))

    sent, _ = ntfy.publish(cfg, f"{agent_name}: {tool_name}",
                           _format_permission_message(tool_name, input.get("tool_input")),
                           actions=actions)
    if not sent:
        return None

    resp = response.wait_for_response(cfg, request_id, wait_seconds)
    if not isinstance(resp, dict):
        return None  # 手机超时 → 回退终端
    decision: dict = {"behavior": "deny" if resp.get("approved") is False else "allow"}
    if resp.get("alwaysAllow") is True and suggestions:
        decision["updatedPermissions"] = suggestions
    return {"hookSpecificOutput": {
        "hookEventName": "PermissionRequest", "decision": decision,
    }}


# ---------------- 任务完成通知 ----------------

def last_session_path() -> Path:
    return get_data_dir() / "last_session.json"


def _save_last_session(input: dict, agent_name: str) -> None:
    """记录最近会话，供后续「会话续聊」使用（幂等覆盖写）。"""
    session_id = input.get("session_id")
    if not session_id:
        return
    try:
        data = {
            "session_id": session_id,
            "cwd": input.get("cwd") or "",
            "agent": agent_name,
            "transcript_path": input.get("transcript_path") or "",
            "ts": int(time.time() * 1000),
        }
        last_session_path().write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    except OSError:
        logger.warning("最近会话记录写入失败", exc_info=True)


def session_topic(input: dict) -> str:
    """会话的话题名称（统一样式第 1 行）：transcript 第一条用户消息，
    取不到退回项目目录名，再退回空串（format_notice 兜底「a4agent 通知」）。"""
    topic = extract_first_prompt(input.get("transcript_path"))
    if topic:
        return topic
    return Path((input.get("cwd") or "").strip()).name


def handle_stop(input: dict, agent_name: str, cfg: dict, desktop_on: bool = True) -> None:
    """Stop 事件：桌面横幅 + 手机推送，统一样式（话题名称/已完成/应用名）。
    Stop Hook 无需输出。"""
    _save_last_session(input, agent_name)
    if input.get("_resumed"):
        return None  # 续聊子进程的结果已由续聊链路推回手机，不重复推送
    topic = session_topic(input)
    if desktop_on:
        deskqueue.queue_notify(*format_notice(topic, "已完成", agent_name))
    if cfg.get("topic"):
        last_output = resolve_last_output(input) or ""
        ntfy.publish(cfg, *format_notice(topic, "已完成", agent_name,
                                         last_output, include_last_output=True))
    return None
