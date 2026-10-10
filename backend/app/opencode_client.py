"""OpenCode HTTP 客户端：连接、会话生命周期与事件订阅。

**为什么用 HTTP 而不是拉 CLI 子进程**：OpenCode 本来就跑着一个常驻后台服务
（本机 127.0.0.1:49374），直接对接它的 API 比每次新起一个 CLI 进程
更贴合真实形态，且换来三样 CLI 给不了的能力：

- **会话级免审批**：POST /api/session 接受 permissions 规则，无头任务因此不必
  改用户的全局配置（其它端只能靠各自的 --yolo 类开关）
- **结构化终局**：Session.Info 直接给 outcome / cost / tokens，失败原因在
  assistant 消息的 error 字段里（type + message），不必从退出码猜
- **可中断**：POST /api/session/{id}/interrupt 取消，DELETE 清会话

**鉴权**（实测确认，见 docs/OpenCode-实测记录.md）：HTTP Basic，用户名固定
`opencode`，密码来自 ~/.config/opencode/service.json，由本文件自动读取——
与其他端「装了就在」一致，没有任何连接配置界面。

**降级策略**：所有端点失败都不抛到调用方之外——连接问题统一以 OpenCodeError
返回，由调用方决定是标记引擎不可用还是如实落库成失败任务。
"""
from __future__ import annotations

import base64
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:49374"
SERVICE_JSON = "service.json"
CONNECT_TIMEOUT = 5.0
DEFAULT_TIMEOUT = 300.0
# 服务端消息保留上限远大于本工具产出文件；导出失败时回退到 /message
EXPORT_LIMIT = 4 * 1024 * 1024

# ---------------- a4agent 自己下发的会话登记 ----------------
# 事件订阅对服务上**所有**会话推终局，而下发任务已有任务终态通知；不登记去重
# 的话，同一次下发用户会收到两条推送。task_runner 建会话时登记，订阅回调查询。
_DISPATCHED_MAX = 1000
_dispatched: "OrderedDict[str, float]" = OrderedDict()
_dispatched_lock = threading.Lock()


def mark_dispatched(session_id: str) -> None:
    """登记一个 a4agent 下发的会话（有界，防长跑进程无限增长）。"""
    with _dispatched_lock:
        _dispatched[session_id] = time.time()
        while len(_dispatched) > _DISPATCHED_MAX:
            _dispatched.popitem(last=False)


def is_dispatched(session_id: str) -> bool:
    with _dispatched_lock:
        return session_id in _dispatched


class OpenCodeError(Exception):
    """对 OpenCode 服务的一切失败（连接、鉴权、协议）。"""


# ---------------- 连接配置 ----------------


def service_json_path() -> Path:
    return Path.home() / ".config" / "opencode" / SERVICE_JSON


def read_local_password() -> str:
    """本机模式：读 ~/.config/opencode/service.json 的明文密码；读不到返回空串。

    这是用户自己的服务注册文件，与 OpenCode 的正常行为一致（本机 `opencode api`
    也是走同一套凭据）。读不到不算错误，交由上层如实提示「未取得凭据」。
    """
    path = service_json_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    return str(data.get("password") or "") if isinstance(data, dict) else ""


def resolve_connection() -> tuple[str, str]:
    """返回 (base_url, password)：默认端口 + 本机 service.json 自动发现。"""
    return DEFAULT_BASE_URL, read_local_password()


# ---------------- 底层请求 ----------------


def _auth_header(password: str) -> str:
    token = base64.b64encode(f"opencode:{password}".encode()).decode()
    return f"Basic {token}"


def request(
    method: str,
    path: str,
    body: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    params: dict | None = None,
) -> tuple[int, str]:
    """发起一次请求，返回 (status, text)。仅在网络层失败时抛 OpenCodeError。

    HTTP 错误**不抛**：401/404/5xx 的响应体带结构化错误（UnauthorizedError 等），
    由调用方按状态码判断（如 experimental 端点缺失要降级而不是当致命故障）。
    """
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if password:
        headers["Authorization"] = _auth_header(password)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise OpenCodeError(f"无法连接 OpenCode 服务（{base_url}）：{e}") from e


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def api(
    method: str,
    path: str,
    body: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    params: dict | None = None,
):
    """请求并解析 JSON；非 2xx 抛 OpenCodeError（带服务端原始说明）。"""
    status, text = request(method, path, body, base_url, password, timeout, params)
    data = _json_or_none(text)
    if not (200 <= status < 300):
        reason = ""
        if isinstance(data, dict):
            reason = str(data.get("message") or data.get("error") or data.get("_tag") or "")
        raise OpenCodeError(f"HTTP {status}{('：' + reason) if reason else ''}")
    return data


