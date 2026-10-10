"""配置文件公共 IO 层：备份、原子写入、容错读写。

抽取背景：此前每个目标应用各自实现一遍read / backup / atomic_write，
函数体逐行相同（6 个备份函数、4 个原子写入函数），加一个目标就要再抄一遍。
这里收敛为一套公共实现，各目标只声明「路径 + 序列化格式 + 文件名模板」。

三种序列化格式对应三类真实需求：
- json：Claude Code / Codex 之外的多数端
- json_sig：允许 UTF-8 BOM 的 JSON（Codex / pi 的配置常见）
- toml：Codex config.toml
- yaml：dsh / ZCode 的分段文档

原子写入统一走「临时文件 + fsync + os.replace」，杜绝写入中断导致的配置损坏。
"""
import io
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

DEFAULT_BACKUP_KEEP = 5


# ---------------- 序列化适配 ----------------


def _dump_json(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def _load_json(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8-sig"))


def _dump_yaml(data: dict) -> bytes:
    import yaml

    body = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False
    )
    return body.encode("utf-8")


def _load_yaml(raw: bytes) -> dict:
    import yaml

    value = yaml.safe_load(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _dump_toml(data: dict) -> bytes:
    import tomli_w

    # 用 BytesIO 而非 SpooledTemporaryFile：后者超过阈值会滚动到磁盘，
    # 此时 getvalue() 直接抛 AttributeError（配置较大时才会触发）。
    buf = io.BytesIO()
    tomli_w.dump(data, buf)
    return buf.getvalue()


def _load_toml(raw: bytes) -> dict:
    try:
        import tomllib  # type: ignore[import-found]
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return tomllib.loads(raw.decode("utf-8"))


SERIALIZERS: dict[str, tuple[Callable[[dict], bytes], Callable[[bytes], dict]]] = {
    "json": (_dump_json, _load_json),
    "json_sig": (_dump_json, _load_json),   # 读侧统一按 utf-8-sig 解，容忍 BOM
    "toml": (_dump_toml, _load_toml),
    "yaml": (_dump_yaml, _load_yaml),
}


# ---------------- 读 ----------------


def read_doc(path: Path, fmt: str = "json") -> dict:
    """读取配置文档；文件不存在或损坏时返回空字典（永不抛异常）。

    配置损坏时返回空字典而不是报错，是为了让「切换」能在写新配置时用
    空字典做基底重建，而不是卡在一个无法解析的旧文件上。
    """
    if not path.exists():
        return {}
    _, loader = SERIALIZERS[fmt]
    try:
        return loader(path.read_bytes())
    except Exception:
        # 兜底必须从宽：yaml.YAMLError 直接继承 Exception 而非 ValueError，
        # 只列 (OSError, ValueError, TypeError) 时损坏的 yaml 会穿透出去
        # （如 /removal/status 直接 500）。读失败按空文档处理是本函数的契约。
        return {}


# ---------------- 备份 ----------------


def backup_doc(path: Path, name_template: str, keep: int = DEFAULT_BACKUP_KEEP) -> Path | None:
    """备份单个文件，返回备份路径；原文件不存在时返回 None。

    name_template 形如``settings.{stamp}.json.bak``。滚动保留最近 keep 份，
    超出的删掉——这是「切换」用的短期备份，永久留档请用 removal_backup。
    """
    if not path.exists():
        return None
    from .config_manager import backup_dir

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_dir() / name_template.format(stamp=stamp, ext=path.suffix or "")
    n = 1
    while dest.exists():  # 秒级时间戳同秒互写会静默覆盖，加后缀去重
        dest = backup_dir() / name_template.format(stamp=f"{stamp}_{n}", ext=path.suffix or "")
        n += 1
    shutil.copy2(path, dest)
    for old in _prune(dest.parent, name_template, keep):
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def backup_many(
    items: list[tuple[str, Path, str]], keep: int = DEFAULT_BACKUP_KEEP
) -> dict[str, str | None]:
    """批量备份：items 为 (标签, 路径, 名称模板)，返回 {标签: 备份路径|None}。"""
    return {label: _as_str(backup_doc(path, tpl, keep)) for label, path, tpl in items}


def _prune(parent: Path, name_template: str, keep: int) -> list[Path]:
    """同一模板前缀下超出 keep 的旧备份（newest 时间戳最大、排序居末，不会被删）。

    前缀必须从**模板**取：传入已 format 的文件名时 split("{stamp}") 得到的
    前缀恒等于完整文件名，glob 永不命中，keep 等于没设（备份无界增长）。
    """
    prefix = name_template.split("{stamp}")[0]
    if not prefix:
        return []
    same = sorted(p for p in parent.glob(f"{prefix}*") if p.is_file())
    return same[:-keep] if len(same) > keep else []


def _as_str(path: Path | None) -> str | None:
    return str(path) if path else None


# ---------------- 原子写入 ----------------


def atomic_write_doc(path: Path, data: dict, fmt: str = "json") -> None:
    """原子写入配置文档：临时文件 → fsync → os.replace。

    任何一步失败都清理临时文件并抛异常，不会留下半截配置。``os.replace``
    在同一文件系统内是原子操作，因此不存在「写到一半被读到」的状态。
    """
    dumper, _ = SERIALIZERS[fmt]
    payload = dumper(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------- 路径解析 ----------------


def resolve_path(env_names: tuple[str, ...], default_fn: Callable[[], Path]) -> Path:
    """按 env_names 顺序取第一个环境变量覆盖，否则用默认路径。

    每个环境变量都取「新名, 旧名」二元组，读取走 env_first，兼容品牌改名历史。
    """
    from .env_compat import env_first

    override = env_first(*env_names)
    return Path(override) if override else default_fn()