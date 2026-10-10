"""通知配置：手机推送（话题/服务器/事件开关）与桌面横幅总开关，
落运行时数据目录 phone_config.json。

话题名即凭据（知晓者即可向用户手机推送），随机生成、可一键重置；
访问令牌仅自建 ntfy / 开启鉴权时需要，明文存数据目录（目录本身已收 ACL 权限）。

通知分两条独立通道：
  手机推送  ntfy → App，事件与话题/令牌/推送时机绑定
  桌面横幅  Win32 toast，由 a4agent 常驻进程代发，总开关 desktop
两条通道互不依赖，可各自关闭。
"""
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ..database import get_data_dir

DEFAULT_SERVER = "https://ntfy.sh"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

# 推送时机开关：取消是用户自己按的，默认不推；其余终态默认都推
DEFAULT_EVENTS = {"success": True, "failed": True, "timeout": True, "cancelled": False}

# 会话交互（hook）默认值：终端优先不阻塞；外出模式等手机作答 60s、计划审批 300s
DEFAULT_HOOK: dict = {"mode": "home", "timeout": 60, "plan_timeout": 300}

# 桌面横幅总开关。默认开：任务下发的桌面提醒是既有行为，不因新增开关而消失；
# 这里管的是hook 链路的桌面弹窗（提问 / 权限请求 / 任务完成）。
DEFAULT_DESKTOP = True


def config_path() -> Path:
    return get_data_dir() / "phone_config.json"


def new_topic() -> str:
    """随机话题名：a4ag- 前缀与 a4phone（a4p-）区分，避免双装用户推送混杂。"""
    return f"a4ag-{secrets.token_hex(5)}"


def _normalize(raw: dict) -> dict:
    server = str(raw.get("server") or "").strip().rstrip("/") or DEFAULT_SERVER
    topic = str(raw.get("topic") or "").strip()
    token = str(raw.get("token") or "").strip()
    enabled = bool(raw.get("enabled", False))
    raw_events = raw.get("events")
    events_in = raw_events if isinstance(raw_events, dict) else {}
    events = {k: bool(events_in.get(k, dft)) for k, dft in DEFAULT_EVENTS.items()}
    raw_hook = raw.get("hook")
    hook_in = raw_hook if isinstance(raw_hook, dict) else {}
    hook = {
        "mode": "out" if hook_in.get("mode") == "out" else "home",
        "timeout": _clamp_int(hook_in.get("timeout"), DEFAULT_HOOK["timeout"], 15, 600),
        "plan_timeout": _clamp_int(hook_in.get("plan_timeout"), DEFAULT_HOOK["plan_timeout"], 30, 1800),
        # OpenCode 服务事件监听的注册位（会话交互表第七行）：无 hook 协议的端
        # 注册时不写宿主文件，只翻转这个开关（语义见 opencode_listen 模块头）
        "opencode": bool(hook_in.get("opencode")),
    }
    # 桌面横幅独立开关（顶层字段，与 enabled/events 同级）：只有显式 False 才关，
    # 缺字段的老配置一律按默认开，不能因为新增开关让既有用户静默
    desktop = raw.get("desktop")
    desktop = DEFAULT_DESKTOP if desktop is None else bool(desktop)
    return {"enabled": enabled, "server": server, "topic": topic,
            "token": token, "events": events, "hook": hook, "desktop": desktop}


def _clamp_int(value, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def load() -> dict:
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
        data = raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        data = {}
    return _normalize(data)


def save(cfg: dict) -> dict:
    """规范化并原子写配置，返回落盘后的完整配置。"""
    data = _normalize(cfg)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".phone_config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return data


def ensure_topic(cfg: dict) -> dict:
    """首次使用时生成话题并落盘，保证展示给用户的二维码稳定不变。"""
    if cfg.get("topic"):
        return cfg
    cfg["topic"] = new_topic()
    return save(cfg)


def validate(server: str, topic: str = "") -> str:
    """返回错误原因，空串表示通过。服务器必查；话题仅在非空时查（空由 ensure_topic 补）。"""
    parsed = urlparse(server or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"服务器地址需要 http(s) URL：{server}"
    if topic and not TOPIC_RE.match(topic):
        return f"话题名只能含字母数字、下划线、连字符，长度 3–64：{topic}"
    return ""


def subscribe_url(cfg: dict) -> str:
    return f"{cfg['server']}/{cfg['topic']}"


def qr_payload(cfg: dict) -> str:
    """二维码内容用 ntfy 深链（App 原生识别，与 a4phone 一致）。"""
    host = urlparse(cfg["server"]).netloc
    return f"ntfy://{host}/{cfg['topic']}"