# ---------------- 能力探测 ----------------


def server_info(base_url: str, password: str) -> dict:
    """GET /api/info：服务身份、地址与 capabilities。"""
    return api("GET", "/api/info", base_url=base_url, password=password, timeout=CONNECT_TIMEOUT) or {}


def probe(refresh: bool = False) -> dict:
    """探测 OpenCode 端可用性（探测结果缓存 5 分钟，与其它端一致）。

    不用 CLI 探测：本机 PATH 上未必有 opencode，而后台服务只要在跑就能连——
    这正是「对接运行中的实例」的意义。未装 OpenCode（读不到 service.json）
    或服务没起都如实反映在 error 里，装好即自愈，无需任何配置动作。
    """
    now = time.time()
    base, pwd = resolve_connection()
    cached = _probe_cache.get(base)
    if cached and not refresh and now - cached[0] < _PROBE_CACHE_SECONDS:
        return cached[1]
    info: dict = {
        "tool": "opencode",
        "label": "OpenCode",
        "installed": False,
        "command": base,
        "version": "",
        "error": "",
    }
    if not pwd:
        info["error"] = (
            "未检测到本机 OpenCode 服务（~/.config/opencode/service.json 不存在），"
            "启动 OpenCode 后自动可用"
        )
    else:
        try:
            data = server_info(base, pwd)
            info["installed"] = True
            info["version"] = str(data.get("version") or "")
            caps = data.get("capabilities")
            if isinstance(caps, dict) and caps:
                # capabilities 随版本增减，原样带出便于排查（实测 v2.0.26 无 persistentPty）
                info["capabilities"] = caps
        except OpenCodeError as e:
            info["error"] = f"无法连接 OpenCode 服务：{e}"
    _probe_cache[base] = (now, info)
    return info


_PROBE_CACHE_SECONDS = 300
_probe_cache: dict = {}


# ---------------- 会话生命周期 ----------------


def candidate_models(base_url: str = DEFAULT_BASE_URL, password: str = "",
                      limit: int = 3) -> list[dict]:
    """可尝试的无头模型候选（按服务端清单顺序，最多 limit 个）。

    **为什么不信任 enabled**：实测清单首项 exo-free 已被上游废弃，服务端仍标
    enabled=true，真跑才报 provider.invalid-request；清单顺序也不代表偏好。

    因此调用方应「先用服务端默认，失败再逐个试候选」：模型被拒是瞬时失败
    （不消耗 token），而任务因此整体不可用却是用户不可接受的。候选数封顶，
    避免在「全部模型都坏」时无谓地连跑很多次。
    """
    data = api("GET", "/api/model", base_url=base_url, password=password, timeout=CONNECT_TIMEOUT)
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    out: list[dict] = []
    if not isinstance(rows, list):
        return out
    for m in rows:
        if not isinstance(m, dict) or not m.get("enabled"):
            continue
        if not (m.get("providerID") and m.get("id")):
            continue
        out.append({"providerID": str(m["providerID"]), "id": str(m["id"])})
        if len(out) >= limit:
            break
    return out


# 服务端明确说了「这个模型不能用」——值得换下一个再试，而不是直接判定任务失败
_REJECTED_MODEL_MARKERS = ("deprecated", "invalid-request", "invalid_request",
                           "model_not_found", "not found")


