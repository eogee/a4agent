"""任务级 skill 注入与回收。

七端 CLI 都没有「指定使用哪个 skill」的命令行参数，skill 只靠目录发现
（`~/.<tool>/skills` 与 `<工作目录>/.<tool>/skills`）。因此「配置使用 skill」
只能落成**让选中的 skill 出现在本次任务工作目录的发现路径里**：

    注入  mount(task, db)    → 写 agent_tasks.skill_links 并立即 commit
    执行  task_runner 原有路径（prompt 前缀由 prompt_prefix 提供，不改 task.prompt）
    回收  unmount(task_id)   → 只删本次自己建的条目
    兜底  sweep()            → 启动清扫崩溃残留

三条语义边界（与 skill_manager.migrate 的「用户主动搬一份」完全不同，别照着改）：

1. **只能加不能减**：全局根里已有的其它 skill 照样会被加载。
2. **冲突一律让路**：目标位置已有同名目录时只跳过，绝不覆盖、绝不进回收站。
3. **只动自己建的**：回收只认 `skill_links.created` 里的条目，其余一概不碰。

Python 版本上限 3.10（venv 实测 3.10.18），不要用 3.11+ 的语法与标准库。
"""
import json
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from sqlalchemy import text

from . import models, skill_manager
from .database import SessionLocal

logger = logging.getLogger(__name__)

# 与 task_runner.TERMINAL_STATUSES 同值。这里刻意不 import task_runner：批 2 会在
# task_runner 里 import 本模块，反向 import 会成环。
TERMINAL_STATUSES = ("success", "failed", "timeout", "cancelled", "precheck_failed")

# 进程内引用计数：{路径比较键: 持有它的任务 id 集合}。mount 加、unmount 减，
# 归零才真正删目录。不做「查 DB 里其他活跃任务」的原因见方案 §5.6（SQLite 上
# 无索引 LIKE 全表扫，且桌面壳与后端同进程，跨进程没有意义）；跨重启由 sweep 兜底。
_skill_refs: dict = {}
# 创建时记录的条目类型 {路径比较键: "junction"|"copy"}。接管共享引用时用它
# 还原 kind（接管方没有真的创建过目录，只能读这里）。
_skill_kinds: dict = {}
_refs_lock = threading.RLock()


class SkillMountError(Exception):
    """注入阶段失败（源已不存在 / 目录不可用 / 建链接与复制都失败）。

    调用方（task_runner）据此把任务落 FAILED 并**不拉起进程**；本模块在抛出前
    已经清掉本次刚建的东西，不留半截目录。
    """


# ---------------- 公开接口 ----------------


