"""ntfy 推送：HTTP POST JSON，零第三方依赖（与 updater.py 同走 urllib）。

publish 永不抛异常——推送失败只影响这一条提醒，绝不能拖累任务线程与调用方。
"""
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 8.0
MAX_MESSAGE = 4000  # ntfy 单条消息上限，超长由服务端拒绝；提前截断保推送可达


def publish(cfg: dict, title: str, message: str, timeout: float = HTTP_TIMEOUT,
            actions: list | None = None) -> tuple[bool, str]:
    """推送一条通知，返回 (是否成功, 失败原因)。actions 为 ntfy 按钮定义
    （如手机作答的 http 回执按钮），普通通知不传。"""
    if not cfg.get("topic"):
        return False, "话题未配置"
    body = {
        "topic": cfg["topic"],
        "title": title[:500],
        "message": (message or "")[:MAX_MESSAGE],
    }
    if actions:
        body["actions"] = actions
    headers = {"Content-Type": "application/json"}
    token = cfg.get("token")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = Request(cfg["server"] + "/", data=json.dumps(body).encode("utf-8"),
                      headers=headers, method="POST")
        with urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except HTTPError as e:
        reason = f"HTTP {e.code}"
        if e.code == 403:
            reason += "（令牌无效或话题无写权限）"
        elif e.code == 429:
            reason += "（触发免费服务限速，请降低推送频率）"
        logger.warning("手机通知推送失败：%s", reason)
        return False, reason
    except (URLError, OSError) as e:
        logger.warning("手机通知推送失败：%s", e)
        return False, str(e)
