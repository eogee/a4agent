"""任务级 skill 注入器测试：mount / unmount / sweep / prompt_prefix。

全部用**真实临时目录**（tmp_path），不碰 `%LOCALAPPDATA%` 与用户真实的 skill 根：
全局根、项目根都 monkeypatch 到临时目录，`SessionLocal` 换成临时 sqlite。

批 1 阶段 `agent_tasks` 还没有 skills / skill_links 列（批 2 才补），夹具按批 2 的
最终列结构补上，让 skill_links 的落库与清空路径能真跑。
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.app import skill_manager, task_skills
from backend.app.database import Base
from backend.app.models import AgentTask


def _make_skill(parent: Path, dir_name: str = "pdf", name: str = "pdf",
                description: str = "处理 PDF 文件") -> Path:
    """造一个最小可用 skill bundle（frontmatter + 一个 SKILL.md）。"""
    skill_dir = parent / dir_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# 正文\n",
        encoding="utf-8",
    )
    return skill_dir


def _ref(path: Path, name: str = "pdf", dir_name: str = "pdf",
         tool: str = "claude", scope: str = "global", project: str = "") -> dict:
    """skills 快照元素（字段与方案 §4.1 一致）。"""
    return {
        "name": name,
        "dir_name": dir_name,
        "source_tool": tool,
        "source_scope": scope,
        "source_project": project,
        "source_path": str(path),
    }


def _tree_hash(root: Path) -> str:
    """目录内容哈希：比对「原目录未被改动」用（名称 + 内容，跳过链接本身）。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _purge_links(root: Path) -> None:
    """清掉临时目录里的 junction：py3.10 的 rmtree 遇到联接会直接抛。"""
    if not root.exists():
        return
    for dirpath, dirnames, _files in os.walk(str(root), topdown=False):
        for name in dirnames:
            path = Path(dirpath) / name
            if os.path.islink(str(path)) or (
                os.name == "nt" and skill_manager._is_reparse_point(str(path))
            ):
                task_skills._remove_created(path, "junction")


@pytest.fixture(autouse=True)
def _reset_refs():
    """引用计数是模块级单例：每个用例都必须从干净状态开始。"""
    task_skills._skill_refs.clear()
    task_skills._skill_kinds.clear()
    yield
    task_skills._skill_refs.clear()
    task_skills._skill_kinds.clear()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    engine = create_engine(f"sqlite:///{tmp_path / 'tasks.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE agent_tasks ADD COLUMN skills TEXT DEFAULT ''"))
        conn.execute(text("ALTER TABLE agent_tasks ADD COLUMN skill_links TEXT DEFAULT ''"))
    maker = sessionmaker(bind=engine)

    # 全局根与项目扫描全部沙箱化：否则 discover() 会扫真机所有盘符
    global_root = tmp_path / "global"
    global_root.mkdir()
    monkeypatch.setattr(
        skill_manager, "global_skill_roots",
        lambda: {tool: global_root / tool for tool in skill_manager.TOOLS},
    )
    monkeypatch.setattr(skill_manager, "project_dirs", lambda roots=None: [])
    monkeypatch.setattr(
        skill_manager, "all_known_skill_roots",
        lambda: [global_root / tool for tool in skill_manager.TOOLS],
    )
    monkeypatch.setattr(task_skills, "SessionLocal", maker)

    workdir = tmp_path / "proj"
    workdir.mkdir()
    try:
        yield SimpleNamespace(
            maker=maker, tmp=tmp_path, global_root=global_root, workdir=workdir
        )
    finally:
        _purge_links(tmp_path)


