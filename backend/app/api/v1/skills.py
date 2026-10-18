"""Skill 管理接口（发现 / 迁移 / 回收站 / 打开 / 预览 / 项目根配置）。"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import skill_manager, schemas
from ...database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/skills/discover")
def discover_skills():
    """全量发现：全局与各项目的 skill 聚合结果（含重复标注）。"""
    return skill_manager.discover()


@router.get("/skills/project-roots")
def get_project_roots():
    return {"roots": skill_manager.load_project_roots(),
            "defaults": skill_manager.default_project_roots()}


@router.put("/skills/project-roots")
def put_project_roots(body: schemas.ProjectRootsIn):
    try:
        roots = skill_manager.save_project_roots(body.roots)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"roots": roots}


@router.get("/skills/project-sources")
def get_project_sources():
    """项目来源汇总（根目录弹窗用）：扫描范围 + 默认值 + 忽略项 + 自动发现。"""
    return skill_manager.project_sources()


@router.post("/skills/project-sources/refresh")
def refresh_project_sources():
    """强制重扫历史线索（不看缓存）。"""
    return skill_manager.project_sources(refresh=True)


@router.put("/skills/project-sources/ignored")
def put_ignored_projects(body: schemas.IgnoredProjectsIn):
    try:
        ignored = skill_manager.save_ignored_projects(body.paths)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ignored": ignored}


@router.post("/skills/migrate")
def migrate_skills(
    body: schemas.SkillMigrateIn, db: Session = Depends(get_db)
):
    """一键迁移：非破坏性复制；目标端同名 skill 先进回收站。"""
    try:
        return skill_manager.migrate(
            db,
            [s.model_dump() for s in body.sources],
            [t.model_dump() for t in body.targets],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/skills/migrations")
def migration_logs(db: Session = Depends(get_db)):
    """迁移日志（新→旧）。"""
    return skill_manager.list_migrations(db)


@router.post("/skills/delete")
def delete_skill(body: schemas.SkillPathIn, db: Session = Depends(get_db)):
    """删除 skill：移入回收站（30 天内可恢复）。"""
    try:
        result = skill_manager.delete_to_trash(db, body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result.get("purged_expired"):
        result["message"] = (
            f"已移入回收站，并顺带清理了 {result['purged_expired']} 条过期条目"
        )
    else:
        result["message"] = "已移入回收站（30 天内可在回收站恢复）"
    return result


@router.get("/skills/trash")
def trash_list(db: Session = Depends(get_db)):
    """回收站列表；返回值含本次惰性清理的过期条数。"""
    return skill_manager.list_trash(db)


@router.post("/skills/trash/{trash_id}/restore")
def trash_restore(trash_id: int, db: Session = Depends(get_db)):
    try:
        return skill_manager.restore_from_trash(db, trash_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.delete("/skills/trash/{trash_id}")
def trash_purge_one(trash_id: int, db: Session = Depends(get_db)):
    try:
        return skill_manager.delete_permanent(db, trash_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/skills/content")
def skill_content(path: str):
    """读取 SKILL.md 内容（frontmatter + 正文）供预览。"""
    try:
        return skill_manager.read_content(path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/skills/open")
def open_skill(body: schemas.SkillPathIn):
    """在系统资源管理器中打开该 skill 目录。"""
    try:
        return skill_manager.open_in_explorer(body.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
