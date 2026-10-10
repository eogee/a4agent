"""OpenCode 服务事件监听：会话终局提醒 + 权限请求转推手机批办。

在「会话交互」表里注册 OpenCode = 开启本监听，移除 = 停止——与其他六端的
hook 注册同权同位。但监听是 a4agent 侧的订阅（SSE），**不写宿主任何文件**：
OpenCode 没有 hook 协议，它把同类能力做成了服务事件 + permission reply API
（实测见 docs/OpenCode-实测记录.md）：

- 终局提醒  session.execution.succeeded/failed/interrupted → 与任务终态同一条
  桌面横幅 + 手机推送链路（task_notify）。a4agent 自己下发的会话已在
  opencode_client.mark_dispatched 登记，这里跳过——下发任务另有任务终态通知，
  不去重用户会收到两条。
- 权限批办  permission.asked → 外出模式推手机（批准 / 总是允许 / 拒绝三个
  按钮，ntfy 的 3 按钮上限恰好够用）；手机作答由本模块的响应 Watcher 代收，
  POST /api/session/{id}/permission/{rid}/reply {"decision": ...} 回给服务，
  会话随即继续。终端优先模式只发桌面横幅、手机不参与——与六端语义一致
  （此时由 OpenCode 自己的界面展示待批提示）。permission.replied 表示用户
  已在 OpenCode 界面里亲自批复，这里清掉待办，迟到的手机作答按已失效处理。

自愈：注册开关、service.json 密码都在独立循环里周期复查（30s）——没装
OpenCode（无密码）静默待机，装好自动拉起；EventStream 断线自带指数退避
重连。旧版「OpenCode 接入」卡片的 opencode.json（enabled=true）首次启动
迁移成注册标记后删除。
"""
import json
import logging
import threading
import time

from . import opencode_client, task_notify
from .hooks import deskqueue, response
from .phone import config as phone_config
from .phone import ntfy
from .phone.notifier import format_notice

logger = logging.getLogger(__name__)

_SCAN_INTERVAL_SECONDS = 30.0
_PENDING_TTL_SECONDS = 6 * 3600  # 待批条目寿命上限：OpenCode 界面不会无限期挂着

_stop = threading.Event()
_thread: threading.Thread | None = None
_stream = None
_stream_lock = threading.Lock()

# 待批权限：request_id(a4agent 生成) -> {session_id, permission_id, base, password, ts}
_pending: dict = {}
_pending_lock = threading.Lock()
_watcher_alive = False


# ---------------- 注册开关（会话交互表格行的后端） ----------------


def listen_enabled() -> bool:
    return bool(phone_config.load().get("hook", {}).get("opencode"))


def set_listen(enabled: bool) -> None:
    cfg = phone_config.load()
    hook = dict(cfg.get("hook") or {})
    hook["opencode"] = bool(enabled)
    cfg["hook"] = hook
    phone_config.save(cfg)


