"""钉定版本的 llama.cpp 引擎包目录（移植自 a4agent EnginePack.cs / EngineCatalog）。

升级 llama.cpp：改 REPO_TAG、重新核对资产名与体积（release 页面）后同步更新即可；
本文件是引擎版本的单一事实来源。
"""
import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

REPO_OWNER = "ggml-org"
REPO_NAME = "llama.cpp"
REPO_TAG = "b11719"  # 2026-10-18 nightly，资产名与体积已按 GitHub API 核对


@dataclass(frozen=True)
class EnginePack:
    pack_id: str           # cuda134 / cuda124 / vulkan / cpu
    label: str             # 显示名（不含体积）
    note: str              # 一行适配说明
    zips: tuple            # 需依次下载的官方 zip 资产名
    total_bytes: int       # 所有 zip 体积合计，用于界面展示
    min_nvidia_driver: str  # NVIDIA 最低驱动大版本号（空 = 不要求）
    needs_nvidia: bool     # true = 仅 NVIDIA 显卡可用

    @property
    def size_label(self) -> str:
        if self.total_bytes >= 512 * 1024 * 1024:
            return f"{self.total_bytes / (1024 ** 3):.1f} GB"
        return f"{self.total_bytes / (1024 ** 2):.0f} MB"

    def to_dict(self) -> dict:
        return {
            "id": self.pack_id,
            "label": self.label,
            "note": self.note,
            "zips": list(self.zips),
            "total_bytes": self.total_bytes,
            "size_label": self.size_label,
            "min_nvidia_driver": self.min_nvidia_driver,
            "needs_nvidia": self.needs_nvidia,
        }


PACKS = [
    EnginePack(
        "cuda134", "CUDA 13.4",
        "NVIDIA 显卡，驱动 ≥ 580（较新驱动，性能优先）",
        ("llama-b11719-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"),
        int((147.9 + 403.9) * 1024 * 1024), "580", True),
    EnginePack(
        "cuda124", "CUDA 12.4",
        "NVIDIA 显卡，驱动 ≥ 550（兼容旧驱动）",
        ("llama-b11719-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"),
        int((255.1 + 373.3) * 1024 * 1024), "550", True),
    EnginePack(
        "vulkan", "Vulkan",
        "通用后端：NVIDIA / AMD / Intel 独显与核显均可",
        ("llama-b11719-bin-win-vulkan-x64.zip",),
        int(30.8 * 1024 * 1024), "", False),
    EnginePack(
        "cpu", "CPU",
        "无可用显卡时的兜底方案，速度受限",
        ("llama-b11719-bin-win-cpu-x64.zip",),
        int(18.8 * 1024 * 1024), "", False),
]


def zip_url(asset: str) -> str:
    return f"https://github.com/{REPO_OWNER}/{REPO_NAME}/releases/download/{REPO_TAG}/{asset}"


def pack_url(pack: EnginePack, index: int) -> str:
    return zip_url(pack.zips[index])


_LEGACY_PACK_IDS = {"cuda133": "cuda134"}  # 旧配置里已存过的引擎包标识映射


def find(pack_id: str | None) -> EnginePack | None:
    if not pack_id:
        return None
    pack_id = _LEGACY_PACK_IDS.get(pack_id, pack_id)
    return next((p for p in PACKS if p.pack_id == pack_id), None)


def parse_driver_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return 0


def recommend(gpu) -> EnginePack:
    """按显卡自动推荐：NVIDIA 看驱动版本选 CUDA，其余选 Vulkan，无卡选 CPU。"""
    if gpu is not None and gpu.is_nvidia:
        major = parse_driver_major(gpu.driver_version)
        nvidia_packs = sorted(
            (p for p in PACKS if p.needs_nvidia),
            key=lambda p: parse_driver_major(p.min_nvidia_driver),
            reverse=True,
        )
        for p in nvidia_packs:
            if major >= parse_driver_major(p.min_nvidia_driver):
                return p
    # 非 NVIDIA（或驱动过旧）：有显卡给 Vulkan，裸机给 CPU
    fallback = "vulkan" if gpu is not None else "cpu"
    pack = find(fallback)
    assert pack is not None  # 目录内建保证存在
    return pack


# ───────────────────────── 已装引擎的后端识别 ─────────────────────────
#
# llama-server.exe 本身不含后端：运行时按同目录里存在的 ggml-*.dll 加载。
# 所以「当前用的是 CUDA 还是 Vulkan」只能从文件名判断，不能从 exe 判断。

BACKEND_LABELS = {"cuda": "CUDA", "vulkan": "Vulkan", "cpu": "CPU"}

# 顺序即优先级：CUDA / Vulkan 包都会附带 ggml-cpu-*.dll（各 CPU 指令集变体），
# 先匹配专用后端才不会把有显卡的引擎误判成 CPU。
_BACKEND_PROBES = (
    ("cuda", "ggml-cuda*.dll"),
    ("vulkan", "ggml-vulkan*.dll"),
    ("cpu", "ggml-cpu*.dll"),
)


def detect_backend(engine_dir) -> str:
    """探测引擎目录实际使用的后端：cuda / vulkan / cpu；认不出返回空串。"""
    d = Path(engine_dir)
    try:
        with os.scandir(d) as it:
            names = [e.name.lower() for e in it if e.is_file()]
    except OSError:
        return ""
    for backend, pattern in _BACKEND_PROBES:
        if any(fnmatch.fnmatch(n, pattern) for n in names):
            return backend
    return ""


def backend_label(backend: str) -> str:
    """后端标识的展示名；未知后端给空串，由调用方决定兜底文案。"""
    return BACKEND_LABELS.get(backend, "")


def backend_preference(gpu) -> tuple:
    """按显卡给出后端优先顺序，用于在多个可用引擎之间排序。"""
    if gpu is not None and gpu.is_nvidia:
        return ("cuda", "vulkan", "cpu")
    if gpu is not None:
        return ("vulkan", "cpu")
    return ("cpu",)


def pack_backend(pack: EnginePack) -> str:
    """引擎包对应的后端标识：CUDA 包（needs_nvidia）→ cuda，其余包 id 本身就是后端名。"""
    return "cuda" if pack.needs_nvidia else pack.pack_id
