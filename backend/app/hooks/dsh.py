"""DSH 事件处理：把 DSH 进程内插件上报的事件转成手机交互。

与 handlers.py（外部 hook 协议）的关系：复用同一批底层原语
（response.new_request_id / wait_for_response、ntfy.publish、deskqueue），
但输出契约完全不同——

  handlers  返回 Claude Code 协议的 hookSpecificOutput，由宿主解析；
  本模块    返回 DSH 的原生契约（answers 数组 / ApprovalOutcome 字符串），
            插件直接把它作为 waterfall 的返回值，无需再翻译。

DSH 没有「外部 hook 命令」协议，插件跑在宿主进程内，只能通过 HTTP 回调
a4agent 常驻服务。请求挂住直到手机作答或超时（长轮询），超时/推送失败/
终端优先一律返回 None，由插件放行 DSH 原生交互——与 a4phone 的
phone-hooks.mjs 回退策略一致。
"""
import json
import logging

from ..phone import config as phone_config
from ..phone import ntfy
from . import deskqueue, response

logger = logging.getLogger(__name__)

# DSH ask_user_question 的单次作答等待上限（秒）。取配置里的 hook.timeout，
# 再加一点传输余量：插件侧 HTTP 超时必须大于服务端等待，否则插件先断、
# 服务端还在等，白等一轮。
DSH_HTTP_GRACE_S = 15


def _response_url(cfg: dict) -> str:
    return f"{cfg['server']}/{cfg['topic']}-response"


def wait_seconds(cfg: dict) -> int:
    return int(cfg.get("hook", {}).get("timeout", 60))


def http_timeout(cfg: dict) -> int:
    """插件 HTTP 读超时：需覆盖服务端等待 + 桌面弹窗代发开销。"""
    return wait_seconds(cfg) + DSH_HTTP_GRACE_S


def phone_active(cfg: dict) -> bool:
    """外出模式 + 已配置话题才拦截；否则放行原生交互（不阻塞终端）。"""
    return cfg.get("hook", {}).get("mode") == "out" and bool(cfg.get("topic"))


# ---------------- 任务完成 ----------------

def handle_task_complete(payload: dict) -> None:
    """turn/end 且 reason=completed：桌面弹窗 + 手机推送（含 AI 最后输出）。"""
    cfg = phone_config.load()
    desktop_on = cfg.get("desktop", True)
    session_id = payload.get("sessionId") or ""
    short_id = session_id[:8] if session_id else "—"

    if desktop_on:
        deskqueue.queue_notify("DSH", "任务已完成")
    if not cfg.get("topic"):
        return

    turn = payload.get("turn")
    details = f"任务已完成\n会话: {short_id}" + (f"\n轮次: {turn}" if turn is not None else "")
    last_output = payload.get("lastOutput") or ""
    message = f"{details}\n\nAI 最后输出：\n{last_output}" if last_output else details
    ntfy.publish(cfg, "DSH", message)


# ---------------- 提问作答 ----------------

def handle_ask_user_question(payload: dict) -> dict | None:
    """拦截 ask_user_question：推手机点选，返回 DSH 的 { answers: [...] }。

    返回 None 表示放行 DSH 原生提问（终端优先 / 推送失败 / 手机超时）。
    无选项的提问无法手机作答，直接跳过该题，避免推送后干等超时回退。
    """
    cfg = phone_config.load()
    if cfg.get("desktop", True):
        deskqueue.queue_notify("DSH", "有提问需要处理")
    if not phone_active(cfg):
        return None

    questions = payload.get("questions") or []
    if not questions:
        return None

    wait = wait_seconds(cfg)
    answers: list[dict] = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        options = [o for o in (q.get("options") or []) if isinstance(o, dict)]
        if not options:
            continue
        request_id = response.new_request_id()
        labels = [str(o.get("label") or "") for o in options]
        # ntfy 最多 3 个按钮（超过返回 HTTP 400）：>3 降级为编号列表 + 文字作答
        use_actions = len(options) <= 3
        actions = None
        if use_actions:
            actions = [{
                "action": "http", "label": label,
                "url": _response_url(cfg), "method": "POST", "clear": False,
                "body": json.dumps({"requestId": request_id, "answer": label}),
            } for label in labels]

        lines = [str(q.get("question") or "Question")]
        lines += [f"{i + 1}. {label}" for i, label in enumerate(labels)]
        lines.append("")
        lines.append(
            f"（也可直接向话题 {cfg['topic']}-response 发送文字作答）" if use_actions
            else f"（选项较多，请回复编号如「3」，或向话题 {cfg['topic']}-response 发送文字作答）")
        lines.append(f"（如未订阅响应话题，可浏览器打开 {_response_url(cfg)} 直接回复）")
        if not use_actions:
            logger.info("DSH 提问选项 %d 个超 ntfy 按钮上限(3)，已降级为文字作答", len(options))

        title = f"DSH: {q.get('header') or 'Question'}"
        sent, _ = ntfy.publish(cfg, title, "\n".join(lines), actions=actions)
        if not sent:
            return None  # 推送失败 → 回退原生

        resp = response.wait_for_response(cfg, request_id, wait)
        if not isinstance(resp, dict) or not resp.get("answer"):
            return None  # 手机超时 → 回退原生

        answer = str(resp["answer"])
        if not use_actions:
            # 降级场景手机可能回编号（如「3」），映射回选项 label；自由文本原样作答
            try:
                idx = int(answer.strip())
                if 1 <= idx <= len(labels):
                    answer = labels[idx - 1]
            except (TypeError, ValueError):
                pass
        item: dict = {"id": q.get("id"), "selected": [answer]}
        if resp.get("custom"):
            item["custom"] = resp["custom"]
        answers.append(item)

    return {"answers": answers} if answers else None


# ---------------- 权限审批 ----------------

def handle_permission_request(payload: dict) -> str | None:
    """拦截 approval/request：推手机 Approve/Deny，返回 DSH ApprovalOutcome。

    DSH 的取值契约是 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'，
    手机点选映射为前两者。返回 None 表示放行原生审批链。
    """
    cfg = phone_config.load()
    if cfg.get("desktop", True):
        deskqueue.queue_notify("DSH", "有权限请求需要处理")
    if not phone_active(cfg):
        return None

    tool_name = str(payload.get("toolName") or "Unknown")
    message = str(payload.get("reason") or f"工具 {tool_name} 请求权限")
    request_id = response.new_request_id()
    url = _response_url(cfg)

    def _action(label: str, approved: bool) -> dict:
        return {"action": "http", "label": label, "url": url, "method": "POST",
                "clear": False, "body": json.dumps({"requestId": request_id,
                                                    "approved": approved})}

    actions = [_action("Approve", True), _action("Deny", False)]
    sent, _ = ntfy.publish(cfg, f"DSH: {tool_name}", message, actions=actions)
    if not sent:
        return None

    resp = response.wait_for_response(cfg, request_id, wait_seconds(cfg))
    if not isinstance(resp, dict):
        return None  # 手机超时 → 回退原生
    return "rejected" if resp.get("approved") is False else "allowed-once"
