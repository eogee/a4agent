"""会话记录（transcript）抽取「AI 最后输出的一段话」。

四家宿主的 JSONL 结构差异（均经 a4phone 实测确认）：
  Claude Code  type="assistant"，role 在 message.role，文本块 type="text"
  ZCode        hook 触发时的合成单行伪 transcript：结构同上但顶层无 type
  WorkBuddy    顶层 type="message" + role="assistant"，文本块 type="output_text"
  Codex        response_item 且 payload.type="message"（即时写入，首选）；
               event_msg/task_complete 兜底（晚 ~1.4s 写入，仅文件稳定时可用）
"""
import json
import logging
from pathlib import Path

from ..textclean import one_line, strip_host_markup

logger = logging.getLogger(__name__)

MAX_LENGTH = 1000  # 推送截断（中文约 3000 字节，留足 ntfy 单条上限余量）


def clamp_output(text: str | None) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    return cleaned[:MAX_LENGTH] + ("..." if len(cleaned) > MAX_LENGTH else "")


def extract_last_output(transcript_path: str | None) -> str | None:
    if not transcript_path:
        return None
    text = ""
    try:
        content = Path(transcript_path).read_text(encoding="utf-8")
    except OSError:
        return None
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue  # 非 JSON 行（如 Codex 的纯文本记录）跳过
        if not isinstance(o, dict):
            continue
        # Claude Code / ZCode 合成 transcript：assistant 消息的 text 块（多轮取最后一条）
        message = o.get("message")
        if (o.get("type") == "assistant" or (isinstance(message, dict) and message.get("role") == "assistant")) \
                and isinstance(message, dict) and isinstance(message.get("content"), list):
            parts = [b["text"] for b in message["content"]
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            if parts:
                text = "\n".join(parts)
        # WorkBuddy：顶层 type="message" + role="assistant"，块 type="output_text"
        if o.get("type") == "message" and o.get("role") == "assistant" and isinstance(o.get("content"), list):
            parts = [b["text"] for b in o["content"]
                     if isinstance(b, dict) and b.get("type") in ("output_text", "text") and b.get("text")]
            if parts:
                text = "\n".join(parts)
        # Codex：response_item 的 assistant 消息（随消息即时写入，Stop 时已可用）
        payload = o.get("payload")
        if o.get("type") == "response_item" and isinstance(payload, dict) \
                and payload.get("type") == "message" and payload.get("role") == "assistant":
            parts = [b["text"] for b in (payload.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "output_text" and b.get("text")]
            if parts:
                text = "\n".join(parts)
        # Codex 兜底：task_complete 自带最后一条 agent 消息（仅文件稳定时可用）
        if o.get("type") == "event_msg" and isinstance(payload, dict) \
                and payload.get("type") == "task_complete" and payload.get("last_agent_message"):
            text = payload["last_agent_message"]
    return clamp_output(text)


def resolve_last_output(input: dict) -> str | None:
    """ZCode 在 Stop 载荷直送 last_assistant_message（首选，免文件解析）；
    其余宿主无此字段，回退读 transcript 文件。"""
    return clamp_output(input.get("last_assistant_message")) or extract_last_output(
        input.get("transcript_path"))


def _user_text(o: dict) -> str | None:
    """单条 JSONL 记录里的用户消息文本；不是用户消息返回 None。"""
    message = o.get("message")
    # Claude Code / ZCode：type=user，role 在 message.role；content 可为文本或块列表
    if o.get("type") == "user" or (isinstance(message, dict) and message.get("role") == "user"):
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict)
                         and b.get("type") in ("text", "input_text") and b.get("text")]
                if parts:
                    return "\n".join(parts)
    # WorkBuddy：顶层 type="message" + role="user"
    if o.get("type") == "message" and o.get("role") == "user" and isinstance(o.get("content"), list):
        parts = [b.get("text", "") for b in o["content"] if isinstance(b, dict)
                 and b.get("type") in ("input_text", "output_text", "text") and b.get("text")]
        if parts:
            return "\n".join(parts)
    # Codex：response_item 且 payload.type="message"、role="user"
    payload = o.get("payload")
    if o.get("type") == "response_item" and isinstance(payload, dict) \
            and payload.get("type") == "message" and payload.get("role") == "user":
        parts = [b.get("text", "") for b in (payload.get("content") or [])
                 if isinstance(b, dict) and b.get("type") in ("input_text", "text") and b.get("text")]
        if parts:
            return "\n".join(parts)
    return None


def strip_injected(text: str) -> str:
    """剥掉宿主注入的上下文块与 @ 占位符，剩真实用户输入（空串=整条都是注入）。

    规则本身在 app.textclean：通知链的任务侧（phone.notifier）也要用同一份，
    放这里会被反向 import 打环。
    """
    return strip_host_markup(text)


def extract_first_prompt(transcript_path: str | None, max_length: int = 80,
                         max_bytes: int | None = None) -> str | None:
    """transcript 里第一条用户消息——hook 侧会话没有标题，拿它当「话题名称」。

    工具结果也挂在 user 名下（tool_result 块无 text），按块类型自然滤掉；
    WorkBuddy/Codex 会在首条消息注入上下文（system-reminder/AGENTS.md 等），
    剥离后为空说明整条都是注入（真话在后面几条），继续往下找；
    WorkBuddy 前端的附件/技能/长文本占位符（@image#1:…、@skill:"…"、
    @long-text:"…"）同样剥掉——它们不是用户原话，进通知标题就是一堆标签；
    ZCode 的合成伪 transcript 只有 assistant 消息，抽不到返回 None，由调用方退回目录名。

    max_bytes 限定只读文件头：会话雷达在提醒热路径上按需取标题，不能整份读大文件
    （首条用户消息必然在文件最前面，读 256KB 足够；半行会被 JSON 解析丢弃，不影响结果）。
    解码用 replace：宿主日志可能出现被截断的 UTF-8 字节，宁可少一个字也不要整条失败。

    返回前压成单行（标题带换行会在 toast/推送里断成几行）。
    """
    if not transcript_path:
        return None
    try:
        with Path(transcript_path).open("rb") as fh:
            raw = fh.read(max_bytes) if max_bytes else fh.read()
    except OSError:
        return None
    content = raw.decode("utf-8", errors="replace")
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict):
            continue
        text = _user_text(o)
        if not text or not text.strip():
            continue
        title = one_line(strip_injected(text), max_length)
        if not title:
            continue
        return title
    return None
