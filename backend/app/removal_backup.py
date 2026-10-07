"""移除三端API 托管前的配置快照。

背景：v0.5.1 起 a4agent 不再代管 dsh / ZCode / pi 的 API 配置（这三端应用
自身都自带完整供应商配置界面）。移除前先把用户配置文件原样快照一份，
存到 <数据目录>/backups/pre-switch-removal/，**永不自动清理**——这是唯一的
兜底，用户哪天发现配置不对时唯一的回滚依据。

与 config_manager.backup_* 的区别：那些是为「切换」服务的滚动备份（保留最近
N 份，会被清理），这里是为「一次性迁移」服务的永久留档。
"""
import shutil
from datetime import datetime
from pathlib import Path

from . import config_manager
from .database import get_data_dir

SNAPSHOT_DIRNAME = "pre-switch-removal"


def snapshot_dir() -> Path:
    """永久快照目录（每次调用都创建，不做滚动清理）。"""
    d = get_data_dir() / "backups" / SNAPSHOT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _targets() -> list[tuple[str, Path]]:
    """(标签, 路径) 列表。路径不存在时跳过，不制造空快照。

    dsh 的 settings 有两个候选落点（settings.yaml 与 settings.yaml.imported），
    都要备份——清理动作会覆盖这两个文件，少备一个就等于没有回滚依据。
    """
    items = [
        ("dsh-credentials", config_manager.dsh_credentials_path()),
        ("zcode-cli", config_manager.zcode_cli_config_path()),
        ("zcode-v2", config_manager.zcode_v2_config_path()),
        ("pi-models", config_manager.pi_models_config_path()),
        ("pi-settings", config_manager.pi_settings_path()),
    ]
    for path in config_manager.dsh_settings_candidates():
        label = "dsh-settings" if path.suffixes[-1:] != [".imported"] else "dsh-settings-imported"
        items.insert(0, (label, path))
    return items


def take_snapshot(dry_run: bool = False) -> dict:
    """快照三端全部配置文件，返回 {label: 快照路径|None, "dir": 快照目录}。

    单个文件复制失败不影响其余（记入 errors），任一文件都不会被修改——这是
    只读操作，绝不允许因为备份失败而阻断或改变用户的配置。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = snapshot_dir() / stamp
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    result: dict = {"dir": str(dest), "files": {}, "errors": []}
    for label, src in _targets():
        if not src.exists():
            result["files"][label] = None
            continue
        target = dest / f"{label}{_suffix(src)}"
        if dry_run:
            result["files"][label] = str(target)
            continue
        try:
            shutil.copy2(src, target)
            result["files"][label] = str(target)
        except OSError as e:
            result["errors"].append(f"{label}: {e}")
            result["files"][label] = None
    return result


def _suffix(src: Path) -> str:
    """保留原扩展名（.yaml / .json），便于人工比对。"""
    return src.suffix or ".bak"


def latest_snapshot() -> Path | None:
    """最近一次快照目录（按时间戳倒序取第一个），没有则 None。"""
    root = get_data_dir() / "backups" / SNAPSHOT_DIRNAME
    if not root.is_dir():
        return None
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    return dirs[0] if dirs else None