def create_session(
    text: str,
    directory: str | None = None,
    model: dict | None = None,
    agent: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    password: str = "",
    title: str | None = None,
) -> str:
    """建会话并投递首条提示，返回 sessionID（毫秒级返回，不等执行完成）。

    permissions 一律给 allow-all：这是无头任务，无人在场审批。规则是**会话级**的
    （实测会话对象原样回显 permissions），不写进用户的全局配置，任务结束即失效。
    这与其它端必须用 --yolo / bypassPermissions 全局开关相比更干净。

    model 传 None 时用服务端默认。实测服务端默认模型可能已废弃而配置看不出
    异常，要不要换模型由调用方按 candidate_models 决定（见 task_runner 的重试）。
    """
    body: dict = {
        "title": title or (text[:60] or "a4agent 任务"),
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }
    if directory:
        body["location"] = {"directory": directory}
    if model:
        body["model"] = model
    if agent:
        body["agent"] = agent
    data = api("POST", "/api/session", body, base_url=base_url, password=password, timeout=60)
    info = (data or {}).get("data") if isinstance(data, dict) else None
    sid = (info or {}).get("id")
    if not sid:
        raise OpenCodeError("OpenCode 未返回 sessionID")
    send_prompt(sid, text, base_url=base_url, password=password)
    return str(sid)


