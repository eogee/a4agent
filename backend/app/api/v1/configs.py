"""配置方案接口。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import crud, schemas
from ...crypto import encrypt_text
from ...database import get_db

router = APIRouter(prefix="/configs")


def _require_provider(db: Session, provider_id: int):
    p = crud.get_provider(db, provider_id)
    if not p:
        raise HTTPException(400, "所选服务商不存在")
    return p


def _validate_targets(provider, targets: str | None) -> None:
    """Codex 依赖 OpenAI 兼容接口：目标含 codex 但服务商非 openai 时拒绝保存。

    与切换时 [switch.py] 的校验保持一致，让错误在保存阶段就暴露，而非留到切换才报。
    """
    if not targets:
        return
    has_codex = any(t.strip() == "codex" for t in targets.split(","))
    if has_codex and provider.api_type != "openai":
        raise HTTPException(
            400,
            "Codex 需使用 OpenAI 兼容接口，"
            f"当前服务商「{provider.name}」不是 OpenAI 兼容类型，请更换服务商或去掉 Codex 目标",
        )


@router.get("", response_model=list[schemas.ConfigOut])
def list_configs(db: Session = Depends(get_db)):
    return crud.get_configs(db)


@router.post("", response_model=schemas.ConfigOut)
def create_config(body: schemas.ConfigCreate, db: Session = Depends(get_db)):
    provider = _require_provider(db, body.provider_id)
    _validate_targets(provider, body.targets)
    data = body.model_dump()
    data["api_key_encrypted"] = encrypt_text(body.api_key)
    data.pop("api_key")
    return crud.create_config(db, data)


@router.get("/{config_id}", response_model=schemas.ConfigOut)
def get_config(config_id: int, db: Session = Depends(get_db)):
    c = crud.get_config(db, config_id)
    if not c:
        raise HTTPException(404, "配置方案不存在")
    return c


@router.put("/{config_id}", response_model=schemas.ConfigOut)
def update_config(config_id: int, body: schemas.ConfigUpdate, db: Session = Depends(get_db)):
    c = crud.get_config(db, config_id)
    if not c:
        raise HTTPException(404, "配置方案不存在")
    data = body.model_dump(exclude_unset=True)
    provider = c.provider
    if "provider_id" in data:
        provider = _require_provider(db, data["provider_id"])
    _validate_targets(provider, data.get("targets", c.targets))
    if body.api_key:
        data["api_key_encrypted"] = encrypt_text(body.api_key)
    data.pop("api_key", None)
    return crud.update_config(db, c, data)


@router.delete("/{config_id}")
def delete_config(config_id: int, db: Session = Depends(get_db)):
    ok = crud.delete_config(db, config_id)
    if not ok:
        raise HTTPException(404, "配置方案不存在")
    return {"success": True}


@router.post("/{config_id}/activate", response_model=schemas.ConfigOut)
def activate_config(config_id: int, db: Session = Depends(get_db)):
    """仅将方案标记为当前（不写 settings.json），切换走 /switch 接口。"""
    c = crud.get_config(db, config_id)
    if not c:
        raise HTTPException(404, "配置方案不存在")
    crud.set_active(db, c)
    return c
