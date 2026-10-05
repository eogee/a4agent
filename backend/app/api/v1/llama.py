"""本地模型（llama.cpp）接口：状态/服务控制/引擎获取/模型库/向导/设置/接入。"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ... import config_manager, crud
from ...crypto import decrypt_text, encrypt_text
from ...database import get_db
from ...llama import catalog
from ...llama import gpu as llama_gpu
from ...llama import runtime as rt
from ...models import Configuration, Provider

router = APIRouter(prefix="/llama")


# ───────────────────────── 状态与控制 ─────────────────────────

@router.get("/status")
def status():
    return rt.runtime().status()


@router.post("/start")
def start():
    result = rt.runtime().start()
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "启动失败"))
    return result


@router.post("/stop")
def stop():
    return rt.runtime().stop()


@router.get("/logs")
def logs(after_id: int = 0):
    return {"lines": rt.runtime().logs_after(after_id)}


# ───────────────────────── 引擎获取 ─────────────────────────

@router.get("/engine/packs")
def engine_packs():
    r = rt.runtime()
    rec = catalog.recommend(llama_gpu.best())
    return {
        "repo_tag": catalog.REPO_TAG,
        "packs": [p.to_dict() for p in catalog.PACKS],
        "recommended_pack_id": rec.pack_id,
        "engine_present": r.engine_present(),
        "progress": r.downloader.progress(),
    }


@router.post("/engine/download")
def engine_download(body: dict):
    pack = catalog.find(body.get("pack_id"))
    if not pack:
        raise HTTPException(404, f"未知引擎包: {body.get('pack_id')}")
    r = rt.runtime()
    result = r.downloader.start(pack, r.engine_dir())
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "无法开始下载"))
    return {"ok": True}


@router.post("/engine/offline")
def engine_offline(body: dict):
    result = rt.runtime().wizard_engine_offline(body.get("dir", ""))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "离线安装失败"))
    return result


@router.post("/engine/cancel")
def engine_cancel():
    return rt.runtime().downloader.cancel()


@router.get("/engine/progress")
def engine_progress():
    return rt.runtime().downloader.progress()


# ───────────────────────── 硬件检测 ─────────────────────────

@router.get("/hardware")
def hardware():
    """显卡列表 + 推荐推理预设（向导与设置页共用）。"""
    r = rt.runtime()
    return r.wizard_packs()


# ───────────────────────── 模型库 ─────────────────────────

@router.get("/models")
def models():
    return rt.runtime().models_payload()


@router.post("/models/scan")
def models_scan():
    return rt.runtime().scan_models(force=True) and rt.runtime().models_payload()


@router.post("/models/dirs")
def add_model_dir(body: dict):
    path = (body.get("path") or "").strip()
    if not path or not Path(path).is_dir():
        raise HTTPException(400, "目录不存在")
    return rt.runtime().add_model_dir(path)


@router.post("/models/dirs/remove")
def remove_model_dir(body: dict):
    return rt.runtime().remove_model_dir(body.get("path", ""))


@router.post("/models/default")
def set_default_model(body: dict):
    result = rt.runtime().set_default_model(body.get("path", ""))
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "设置失败"))
    return result


class RiskBody(BaseModel):
    path: str
    context_tokens: int | None = None
    kv_level: str | None = None


@router.post("/models/risk")
def model_risk(body: RiskBody):
    if not Path(body.path).is_file():
        raise HTTPException(404, "模型文件不存在")
    return rt.runtime().model_risk(body.path, body.context_tokens, body.kv_level)


# ───────────────────────── 向导 ─────────────────────────

@router.post("/wizard/offline")
def wizard_offline(body: dict):
    return engine_offline(body)


@router.post("/wizard/finish")
def wizard_finish(body: dict):
    result = rt.runtime().wizard_finish(
        port=int(body.get("port", 8080)),
        default_model_path=body.get("default_model_path", ""),
        preset=body.get("preset"),
    )
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "向导保存失败"))
    return result


# ───────────────────────── 设置 ─────────────────────────

@router.get("/settings")
def settings():
    return rt.runtime().settings_payload()


@router.post("/settings")
def save_settings(body: dict):
    result = rt.runtime().save_settings(body)
    if not result.get("ok"):
        raise HTTPException(409, result.get("error", "保存失败"))
    return result


# ───────────────────────── 接入 ─────────────────────────

@router.get("/connect")
def connect():
    return rt.runtime().connect()


class IntegrateBody(BaseModel):
    targets: str = "claude"       # claude / codex / dsh / zcode / pi，逗号分隔
    activate: bool = False        # 创建后是否立即切换


@router.post("/integrate")
def integrate(body: IntegrateBody, db: Session = Depends(get_db)):
    """把本地 llama-server 一键接入配置方案：创建/复用「本地 llama-server」
    供应商（is_custom，端口跟随当前配置）并创建配置方案。"""
    r = rt.runtime()
    if not r.cfg.default_model_path:
        raise HTTPException(409, "请先在模型库中设置默认模型")
    if not r.engine_present():
        raise HTTPException(409, "尚未安装推理引擎")

    api_base = f"http://127.0.0.1:{r.cfg.port}/v1"
    model = Path(r.cfg.default_model_path).stem
    # 鉴权开启时沿用服务的 --api-key；未开启时写占位值。空 Key 会让切换在
    # 「API Key 解密失败」处直接失败（本地方案根本切不动），且 pi 的
    # models.json 要求 apiKey 非空，"none" 正是本机服务的通行写法。
    api_key = r.cfg.infer.api_key.strip() or "none"

    provider = db.query(Provider).filter(
        Provider.name == "本地 llama-server").first()
    if provider is None:
        provider = crud.create_provider(db, {
            "name": "本地 llama-server",
            "api_base": api_base,
            "api_type": "openai",
            "is_custom": True,
        })
    elif provider.api_base != api_base:
        provider.api_base = api_base  # type: ignore[assignment]
        db.commit()

    # 勾选的目标先规范化：未知端名丢弃、空选择回落 claude（本方案由接入功能托管，
    # 新建与重复接入都按这份规范化结果写入）
    wanted = ",".join(config_manager.target_list(body.targets))

    existing = db.query(Configuration).filter(
        Configuration.provider_id == provider.id).first()
    if existing:
        if existing.model != model:
            existing.model = model  # type: ignore[assignment]
            db.commit()
        if decrypt_text(existing.api_key_encrypted) != api_key:
            # 本地方案的 Key 就是服务自身的鉴权值（未开鉴权时为占位 none）：
            # 跟随当前服务刷新，同时补上早期版本留下的空 Key
            existing.api_key_encrypted = encrypt_text(api_key)
            db.commit()
        if existing.targets != wanted:
            existing.targets = wanted  # type: ignore[assignment]
            db.commit()
        config_id = existing.id
        created = False
    else:
        data = {
            "name": f"本地模型 · {model}",
            "provider_id": provider.id,
            "api_key_encrypted": encrypt_text(api_key),
            "model": model,
            "targets": wanted,
            "max_tokens": None,
        }
        config = crud.create_config(db, data)
        config_id = config.id
        created = True

    activated = False
    if body.activate:
        cfg_obj = db.query(Configuration).filter(
            Configuration.id == config_id).first()
        if cfg_obj is not None:
            crud.set_active(db, cfg_obj)
            activated = True

    return {"ok": True, "config_id": config_id, "created": created,
            "provider_id": provider.id, "api_base": api_base,
            "model": model, "activated": activated}