def send_prompt(sid: str, text: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> None:
    """向已有会话追加一条提示（10ms 级返回，执行在后台）。"""
    api("POST", f"/api/session/{sid}/prompt", {"text": text},
        base_url=base_url, password=password, timeout=60)


def wait_idle(
    sid: str,
    base_url: str = DEFAULT_BASE_URL,
    password: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> bool:
    """POST /wait：阻塞到 agent loop 空闲（204）。

    实测失败会话也会立刻返回（终局即为空闲），因此返回值只表示「已空闲」，
    成功与否要再读 session 的 outcome。experimental 端点缺失时返回 False，
    交由调用方改用轮询降级路径。
    """
    try:
        status, _ = request("POST", f"/api/experimental/session/{sid}/wait", {},
                            base_url, password, timeout)
    except OpenCodeError:
        return False
    return status == 204


def get_session(sid: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> dict:
    data = api("GET", f"/api/session/{sid}", base_url=base_url, password=password, timeout=60)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def list_messages(sid: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> list[dict]:
    data = api("GET", f"/api/session/{sid}/message", base_url=base_url, password=password, timeout=60)
    if isinstance(data, dict):
        rows = data.get("data")
        return rows if isinstance(rows, list) else []
    return data if isinstance(data, list) else []


def export_session(sid: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> str:
    """导出完整会话转录（experimental）；端点缺失时回退到消息列表序列化。"""
    try:
        status, text = request("GET", f"/api/experimental/session/{sid}/export",
                               None, base_url, password, 120)
        if 200 <= status < 300:
            data = _json_or_none(text)
            if isinstance(data, dict) and data.get("data"):
                return json.dumps(data["data"], ensure_ascii=False, indent=2)
            return text
    except OpenCodeError:
        pass
    return json.dumps({"messages": list_messages(sid, base_url, password)},
                      ensure_ascii=False, indent=2)


def interrupt(sid: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> bool:
    """中断正在执行的会话。"""
    try:
        status, _ = request("POST", f"/api/session/{sid}/interrupt", {},
                            base_url, password, 30)
    except OpenCodeError:
        return False
    return 200 <= status < 300


def delete_session(sid: str, base_url: str = DEFAULT_BASE_URL, password: str = "") -> bool:
    """删除会话（递归删除子会话）。取消/超时后调用，避免会话堆积。"""
    try:
        status, _ = request("DELETE", f"/api/session/{sid}", None, base_url, password, 30)
    except OpenCodeError:
        return False
    return 200 <= status < 300


# ---------------- 产出解析 ----------------


def _iter_assistant(messages: list[dict]):
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "assistant":
            yield m


def parse_output(messages: list[dict], session: dict | None = None,
                 exit_code: int = 0, timed_out: bool = False) -> dict:
    """会话消息流 → 归一 {ok, text, error, usage}（与 task_engines 各端同形）。

    比其它端省事的地方：成败有三处独立信号可交叉验证——idle 消息的 outcome、
    Session.Info.outcome、assistant 消息的 finish/error。实测过 pi 那种
    「退出码 0 但自报失败」的情况，这里按 outcome 为准，不用猜。
    """
    usage = {"input": 0, "output": 0, "totalTokens": 0, "cost": 0.0}
    texts: list[str] = []
    errors: list[str] = []

    for m in _iter_assistant(messages):
        err = m.get("error")
        if isinstance(err, dict) and err.get("message"):
            errors.append(f"{err.get('type') or 'error'}：{err['message']}")
        for part in m.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "text":
                t = str(part.get("text") or "").strip()
                if t:
                    texts.append(t)

    outcome = ""
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "idle":
            outcome = str(m.get("outcome") or "")
            break
    if not outcome and isinstance(session, dict):
        outcome = str(session.get("outcome") or "")

    info = session or {}
    tokens = info.get("tokens")
    if isinstance(tokens, dict):
        usage["input"] = int(tokens.get("input") or 0)
        usage["output"] = int(tokens.get("output") or 0)
        usage["totalTokens"] = usage["input"] + usage["output"]
    try:
        usage["cost"] = float(info.get("cost") or 0.0)
    except (TypeError, ValueError):
        usage["cost"] = 0.0

    text = "\n\n".join(texts).strip()
    if timed_out:
        return {"ok": False, "text": text, "error": "任务超时，已中断 OpenCode 会话", "usage": usage}
    if outcome == "interrupted":
        return {"ok": False, "text": text, "error": "任务已被中断", "usage": usage}
    if errors:
        return {"ok": False, "text": text, "error": "；".join(errors[:3]), "usage": usage}
    if outcome == "failed":
        return {"ok": False, "text": text,
                "error": "OpenCode 会话失败" + ("（无文本产出）" if not text else ""), "usage": usage}
    if not text:
        return {"ok": False, "text": "", "error": "OpenCode 无文本产出", "usage": usage}
    if outcome != "succeeded":
        # 拿不到 outcome（experimental wait 不可用而走了轮询降级）时，
        # 有文本即视为成功，与其它端「有产出无错误信号」的判定一致
        return {"ok": True, "text": text, "error": "", "usage": usage}
    return {"ok": True, "text": text, "error": "", "usage": usage}


def last_assistant_text(messages: list[dict]) -> str:
    """最后一条带文本的 assistant 消息的文本——会话通知的「AI 最后输出」。

    与 parse_output 的 text（全部文本块拼接）不同：多轮会话里中间轮的
    过程叙述不算产出，手机上用户想看的是收尾那段话。
    """
    for m in reversed(list(_iter_assistant(messages))):
        parts = [str(p.get("text") or "").strip()
                 for p in (m.get("content") or [])
                 if isinstance(p, dict) and p.get("type") == "text"]
        parts = [t for t in parts if t]
        if parts:
            return "\n".join(parts)
    return ""


# ---------------- 事件订阅（SSE） ----------------


class EventStream:
    """GET /api/event 的 SSE 订阅线程。

    **实测的关键细节**：SSE 协议自身的 event: 字段是空的，事件类型在 data 里
    JSON 的 type 字段（如 session.execution.succeeded）。按 event: 分派会永远收不到。

    事件**绝不限流**：早期只为终局提醒时按 1 条/秒节流过，但权限批办走同一
    条流后，permission.asked 撞上 step 事件风暴会被静默丢弃，用户手机永远
    收不到审批推送。下游各自轻量，风暴放行无害。

    重连策略：指数退避 1s→2s→4s…上限 30s；回调抛异常不影响订阅线程存活。
    stop() 幂等，供应用退出调用。
    """

    def __init__(self, base_url: str, password: str, on_event):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.on_event = on_event
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="a4agent-oc-events")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._consume()
                backoff = 1.0  # 连上过就重置退避
            except OpenCodeError as e:
                logger.debug("OpenCode 事件流断开：%s", e)
            except Exception:  # noqa: BLE001 - 订阅线程必须活着
                logger.exception("OpenCode 事件流异常")
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _consume(self) -> None:
        req = urllib.request.Request(
            self.base_url + "/api/event",
            headers={"Accept": "text/event-stream",
                     **({"Authorization": _auth_header(self.password)} if self.password else {})},
        )
        with urllib.request.urlopen(req, timeout=None) as resp:
            buf = ""
            while not self._stop.is_set():
                chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    raise OpenCodeError("事件流已关闭")
                buf += chunk.decode("utf-8", "replace")
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    self._handle_block(block)

    def _handle_block(self, block: str) -> None:
        data = ""
        for line in block.splitlines():
            if line.startswith("data:"):
                data += line[5:].strip()
        if not data:
            return
        payload = _json_or_none(data)
        if not isinstance(payload, dict):
            return
        try:
            self.on_event(payload)
        except Exception:  # noqa: BLE001 - 单条回调失败不能拖垮订阅
            logger.exception("OpenCode 事件回调失败")