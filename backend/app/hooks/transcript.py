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
import re
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


# 宿主注入上下文块白名单：配对标签整段移除（各家实测样本见下）。
# 不做通用 XML 剥离——用户第一条消息贴代码/贴 XML 很常见，白名单只认
# 宿主保留名，避免把用户自己的内容当注入误吃。
_INJECTED_TAG_BLOCKS = (
    "system-reminder",      # WorkBuddy / Claude Code：<system-reminder data-role=...>
    "user_instructions",    # Codex
    "environment_context",  # Codex：<environment_context>…日期/文件系统…
    "INSTRUCTIONS",         # Codex：# AGENTS.md instructions 标题下的 <INSTRUCTIONS> 块
)
# 无正文语义的注入行（标题/告示），整行移除
_INJECTED_LINE_PATTERNS = (
    re.compile(r"^# AGENTS\.md instructions[^\n]*\n", re.MULTILINE),  # Codex 标题行
    re.compile(r"^Caveat:[^\n]*\n", re.MULTILINE),  # Claude Code 本地命令告示前缀
)
# WorkBuddy 把真实提问包在 <user_query> 壳里挂在注入块之后：只剥壳留内容
_USER_QUERY_SHELL = re.compile(r"</?user_query\b[^>]*>")


def strip_injected(text: str) -> str:
    """剥掉宿主注入的上下文块，剩真实用户输入；整条都是注入时返回空串。"""
    for tag in _INJECTED_TAG_BLOCKS:
        text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text,
                      flags=re.DOTALL | re.IGNORECASE)
    for pat in _INJECTED_LINE_PATTERNS:
        text = pat.sub("", text)
    return _USER_QUERY_SHELL.sub("", text).strip()


def extract_first_prompt(transcript_path: str | None, max_length: int = 80) -> str | None:
    """transcript 里第一条用户消息——hook 侧会话没有标题，拿它当「话题名称」。

    工具结果也挂在 user 名下（tool_result 块无 text），按块类型自然滤掉；
    WorkBuddy/Codex 会在首条消息注入上下文（system-reminder/AGENTS.md 等），
    剥离后为空说明整条都是注入（真话在后面几条），继续往下找；
    ZCode 的合成伪 transcript 只有 assistant 消息，抽不到返回 None，由调用方退回目录名。
    """
    if not transcript_path:
        return None
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
            continue
        if not isinstance(o, dict):
            continue
        text = _user_text(o)
        if not text or not text.strip():
            continue
        cleaned = strip_injected(text)
        if not cleaned:
            continue
        return cleaned[:max_length] + ("..." if len(cleaned) > max_length else "")
    return None
