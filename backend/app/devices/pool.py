"""发现广播设备池（CrossDevice 接口契约 §19.2 口径，M4/B12/B31/D38）。

UDP 51889 监听线程：收到 XDCAST 广播帧后按契约校验
（数据报 ≤512B、UTF-8 单行 JSON、magic=="XDCAST"、proto==1、device_id 1–12 位
十六进制），任何校验失败静默丢弃——广播来源不可信，不告警防日志洪泛。
按 (源IP, device_id) 每秒最多接受 1 帧（限频去重，超出不计数不刷新池）；
30 秒无帧判离线。

设备池是纯内存结构：广播帧是**未认证信息**（D38），只进"发现池"（待配对列表），
绝不写入已配对存储、绝不自动建立信任。绑定失败只记日志（a4agent 其余功能照常），
不落盘、不跨进程。
"""
import json
import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)

BROADCAST_PORT = 51889          # 发现广播端口（契约 §19.1，仅 UDP）
MAX_FRAME_BYTES = 512           # 帧上限（§19.2）
OFFLINE_AFTER_SECONDS = 30.0    # 30 秒无帧判离线（D25）
RATE_LIMIT_SECONDS = 1.0        # (源IP, device_id) 每秒最多 1 帧（B31）

_MAGIC = "XDCAST"
_PROTO = 1
_HEX = set("0123456789abcdefABCDEF")
_RECV_BUF = 4096                # 比 512B 大，超长报文按超长处理（丢弃）

ENTRY_KEYS = ("device_id", "ip", "hostname", "port", "version", "caps",
              "first_seen", "last_seen", "frames")


def parse_frame(data, source_ip):
    """校验并解析一帧广播（契约 §19.2）。任何校验失败返回 None（静默丢弃）。

    校验清单即契约全量清单：≤512B / UTF-8 单行 JSON / magic / proto /
    device_id 1–12 位十六进制。报文其余字段（hostname/port/version/caps/ts）
    只做宽化取值，不作为信任依据（D38：不落库为凭据）。
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_FRAME_BYTES:
        return None
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\n" in text or "\r" in text:
        return None
    try:
        frame = json.loads(text)
    except ValueError:
        return None
    if not isinstance(frame, dict):
        return None
    if frame.get("magic") != _MAGIC or frame.get("proto") != _PROTO:
        return None
    device_id = frame.get("device_id")
    if not isinstance(device_id, str) or not 1 <= len(device_id) <= 12:
        return None
    if any(c not in _HEX for c in device_id):
        return None
    port = frame.get("port")
    hostname = frame.get("hostname")
    version = frame.get("version")
    caps = frame.get("caps")
    return {
        "device_id": device_id,
        "ip": source_ip,
        "hostname": hostname if isinstance(hostname, str) else "",
        "port": port if isinstance(port, int) and 0 < port < 65536 else 0,
        "version": version if isinstance(version, str) else "",
        "caps": [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else [],
    }


class DevicePool:
    """设备池（内存结构，线程安全）。独立于监听线程，便于单测。"""

    def __init__(self, now_fn=time.time):
        self._lock = threading.Lock()
        self._entries = {}          # device_id -> 条目
        self._last_frame_ts = {}    # (源IP, device_id) -> 上次接受帧的时刻
        self._now = now_fn

    def accept(self, frame, source_ip):
        """接受一帧（frame 已过 parse_frame 校验）。限频去重后刷新条目。

        返回 True 表示本帧被接受并刷新了池；False 表示被限频丢弃
        （不计数、不刷新池，契约 §19.2）。
        """
        if not frame:
            return False
        device_id = frame["device_id"]
        now = self._now()
        key = (source_ip, device_id)
        with self._lock:
            last = self._last_frame_ts.get(key)
            if last is not None and now - last < RATE_LIMIT_SECONDS:
                return False
            self._last_frame_ts[key] = now
            entry = self._entries.get(device_id)
            if entry is None:
                entry = {
                    "device_id": device_id,
                    "ip": source_ip,
                    "hostname": frame.get("hostname", ""),
                    "port": frame.get("port", 0),
                    "version": frame.get("version", ""),
                    "caps": list(frame.get("caps", [])),
                    "first_seen": now,
                    "last_seen": now,
                    "frames": 0,
                }
                self._entries[device_id] = entry
            else:
                entry["ip"] = source_ip
                entry["hostname"] = frame.get("hostname", entry["hostname"])
                entry["port"] = frame.get("port", entry["port"])
                entry["version"] = frame.get("version", entry["version"])
                entry["caps"] = list(frame.get("caps", entry["caps"]))
                entry["last_seen"] = now
            entry["frames"] += 1
            return True

    def snapshot(self, now=None):
        """池快照：按首次出现时间升序，附 online 标注（30 秒无帧判离线）。"""
        now = self._now() if now is None else now
        with self._lock:
            entries = [dict(e) for e in self._entries.values()]
        for e in entries:
            e["online"] = (now - e["last_seen"]) <= OFFLINE_AFTER_SECONDS
        entries.sort(key=lambda e: (e["first_seen"], e["device_id"]))
        return entries

    def find(self, device_id, now=None):
        """按 device_id 查条目（供配对登记时取 hostname 等线索）。"""
        now = self._now() if now is None else now
        with self._lock:
            entry = self._entries.get(device_id)
            if entry is None:
                return None
            out = dict(entry)
            out["online"] = (now - out["last_seen"]) <= OFFLINE_AFTER_SECONDS
            return out

    def forget_before(self, ts):
        """清理 last_seen 早于 ts 的条目（内存池防无界增长，仅供测试/维护）。"""
        with self._lock:
            stale = [k for k, v in self._entries.items() if v["last_seen"] < ts]
            for k in stale:
                del self._entries[k]
            for k in [k for k in self._last_frame_ts if self._last_frame_ts[k] < ts]:
                del self._last_frame_ts[k]
            return len(stale)


class ManualTargets:
    """手动添加的连接线索（广播不可达时的补充入口）。

    与设备池同等地位：只是"连接线索"，不构成任何信任；配对仍走 §23 一次确认。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}    # "ip:port" -> {"ip", "port", "added_at", "label"}

    def add(self, ip, port, label=""):
        ip = str(ip).strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError("端口需要是整数")
        if not 0 < port < 65536:
            raise ValueError("端口超出范围")
        try:
            socket.inet_aton(ip)
        except OSError as e:
            raise ValueError(f"IP 地址无效：{ip}") from e
        key = f"{ip}:{port}"
        with self._lock:
            if key not in self._items:
                self._items[key] = {"ip": ip, "port": port,
                                    "added_at": time.time(), "label": label}
        return key

    def remove(self, ip, port):
        key = f"{ip}:{int(port)}"
        with self._lock:
            return self._items.pop(key, None) is not None

    def list(self):
        with self._lock:
            items = [dict(v) for v in self._items.values()]
        items.sort(key=lambda i: i["added_at"])
        return items


