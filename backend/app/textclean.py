"""宿主写进用户消息的占位标记清理（通知标题/话题名用）。

宿主会把附件、技能引用、长文本引用折叠成 @ 占位符写进消息体，后台任务
事件还整块塞 <task-notification>。这些都不是用户原话，原样进通知标题就是
一堆标签（实测 134 个会话里 35 个标题被 @ 标签污染，另有若干标题混入
<image_local_path> 的绝对路径）。

白名单原则（沿用 hooks.transcript 的约定）：只认宿主保留名，不做通用 XML
剥离——用户贴 HTML/XML 很常见（<iframe>、<html>、自定义 <eogee> 标签），
误吃用户的正文比少剥一层更糟。

本模块零依赖，给 hooks（标题抽取）和 phone（通知组装）共用，避免
hooks → phone 的单向依赖被反向 import 打环。
"""
import re

# ---- 块级注入：配对标签整段移除（各家实测样本见注释） ----
INJECTED_TAG_BLOCKS = (
    "system-reminder",      # WorkBuddy / Claude Code：<system-reminder data-role=...>
    "user_instructions",    # Codex
    "environment_context",  # Codex：<environment_context>…日期/文件系统…
    "INSTRUCTIONS",         # Codex：# AGENTS.md instructions 标题下的 <INSTRUCTIONS> 块
    "task-notification",    # WorkBuddy：后台任务事件（<task-id>/<status>/<summary>）
    "long_text_quote",      # WorkBuddy：@long-text 引用的完整正文
    "image_local_path",     # WorkBuddy：附件在本机的绝对路径
)

# ---- 行级注入：无正文语义的告示行，整行移除 ----
INJECTED_LINE_PATTERNS = (
    re.compile(r"^# AGENTS\.md instructions[^\n]*(?:\n|$)", re.MULTILINE),  # Codex 标题行
    re.compile(r"^Caveat:[^\n]*(?:\n|$)", re.MULTILINE),  # Claude Code 本地命令告示前缀
    # WorkBuddy 后台任务事件的两行英文告示（跟随 <task-notification> 之后）
    re.compile(r"^Use the TaskOutput tool with task_id=[^\n]*(?:\n|$)", re.MULTILINE),
    re.compile(r"^\[SYSTEM NOTIFICATION - NOT USER INPUT\][^\n]*(?:\n|$)", re.MULTILINE),
)

# WorkBuddy 把真实提问包在 <user_query> 壳里挂在注入块之后：只剥壳留内容
_USER_QUERY_SHELL = re.compile(r"</?user_query\b[^>]*>")

# ---- @ 占位符：宿主前端写入的附件/引用标记，不是用户打的字 ----
# @image#1:xxx.png / @image#1:"带 空格 的名字.png" → 直接删除
_IMAGE_TOKEN = re.compile(r'@image#\d+\s*:\s*(?:"[^"\n]*"|[^\s"\n]+)\s*')
# @long-text:"预览…"（连字符形态在版本间变过，下划线也认）→ 保留预览文字
_LONG_TEXT_TOKEN = re.compile(r'@long[-_]text\s*:\s*"([^"\n]*)"\s*')
# @skill:"C:\…\xxx\SKILL.md" → 保留技能目录名
_SKILL_TOKEN = re.compile(r'@skill\s*:\s*"([^"\n]*)"\s*')
# @"C:/…/录音 (3).txt" 这类本地文件/相对路径引用 → 删除
_QUOTED_REF = re.compile(r'@"[^"\n]*"\s*')

_SKILL_FILE = "skill.md"


def skill_name(path: str) -> str:
    """`…\\video-cover\\SKILL.md` → `video-cover`；解析不出返回空串。"""
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if parts and parts[-1].lower() == _SKILL_FILE:
        parts.pop()
    return parts[-1] if parts else ""


def strip_host_markup(text: str) -> str:
    """剥掉宿主注入的上下文块与 @ 占位符，剩真实用户输入；整条都是注入时返回空串。"""
    if not text:
        return ""
    for tag in INJECTED_TAG_BLOCKS:
        text = re.sub(rf"<{tag}\b[^>]*>.*?</{tag}>", "", text,
                      flags=re.DOTALL | re.IGNORECASE)
    for pat in INJECTED_LINE_PATTERNS:
        text = pat.sub("", text)
    text = _USER_QUERY_SHELL.sub("", text)
    # 先处理带捕获的（保留可读内容），再删纯占位符
    text = _LONG_TEXT_TOKEN.sub(lambda m: m.group(1).rstrip(".．…") + " ", text)
    text = _SKILL_TOKEN.sub(lambda m: (skill_name(m.group(1)) + " "), text)
    text = _IMAGE_TOKEN.sub("", text)
    text = _QUOTED_REF.sub("", text)
    return text.strip()


def one_line(text: str, max_length: int = 80) -> str:
    """压成单行并截断：通知标题带换行会在 toast/推送里断成几行，读起来很碎。"""
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    return cleaned[:max_length] + ("..." if len(cleaned) > max_length else "")


def clean_title(text: str, max_length: int = 80) -> str:
    """通知标题：先剥宿主占位符，再压单行截断。幂等，已清洗过的话题名再过一遍无变化。"""
    return one_line(strip_host_markup(text), max_length)
