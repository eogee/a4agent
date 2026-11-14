"""已配对设备存储（M4/B10 DPAPI）。

a4agent 管理的控制端侧设备凭据（身份令牌、TOFU 证书指纹）经 crypto.py 的
Windows DPAPI 加密后落盘到 a4agent 自己的数据目录 devices.json；
令牌只在内存解密使用，任何接口与日志都不回传明文。

**决策声明（偏差留痕）**：被控端 node 的 node.json 保持明文不加密——
D10 过渡口径延续，node 运行时零第三方依赖约束优先；a4agent 侧只通过 /v1/*
协议与 node 交互，不读 node 配置文件里的令牌。

写入原子（复用 config_io.atomic_write_doc），进程内 threading.Lock 串行化
读改写（a4agent 单进程，无跨进程并发写场景）。
"""
import logging
import threading
import time

from ..config_io import atomic_write_doc, read_doc
from ..crypto import decrypt_text, encrypt_text

logger = logging.getLogger(__name__)

FILE_NAME = "devices.json"

# 条目字段（对齐契约 §9.2 控制端注册表口径，mac/ips 等一期未用字段不设）
_ENTRY_KEYS = ("name", "device_id", "ip", "port", "identity", "token_enc",
               "cert_fingerprint", "hostname", "added_at", "last_seen", "info")


def _sanitize(entry: dict) -> dict:
    """补齐/裁剪条目字段，未知键丢弃，缺省键补默认值。"""
    out = {
        "name": str(entry.get("name", "")).strip(),
        "device_id": entry.get("device_id", ""),
        "ip": entry.get("ip", ""),
        "port": int(entry.get("port", 51888) or 51888),
        "identity": entry.get("identity", "default"),
        "token_enc": entry.get("token_enc", ""),
        "cert_fingerprint": entry.get("cert_fingerprint", ""),
        "hostname": entry.get("hostname", ""),
        "added_at": float(entry.get("added_at", 0) or 0),
        "last_seen": entry.get("last_seen"),
        "info": entry.get("info", {}) if isinstance(entry.get("info"), dict) else {},
    }
    return out


def unique_name(existing: set, base: str) -> str:
    """名称冲突自动后缀 -2/-3…（契约 §17.3 注册表命名口径）。"""
    base = (base or "").strip() or "device"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


class DeviceStore:
    """已配对设备注册表（单文件 JSON + DPAPI 加密令牌）。"""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    # ---------- 读写原语 ----------

    def _load(self) -> dict:
        doc = read_doc(self.path, "json")
        devices = doc.get("devices")
        return devices if isinstance(devices, dict) else {}

    def _save_locked(self, devices: dict) -> None:
        atomic_write_doc(self.path, {"version": 1, "devices": devices}, "json")

    # ---------- 查询 ----------

    def list(self) -> list:
        """全部条目（不含明文令牌；token_enc 内部字段同样不外泄）。"""
        with self._lock:
            devices = self._load()
        out = []
        for entry in devices.values():
            item = _sanitize(entry)
            item.pop("token_enc", None)
            out.append(item)
        out.sort(key=lambda e: (e["added_at"], e["name"]))
        return out

    def get(self, name: str):
        """按名称取条目副本（不含明文令牌）。不存在返回 None。"""
        with self._lock:
            entry = self._load().get(name)
        if entry is None:
            return None
        item = _sanitize(entry)
        item.pop("token_enc", None)
        return item

    def token_of(self, name: str) -> str:
        """解密并返回设备令牌；缺失/解密失败返回空串（调用方按未授权处理）。

        DPAPI 与当前 Windows 用户绑定，换机/换用户后解不开属预期，
        由调用方提示重新配对。
        """
        with self._lock:
            entry = self._load().get(name)
        if not entry:
            return ""
        token = decrypt_text(entry.get("token_enc", ""))
        return token or ""

    def fingerprint_of(self, name: str) -> str:
        with self._lock:
            entry = self._load().get(name)
        return (entry or {}).get("cert_fingerprint", "") or ""

    # ---------- 修改（读改写整体持锁 + 原子写） ----------

    def register(self, *, name=None, device_id="", ip, port, identity,
                 token, cert_fingerprint, hostname="", info=None) -> dict:
        """配对成功后登记设备（或更新既有 device_id 的凭据）。

        - 显示名缺省时取 hostname，再退回 device_id/ip；冲突自动后缀；
        - 同 device_id 已存在 → 原地更新（新令牌/新指纹/新地址），名称保留；
        - 令牌经 DPAPI 加密后落盘。
        返回登记后的条目（不含明文令牌）。
        """
        token_enc = encrypt_text(token)
        with self._lock:
            devices = self._load()
            existed = None
            for entry in devices.values():
                if device_id and entry.get("device_id") == device_id:
                    existed = entry
                    break
            if existed is not None:
                name = existed["name"]
            else:
                base = (name or "").strip() or (hostname or "").strip() \
                    or device_id or f"node-{ip}"
                name = unique_name(set(devices.keys()), base)
            entry = _sanitize({
                "name": name,
                "device_id": device_id,
                "ip": ip,
                "port": port,
                "identity": identity,
                "token_enc": token_enc,
                "cert_fingerprint": cert_fingerprint,
                "hostname": hostname,
                "added_at": (existed or {}).get("added_at", time.time()),
                "last_seen": time.time(),
                "info": info if isinstance(info, dict) else {},
            })
            devices[name] = entry
            self._save_locked(devices)
        out = dict(entry)
        out.pop("token_enc", None)
        return out

    def _mutate_locked(self, devices: dict, name: str, fn):
        entry = devices.get(name)
        if entry is None:
            return False
        entry = _sanitize(entry)
        fn(entry)
        devices[name] = entry
        self._save_locked(devices)
        return True

    def touch(self, name: str) -> None:
        """记录一次成功交互（last_seen）。"""
        with self._lock:
            devices = self._load()
            self._mutate_locked(devices, name,
                                lambda e: e.update({"last_seen": time.time()}))

    def update_fingerprint(self, name: str, fingerprint: str) -> None:
        """登记/重置 TOFU 指纹（首连信任或用户显式重置后重连）。"""
        with self._lock:
            devices = self._load()
            self._mutate_locked(
                devices, name,
                lambda e: e.update({"cert_fingerprint": (fingerprint or "").lower()}))

    def move(self, name: str, ip: str, port: int) -> None:
        """漂移自愈：更新设备地址（沿用一期口径，只动地址与 last_seen）。"""
        with self._lock:
            devices = self._load()

            def _mv(e):
                e["ip"] = ip
                e["port"] = int(port)
                e["last_seen"] = time.time()
            self._mutate_locked(devices, name, _mv)

    def remove(self, name: str) -> bool:
        """忘记配对（仅删本地凭据；node 侧身份不受影响）。"""
        with self._lock:
            devices = self._load()
            if name not in devices:
                return False
            del devices[name]
            self._save_locked(devices)
            return True


def default_store() -> DeviceStore:
    """默认存储：a4agent 数据目录下 devices.json（尊重 A4AGENT_DATA_DIR）。"""
    from ..database import get_data_dir
    return DeviceStore(get_data_dir() / FILE_NAME)


store = None   # 惰性单例（首次访问时绑定数据目录，便于测试隔离）


def get_store() -> DeviceStore:
    global store
    if store is None:
        store = default_store()
    return store
