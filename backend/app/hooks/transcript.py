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
