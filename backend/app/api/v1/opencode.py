"""OpenCode 连接配置接口：读写、测试连通、查看服务端能力。

配置落 get_data_dir()/opencode.json，密码经 DPAPI 加密（复用 crypto），
接口永不回显明文——与本项目对 API Key 的既有约定一致。

三种配置口径：
- 未启用：a4agent 不碰 OpenCode，任务下发的引擎列表里该端置灰
- 本机模式（默认）：不填地址，直接读 ~/.config/opencode/service.json 的密码，
  端口默认 49374——用户装了 OpenCode 就能用，无需任何手工配置
- 远程模式：显式填 base_url + password，密码加密落盘
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import crypto, opencode_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opencode")
CONFIG_FILENAME = "opencode.json"


def _config_path():
    from ...database import get_data_dir

    return get_data_dir() / CONFIG_FILENAME


def _normalize(raw: dict) -> dict:
    base = str(raw.get("base_url") or "").strip()
    if base and not base.startswith(("http://", "https://")):
        base = "http://" + base
    return {
        "enabled": bool(raw.get("enabled", False)),
        "base_url": base.rstrip("/"),
        "remote": bool(raw.get("remote", False)),
    }


def load() -> dict:
    """读取配置；损坏时返回默认（不抛，避免一条坏文件卡住整个应用）。"""
    path = _config_path()
    if not path.exists():
        return {"enabled": False, "base_url": "", "remote": False, "password": ""}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"enabled": False, "base_url": "", "remote": False, "password": ""}
    data = raw if isinstance(raw, dict) else {}
    out = _normalize(data)
    out["password"] = (crypto.decrypt_text(str(data.get("password")))
                       if data.get("password") else "")
    return out


def save(cfg: dict) -> dict:
    """规范化并落盘；密码以 DPAPI 密文存储。"""
    data = _normalize(cfg)
    pwd = str(cfg.get("password") or "").strip()
    if pwd:
        data["password"] = crypto.encrypt_text(pwd)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = None, None
    import os
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".opencode.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return load()


class OpenCodeConfigIn(BaseModel):
    enabled: Optional[bool] = None
    base_url: Optional[str] = None
    remote: Optional[bool] = None
    password: Optional[str] = None


class OpenCodeStatusOut(BaseModel):
    enabled: bool = False
    remote: bool = False
    base_url: str = ""
    password_set: bool = False
    # 未启用时没有「连不连得上」这回事，给默认 False 而不是要求调用方每个分支都填
    reachable: bool = False
    version: str = ""
    detail: str = ""
    capabilities: dict = Field(default_factory=dict)


def _status_out(cfg: dict) -> OpenCodeStatusOut:
    out = OpenCodeStatusOut(
        enabled=bool(cfg.get("enabled")),
        remote=bool(cfg.get("remote")),
        base_url=str(cfg.get("base_url") or ""),
        password_set=bool(cfg.get("password")),
    )
    if not out.enabled:
        out.detail = "未启用"
        return out
    base, pwd = opencode_client.resolve_connection(cfg)
    out.base_url = base
    probe = opencode_client.probe(cfg)
    out.reachable = bool(probe.get("installed"))
    out.version = str(probe.get("version") or "")
    out.capabilities = probe.get("capabilities") or {}
    out.detail = "" if out.reachable else str(probe.get("error") or "无法连接")
    return out


@router.get("/config", response_model=OpenCodeStatusOut)
def get_config():
    return _status_out(load())


@router.put("/config", response_model=OpenCodeStatusOut)
def put_config(body: OpenCodeConfigIn):
    cfg = load()
    data = body.model_dump(exclude_none=True)
    # 空字符串的密码表示「保持不变」，而不是清空——界面不回显明文，
    # 若把空值当清空，用户点一次保存就会误删已保存的密码
    if data.get("password") == "":
        data.pop("password")
    for key in ("enabled", "base_url", "remote", "password"):
        if key in data:
            cfg[key] = data[key]
    if cfg.get("remote") and not str(cfg.get("base_url") or "").strip():
        raise HTTPException(422, "远程模式需要填写服务地址")
    return _status_out(save(cfg))


@router.post("/test", response_model=OpenCodeStatusOut)
def test_connection():
    """连通性测试：用当前保存（或本机自动发现）的凭据打一次 /api/info。"""
    cfg = load()
    if not cfg.get("enabled"):
        raise HTTPException(422, "请先启用 OpenCode 接入")
    return _status_out(cfg)


@router.post("/pair", response_model=dict)
def pair_link():
    """请求一次性配对码（POST /api/pair）：用于用户不方便手工填密码的场景。

    链接 5 分钟有效且只能用一次，凭据与 session 都留在用户自己的服务上，
    本工具不落地任何 token。
    """
    cfg = load()
    base, pwd = opencode_client.resolve_connection(cfg)
    data = opencode_client.api("POST", "/api/pair", {}, base_url=base, password=pwd, timeout=15)
    return {"data": data if isinstance(data, dict) else {}}