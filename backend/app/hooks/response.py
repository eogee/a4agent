"""ntfy 响应话题订阅：手机作答（按钮回执 / 自由文字）的等待与匹配。

手机按钮点击 → ntfy App 对按钮配置的 http action 发 POST，正文
{requestId, answer|approved|alwaysAllow}，发布到 {topic}-response；
自由文字 → 同话题纯文本消息。订阅用短周期轮询（since 重叠窗口）而非
单条长连接：hook 等待受宿主超时硬约束（WorkBuddy 60s），长连接在流静默
时 readline 会阻塞整个 socket 超时、突破等待上限；轮询把时延上界压到
单周期（5s），since 重叠保证不丢消息。
"""
import json
import logging
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

POLL_CYCLE_SECONDS = 5.0


def new_request_id() -> str:
    return str(uuid.uuid4())


def _parse_message(raw: str):
    """JSON 对象原样返回；JSON 标量/非 JSON 文本按原始文本返回（作答语义）。"""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else raw
    except ValueError:
        return raw


def wait_for_response(cfg: dict, request_id: str, timeout_s: float) -> dict | None:
    """等待 {topic}-response 上的作答；requestId 匹配或自由文本（无 requestId 且
    非空 answer）即返回消息对象，超时/网络失败返回 None。"""
    if not cfg.get("topic"):
        return None
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    since = int(time.time()) - 5  # 重叠窗口，覆盖重连间隙
    headers = {"User-Agent": "a4agent-hook/1.0"}
    if cfg.get("token"):
        headers["Authorization"] = f"Bearer {cfg['token']}"
    while time.monotonic() < deadline:
        remain = deadline - time.monotonic()
        url = f"{cfg['server']}/{cfg['topic']}-response/json?since={since}"
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=max(1.0, min(POLL_CYCLE_SECONDS, remain))) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    # open/keepalive 等控制事件没有 message 字段，跳过
                    if event.get("event") != "message" or event.get("message") is None:
                        continue
                    parsed = _parse_message(str(event["message"]))
                    msg = parsed if isinstance(parsed, dict) else {"answer": parsed}
                    if msg.get("requestId") == request_id:
                        return msg
                    if "requestId" not in msg and msg.get("answer"):
                        return msg
        except (HTTPError, URLError, TimeoutError, OSError):
            pass  # 单周期失败（含读超时）静默重连，since 保证不丢
        since = int(time.time()) - 5
    return None