def mount(task, db) -> dict:
    """把 `task.skills` 快照挂进 `task.working_dir` 的 `.<tool>/skills`。

    返回 skill_links 结构（字段见方案 §4.1）并**立即 commit**——这一步必须独立于
    调用方事务、且必须早于拉起引擎进程：`_execute` 的 except 分支会 rollback()，
    晚于注入的 commit 就会把回收依据一起回滚掉（方案 §5.5）。

    注入失败抛 SkillMountError，抛出前已回收本次创建的条目。
    """
    skills = _load_snapshot(task)
    if not skills:
        return _empty_links(skipped_reason="no_skills")

    workdir = str(getattr(task, "working_dir", "") or "").strip()
    if not workdir:
        # 方案 §5.3：工作目录为空时任务跑在用户主目录，往主目录的 .<tool>/skills
        # 挂任务级 skill 等于污染全局视角，与「只影响这个项目」冲突。不建任何目录。
        return _empty_links(skipped_reason="no_workdir")

    tool = str(getattr(task, "tool", "") or "").strip()
    try:
        target_root = skill_manager.custom_project_skills_root(workdir, tool)
    except ValueError as e:
        raise SkillMountError(f"工作目录不可用：{e}") from e

    task_id = getattr(task, "id", None)
    links = {
        "target_root": str(target_root),
        "root_created": False,
        "created": [],
        "skipped_existing": [],
        # 归一化被跳过的条目：{"name": ..., "reason": ...}。字段名本模块自定，
        # 方案只要求「跳过并提示」，不参与回收。
        "skipped_invalid": [],
        # 端相关提示（pi / OpenCode），只用于回显，不阻断注入（附录 C3）。
        "notices": [],
    }
    known_roots = _known_roots(workdir)
    try:
        for ref in skills:
            _mount_one(task_id, tool, target_root, ref, links, known_roots)
        if links["created"]:
            # 落库失败同样算注入失败：必须把刚建的目录一起清掉，否则留下一批
            # 没有回收依据的残留
            _write_links(db, task, json.dumps(links, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 - 统一成 SkillMountError，且必须清场
        _rollback_mount(task_id, links)
        if isinstance(e, SkillMountError):
            raise
        raise SkillMountError(f"技能注入失败：{e}") from e
    return links


def unmount(task_id: int, db=None) -> dict:
    """按 `skill_links` 回收本次新建的条目；只删自己建的，删不掉只记日志。

    返回 {"removed": [...], "failed": [...], "root_removed": bool}。
    回收阶段异常一律不外抛（方案 §3）：删不掉留给下次启动的 sweep。
    """
    own_session = db is None
    session = db if db is not None else SessionLocal()
    result = {"removed": [], "failed": [], "root_removed": False}
    try:
        return _unmount_inner(session, task_id, result)
    except Exception:  # noqa: BLE001 - 回收阶段只记日志，绝不外抛
        logger.exception("回收技能链接异常：任务 #%s", task_id)
        return result
    finally:
        if own_session:
            session.close()


def _unmount_inner(session, task_id: int, result: dict) -> dict:
    task = session.get(models.AgentTask, task_id)
    if task is None:
        return result
    links = _parse_links(_read_links(session, task))
    if not links:
        return result

    for item in links.get("created") or []:
        path_text = str(item.get("path") or "").strip()
        if not path_text:
            continue
        key = skill_manager._normkey(path_text)
        with _refs_lock:
            holders = _skill_refs.get(key)
            if holders is not None:
                holders.discard(task_id)
                still_held = bool(holders)
                if not still_held:
                    _skill_refs.pop(key, None)
            else:
                # 进程内没有计数（重启后 / 非本进程 mount）：按本行 links 回收
                still_held = False
        if still_held:
            continue

        kind = item.get("kind") or _skill_kinds.pop(key, "junction")
        error = _remove_created(Path(path_text), kind)
        with _refs_lock:
            _skill_kinds.pop(key, None)
        if error:
            result["failed"].append(path_text)
            logger.warning("回收技能链接失败（留给启动 sweep）：%s：%s", path_text, error)
        else:
            result["removed"].append(path_text)

    root_text = str(links.get("target_root") or "").strip()
    if links.get("root_created") and root_text:
        result["root_removed"] = _remove_root_if_empty(Path(root_text))

    # 回收依据清空、快照（task.skills）保留：详情与复跑要用快照。
    _write_links(session, task, "")
    return result


def sweep() -> dict:
    """启动清扫：扫 `skill_links` 非空且已终态的任务行，逐个 unmount。

    pending / running 的行不碰（可能正在跑）。返回 {"cleaned": n, "failed": m}。
    """
    cleaned = failed = 0
    db = SessionLocal()
    try:
        if not _has_links_column(db):
            # 批 1 尚未给 agent_tasks 补 skill_links 列（批 2 补），此时不存在残留。
            logger.warning("agent_tasks 缺少 skill_links 列，跳过本次 skill 清扫")
            return {"cleaned": 0, "failed": 0}
        statuses = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
        rows = db.execute(
            text(
                "SELECT id FROM agent_tasks "
                f"WHERE COALESCE(skill_links, '') <> '' AND status IN ({statuses})"
            )
        ).fetchall()
        for (task_id,) in rows:
            try:
                result = unmount(task_id, db)
            except Exception:  # noqa: BLE001 - 单行异常不影响其余行（unmount 已自带兜底）
                failed += 1
                db.rollback()
                logger.exception("清扫技能链接失败：任务 #%s", task_id)
                continue
            if result.get("failed"):
                failed += 1
            else:
                cleaned += 1
    finally:
        db.close()
    if cleaned or failed:
        logger.info("skill 清扫：清理 %s 条，失败 %s 条", cleaned, failed)
    return {"cleaned": cleaned, "failed": failed}


def prompt_prefix(skills: list[dict]) -> str:
    """生成 prompt 前缀（只返回字符串，不写回 task.prompt）。

    列表为空返回空串；调用方只在 `task.skills` 非空时才拼接。
    """
    names = []
    for item in skills or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("dir_name") or "").strip()
        if name:
            names.append(name)
    if not names:
        return ""
    return f"本次可用的技能：{'、'.join(names)}。请优先使用它们完成任务。\n\n"


# ---------------- 注入实现 ----------------


def _mount_one(task_id, tool: str, target_root: Path, ref: dict, links: dict, known_roots) -> None:
    """注入单个 skill：判冲突 → 定位源 → 归一化 → 建链接（失败回退复制）。"""
    name = str(ref.get("name") or "").strip()
    dir_name = str(ref.get("dir_name") or "").strip() or name
    if not dir_name:
        return  # 快照元素来自 API 校验，正常不会出现空名条目
    dest = target_root / dir_name
    key = skill_manager._normkey(dest)

    if os.path.lexists(str(dest)):
        with _refs_lock:
            holders = set(_skill_refs.get(key) or ())
        if holders - {task_id}:
            # 方案 A（§5.6 并发接管）：该链接已被本应用**另一个活任务**持有。
            # 不重建、不覆盖，只把本任务加进引用集合；回收同样走计数，先结束的
            # 那个不删，等最后一个持有者退出才真删。
            with _refs_lock:
                _skill_refs.setdefault(key, set()).add(task_id)
            links["created"].append(
                {
                    "name": name or dir_name,
                    "kind": _skill_kinds.get(key, "junction"),
                    "path": str(dest),
                    "shared": True,
                }
            )
            return
        # 目标已存在同名目录：一律跳过，绝不覆盖、绝不进回收站。
        # 这与 migrate() 的冲突策略刻意相反——迁移是用户主动搬一份，冲突时把
        # 旧版移入回收站；任务只是临时借用，冲突时只能让路（方案 §5.4）。
        links["skipped_existing"].append(dir_name)
        return

    source = _resolve_source(ref, tool, known_roots)
    if source is None:
        raise SkillMountError(f"技能「{name or dir_name}」已不存在，请重新选择")
    info = skill_manager.read_skill(source)
    if info is None:
        raise SkillMountError(f"技能「{name or dir_name}」的 {skill_manager.SKILL_FILE} 不可读，请重新选择")

    # 归一化只按 description 判定（附录 C3）：缺 description / 为空 → 跳过并提示。
    # name 不合规（大写/中文/空格/下划线/bad--name）只作提示、不阻断、不改名——
    # pi 1.0.2 实测对不合规 name 只告警照常加载，pi_skill_notice() 的返回值不能
    # 当阻断条件（它对合规名也会报「不会加载」）。
    if not str(info.get("description") or "").strip():
        links["skipped_invalid"].append(
            {"name": name or dir_name, "reason": "frontmatter 缺少非空 description，模型端不会使用该技能"}
        )
        return

    notice = _end_notice(tool, source)
    if notice:
        links["notices"].append({"name": name or dir_name, "message": notice})

    # 附录 C1.1：mklink 不建父目录，必须先 mkdir(parents=True)
    links["root_created"] = _ensure_root(target_root) or links["root_created"]
    kind = _create_link(dest, source)
    with _refs_lock:
        _skill_refs.setdefault(key, set()).add(task_id)
        _skill_kinds[key] = kind
    links["created"].append({"name": name or dir_name, "kind": kind, "path": str(dest)})


def _resolve_source(ref: dict, tool: str, known_roots):
    """三级兜底定位源目录（方案 §5.2，前一条命中即止）。三条全落空返回 None。"""
    name = str(ref.get("name") or "").strip()

    # ① 快照里的 source_path：必须落在已知 skill 根之内（防任意路径）且可读。
    #    越界路径直接不采信（§8 的校验在 API 层，注入层再守一道）。
    raw_path = str(ref.get("source_path") or "").strip()
    if raw_path:
        candidate = Path(raw_path)
        if _in_known_roots(candidate, known_roots):
            if skill_manager.read_skill(candidate) is not None:
                return candidate
            logger.info("快照中的来源已不可读，改按名称重新定位：%s", candidate)
        else:
            logger.warning("快照来源不在已知 skill 根内，已忽略：%s", candidate)

    # ② 按 {scope, tool, project, name} 定位（与迁移同一套口径，不做第二套实现）
    scope = str(ref.get("source_scope") or "").strip()
    source_tool = str(ref.get("source_tool") or "").strip()
    project = str(ref.get("source_project") or "").strip() or None
    if scope in ("global", "project") and source_tool in skill_manager.TOOLS and name:
        try:
            path, _entry = skill_manager.resolve_source(
                {"scope": scope, "tool": source_tool, "project": project, "name": name}
            )
            return path
        except (ValueError, OSError) as e:
            logger.info("按快照描述符未找到技能「%s」（%s），转跨端兜底", name, e)

    if not name:
        return None

    # ③ 跨端兜底：discover() 聚合结果里找同名条目的任意可读副本。
    #    取源优先「与目标任务同端」，其次项目级，最后任意（§5.2 跨端取源原则）。
    try:
        data = skill_manager.discover()
    except Exception:  # noqa: BLE001 - 发现失败按「未找到」处理，由调用方报错
        logger.exception("跨端兜底发现失败：%s", name)
        return None
    lowered = name.lower()
    buckets = [data.get("global") or []]
    buckets.extend(proj.get("skills") or [] for proj in data.get("projects") or [])
    for bucket in buckets:
        for item in bucket:
            aliases = {
                str(item.get("name") or "").lower(),
                str(item.get("display_name") or "").lower(),
            }
            if lowered not in aliases:
                continue
            copies = sorted(
                item.get("copies") or [],
                key=lambda c: (c.get("tool") != tool, c.get("scope") != "project"),
            )
            for copy in copies:
                path = Path(str(copy.get("path") or ""))
                if skill_manager.read_skill(path) is not None:
                    return path
    return None


def _create_link(dest: Path, source: Path) -> str:
    """优先建目录联接，失败回退复制；返回 kind（"junction" / "copy"）。"""
    code, output = _mklink(dest, source)
    if code == 0 and skill_manager._safe_is_dir(dest):
        return "junction"
    if code == 0:
        # 附录 C1.2：mklink **不校验源**，源在检查之后消失时退出码仍为 0，只会留下
        # 悬空联接（lexists=True、isdir=False）。这种链接必须立刻摘掉，否则等于
        # 注入了一个空技能、还往 skill_links 记一条成功项。
        logger.warning("联接指向的源已不可读，撤销悬空联接：%s", dest)
        _remove_created(dest, "junction")
    else:
        # 附录 C1.3：回退判据只看 returncode，中文文案（完成该操作需要本地卷 /
        # 系统找不到指定的路径 / 拒绝访问）仅进日志。
        logger.warning("目录联接失败（退出码 %s）：%s", code, (output or "").strip())
    _copy_into(dest, source)
    return "copy"


def _mklink(dest: Path, source: Path) -> tuple:
    """建目录联接（Windows）/ 目录符号链接（其他平台），返回 (退出码, 输出)。

    argv 列表传参、不拼字符串：链接名与路径含中文或空格时同样成立。
    """
    if os.name == "nt":
        argv = ["cmd", "/c", "mklink", "/J", str(dest), str(source)]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return 1, str(e)
        return proc.returncode, " ".join(p for p in (proc.stdout, proc.stderr) if p)
    try:
        os.symlink(str(source), str(dest), target_is_directory=True)
        return 0, ""
    except OSError as e:
        return 1, str(e)


def _copy_into(dest: Path, source: Path) -> None:
    """staging 复制 + rename 换入，失败不留半截（复用 migrate 的成熟写法）。

    ignore=skill_manager._ignore_links：技能包里的链接不展开，避免环状链接爆栈
    与 junction 被复制成实体大目录。
    """
    staging = dest.parent / f".{dest.name}.a4agent-staging"
    if os.path.lexists(str(staging)):
        shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(source, staging, ignore=skill_manager._ignore_links)
        staging.rename(dest)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise SkillMountError(f"技能复制失败（{dest.name}）：{e}") from e


def _ensure_root(root: Path) -> bool:
    """建目标根（附录 C1.1：必须在 mklink 之前）。返回本次是否新建。"""
    if skill_manager._safe_is_dir(root):
        return False
    root.mkdir(parents=True, exist_ok=True)
    return True


def _rollback_mount(task_id, links: dict) -> None:
    """注入失败时清掉本次刚建的东西（§3：不留半截目录）。"""
    for item in list(links.get("created") or []):
        path_text = str(item.get("path") or "")
        key = skill_manager._normkey(path_text)
        if item.get("shared"):
            # 接管来的引用只退计数，目录本身是别的任务建的
            with _refs_lock:
                holders = _skill_refs.get(key)
                if holders is not None:
                    holders.discard(task_id)
                    if not holders:
                        _skill_refs.pop(key, None)
            continue
        _remove_created(Path(path_text), str(item.get("kind") or "junction"))
        with _refs_lock:
            _skill_refs.pop(key, None)
            _skill_kinds.pop(key, None)
    links["created"] = []
    if links.get("root_created") and links.get("target_root"):
        _remove_root_if_empty(Path(links["target_root"]))


# ---------------- 回收实现 ----------------


def _remove_created(path: Path, kind: str) -> str:
    """删掉一个自己建的条目，返回空串表示成功（或本就不存在），否则返回原因。"""
    if not os.path.lexists(str(path)):
        return ""
    link_like = os.path.islink(str(path)) or (
        os.name == "nt" and skill_manager._is_reparse_point(str(path))
    )
    if kind == "copy" and not link_like:
        try:
            shutil.rmtree(path)
            return ""
        except OSError as e:
            return str(e)
    # 附录 C2.2：junction 绝不能用 rmtree（py3.10 直接抛
    # "Cannot call rmtree on a symbolic link"，兜底等于没有）。
    # 正确顺序：os.rmdir → os.unlink → 只记日志。
    try:
        os.rmdir(str(path))
        return ""
    except OSError:
        pass
    try:
        os.unlink(str(path))
        return ""
    except OSError as e:
        return str(e)


def _remove_root_if_empty(root: Path) -> bool:
    """root_created 的根目录空了才删；非空或失败都返回 False。"""
    try:
        if not root.is_dir():
            return False
        if any(root.iterdir()):
            return False
        os.rmdir(str(root))
        return True
    except OSError as e:
        logger.warning("删除空的技能根目录失败：%s：%s", root, e)
        return False


# ---------------- skill_links / 快照读写 ----------------


def _empty_links(skipped_reason: str = "") -> dict:
    links = {
        "target_root": "",
        "root_created": False,
        "created": [],
        "skipped_existing": [],
        "skipped_invalid": [],
        "notices": [],
    }
    if skipped_reason:
        links["skipped_reason"] = skipped_reason
    return links


def _load_snapshot(task) -> list:
    """读 `task.skills`（JSON 字符串快照）；空/损坏按「没有技能」处理。"""
    raw = getattr(task, "skills", None)
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("task.skills 不是合法 JSON，按未选技能处理")
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _parse_links(raw: str) -> dict:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning("skill_links 不是合法 JSON，按无回收项处理")
        return {}
    if not isinstance(data, dict):
        return {}
    data.setdefault("created", [])
    return data


def _read_links(db, task) -> str:
    """读 skill_links：优先 ORM 属性，其次按 id 直查（批 2 映射前属性不在实例上）。"""
    raw = getattr(task, "skill_links", None)
    if isinstance(raw, str):
        return raw
    task_id = getattr(task, "id", None)
    if task_id is None or not _has_links_column(db):
        return ""
    row = db.execute(
        text("SELECT skill_links FROM agent_tasks WHERE id = :i"), {"i": task_id}
    ).first()
    return (row[0] or "") if row else ""


def _write_links(db, task, value: str) -> None:
    """把 skill_links 写进任务行并**立即 commit**（方案 §5.5）。

    批 2 给模型补上映射列后，setattr + commit 就是唯一路径；批 1 阶段该列尚未
    映射在 ORM 上，需要额外一条 UPDATE 才能真落库（测试按批 2 的列结构建库）。
    """
    task.skill_links = value
    task_id = getattr(task, "id", None)
    if task_id is not None and "skill_links" not in models.AgentTask.__table__.columns:
        if _has_links_column(db):
            db.execute(
                text("UPDATE agent_tasks SET skill_links = :v WHERE id = :i"),
                {"v": value, "i": task_id},
            )
        else:
            logger.warning("agent_tasks 尚无 skill_links 列，回收依据只存在进程内（批 2 补列后落库）")
    db.commit()


def _has_links_column(db) -> bool:
    try:
        rows = db.execute(text("PRAGMA table_info(agent_tasks)")).fetchall()
    except Exception:  # noqa: BLE001 - 非 SQLite / 连接异常时按「没有该列」处理
        return False
    return any(row[1] == "skill_links" for row in rows)


# ---------------- 杂项 ----------------


def _in_known_roots(path: Path, known_roots) -> bool:
    parent = skill_manager._normkey(path.parent)
    return any(skill_manager._normkey(root) == parent for root in known_roots)


def _known_roots(workdir: str) -> list:
    """可作为注入源的 skill 根：全局根 + 已知项目级根 + 本任务工作目录的项目级根。"""
    roots: list = []
    try:
        roots.extend(skill_manager.all_known_skill_roots())
    except Exception:  # noqa: BLE001 - 枚举失败时退回全局根，不阻断注入
        logger.warning("枚举已知 skill 根失败，改用全局根兜底", exc_info=True)
        roots.extend(skill_manager.global_skill_roots().values())
    try:
        roots.extend(skill_manager.project_skill_roots(Path(workdir)).values())
    except (OSError, ValueError):
        pass
    return roots


def _end_notice(tool: str, source: Path) -> str:
    """端相关提示（非阻断）：pi 的 name/description 规则、OpenCode 的目录名身份。"""
    if tool == "pi":
        return skill_manager.pi_skill_notice(source)
    if tool == "opencode":
        return skill_manager.opencode_skill_notice(source)
    return ""
