"""llama 控制台配置模型与 JSON 持久化（移植自 a4agent AppConfig/ConfigStore）。

存储位置与 a4agent 其他运行时数据一致：get_data_dir() / llama_config.json。
"""
import json
import logging
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path

from ..database import get_data_dir

logger = logging.getLogger(__name__)

CONFIG_NAME = "llama_config.json"
ENGINE_DIR_NAME = "engine"

_lock = threading.Lock()


@dataclass
class ModelEntry:
    path: str = ""
    alias: str = ""  # 空 = 使用文件名
    added_at: str = ""


@dataclass
class InferParams:
    context_tokens: int = 32768
    kv_level: str = "q4_0"  # f16 | q8_0 | q4_0
    flash_attention: bool = True
    ngl: int = 99           # GPU 层数，99 = 全部
    threads: int = 0        # 0 = 自动
    ubatch: int = 512
    parallel: int = 1
    mtp_steps: int = 0      # 投机解码步数，0 = 关闭（需后端与模型支持）
    api_key: str = ""       # 非空时启用 --api-key 鉴权；空 = 不鉴权
    extra_args: str = ""    # 高级逃生舱：原样追加到 llama-server 命令行


@dataclass
class RuntimeSettings:
    auto_trim_ram: bool = True
    trim_delay_seconds: int = 90
    auto_restart_on_crash: bool = False


@dataclass
class LlamaConfig:
    wizard_done: bool = False
    port: int = 8080
    host: str = "127.0.0.1"  # 127.0.0.1 | 0.0.0.0
    engine_dir: str = ""     # llama-server.exe 所在目录；空 = 数据目录 engine
    engine_pack_id: str = ""  # 向导自动下载的引擎包标识（vulkan/cuda124/cuda134/cpu，旧配置 cuda133 自动映射）
    model_dirs: list = field(default_factory=list)
    models: list = field(default_factory=list)  # ModelEntry.asdict() 列表
    default_model_path: str = ""
    mmproj_path: str = ""    # 多模态投影模型；空 = 不加载
    infer: InferParams = field(default_factory=InferParams)
    run: RuntimeSettings = field(default_factory=RuntimeSettings)

    def effective_engine_dir(self, base_dir: Path | None = None) -> Path:
        """llama-server.exe 所在目录：配置优先，否则数据目录下 engine。"""
        if self.engine_dir.strip():
            return Path(self.engine_dir.strip())
        return default_engine_dir(base_dir)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    def apply_dict(self, d: dict) -> None:
        """从 JSON dict 恢复；未知字段忽略，嵌套对象按字段合并。"""
        known = {"wizard_done", "port", "host", "engine_dir", "engine_pack_id",
                 "model_dirs", "models", "default_model_path", "mmproj_path"}
        for k in known & d.keys():
            setattr(self, k, d[k])
        if isinstance(d.get("infer"), dict):
            _merge_dataclass(self.infer, d["infer"])
        if isinstance(d.get("run"), dict):
            _merge_dataclass(self.run, d["run"])


def _merge_dataclass(obj, d: dict) -> None:
    fields = {f.name for f in obj.__dataclass_fields__.values()}
    for k in fields & d.keys():
        setattr(obj, k, d[k])


def config_path() -> Path:
    return get_data_dir() / CONFIG_NAME


def default_engine_dir(base_dir: Path | None = None) -> Path:
    return (base_dir or get_data_dir()) / ENGINE_DIR_NAME


def load_config() -> LlamaConfig:
    """损坏的配置按全新处理（与 a4agent 行为一致）。"""
    cfg = LlamaConfig()
    try:
        p = config_path()
        if p.exists():
            cfg.apply_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning("llama 配置读取失败，按默认值处理：%s", e)
    return cfg


def save_config(cfg: LlamaConfig) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        p.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