def migrate_legacy_enabled() -> None:
    """旧「OpenCode 接入」卡片勾过「启用接入」的，迁移成注册标记后清掉遗留配置。"""
    from .database import get_data_dir

    legacy = get_data_dir() / "opencode.json"
    try:
        raw = json.loads(legacy.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return
    try:
        if isinstance(raw, dict) and raw.get("enabled") and not listen_enabled():
            set_listen(True)
    finally:
        try:
            legacy.unlink()
        except OSError:
            pass


# ---------------- 生命周期 ----------------


def start() -> None:
    """应用启动时调用：拉起监听守护线程（幂等）。永不抛错——通知是尽力而为。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    migrate_legacy_enabled()
    _stop.clear()
    _thread = threading.Thread(target=_lifecycle_loop, daemon=True,
                               name="a4agent-oc-listen")
    _thread.start()


def stop() -> None:
    """应用退出时调用（幂等）。"""
    _stop.set()
    _stop_stream()


def _stop_stream() -> None:
    global _stream
    with _stream_lock:
        if _stream is not None:
            try:
                _stream.stop()
            except Exception:  # noqa: BLE001
                pass
            _stream = None


def _lifecycle_loop() -> None:
    """注册开关 / 凭据变化的复查循环：流本身断线自连，这里只管换血。

    - 未注册 → 不订阅（移除后通知立刻静默，与六端卸载 hook 同效）
    - 没装 OpenCode（读不到 service.json）→ 静默待机，装好自动拉起
    - 密码变化（OpenCode 重装/重置）→ 用新凭据重建流
    """
    global _stream
    while not _stop.is_set():
        try:
            if not listen_enabled():
                _stop_stream()
            else:
                base, password = opencode_client.resolve_connection()
                with _stream_lock:
                    stale = (_stream is not None
                             and (_stream.password != password or _stream.base_url != base))
                if stale:
                    _stop_stream()
                if password:
                    with _stream_lock:
                        if _stream is None:
                            _stream = opencode_client.EventStream(base, password, _on_event)
                            _stream.start()
                            logger.info("已订阅 OpenCode 事件流：%s", base)
                else:
                    _stop_stream()
        except Exception:  # noqa: BLE001 - 监听任何异常都不拖垮应用
            logger.exception("OpenCode 监听循环异常")
        _stop.wait(_SCAN_INTERVAL_SECONDS)
    _stop_stream()


# ---------------- 事件路由 ----------------


_OC_STATUS_BY_EVENT = {
    "session.execution.succeeded": "success",
    "session.execution.failed": "failed",
    "session.execution.interrupted": "cancelled",
}


def _on_event(payload: dict) -> None:
    typ = str(payload.get("type") or "")
    raw = payload.get("data")
    data: dict = raw if isinstance(raw, dict) else {}
    status = _OC_STATUS_BY_EVENT.get(typ)
    if status:
        _handle_execution(status, data)
    elif typ == "permission.asked":
        _handle_permission_asked(data)
    elif typ == "permission.replied":
        _handle_permission_replied(data)


def _handle_execution(status: str, data: dict) -> None:
    session_id = str(data.get("sessionID") or "")
    if not session_id or opencode_client.is_dispatched(session_id):
        return  # a4agent 下发的会话由任务终态通知，不重复推
    info: dict = {}
    last_output = ""
    try:
        base, password = opencode_client.resolve_connection()
        try:
            info = opencode_client.get_session(session_id, base, password)
        except opencode_client.OpenCodeError:
            pass
        # 会话标题开场即定死；「AI 最后输出」得另拉消息流（与六端 Stop 通知
        # 同语义）。拉不到就退回只推标题，不影响提醒本身。
        last_output = opencode_client.last_assistant_text(
            opencode_client.list_messages(session_id, base, password))
    except opencode_client.OpenCodeError:
        pass
    task_notify.notify(
        {
            "id": f"oc:{session_id[-8:]}",
            "tool": "opencode",
            "status": status,
            "error": str(info.get("error") or ""),
            "prompt": (str(info.get("title") or "OpenCode 会话"))[:80],
            "session_id": session_id,
            "last_output": last_output,
        }
    )


# ---------------- 权限批办 ----------------


def _pending_snapshot() -> dict:
    with _pending_lock:
        now = time.time()
        for rid in [r for r, v in _pending.items() if now - v["ts"] > _PENDING_TTL_SECONDS]:
            _pending.pop(rid, None)
        return dict(_pending)


def _pending_pop(request_id: str):
    with _pending_lock:
        return _pending.pop(request_id, None)


def _pending_pop_by_permission(permission_id: str):
    with _pending_lock:
        for rid, v in list(_pending.items()):
            if v["permission_id"] == permission_id:
                return _pending.pop(rid)
    return None


def _handle_permission_asked(data: dict) -> None:
    cfg = phone_config.load()
    permission_id = str(data.get("id") or "")
    session_id = str(data.get("sessionID") or "")
    # 桌面横幅两条模式都发（与六端一致）；话题名称取会话标题，取不到用兜底词
    if cfg.get("desktop", True):
        topic = "OpenCode 会话"
        try:
            base, password = opencode_client.resolve_connection()
            info = opencode_client.get_session(session_id, base, password)
            topic = str(info.get("title") or "").strip() or topic
        except opencode_client.OpenCodeError:
            pass
        deskqueue.queue_notify(*format_notice(topic, "权限申请", "OpenCode"))
    if not (cfg.get("enabled") and cfg.get("topic")
            and cfg.get("hook", {}).get("mode") == "out"):
        return
    if not permission_id or not session_id:
        return
    base, password = opencode_client.resolve_connection()
    action = str(data.get("action") or "tool")
    resources = [str(r) for r in (data.get("resources") or []) if str(r).strip()]
    detail = "\n".join(resources[:5]) or action
    request_id = response.new_request_id()
    url = f"{cfg['server']}/{cfg['topic']}-response"

    def _btn(label: str, reply: str) -> dict:
        return {"action": "http", "label": label, "url": url, "method": "POST",
                "clear": False,
                "body": json.dumps({"requestId": request_id, "reply": reply})}

    sent, _ = ntfy.publish(
        cfg, f"OpenCode: {action}", detail,
        actions=[_btn("批准", "once"), _btn("总是允许", "always"), _btn("拒绝", "reject")])
    if not sent:
        return  # 推送失败 → 由 OpenCode 自己的界面兜底
    with _pending_lock:
        _pending[request_id] = {"session_id": session_id, "permission_id": permission_id,
                                "base": base, "password": password, "ts": time.time()}
    _ensure_watcher()


def _handle_permission_replied(data: dict) -> None:
    """用户在 OpenCode 界面里自己批了：清待办，迟到的手机作答按已失效处理。"""
    permission_id = str(data.get("requestID") or "")
    if permission_id:
        _pending_pop_by_permission(permission_id)


def _ensure_watcher() -> None:
    """待批非空时保证响应 Watcher 活着；空转后自动退出，下一条再拉起。"""
    global _watcher_alive
    with _pending_lock:
        if _watcher_alive:
            return
        _watcher_alive = True
    threading.Thread(target=_response_watcher, daemon=True,
                     name="a4agent-oc-reply").start()


def _response_watcher() -> None:
    """代收手机作答：轮询 {topic}-response，命中待批 request_id 就回给服务。

    与 hooks.response.wait_for_response 的短周期轮询同款（5s，since 重叠窗口），
    但没有宿主超时硬约束——OpenCode 的待批提示一直挂着，作答窗口只受 TTL 约束。
    """
    global _watcher_alive
    try:
        while _pending_snapshot():
            cfg = phone_config.load()
            since = int(time.time()) - _SINCE_OVERLAP_SECONDS
            deadline = time.monotonic() + response.POLL_CYCLE_SECONDS
            _poll_once(cfg, since)
            remain = deadline - time.monotonic()
            if remain > 0:
                _stop.wait(remain)
    except Exception:  # noqa: BLE001 - Watcher 异常只影响待批条目，不拖垮应用
        logger.exception("OpenCode 手机作答Watcher 异常")
    finally:
        with _pending_lock:
            _watcher_alive = False


# since 重叠窗口：批复消息在窗口内会被多轮重放，pending 弹出天然幂等；
# 窗口太窄（5s）时长轮询换挡间隙可能漏掉刚发布的作答，批复永远到不了服务
_SINCE_OVERLAP_SECONDS = 15


def _poll_once(cfg: dict, since: int) -> None:
    """拉一轮响应话题；命中待批就调 reply API。单轮失败静默，下轮补上。"""
    from urllib.request import Request, urlopen

    if not cfg.get("topic"):
        return
    headers = {"User-Agent": "a4agent-oc-listen/1.0"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    url = f"{cfg['server']}/{cfg['topic']}-response/json?since={since}"
    try:
        req = Request(url, headers=headers)
        with urlopen(req, timeout=response.POLL_CYCLE_SECONDS) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") != "message" or event.get("message") is None:
                    continue
                _match_reply(str(event["message"]))
    except Exception:  # noqa: BLE001 - 网络失败下一轮重试
        pass


def _match_reply(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except ValueError:
        return
    if not isinstance(msg, dict) or not msg.get("requestId"):
        return  # 自由文本没有 requestId，无法映射成 once/always/reject，忽略
    entry = _pending_pop(str(msg["requestId"]))
    if entry is None:
        return
    decision = str(msg.get("reply") or "")
    if decision not in ("once", "always", "reject"):
        decision = "reject"  # 未知回执宁可拒绝，不能误放行
    try:
        opencode_client.api(
            "POST",
            f"/api/session/{entry['session_id']}/permission/{entry['permission_id']}/reply",
            {"decision": decision},
            base_url=entry["base"], password=entry["password"], timeout=30)
    except opencode_client.OpenCodeError as e:
        # 批复没送达（服务没起/网络抖动）：放回待办，重叠窗口的重放会再试；
        # 超过重试上限就放弃——权限可能已在 OpenCode 界面被处理
        entry["attempts"] = entry.get("attempts", 0) + 1
        if entry["attempts"] < 3:
            with _pending_lock:
                _pending[str(msg["requestId"])] = entry
        logger.warning("OpenCode 权限批复未送达（第 %s 次）：%s",
                       entry["attempts"], e)
