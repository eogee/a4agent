"""B 侧状态写入：hook 进程在关键节点把一条状态原子写入本机 node 暂存目录。

路径口径与 node_proc 的默认实例一致：<a4agent 数据目录>/node/tmp/status。
多实例（--instance，staging 为 tmp-<名>）与自定义 --config 目录暂不覆盖，
属于已知边界；状态写入失败只记日志，绝不影响 hook 主流程。
"""
import json
import logging
import os
import time
from pathlib import Path

from ..database import get_data_dir
from . import status_schema

logger = logging.getLogger(__name__)


def status_root() -> Path:
    """默认实例的 node 暂存状态目录。"""
    return get_data_dir() / "node" / "tmp" / status_schema.STATUS_DIR_NAME


def report(*, event, state, session_id="", agent="", workspace="",
           summary="", root=None) -> bool:
    """原子写一条状态；任何失败返回 False，不抛出。"""
    if os.environ.get("A4AGENT_STATUS_REPORT", "1") == "0":
        return False
    tmp = None
    try:
        payload = status_schema.build_payload(
            event=event, state=state, session_id=session_id,
            agent=agent, workspace=workspace, summary=summary)
        directory = Path(root) if root is not None else status_root()
        directory.mkdir(parents=True, exist_ok=True)
        name = status_schema.sanitize_filename(payload["session_id"]) + ".json"
        target = directory / name
        tmp = directory / f".{name}.{os.getpid()}.{time.time_ns()}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001 - 状态汇报不能拖累 hook 主流程
        logger.warning("设备状态写入失败", exc_info=True)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False
