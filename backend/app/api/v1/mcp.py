"""MCP server 管理接口（发现 / 迁移 / 回收站 / 内容预览）。"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ... import mcp_manager, schemas
from ...database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/mcp/discover")
def discover_mcp():
    """全量发现：全局各端与各项目的 MCP server 聚合结果。

    作用域随端而异：claude/codex/dsh/zcode/pi/qoder/opencode 各自的文件，
    Qoder 项目级复用 Claude 的 .mcp.json，dsh 只有全局 profile 层。
    """
    return mcp_manager.discover()


@router.post("/mcp/migrate")
def migrate_mcp(body: schemas.McpMigrateIn, db: Session = Depends(get_db)):
    """一键迁移：源端保留；目标端同名 server 先进回收站（快照）。"""
    try:
        return mcp_manager.migrate(
            db,
            [s.model_dump() for s in body.sources],
            [t.model_dump() for t in body.targets],
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/mcp/migrations")
def mcp_migration_logs(db: Session = Depends(get_db)):
    """迁移日志（新→旧）。"""
    return mcp_manager.list_migrations(db)


@router.post("/mcp/delete")
def delete_mcp_server(body: schemas.McpServerRefIn, db: Session = Depends(get_db)):
    """删除某端某个 server：移除配置片段并快照进回收站。"""
    try:
        result = mcp_manager.delete_to_trash(db, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result.get("purged_expired"):
        result["message"] = (
            f"已移入回收站，并顺带清理了 {result['purged_expired']} 条过期条目"
        )
    else:
        result["message"] = "已移入回收站（30 天内可在回收站恢复）"
    return result


@router.get("/mcp/content")
def mcp_content(scope: str, tool: str, name: str, project: str | None = None):
    """读取单个 server 详情（env/headers 已脱敏）供预览。"""
    try:
        return mcp_manager.server_content(scope, tool, project, name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/mcp/trash")
def mcp_trash_list(db: Session = Depends(get_db)):
    """回收站列表；返回值含本次惰性清理的过期条数。"""
    return mcp_manager.list_trash(db)


@router.post("/mcp/trash/{trash_id}/restore")
def mcp_trash_restore(trash_id: int, db: Session = Depends(get_db)):
    try:
        return mcp_manager.restore_from_trash(db, trash_id)
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.delete("/mcp/trash/{trash_id}")
def mcp_trash_purge_one(trash_id: int, db: Session = Depends(get_db)):
    try:
        return mcp_manager.delete_permanent(db, trash_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/mcp/descriptions")
def mcp_descriptions_list():
    """用户自定义 MCP 功能介绍表 {server_name: description}。"""
    return mcp_manager.load_mcp_descriptions()


@router.put("/mcp/descriptions")
def mcp_description_save(body: schemas.McpDescriptionIn):
    """保存/清空某个 server 的功能介绍；description 空串删除。返回最新表。"""
    return mcp_manager.save_mcp_description(body.name, body.description)


@router.get("/mcp/projects")
def mcp_projects_list():
    """可安装 MCP 的项目名列表（dsh 无项目级）。"""
    from ...skill_manager import project_dirs

    return {"projects": [p["project"] for p in project_dirs()]}


@router.post("/mcp/servers")
def mcp_server_create(body: schemas.McpServerCreate):
    """向指定端安装（新建）一个 MCP server；同名已存在返回 409。"""
    try:
        return mcp_manager.create_server(
            body.scope, body.tool, body.project, body.model_dump()
        )
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/mcp/servers/import")
def mcp_server_import(body: schemas.McpImportIn):
    """粘贴 JSON 配置片段批量安装；整体 JSON 非法返回 400，单条失败不中断。"""
    try:
        parsed = mcp_manager.parse_mcp_json(body.config_json)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mcp_manager.import_servers(body.scope, body.tool, body.project, parsed)