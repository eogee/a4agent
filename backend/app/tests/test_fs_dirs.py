"""本机目录浏览接口：只回子目录、名称排序、上级与盘符层、拒绝相对与不存在路径。"""
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.app.api.v1 import fs


def test_lists_dirs_sorted_and_skips_files(tmp_path):
    (tmp_path / "b_dir").mkdir()
    (tmp_path / "a_dir").mkdir()
    (tmp_path / "note.txt").write_text("x", encoding="utf-8")

    data = fs.list_dirs(str(tmp_path))

    assert [d["name"] for d in data["dirs"]] == ["a_dir", "b_dir"]
    assert Path(data["dirs"][0]["path"]).is_dir()
    assert data["truncated"] is False
    assert Path(data["parent"]) == Path(data["path"]).parent


def test_expands_tilde_to_home():
    data = fs.list_dirs("~")
    assert Path(data["path"]).samefile(Path.home())
    assert Path(data["home"]).samefile(Path.home())


def test_truncates_huge_dir(tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / f"d{i}").mkdir()
    monkeypatch.setattr(fs, "MAX_DIRS", 2)

    data = fs.list_dirs(str(tmp_path))

    assert len(data["dirs"]) == 2
    assert data["truncated"] is True


def test_rejects_relative_and_missing(tmp_path):
    with pytest.raises(HTTPException) as e:
        fs.list_dirs("relative/dir")
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:
        fs.list_dirs(str(tmp_path / "nope"))
    assert e.value.status_code == 404


def test_drive_root_goes_back_to_my_computer(tmp_path, monkeypatch):
    """Windows 盘符根的上级是「我的电脑」（空路径）；非 Windows 没有这一层。"""
    is_nt = os.name == "nt"
    monkeypatch.setattr(fs, "drive_roots", lambda: ["C:\\"] if is_nt else [])
    data = fs.list_dirs("C:\\" if is_nt else "/")
    assert data["parent"] == ("" if is_nt else None)

    top = fs.list_dirs("")
    assert top["path"] == ("" if is_nt else "/")
    assert top["parent"] is None
    assert top["drives"] == (["C:\\"] if is_nt else [])
