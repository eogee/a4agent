"""跨设备状态汇报的共享口径：B 侧写、A 侧拉。

状态文件是单条 JSON，字段最小化：不含令牌、证书指纹、完整对话内容；
workspace 只保留目录名，summary 由调用方保证是面向人的短摘要。
"""
import json
import ntpath
import re
import time
import uuid

SCHEMA_VERSION = 1
STATUS_DIR_NAME = "status"
POLL_INTERVAL_SECONDS = 15.0
STALE_AFTER_SECONDS = POLL_INTERVAL_SECONDS * 3
RETENTION_DAYS = 7
MAX_ROWS_PER_DEVICE = 1000
MAX_SESSIONS_PER_PULL = 20
MAX_SUMMARY = 200
MAX_TEXT = 500
MAX_FILE_BYTES = 64 * 1024

# 需要额外发一条人读提醒的关键事件（其余事件只入库，不打扰人）。
KEY_EVENTS = (
    "hook.stop",
    "hook.permission",
    "hook.question",
    "dsh.complete",
    "dsh.question",
    "dsh.approval",
)

EVENT_LABELS = {
    "hook.stop": "任务完成",
    "hook.permission": "权限申请",
    "hook.question": "提问",
    "dsh.complete": "DSH 任务完成",
    "dsh.question": "DSH 提问",
    "dsh.approval": "DSH 审批",
}

STATE_LABELS = {
    "done": "已完成",
    "waiting": "等待处理",
    "approved": "已批准",
    "denied": "已拒绝",
    "answered": "已回答",
    "failed": "失败",
}

_CTRL = re.compile(r"[\x00-\x1f\x7f]+")
_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]+")


def clean_text(value, limit=MAX_TEXT):
    """去控制字符、截断；只用于状态字段，不做任何业务解释。"""
    text = _CTRL.sub(" ", str(value or "")).strip()
    return text[:limit]


def workspace_name(value):
    """只保留目录名，避免把完整工作区路径带进状态与提醒。"""
    raw = clean_text(value, MAX_TEXT).rstrip("\\/")
    if not raw:
        return ""
    name = ntpath.basename(raw)
    return clean_text(name, 80)


def sanitize_filename(value, fallback="session"):
    """会话 ID 转安全文件名：杜绝 ..、斜杠与非法字符造成的路径穿越。"""
    raw = clean_text(value, 120)
    safe = _SAFE_NAME.sub("-", raw).replace("..", "-").strip(".-_")
    return (safe or fallback)[:120]


def build_payload(*, event, state, session_id="", agent="", workspace="",
                  summary="", seq=None, ts=None):
    """构造一条状态；seq 默认用纳秒时间戳，保证同一会话内单调递增。"""
    now = time.time()
    session = clean_text(session_id, 120)
    if not session:
        session = f"{clean_text(agent, 20) or 'agent'}-unknown"
    return {
        "schema": SCHEMA_VERSION,
        "session_id": session,
        "seq": int(seq if seq is not None else time.time_ns()),
        "event": clean_text(event, 50),
        "state": clean_text(state, 20),
        "ts": float(ts if ts is not None else now),
        "agent": clean_text(agent, 50),
        "workspace": workspace_name(workspace),
        "summary": clean_text(summary, MAX_SUMMARY),
    }


def parse_payload(raw):
    """解析并校验状态 JSON；非法或缺关键字段返回 None。

    容忍 UTF-8 BOM：其他工具（PowerShell、编辑器）写出的 JSON 可能带 BOM，
    不能因此把合法状态当成损坏文件丢掉。
    """
    try:
        if isinstance(raw, str):
            data = json.loads(raw.lstrip("\ufeff"))
        else:
            data = raw
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    session_id = clean_text(data.get("session_id"), 120)
    event = clean_text(data.get("event"), 50)
    state = clean_text(data.get("state"), 20)
    if not session_id or not event or not state:
        return None
    try:
        seq = int(data.get("seq") or 0)
        ts = float(data.get("ts") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "schema": SCHEMA_VERSION,
        "session_id": session_id,
        "seq": seq,
        "event": event,
        "state": state,
        "ts": ts,
        "agent": clean_text(data.get("agent"), 50),
        "workspace": workspace_name(data.get("workspace")),
        "summary": clean_text(data.get("summary"), MAX_SUMMARY),
    }


def labels(payload):
    """事件/状态的展示文案；未知值原样回退，避免页面出现空白。"""
    event = str(payload.get("event") or "")
    state = str(payload.get("state") or "")
    return {
        "event_label": EVENT_LABELS.get(event, event),
        "state_label": STATE_LABELS.get(state, state),
    }