def _new_task(db, skills: list, tool: str = "claude", workdir="", status: str = "pending"):
    task = AgentTask(
        prompt="干活",
        tool=tool,
        working_dir=str(workdir),
        status=status,
        timeout_seconds=60,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    # 批 1 阶段 skills 还没映射到 ORM，直接挂在实例上（批 2 由 API 落库）
    task.skills = json.dumps(skills, ensure_ascii=False) if skills else ""
    return task


# ---------------- 方案 §12.1：十条 ----------------


def test_mount_global_skill_into_project(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    task = _new_task(db, [_ref(src)], workdir=env.workdir)

    links = task_skills.mount(task, db)

    root = env.workdir / ".claude" / "skills"
    assert links["target_root"] == str(root)
    assert links["root_created"] is True
    assert links["skipped_existing"] == []
    assert len(links["created"]) == 1
    item = links["created"][0]
    assert item["name"] == "pdf"
    assert item["path"] == str(root / "pdf")
    assert item["kind"] in ("junction", "copy")
    assert skill_manager.read_skill(root / "pdf")["name"] == "pdf"
    # §5.5：skill_links 必须已独立落库（调用方之后才拉起进程）
    stored = db.execute(
        text("SELECT skill_links FROM agent_tasks WHERE id = :i"), {"i": task.id}
    ).scalar()
    assert json.loads(stored)["created"][0]["path"] == str(root / "pdf")

    # 归一化只按 description 拦（附录 C3）：缺 description 只跳过、不报错
    bare = env.global_root / "claude" / "bare"
    bare.mkdir()
    (bare / "SKILL.md").write_text("---\nname: bare\n---\n# 正文\n", encoding="utf-8")
    task2 = _new_task(db, [_ref(bare, name="bare", dir_name="bare")], workdir=env.workdir)
    links2 = task_skills.mount(task2, db)
    assert links2["created"] == []
    assert links2["skipped_invalid"][0]["name"] == "bare"
    assert not os.path.lexists(str(root / "bare"))

    task_skills.unmount(task.id, db)
    db.close()


def test_mount_skips_existing_and_keeps_content(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    root = env.workdir / ".claude" / "skills"
    mine = _make_skill(root, description="用户自己装的那份")
    before = _tree_hash(mine)
    task = _new_task(db, [_ref(src)], workdir=env.workdir)

    links = task_skills.mount(task, db)

    assert links["created"] == []
    assert links["skipped_existing"] == ["pdf"]
    assert links["root_created"] is False
    assert _tree_hash(mine) == before  # 绝不覆盖、绝不进回收站
    assert links["target_root"] == str(root)
    db.close()


def test_mount_without_workdir_creates_nothing(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    task = _new_task(db, [_ref(src)], workdir="")

    links = task_skills.mount(task, db)

    assert links["skipped_reason"] == "no_workdir"
    assert links["target_root"] == ""
    assert links["created"] == []
    assert not (env.workdir / ".claude").exists()
    assert list(env.workdir.iterdir()) == []
    db.close()


def test_mount_missing_source_raises_with_name(env):
    db = env.maker()
    ghost = env.global_root / "claude" / "ghost"
    task = _new_task(db, [_ref(ghost, name="ghost", dir_name="ghost")], workdir=env.workdir)

    with pytest.raises(task_skills.SkillMountError) as exc:
        task_skills.mount(task, db)

    assert "ghost" in str(exc.value)
    assert not (env.workdir / ".claude").exists()  # 失败不留半截目录
    db.close()


def test_mount_falls_back_to_copy_when_mklink_fails(env, monkeypatch):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    monkeypatch.setattr(task_skills, "_mklink", lambda dest, source: (1, "拒绝访问。"))
    task = _new_task(db, [_ref(src)], workdir=env.workdir)

    links = task_skills.mount(task, db)

    item = links["created"][0]
    assert item["kind"] == "copy"
    dest = Path(item["path"])
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == (
        src / "SKILL.md"
    ).read_text(encoding="utf-8")
    staging = dest.parent / f".{dest.name}.a4agent-staging"
    assert not staging.exists()  # 暂存目录不残留

    task_skills.unmount(task.id, db)
    assert not dest.exists()
    db.close()


def test_unmount_removes_only_its_own_entries(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    task = _new_task(db, [_ref(src)], workdir=env.workdir)
    task_skills.mount(task, db)

    root = env.workdir / ".claude" / "skills"
    manual = _make_skill(root, dir_name="handmade", name="handmade", description="手工放的")
    result = task_skills.unmount(task.id, db)

    assert str(root / "pdf") in result["removed"]
    assert not os.path.lexists(str(root / "pdf"))
    assert (manual / "SKILL.md").is_file()
    assert root.exists()  # 还有别人的条目 → 根目录不能删
    db.close()


def test_unmount_removes_empty_self_created_root(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    task = _new_task(db, [_ref(src)], workdir=env.workdir)
    links = task_skills.mount(task, db)
    assert links["root_created"] is True

    result = task_skills.unmount(task.id, db)

    assert result["root_removed"] is True
    assert not (env.workdir / ".claude" / "skills").exists()
    db.close()


def test_ref_count_keeps_link_until_last_task(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    first = _new_task(db, [_ref(src)], workdir=env.workdir)
    second = _new_task(db, [_ref(src)], workdir=env.workdir)

    links_first = task_skills.mount(first, db)
    links_second = task_skills.mount(second, db)
    dest = Path(links_first["created"][0]["path"])
    assert links_second["created"][0]["shared"] is True

    task_skills.unmount(first.id, db)
    assert os.path.lexists(str(dest))  # 先结束的那个不删：还有一个活任务在用

    task_skills.unmount(second.id, db)
    assert not os.path.lexists(str(dest))
    db.close()


def test_sweep_cleans_terminal_residue(env):
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    task = _new_task(db, [_ref(src)], workdir=env.workdir)
    links = task_skills.mount(task, db)
    dest = Path(links["created"][0]["path"])
    task_id = task.id

    # 模拟崩溃：任务已落终态，但 finally 里的 unmount 没跑到
    task.status = "success"
    db.commit()
    db.close()
    task_skills._skill_refs.clear()  # 等价于进程重启：引用计数丢失

    result = task_skills.sweep()

    assert result == {"cleaned": 1, "failed": 0}
    assert not os.path.lexists(str(dest))
    check = env.maker()
    stored = check.execute(
        text("SELECT skill_links FROM agent_tasks WHERE id = :i"), {"i": task_id}
    ).scalar()
    assert stored == ""
    check.close()


def test_mount_rejects_source_outside_known_roots(env):
    db = env.maker()
    outside = _make_skill(env.tmp / "outside", description="越界来源")
    task = _new_task(db, [_ref(outside)], workdir=env.workdir)

    with pytest.raises(task_skills.SkillMountError):
        task_skills.mount(task, db)
    assert not os.path.lexists(str(env.workdir / ".claude" / "skills" / "pdf"))

    # 同名 skill 在合法根里有副本时，只采信合法那份，越界路径永不采信
    legit = _make_skill(env.global_root / "claude", description="合法来源")
    task2 = _new_task(db, [_ref(outside)], workdir=env.workdir)
    links = task_skills.mount(task2, db)
    dest = Path(links["created"][0]["path"])
    assert "合法来源" in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert "越界来源" not in (dest / "SKILL.md").read_text(encoding="utf-8")
    assert str(legit) != str(outside)

    task_skills.unmount(task2.id, db)
    db.close()


# ---------------- 附录 C：三条 ----------------


@pytest.mark.skipif(os.name != "nt", reason="junction 是 Windows 专有行为")
def test_dangling_junction_is_not_left_behind(env, monkeypatch):
    """C1.2：mklink 不校验源，源在自校验之后消失会留下悬空联接且退出码 0。"""
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    real_mklink = task_skills._mklink

    def fake_mklink(dest, source):
        shutil.rmtree(source)  # 模拟「自校验通过后源被删掉」
        return real_mklink(dest, source)

    monkeypatch.setattr(task_skills, "_mklink", fake_mklink)
    task = _new_task(db, [_ref(src)], workdir=env.workdir)

    with pytest.raises(task_skills.SkillMountError):
        task_skills.mount(task, db)

    dest = env.workdir / ".claude" / "skills" / "pdf"
    assert not os.path.lexists(str(dest))  # 悬空联接必须被摘掉
    assert not (env.workdir / ".claude" / "skills").exists()  # 自建空根也回收
    db.close()


def test_mount_creates_missing_parent_dirs(env):
    """C1.1：mklink 不建父目录，`.claude` 与 `skills` 都必须先建。"""
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    assert not (env.workdir / ".claude").exists()
    task = _new_task(db, [_ref(src)], workdir=env.workdir)

    links = task_skills.mount(task, db)

    assert (env.workdir / ".claude" / "skills" / "pdf").is_dir()
    assert links["root_created"] is True
    task_skills.unmount(task.id, db)
    db.close()


def test_second_task_takes_over_existing_owned_link(env):
    """§5.6 方案 A：链接已被本应用另一个活任务持有时接管引用（shared）。"""
    db = env.maker()
    src = _make_skill(env.global_root / "claude")
    first = _new_task(db, [_ref(src)], workdir=env.workdir)
    links_first = task_skills.mount(first, db)
    dest = Path(links_first["created"][0]["path"])

    second = _new_task(db, [_ref(src)], workdir=env.workdir)
    links_second = task_skills.mount(second, db)

    assert links_second["created"][0]["shared"] is True
    assert links_second["created"][0]["path"] == str(dest)
    assert links_second["skipped_existing"] == []
    assert links_second["root_created"] is False
    assert dest.is_dir()

    task_skills.unmount(first.id, db)
    assert os.path.lexists(str(dest))
    task_skills.unmount(second.id, db)
    assert not os.path.lexists(str(dest))
    db.close()
