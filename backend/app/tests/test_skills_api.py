"""Skill 发现接口测试：discover 的 workdir 附加条目（工作目录技能）。

背景：项目发现（project_dirs）只覆盖「扫描范围一级子目录 + 会话线索」，
任务选中的工作目录未必在其中；discover(workdir=...) 把该目录的项目级
skill 扫成一条「工作目录」条目，让选择器能看到未注册目录自己的技能。

全部通过 tmp_path + monkeypatch 隔离，绝不触碰真实用户目录。
"""
from pathlib import Path

import pytest

from backend.app import skill_manager
from backend.app.api.v1.skills import discover_skills


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离数据目录与九端 skill 根；项目发现清空（不扫真实磁盘）。"""
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))
    for tool in ("CLAUDE", "CODEX", "DSH", "ZCODE", "PI", "QODER", "OPENCODE",
                 "WORKBUDDY", "TRAE"):
        monkeypatch.setenv(f"A4AGENT_{tool}_SKILLS_PATH", str(tmp_path / f"{tool.lower()}-skills"))
    monkeypatch.setenv("A4AGENT_OPENCODE_CONFIG_DIR", str(tmp_path / "oc-config"))
    registered: list[dict] = []
    monkeypatch.setattr(skill_manager, "project_dirs", lambda roots=None: registered)
    return {"registered": registered, "tmp": tmp_path}


def _make_skill(root: Path, tool: str, name: str, description: str = "演示技能") -> Path:
    d = root / f".{tool}" / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文", encoding="utf-8")
    return d


def test_discover_includes_workdir_entry(isolated):
    """工作目录里有项目级技能（哪怕目录未注册）→ discover 出一条 source=workdir 的条目。"""
    wd = isolated["tmp"] / "my-proj"
    _make_skill(wd, "claude", "demo")
    _make_skill(wd, "pi", "helper")

    data = discover_skills(workdir=str(wd))
    entries = [p for p in data["projects"] if p.get("source") == "workdir"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["root"] == str(wd)
    assert entry["project"] == "my-proj"
    names = {s["name"] for s in entry["skills"]}
    assert {"demo", "helper"} <= names


def test_discover_dedupes_when_workdir_is_registered_project(isolated):
    """工作目录本身就是已发现项目 → 不再追加 workdir 条目（避免同一目录两张卡）。"""
    wd = isolated["tmp"] / "known-proj"
    _make_skill(wd, "claude", "demo")
    isolated["registered"].append(
        {"project": "known-proj", "root": wd, "source": "hints", "sources": ["自动发现"]})

    data = discover_skills(workdir=str(wd))
    assert not [p for p in data["projects"] if p.get("source") == "workdir"]
    # 已发现项目本身照常出现在结果里
    assert any(Path(p["root"]) == wd for p in data["projects"])


def test_discover_ignores_invalid_workdir(isolated):
    """空 / 相对路径 / 不存在的目录 → 无附加条目也不报错（与不带参数一致）。"""
    for bad in ("", "relative/path", str(isolated["tmp"] / "nope")):
        data = discover_skills(workdir=bad)
        assert not [p for p in data["projects"] if p.get("source") == "workdir"]


def test_discover_without_workdir_entry_when_dir_has_no_skills(isolated):
    """目录存在但没有任何项目级技能 → 不出空条目（避免选择器多一个空分组）。"""
    wd = isolated["tmp"] / "empty-proj"
    wd.mkdir()
    data = discover_skills(workdir=str(wd))
    assert not [p for p in data["projects"] if p.get("source") == "workdir"]


def test_workdir_entry_skills_carry_project_scope(isolated):
    """条目里的技能 scope=project、project=目录名：与真实项目条目同构，前端零特判。"""
    wd = isolated["tmp"] / "scoped"
    _make_skill(wd, "claude", "demo")
    entry = skill_manager.workdir_project_entry(str(wd))
    assert entry is not None
    skill = entry["skills"][0]
    assert skill["copies"], "聚合条目必须带 copies（前端取 source_* 用）"
    assert all(c["scope"] == "project" and c["project"] == "scoped" for c in skill["copies"])
