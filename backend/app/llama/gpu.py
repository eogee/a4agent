"""显卡检测（移植自 a4agent GpuDetector.cs）。

NVIDIA 优先走 nvidia-smi（显存数值准确），再走注册表枚举，
让 AMD / Intel 卡也出现在列表里；虚拟显示适配器被过滤。
"""
import logging
import os
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

try:  # winreg 仅 Windows 存在；非 Windows 下注册表路径静默跳过
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 检测结果缓存：/status 每 1.5s 轮询一次并走 detect()，无缓存时每次都 spawn
# nvidia-smi（驱动异常时各阻塞至多 8 秒，线程池被无谓占满）。显卡是硬件事实，
# 变更频率以重启计，60s TTL 足够新鲜。
_CACHE_TTL_SECONDS = 60.0
_cache_lock = threading.Lock()
_cache: list | None = None
_cache_at = 0.0


class GpuInfo:
    def __init__(self, vendor: str, name: str, vram_bytes: int, driver_version: str = ""):
        self.vendor = vendor
        self.name = name
        self.vram_bytes = vram_bytes
        self.driver_version = driver_version

    @property
    def is_nvidia(self) -> bool:
        return self.vendor == "NVIDIA"

    @property
    def vram_gb(self) -> float:
        return round(self.vram_bytes / (1024 ** 3), 1)

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "name": self.name,
            "vram_gb": self.vram_gb,
            "driver_version": self.driver_version,
            "is_nvidia": self.is_nvidia,
        }


def detect() -> list:
    """检测全部显卡：nvidia-smi 结果在前，注册表去重补齐在后（带 TTL 缓存）。"""
    global _cache, _cache_at
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache_at < _CACHE_TTL_SECONDS:
            return _cache

    result: list = []
    seen = set()

    for g in _try_nvidia_smi():
        if g.name.lower() not in seen:
            seen.add(g.name.lower())
            result.append(g)
    for g in _try_registry():
        # 虚拟显示适配器（GameViewer/OrayIddDriver 之类）没有显存也没有推理价值
        if g.name.lower() not in seen and not (g.vendor == "Unknown" and g.vram_bytes <= 0):
            seen.add(g.name.lower())
            result.append(g)

    with _cache_lock:
        _cache, _cache_at = result, time.monotonic()
    return result


def best(gpus: list | None = None):
    """选择用于推理的主卡：NVIDIA 优先，其次显存大者。"""
    gpus = detect() if gpus is None else gpus
    if not gpus:
        return None
    return sorted(gpus, key=lambda g: (g.is_nvidia, g.vram_bytes), reverse=True)[0]


def _try_nvidia_smi() -> list:
    out: list = []
    exe = shutil.which("nvidia-smi")
    if not exe:
        sys32 = r"C:\Windows\System32\nvidia-smi.exe"
        if os.path.isfile(sys32):
            exe = sys32
    if not exe:
        return out
    try:
        proc = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        )
        for line in proc.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            name = parts[0]
            try:
                mib = int(parts[1])
            except ValueError:
                continue
            driver = parts[2] if len(parts) > 2 else ""
            out.append(GpuInfo("NVIDIA", name, mib * 1024 * 1024, driver))
    except Exception as e:  # nvidia-smi 损坏/缺失 -> 注册表路径兜底
        logger.debug("nvidia-smi 探测失败：%s", e)
    return out


def _try_registry() -> list:
    out: list = []
    if winreg is None:
        return out
    key_root = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_root)
    except OSError:
        return out
    try:
        i = 0
        subs = []
        while True:
            try:
                subs.append(winreg.EnumKey(root, i))
                i += 1
            except OSError:
                break
        for sub in subs:
            if not sub.startswith("00"):
                continue
            try:
                k = winreg.OpenKey(root, sub)
            except OSError:
                continue
            try:
                desc, _ = winreg.QueryValueEx(k, "DriverDesc")
                if not desc:
                    continue
                vram = 0
                for value_name in ("HardwareInformation.qwMemorySize",
                                   "HardwareInformation.MemorySize"):
                    try:
                        v = winreg.QueryValueEx(k, value_name)[0]
                        if isinstance(v, int) and v > 0:
                            vram = v
                            break
                    except OSError:
                        continue
                low = desc.lower()
                if "nvidia" in low or "geforce" in low or "rtx" in low:
                    vendor = "NVIDIA"
                elif "amd" in low or "radeon" in low:
                    vendor = "AMD"
                elif "intel" in low or "arc" in low:
                    vendor = "Intel"
                else:
                    vendor = "Unknown"
                out.append(GpuInfo(vendor, desc.strip(), vram))
            except OSError:
                continue
            finally:
                winreg.CloseKey(k)
    finally:
        winreg.CloseKey(root)
    return out
