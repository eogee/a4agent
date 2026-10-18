"""项目来源测试：默认盘根 / 存量迁移 / 路径归一 / 线索叠加 / 忽略名单。

历史线索默认被 conftest 关掉（否则会读到开发机真实数据），需要验证线索逻辑的
用例自己打开开关并 monkeypatch 提取器。
"""
import json
import os
import pathlib
from pathlib import Path

import pytest

from backend.app import disk_roots, project_hints, skill_manager
from backend.app.api.v1 import skills as skills_api


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离八端 skill 根与数据目录。返回上下文字典。"""
    ctx = {"data": tmp_path / "data", "root": tmp_path / "scan"}
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(ctx["data"]))
    for tool in skill_manager.TOOLS:
        p = tmp_path / f"{tool}-skills"
        p.mkdir(parents=True)
        monkeypatch.setenv(f"A4AGENT_{tool.upper()}_SKILLS_PATH", str(p))
        ctx[tool] = p
    ctx["root"].mkdir(parents=True)
    return ctx


def _write_projects(payload: dict) -> None:
    path = Path(os.environ["A4AGENT_DATA_DIR"]) / "projects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------- 默认扫描范围 ----------------


def test_default_roots_include_all_drives(monkeypatch):
    """所有可见盘符都在默认范围内；空光驱（isdir 为假）被排除，远程盘不探测。"""
    monkeypatch.setattr(disk_roots, "drive_roots", lambda: ["C:\\", "D:\\", "Z:\\"])
    monkeypatch.setattr(disk_roots, "is_remote", lambda p: str(p).startswith("Z"))
    monkeypatch.setattr(disk_roots.os.path, "isdir", lambda p: str(p) in ("C:\\", "D:\\"))
    assert disk_roots.default_project_roots() == ["C:\\", "D:\\", "Z:\\"]


def test_default_roots_fallback_to_home(monkeypatch):
    """探测不到任何可用盘符时退回主目录，保证默认值非空。"""
    monkeypatch.setattr(disk_roots, "drive_roots", lambda: [])
    assert disk_roots.default_project_roots() == [str(Path.home())]


def test_load_roots_uses_defaults_when_file_missing(env, monkeypatch):
    monkeypatch.setattr(skill_manager, "default_project_roots", lambda: ["D:\\"])
    assert skill_manager.load_project_roots() == ["D:\\"]


def test_legacy_default_root_replaced_when_missing(env, tmp_path, monkeypatch):
    """配置里只有历史预置值、且该目录不存在 → 视为从没配置过，换新默认。"""
    legacy = str(tmp_path / "gone")
    monkeypatch.setattr(skill_manager, "LEGACY_DEFAULT_ROOT", legacy)
    monkeypatch.setattr(skill_manager, "default_project_roots", lambda: ["D:\\"])
    _write_projects({"roots": [legacy]})
    assert skill_manager.load_project_roots() == ["D:\\"]


def test_legacy_default_root_kept_when_exists(env, tmp_path, monkeypatch):
    """历史预置值在本机真实存在时按用户配置对待，不擅自覆盖。"""
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    monkeypatch.setattr(skill_manager, "LEGACY_DEFAULT_ROOT", str(legacy))
    monkeypatch.setattr(skill_manager, "default_project_roots", lambda: ["D:\\"])
    _write_projects({"roots": [str(legacy)]})
    assert skill_manager.load_project_roots() == [str(legacy)]


# ---------------- 路径归一与去重 ----------------


def test_save_roots_normalizes_and_dedupes(env, tmp_path):
    """带引号、正斜杠、大小写不同的同一目录只留一条。"""
    root = tmp_path / "proj"
    root.mkdir()
    cleaned = skill_manager.save_project_roots(
        [f'  "{root}"  ', str(root).replace("\\", "/"), str(root)]
    )
    assert cleaned == [str(root)]


def test_normalize_root_text_completes_drive_letter():
    """只写 D: 时补成 D:\\ ——Windows 上 D: 表示「D 盘当前目录」，是另一个意思。"""
    assert skill_manager.normalize_root_text("D:") == "D:\\"
    assert skill_manager.normalize_root_text("'D:\\'") == "D:\\"
    assert skill_manager.normalize_root_text("   ") == ""


# ---------------- 历史线索叠加 ----------------


def test_project_dirs_merge_scan_roots_and_hints(env, tmp_path, monkeypatch):
    """扫描范围的一级子目录 + 线索反查到的深层目录，都要出现在项目列表。"""
    scan_proj = env["root"] / "alpha"
    scan_proj.mkdir()
    deep = tmp_path / "deep" / "beta"
    (deep / ".claude" / "skills").mkdir(parents=True)

    skill_manager.save_project_roots([str(env["root"])])
    monkeypatch.setattr(
        project_hints, "collect",
        lambda force=False: [{"path": str(deep), "sources": ["Claude Code"]}],
    )

    items = {p["project"]: p for p in skill_manager.project_dirs()}
    assert set(items) == {"alpha", "beta"}
    assert items["alpha"]["source"] == "root"
    assert items["beta"]["source"] == "hints"
    assert "自动发现：Claude Code" in items["beta"]["sources"]
    assert str(items["beta"]["root"]) == str(deep)


def test_hints_without_skills_are_skipped(env, tmp_path, monkeypatch):
    """线索里的普通仓库（没有 .<tool>/skills）不进项目列表。"""
    plain = tmp_path / "plain-repo"
    plain.mkdir()
    monkeypatch.setattr(
        project_hints, "collect",
        lambda force=False: [{"path": str(plain), "sources": ["Cursor"]}],
    )
    skill_manager.save_project_roots([str(env["root"])])
    assert skill_manager.project_dirs() == []


def test_global_scope_overlap_is_not_a_project(env, tmp_path, monkeypatch):
    """主目录/全局根本身不能当项目：它与全局 skill 根是同一个目录，会重复计。"""
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setattr(
        skill_manager, "global_skill_roots",
        lambda: {"claude": home / ".claude" / "skills"},
    )
    assert skill_manager._shadows_global_scope(home) is True
    assert skill_manager._shadows_global_scope(home / ".claude") is True
    assert skill_manager._shadows_global_scope(tmp_path / "work") is False
    assert skill_manager._shadows_global_scope(Path("C:\\")) is True


def test_duplicate_project_names_get_disambiguated(env, tmp_path):
    """两个盘下同名项目必须能区分，否则迁移目标会写错目录。"""
    for name in ("a", "b"):
        (tmp_path / name / "mlj").mkdir(parents=True)
    skill_manager.save_project_roots([str(tmp_path / "a"), str(tmp_path / "b")])
    names = [p["project"] for p in skill_manager.project_dirs()]
    assert len(names) == 2
    assert len(set(names)) == 2
    assert all(n.startswith("mlj (") for n in names)


# ---------------- 忽略名单 ----------------


def test_ignored_projects_hidden_and_preserved(env, tmp_path):
    keep = env["root"] / "keep"
    drop = env["root"] / "drop"
    keep.mkdir()
    drop.mkdir()
    skill_manager.save_project_roots([str(env["root"])])
    skill_manager.save_ignored_projects([str(drop)])

    assert [p["project"] for p in skill_manager.project_dirs()] == ["keep"]
    # 只改根不能把忽略名单冲掉
    skill_manager.save_project_roots([str(env["root"])])
    assert skill_manager.load_ignored_projects() == [str(drop)]


# ---------------- 线索提取器 ----------------


def test_collect_reads_claude_codex_and_editor_history(env, tmp_path, monkeypatch):
    monkeypatch.setenv("A4AGENT_PROJECT_HINTS", "1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: home))

    proj = tmp_path / "proj-one"
    proj.mkdir()

    (home / ".claude.json").write_text(
        json.dumps({"projects": {str(proj): {"allowedTools": []}}}), encoding="utf-8"
    )
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "history.jsonl").write_text(
        json.dumps({"project": str(proj)}) + "\n{坏行\n", encoding="utf-8"
    )
    session = home / ".codex" / "sessions" / "2026" / "10"
    session.mkdir(parents=True)
    (session / "a.jsonl").write_text(
        json.dumps({"payload": {"cwd": str(proj)}}) + "\n第二行不该被读\n", encoding="utf-8"
    )

    appdata = tmp_path / "appdata"
    storage = appdata / "Trae CN" / "User" / "globalStorage" / "storage.json"
    storage.parent.mkdir(parents=True)
    storage.write_text(
        json.dumps({"profileAssociations": {"workspaces": {proj.as_uri(): {}}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(appdata))

    items = project_hints.collect(force=True)
    hit = [i for i in items if i["path"] == str(proj)]
    assert hit, items
    labels = hit[0]["sources"]
    assert "claude-code" in labels
    assert "codex" in labels
    assert "Trae" in labels


def test_collect_returns_empty_when_disabled(env, monkeypatch):
    monkeypatch.setenv("A4AGENT_PROJECT_HINTS", "0")
    assert project_hints.collect(force=True) == []


# ---------------- 接口 ----------------


def test_project_sources_api_shape(env, tmp_path):
    project = env["root"] / "demo"
    project.mkdir()
    (project / ".claude" / "skills").mkdir(parents=True)
    skill_manager.save_project_roots([str(env["root"])])

    data = skills_api.get_project_sources()
    assert data["roots"] == [str(env["root"])]
    assert isinstance(data["defaults"], list) and data["defaults"]
    assert data["ignored"] == []
    assert "hints" in data
    demo = next(p for p in data["projects"] if p["project"] == "demo")
    assert demo["has_skills"] is True
    assert demo["source"] == "root"