class PoolListener:
    """UDP 51889 监听线程（守护线程，SO_REUSEADDR，绑定 0.0.0.0）。

    绑定失败只记 WARNING 并保持 bound=False（端口被其他程序占用时
    a4agent 其余功能不受影响；设备页状态里如实标注）。
    """

    def __init__(self, pool):
        self._pool = pool
        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self.bound = False
        self.bind_error = ""

    def start(self):
        if self._thread is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", BROADCAST_PORT))
            sock.settimeout(0.5)
            self._sock = sock
            self.bound = True
        except OSError as e:
            self.bind_error = str(e)
            logger.warning("发现广播监听绑定 UDP %s 失败（端口可能被其他程序占用）：%s",
                           BROADCAST_PORT, e)
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="a4agent-device-pool")
        self._thread.start()
        logger.info("发现广播监听已启动（UDP %s）", BROADCAST_PORT)

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(_RECV_BUF)
            except socket.timeout:
                continue
            except OSError:
                break   # 停止时 close 引发，属正常
            frame = parse_frame(data, addr[0])
            if frame is None:
                continue    # 校验失败静默丢弃（§19.2，不告警防日志洪泛）
            self._pool.accept(frame, addr[0])

    def stop(self):
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None
        self._thread = None
        self.bound = False


class Discovery:
    """发现服务 = 广播设备池 + 手动目标（api 层的全局单例）。"""

    def __init__(self):
        self.pool = DevicePool()
        self.manual = ManualTargets()
        self.listener = PoolListener(self.pool)

    def start(self):
        self.listener.start()

    def stop(self):
        self.listener.stop()

    def snapshot(self):
        return {"entries": self.pool.snapshot(),
                "manual": self.manual.list(),
                "listener": {"bound": self.listener.bound,
                             "port": BROADCAST_PORT,
                             "error": self.listener.bind_error}}


discovery = Discovery()


def start_listener():
    """应用启动时调用（幂等）。"""
    discovery.start()
