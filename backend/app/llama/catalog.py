"""钉定版本的 llama.cpp 引擎包目录（移植自 a4agent EnginePack.cs / EngineCatalog）。

升级 llama.cpp：改 REPO_TAG、重新核对资产名与体积（release 页面）后同步更新即可；
本文件是引擎版本的单一事实来源。
"""
from dataclasses import dataclass

REPO_OWNER = "ggml-org"
REPO_NAME = "llama.cpp"
REPO_TAG = "b11370"  # 2026-10-03 发布，llama-server 含 /v1/systemone 决策模型端点


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
        ("llama-b11370-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"),
        int((145.8 + 403.9) * 1024 * 1024), "580", True),
    EnginePack(
        "cuda124", "CUDA 12.4",
        "NVIDIA 显卡，驱动 ≥ 550（兼容旧驱动）",
        ("llama-b11370-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"),
        int((251.3 + 373.3) * 1024 * 1024), "550", True),
    EnginePack(
        "vulkan", "Vulkan",
        "通用后端：NVIDIA / AMD / Intel 独显与核显均可",
        ("llama-b11370-bin-win-vulkan-x64.zip",),
        int(31.7 * 1024 * 1024), "", False),
    EnginePack(
        "cpu", "CPU",
        "无可用显卡时的兜底方案，速度受限",
        ("llama-b11370-bin-win-cpu-x64.zip",),
        int(18.4 * 1024 * 1024), "", False),
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
