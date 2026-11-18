"""A 侧状态拉取：每 15 秒读一次已配对设备的 node 暂存状态目录。

不新增监听端口：拉取复用设备互联现有的 /v1/info、/v1/fs/list、/v1/fs/read。
状态来源是 B 侧 hook 写入的 <staging>/status/<session>.json；读取失败按
设备离线/无状态处理，不重试轰炸，也不影响其他设备。
"""
import logging
import os
import threading
import time
from urllib.parse import quote

from .. import win_toast
from ..phone import config as phone_config
from ..phone import ntfy
from . import node_client, status_schema, status_store
from .store import get_store

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = status_schema.POLL_INTERVAL_SECONDS
CLEANUP_EVERY_TICKS = 40
STAGING_TTL_SECONDS = 300


class StatusWatcher:
    """常驻拉取线程（守护线程；幂等启动，一拍一查）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._staging = {}       # device name -> (path, cached_at)
        self._last_error = {}    # device name -> message
        self._ticks = 0

    # ---------- 生命周期 ----------

    def start(self):
        """幂等启动守护轮询线程。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="a4agent-status-watch")
            self._thread.start()
            return self

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        while not self._stop_event.wait(POLL_INTERVAL_SECONDS):
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 状态拉取失败不影响主流程
                logger.exception("设备状态拉取单拍异常")

    # ---------- 单拍 ----------

    def _tick(self):
        devices = get_store().list()
        cfg = phone_config.load()
        for entry in devices:
            name = str(entry.get("name") or "")
            try:
                self._poll_device(entry, cfg)
                with self._lock:
                    self._last_error.pop(name, None)
            except Exception as exc:  # noqa: BLE001 - 单设备失败不阻塞其他设备
                with self._lock:
                    self._last_error[name] = str(exc)
                logger.debug("设备 %s 状态拉取失败：%s", name, exc)
        self._ticks += 1
        if self._ticks % CLEANUP_EVERY_TICKS == 0:
            try:
                status_store.cleanup()
            except Exception:  # noqa: BLE001 - 清理失败不影响拉取
                logger.exception("设备状态清理失败")

    def _poll_device(self, entry: dict, cfg: dict) -> None:
        name = str(entry.get("name") or "")
        token = get_store().token_of(name)
        if not token:
            return
        client = node_client.NodeClient(
            entry.get("ip"), int(entry.get("port") or 51888), token,
            entry.get("cert_fingerprint") or None)
        staging = self._staging_path(client, name)
        if not staging:
            return
        # 先列 staging 根：目录不存在时 node 会返回 500，这里用根目录判断
        # 是否已出现 status 子目录，避免把「还没上报过」当成错误。
        root = client.request(
            "GET", "/v1/fs/list?path=" + quote(staging), timeout=10)
        status_dir = next(
            (e.get("path") for e in (root.get("entries") or [])
             if e.get("name") == status_schema.STATUS_DIR_NAME and e.get("dir")),
            None)
        if not status_dir:
            return
        listing = client.request(
            "GET", "/v1/fs/list?path=" + quote(status_dir), timeout=10)
        files = [e for e in (listing.get("entries") or [])
                 if isinstance(e, dict) and not e.get("dir")
                 and str(e.get("name") or "").endswith(".json")]
        files.sort(key=lambda e: e.get("mtime") or 0, reverse=True)
        for item in files[:status_schema.MAX_SESSIONS_PER_PULL]:
            path = item.get("path") or os.path.join(
                status_dir, str(item.get("name") or ""))
            try:
                data = client.request(
                    "GET", "/v1/fs/read?path=" + quote(path) +
                    "&max=" + str(status_schema.MAX_FILE_BYTES), timeout=10)
            except node_client.NodeError:
                continue  # 文件在列举后被清理：跳过本次
            payload = status_schema.parse_payload(data.get("text") or "")
            if payload is None:
                continue
            changed, should_notify = status_store.upsert(name, payload)
            if changed and should_notify:
                self._notify(name, entry, payload, cfg)
                status_store.mark_notified(
                    name, payload["session_id"], payload["seq"])

    def _staging_path(self, client, name: str):
        with self._lock:
            cached = self._staging.get(name)
        if cached and time.time() - cached[1] < STAGING_TTL_SECONDS:
            return cached[0]
        info = client.request("GET", "/v1/info", timeout=10)
        staging = info.get("workspace_staging")
        if staging:
            with self._lock:
                self._staging[name] = (staging, time.time())
            return staging
        return None

    def _notify(self, name: str, entry: dict, payload: dict, cfg: dict) -> None:
        event_label = status_schema.EVENT_LABELS.get(
            payload["event"], payload["event"])
        title = f"设备 {name}"
        message = f"{entry.get('hostname') or name}：{event_label}"
        if payload.get("summary"):
            message += f"\n{payload['summary']}"
        if cfg.get("desktop", True):
            try:
                win_toast.show(title, message[:256])
            except Exception:  # noqa: BLE001 - 提醒失败不影响入库
                logger.warning("设备状态桌面提醒失败", exc_info=True)
        if cfg.get("enabled") and cfg.get("topic"):
            try:
                ntfy.publish(cfg, title, message)
            except Exception:  # noqa: BLE001 - 推送失败不影响入库
                logger.warning("设备状态手机推送失败", exc_info=True)

    def errors(self) -> dict:
        """供 API 展示的最近一次拉取错误（按设备名）。"""
        with self._lock:
            return dict(self._last_error)


_watcher = None   # 惰性单例（应用启动时 start，一次即可）


def get_watcher() -> StatusWatcher:
    global _watcher
    if _watcher is None:
        _watcher = StatusWatcher()
    return _watcher


def start() -> StatusWatcher:
    """应用启动入口：幂等拉起常驻拉取线程。"""
    return get_watcher().start()


def stop() -> None:
    get_watcher().stop()
