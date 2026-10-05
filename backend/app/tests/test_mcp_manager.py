"""MCP 管理业务层测试：发现聚合 / 跨端迁移 / 回收站往返 / 传输能力限制。

三端配置均为「配置文件内子结构」：claude.json 顶层 mcpServers、
config.toml 的 [mcp_servers.*]、cordis.patch.yml 的 dsh-mcp-client insert。
全部通过 tmp_path + monkeypatch 隔离路径，绝不触碰真实用户目录。
"""
import json
import pathlib
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app import mcp_manager, schemas
from backend.app.api.v1 import mcp as mcp_api
from backend.app.database import Base
from backend.app.models import McpMigration, McpTrash


# ---------------- 基础设施 ----------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离六端配置文件、dsh patch、数据目录与项目根列表。"""
    ctx = {
        "data": tmp_path / "data",
        "claude_json": tmp_path / "claude.json",
        "codex_toml": tmp_path / "codex.toml",
        "dsh_patch": tmp_path / "cordis.patch.yml",
        "zcode_cli": tmp_path / "zcode-cli.json",
        "pi_mcp": tmp_path / "pi-mcp.json",
        "qoder_mcp": tmp_path / "qoder-mcp.json",
        "projects_root": tmp_path / "projects",
    }
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(ctx["data"]))
    monkeypatch.setenv("A4AGENT_CLAUDE_MCP_PATH", str(ctx["claude_json"]))
    monkeypatch.setenv("A4AGENT_CODEX_CONFIG_PATH", str(ctx["codex_toml"]))
    monkeypatch.setenv("A4AGENT_DSH_MCP_PATCH_PATH", str(ctx["dsh_patch"]))
    monkeypatch.setenv("A4AGENT_ZCODE_CLI_CONFIG_PATH", str(ctx["zcode_cli"]))
    monkeypatch.setenv("A4AGENT_PI_MCP_PATH", str(ctx["pi_mcp"]))
    monkeypatch.setenv("A4AGENT_QODER_MCP_PATH", str(ctx["qoder_mcp"]))
    # 项目根指向临时目录下的 projects_root（skill 的 project_roots 复用于 MCP）
    ctx["projects_root"].mkdir(parents=True)
    from backend.app import skill_manager

    skill_manager.save_project_roots([str(ctx["projects_root"])])
    return ctx


@pytest.fixture()
def db(env):
    engine = create_engine(f"sqlite:///{env['data'] / 'mcp_test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def make_project(env, name: str):
    p = env["projects_root"] / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_claude_global(env, name, command="npx", args=("-y", "server"), env_vars=None):
    """向 claude.json 写入一个 stdio server，返回文件路径。"""
    data = {"mcpServers": {name: {"type": "stdio", "command": command, "args": list(args)}}}
    if env_vars:
        data["mcpServers"][name]["env"] = dict(env_vars)
    env["claude_json"].write_text(json.dumps(data), encoding="utf-8")
    return env["claude_json"]


def make_zcode_global(env, name, entry):
    """向 zcode cli/config.json 的 mcp.servers 写入一个 server，保留其它顶层键。"""
    loaded = {}
    if env["zcode_cli"].exists():
        try:
            loaded = json.loads(env["zcode_cli"].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            loaded = {}
    loaded = loaded if isinstance(loaded, dict) else {}
    mcp = dict(loaded.get("mcp") or {})
    servers = dict(mcp.get("servers") or {})
    servers[name] = entry
    mcp["servers"] = servers
    loaded["mcp"] = mcp
    env["zcode_cli"].write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
    return env["zcode_cli"]


def make_zcode_project(env, project, name, entry):
    """向 <项目>/.zcode/config.json 的 mcp.servers 写入一个 server。"""
    p = make_project(env, project)
    zc = p / ".zcode" / "config.json"
    zc.parent.mkdir(parents=True, exist_ok=True)
    data = {"mcp": {"servers": {name: entry}}}
    zc.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return zc


# ---------------- 发现 / 读取 ----------------

TEST_PATCH = """
# 手动保留条目（非 dsh-mcp-client，必须原样保留）
- insert:
    - id: dsh-hook
      name: "../../a4phone/dsh/lib/index.js"
"""


def make_dsh_patch(env, servers):
    """写入带 dsh-mcp-client 条目的 cordis.patch.yml；servers 为归一 dict 列表。"""
    import yaml

    entries = [
        {"insert": [mcp_manager.render_dsh(s) for s in servers]},
    ]
    # 保留一个非管理条目，验证重写不丢失
    raw = yaml.safe_load(TEST_PATCH)
    entries = raw + entries
    env["dsh_patch"].write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return env["dsh_patch"]


def make_codex_global(env, name, command="npx", args=("server",)):
    env["codex_toml"].write_text(
        f'[mcp_servers."{name}"]\ncommand = "{command}"\nargs = {json.dumps(args)}\n',
        encoding="utf-8",
    )
    return env["codex_toml"]


def test_discover_aggregates_across_tools_and_masks_env(env):
    """同名 server 跨端聚合标注端数；env 值在响应中脱敏。"""
    make_claude_global(env, "open-web", env_vars={"API_KEY": "secret-value"})
    make_dsh_patch(
        env,
        [
            {
                "name": "open-web",
                "transport": "stdio",
                "command": "node",
                "args": ["server.js"],
                "env": {"MODE": "stdio"},
                "url": None,
                "headers": {},
                "cwd": None,
            }
        ],
    )
    data = mcp_manager.discover()
    groups = {g["name"]: g for g in data["global"]}
    assert set(groups) == {"open-web"}
    g = groups["open-web"]
    assert g["end_count"] == 2
    assert g["duplicate"] is True
    assert g["ends"] == ["claude", "dsh"]
    # copies 中的 env 已脱敏
    for c in g["copies"]:
        assert all(v == mcp_manager.MASK for v in c["env"].values())
    assert "secret-value" not in json.dumps(data)


def test_discover_projects_only_include_ones_with_servers(env):
    """项目级：仅收录含 server 的项目；claude 读 .mcp.json，codex 读 .codex/config.toml。"""
    pa = make_project(env, "proj-a")
    pb = make_project(env, "proj-b")  # 无 server
    pa_mcp = pa / ".mcp.json"
    pa_mcp.write_text(json.dumps({"mcpServers": {"deploy": {"command": "npx", "args": ["d"]}}}), encoding="utf-8")
    (pa / ".codex").mkdir()
    (pa / ".codex" / "config.toml").write_text('[mcp_servers."deploy"]\ncommand = "x"\n', encoding="utf-8")

    data = mcp_manager.discover()
    projects = {p["project"]: p for p in data["projects"]}
    assert "proj-a" in projects
    assert "proj-b" not in projects
    groups = {g["name"]: g for g in projects["proj-a"]["servers"]}
    assert set(groups) == {"deploy"}
    assert groups["deploy"]["end_count"] == 2
    assert groups["deploy"]["ends"] == ["claude", "codex"]


def test_dsh_patch_preserves_non_managed_entries(env):
    """dsh patch 重写时保留非 dsh-mcp-client 条目（如 a4phone hook）。"""
    make_dsh_patch(env, [])
    servers = mcp_manager.read_dsh_servers(env["dsh_patch"])  # 初始空
    assert servers == []
    # 写回一个 server 后，手动条目仍在
    server = {
        "name": "web",
        "transport": "stdio",
        "command": "node",
        "args": ["index.js"],
        "env": {},
        "url": None,
        "headers": {},
        "cwd": None,
    }
    mcp_manager.write_servers("global", "dsh", None, [server])
    data = mcp_manager.discover()
    names = {g["name"] for g in data["global"]}
    assert "web" in names
    # 手动 hook 条目保留（dsh-mcp-client 只重写它自己的块）
    import yaml

    entries = yaml.safe_load(env["dsh_patch"].read_text(encoding="utf-8"))
    hooks = [e for e in entries if isinstance(e, dict) and any(
        isinstance(i, dict) and i.get("name", "").startswith("..") for i in (e.get("insert") or [])
    )]
    assert hooks, "手动条目被重写丢失"


# ---------------- zcode 端 ----------------

def test_discover_includes_zcode_global_and_project(env):
    """zcode 端：全局 mcp.servers 与项目级 .zcode/config.json 均被发现、聚合标注。"""
    make_zcode_global(env, "git-mcp", {"type": "stdio", "command": "npx", "args": ["-y", "@git/mcp"]})
    make_claude_global(env, "git-mcp", command="npx", args=("-y", "@git/mcp"))
    make_zcode_project(env, "proj-z", "deploy", {"command": "node", "args": ["d.js"]})

    data = mcp_manager.discover()
    # roots 含 zcode
    assert data["roots"]["zcode"] == str(env["zcode_cli"])
    assert "zcode" in data["capability"]
    assert data["capability"]["zcode"] == ["http", "sse", "stdio"]

    groups = {g["name"]: g for g in data["global"]}
    assert "git-mcp" in groups
    assert groups["git-mcp"]["end_count"] == 2
    assert groups["git-mcp"]["ends"] == ["claude", "zcode"]

    projects = {p["project"]: p for p in data["projects"]}
    assert "proj-z" in projects
    zg = projects["proj-z"]["servers"][0]
    assert zg["name"] == "deploy"
    assert zg["ends"] == ["zcode"]
    assert zg["copies"][0]["scope"] == "project"
    # 项目级 zcode 定位正确
    location = mcp_manager.mcp_config_file("project", "zcode", "proj-z")
    assert location == env["projects_root"] / "proj-z" / ".zcode" / "config.json"


def test_zcode_normalize_infers_type_and_keeps_whitelisted_keys(env):
    """type 省略推断（command→stdio / url→http）；enabled/timeoutMs 白名单保留。"""
    make_zcode_global(
        env,
        "mixed",
        {"command": "node", "args": ["s.js"], "enabled": True, "timeoutMs": 60000, "zzUnknown": "x"},
    )
    make_zcode_global(
        env,
        "remote",
        {"type": "http", "url": "http://localhost:3331/mcp", "headers": {"Authorization": "Bearer t"}},
    )
    servers = mcp_manager.read_servers("global", "zcode")
    by_name = {s["name"]: s for s in servers}
    assert by_name["mixed"]["transport"] == "stdio"  # 无 type + command → stdio
    assert by_name["mixed"]["extra"] == {"enabled": True, "timeoutMs": 60000}  # 未知键丢弃
    assert by_name["remote"]["transport"] == "http"
    assert by_name["remote"]["headers"] == {"Authorization": "Bearer t"}

    # 写回 round-trip：白名单键保留、未知键不出现
    mcp_manager.write_servers("global", "zcode", None, servers)
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    written = raw["mcp"]["servers"]
    assert written["mixed"]["enabled"] is True
    assert written["mixed"]["timeoutMs"] == 60000
    assert "zzUnknown" not in written["mixed"]


def test_migrate_stdio_to_zcode_global_and_project_keeps_source(env, db):
    """stdio server 迁移到 zcode 全局 + 项目级，源端保留，mcp.servers 正确写入。"""
    make_claude_global(env, "git-mcp", env_vars={"GIT_TOKEN": "tok123"})
    make_project(env, "proj-z")

    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "git-mcp"}],
        [
            {"scope": "global", "tool": "zcode", "project": None},
            {"scope": "project", "tool": "zcode", "project": "proj-z"},
        ],
    )
    assert result["migrated"] == 2, result
    assert result["failed"] == 0

    # 全局：写入 cli/config.json 的 mcp.servers，且其它顶层键（hooks）保留
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    assert "git-mcp" in raw["mcp"]["servers"]
    # 项目级：写入 <proj>/.zcode/config.json
    proj_cfg = env["projects_root"] / "proj-z" / ".zcode" / "config.json"
    assert proj_cfg.exists()
    proj_raw = json.loads(proj_cfg.read_text(encoding="utf-8"))
    assert "git-mcp" in proj_raw["mcp"]["servers"]
    assert proj_raw["mcp"]["servers"]["git-mcp"]["env"] == {"GIT_TOKEN": "tok123"}
    # 源端保留
    assert {s["name"] for s in mcp_manager.read_servers("global", "claude")} == {"git-mcp"}
    logs = db.query(McpMigration).all()
    assert len(logs) == 2
    assert all(l.status == "success" for l in logs)
    assert {l.target_tool for l in logs} == {"zcode"}


def test_migrate_sse_and_http_to_zcode_supported(env, db):
    """sse / http 传输 zcode 均支持：从 claude 迁移到 zcode 成功。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {
            "a-sse": {"type": "sse", "url": "http://x:1/sse"},
            "b-http": {"type": "http", "url": "http://x:2/mcp"},
        }}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [
            {"scope": "global", "tool": "claude", "project": None, "name": "a-sse"},
            {"scope": "global", "tool": "claude", "project": None, "name": "b-http"},
        ],
        [{"scope": "global", "tool": "zcode", "project": None}],
    )
    assert result["migrated"] == 2, result
    assert result["failed"] == 0
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    servers = raw["mcp"]["servers"]
    assert servers["a-sse"]["type"] == "sse"
    assert servers["b-http"]["type"] == "http"
    assert servers["a-sse"]["url"] == "http://x:1/sse"


def test_mcp_schemas_accept_zcode_tool(env, db):
    """McpSourceIn / McpTargetIn / McpServerRefIn 的 tool 校验接受 zcode。"""
    make_zcode_global(env, "from-zc", {"command": "node", "args": ["z.js"]})
    body = schemas.McpMigrateIn(
        sources=[schemas.McpSourceIn(scope="global", tool="zcode", name="from-zc")],
        targets=[schemas.McpTargetIn(scope="global", tool="claude")],
    )
    result = mcp_api.migrate_mcp(body, db)
    assert result["migrated"] == 1
    assert {s["name"] for s in mcp_manager.read_servers("global", "claude")} == {"from-zc"}
    # McpServerRefIn
    ref = schemas.McpServerRefIn(scope="global", tool="zcode", name="from-zc")
    assert ref.tool == "zcode"


# ---------------- 迁移 ----------------

STDIO_SERVER = {
    "name": "git-mcp",
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@git/mcp"],
    "env": {"GIT_TOKEN": "tok123"},
    "url": None,
    "headers": {},
    "cwd": None,
}


def test_migrate_stdio_to_all_three_keeps_source(env, db):
    """stdio server 可迁移到三端，源端保留，日志逐对落库。"""
    make_claude_global(env, "git-mcp", env_vars={"GIT_TOKEN": "tok123"})

    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "git-mcp"}],
        [
            {"scope": "global", "tool": "codex", "project": None},
            {"scope": "global", "tool": "dsh", "project": None},
        ],
    )
    assert result["migrated"] == 2, result
    assert result["failed"] == 0
    # codex 端出现
    codex_servers = mcp_manager.read_codex_servers(env["codex_toml"])
    assert {s["name"] for s in codex_servers} == {"git-mcp"}
    # dsh 端出现
    dsh_servers = mcp_manager.read_dsh_servers(env["dsh_patch"])
    assert {s["name"] for s in dsh_servers} == {"git-mcp"}
    assert dsh_servers[0]["env"] == {"GIT_TOKEN": "tok123"}
    # 源端（claude）保留
    assert {s["name"] for s in mcp_manager.read_servers("global", "claude")} == {"git-mcp"}
    logs = db.query(McpMigration).all()
    assert len(logs) == 2
    assert all(l.status == "success" for l in logs)


def test_migrate_skips_identical_location(env, db):
    make_claude_global(env, "git-mcp")
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "git-mcp"}],
        [{"scope": "global", "tool": "claude", "project": None}],
    )
    assert result["skipped"] == 1
    assert result["migrated"] == 0


def test_migrate_sse_to_dsh_fails_with_reason(env, db):
    """sse 传输 dsh 不支持：整对失败并留日志，dsh 端不写入。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"remote": {"type": "sse", "url": "http://localhost:3331/sse"}}}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "remote"}],
        [{"scope": "global", "tool": "dsh", "project": None}],
    )
    assert result["migrated"] == 0
    assert result["failed"] == 1
    assert "不支持 sse 传输" in result["results"][0]["detail"]
    assert mcp_manager.read_dsh_servers(env["dsh_patch"]) == []
    log = db.query(McpMigration).first()
    assert log.status == "failed"


def test_migrate_trashes_same_name_conflict_before_write(env, db):
    """目标端已有同名 server：先快照进回收站再写入新配置。"""
    make_claude_global(env, "deploy")
    # codex 端已有旧版 deploy
    make_codex_global(env, "deploy", command="old-server")
    # dsh 端已有旧版 deploy
    make_dsh_patch(
        env,
        [
            {
                "name": "deploy",
                "transport": "stdio",
                "command": "node",
                "args": ["old.js"],
                "env": {},
                "url": None,
                "headers": {},
                "cwd": None,
            }
        ],
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "deploy"}],
        [
            {"scope": "global", "tool": "codex", "project": None},
            {"scope": "global", "tool": "dsh", "project": None},
        ],
    )
    assert result["conflicts_trashed"] == 2
    # 两个旧版进入回收站
    rows = db.query(McpTrash).all()
    assert len(rows) == 2
    assert {r.tool for r in rows} == {"codex", "dsh"}
    # 新内容已就位（Windows 上 npx 被归一为 npx.cmd）
    expected_cmd = "npx.cmd" if mcp_manager._portable_command("npx") == "npx.cmd" else "npx"
    codex_servers = mcp_manager.read_codex_servers(env["codex_toml"])
    assert codex_servers[0]["command"] == expected_cmd
    dsh_servers = mcp_manager.read_dsh_servers(env["dsh_patch"])
    assert dsh_servers[0]["command"] == expected_cmd
    # 回收站目录包含快照文件
    trashes = mcp_manager.trash_dir().glob("deploy.*.json")
    assert len(list(trashes)) == 2


def test_migrate_http_to_dsh_uses_streamable(env, db):
    """http 传输迁移到 dsh 时写回为 streamable-http。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"srv": {"type": "http", "url": "http://x:1/mcp"}}}),
        encoding="utf-8",
    )
    # dsh 端不检查 url 类型能力外，验证写回字段
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "srv"}],
        [{"scope": "global", "tool": "dsh", "project": None}],
    )
    assert result["migrated"] == 1
    import yaml

    entries = yaml.safe_load(env["dsh_patch"].read_text(encoding="utf-8"))
    configs = [
        i["config"]
        for e in entries
        if isinstance(e, dict)
        for i in (e.get("insert") or [])
        if isinstance(i, dict) and i.get("name") == mcp_manager.DSH_MCP_CLIENT_PLUGIN
    ]
    assert configs[0]["transport"] == "streamable-http"
    assert configs[0]["url"] == "http://x:1/mcp"


def test_migrate_http_to_codex_fails(env, db):
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"srv": {"type": "http", "url": "http://x:1/mcp"}}}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "srv"}],
        [{"scope": "global", "tool": "codex", "project": None}],
    )
    assert result["failed"] == 1
    assert "不支持 http 传输" in result["results"][0]["detail"]


def test_migrate_unknown_source_fails_cleanly(env, db):
    with pytest.raises(ValueError, match="未找到 MCP server"):
        mcp_manager.migrate(
            db,
            [{"scope": "global", "tool": "claude", "project": None, "name": "nope"}],
            [{"scope": "global", "tool": "dsh", "project": None}],
        )
    assert db.query(McpMigration).count() == 0


def test_migrate_to_project_target_writes_project_files(env, db):
    """项目级迁移：claude 写 .mcp.json，codex 写 .codex/config.toml。"""
    make_claude_global(env, "deploy")
    make_project(env, "real-proj")
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "deploy"}],
        [{"scope": "project", "tool": "codex", "project": "real-proj"}],
    )
    assert result["migrated"] == 1
    proj = env["projects_root"] / "real-proj"
    assert (proj / ".codex" / "config.toml").exists()
    assert {s["name"] for s in mcp_manager.read_servers("project", "codex", "real-proj")} == {"deploy"}


def test_migrate_dsh_project_scope_rejected(env, db):
    make_claude_global(env, "deploy")
    make_project(env, "real-proj")
    with pytest.raises(ValueError, match="dsh 端不支持项目级"):
        mcp_manager.migrate(
            db,
            [{"scope": "global", "tool": "claude", "project": None, "name": "deploy"}],
            [{"scope": "project", "tool": "dsh", "project": "real-proj"}],
        )


def test_migrate_api_route_writes_log(env, db):
    """API 路由层直调：schema 校验 + 业务落库贯通。"""
    make_claude_global(env, "release")
    body = schemas.McpMigrateIn(
        sources=[schemas.McpSourceIn(scope="global", tool="claude", name="release")],
        targets=[schemas.McpTargetIn(scope="global", tool="dsh")],
    )
    result = mcp_api.migrate_mcp(body, db)
    assert result["migrated"] == 1
    logs = mcp_api.mcp_migration_logs(db)
    assert len(logs) == 1
    assert logs[0]["status"] == "success"
    assert "全局" in logs[0]["target"]


# ---------------- 删除 → 回收站 → 恢复 ----------------


def test_delete_restore_roundtrip_keeps_encrypted_env_secret(env, db):
    proj = make_project(env, "proj-x")
    path = proj / ".mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"note": {"type": "stdio", "command": "node", "args": ["s.js"], "env": {"TOKEN": "shh"}}}}),
        encoding="utf-8",
    )
    deleted = mcp_manager.delete_to_trash(
        db,
        {"scope": "project", "tool": "claude", "project": "proj-x", "name": "note"},
    )
    assert deleted["deleted"] is True
    # 文件里已无该 server
    assert mcp_manager.read_servers("project", "claude", "proj-x") == []
    # 快照文件里没有明文密钥
    row = db.query(McpTrash).filter(McpTrash.server_name == "note").first()
    assert row is not None
    snap = json.loads(pathlib.Path(row.trash_path).read_text(encoding="utf-8"))
    snap_text = json.dumps(snap)
    assert "shh" not in snap_text
    assert "TOKEN" in snap_text

    item_id = row.id
    restored = mcp_manager.restore_from_trash(db, item_id)
    assert restored["restored"] is True
    servers = mcp_manager.read_servers("project", "claude", "proj-x")
    assert servers[0]["name"] == "note"
    assert servers[0]["env"] == {"TOKEN": "shh"}  # 恢复时解密回明文
    assert mcp_manager.list_trash(db)["items"] == []


def test_restore_blocked_when_same_name_present(env, db):
    proj = make_project(env, "proj-y")
    path = proj / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"guard": {"command": "node"}}}), encoding="utf-8")
    mcp_manager.delete_to_trash(db, {"scope": "project", "tool": "claude", "project": "proj-y", "name": "guard"})
    item_id = mcp_manager.list_trash(db)["items"][0]["id"]
    # 原位置出现同名 server
    path.write_text(json.dumps({"mcpServers": {"guard": {"command": "new"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="原位置已存在同名"):
        mcp_manager.restore_from_trash(db, item_id)


def test_expired_trash_purged_lazily(env, db):
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"stale": {"command": "a"}, "fresh": {"command": "b"}}}),
        encoding="utf-8",
    )
    mcp_manager.delete_to_trash(db, {"scope": "global", "tool": "claude", "name": "stale"})
    mcp_manager.delete_to_trash(db, {"scope": "global", "tool": "claude", "name": "fresh"})

    items = mcp_manager.list_trash(db)["items"]
    stale = next(i for i in items if i["name"] == "stale")
    row = db.query(McpTrash).filter(McpTrash.id == stale["id"]).first()
    stale_path = pathlib.Path(row.trash_path)
    row.trash_time = datetime.now() - timedelta(days=31)
    db.commit()

    listing = mcp_manager.list_trash(db)
    assert listing["purged_expired"] == 1
    names = {i["name"] for i in listing["items"]}
    assert names == {"fresh"}
    assert not stale_path.exists()


def test_permanent_delete_single_item(env, db):
    make_claude_global(env, "gone")
    mcp_manager.delete_to_trash(db, {"scope": "global", "tool": "claude", "name": "gone"})
    item = mcp_manager.list_trash(db)["items"][0]
    result = mcp_manager.delete_permanent(db, item["id"])
    assert result["deleted_permanently"] is True
    assert mcp_manager.list_trash(db)["items"] == []


# ---------------- 内容预览 / API 包装 ----------------


def test_server_content_masks_env(env):
    make_claude_global(env, "open-web", env_vars={"API_KEY": "secret-value"})
    content = mcp_manager.server_content("global", "claude", None, "open-web")
    assert content["env"] == {"API_KEY": mcp_manager.MASK}
    assert "secret-value" not in json.dumps(content)

    with pytest.raises(ValueError, match="未找到 MCP server"):
        mcp_manager.server_content("global", "claude", None, "ghost")


def test_api_delete_wraps_valueerror(env):
    with pytest.raises(HTTPException) as exc:
        mcp_api.delete_mcp_server(
            schemas.McpServerRefIn(scope="global", tool="claude", name="ghost"), None
        )
    assert exc.value.status_code == 400


# ---------------- MCP 功能介绍（description） ----------------


def test_normalize_reads_config_description(env):
    """四端配置自带的 description 均被归一读取。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"git": {"command": "npx", "description": "Git 仓库管理"}}}),
        encoding="utf-8",
    )
    env["codex_toml"].write_text(
        '[mcp_servers."db"]\ncommand = "node"\ndescription = "数据库查询"\n', encoding="utf-8"
    )
    make_zcode_global(env, "web", {"command": "node", "description": "Web 抓取"})
    make_dsh_patch(
        env,
        [
            {
                "name": "srv",
                "transport": "stdio",
                "command": "node",
                "description": "Dsh 服务",
                "args": [],
                "env": {},
                "url": None,
                "headers": {},
                "cwd": None,
            }
        ],
    )

    assert mcp_manager.read_servers("global", "claude")[0]["description"] == "Git 仓库管理"
    assert mcp_manager.read_servers("global", "codex")[0]["description"] == "数据库查询"
    assert mcp_manager.read_servers("global", "zcode")[0]["description"] == "Web 抓取"
    assert mcp_manager.read_servers("global", "dsh")[0]["description"] == "Dsh 服务"


def test_discover_description_custom_override(env):
    """discover 分组展示 description：自定义覆盖配置描述；custom/config 字段供前端回填。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"git": {"command": "npx", "description": "配置自带描述"}}}),
        encoding="utf-8",
    )

    data = mcp_manager.discover()
    g = next(x for x in data["global"] if x["name"] == "git")
    assert g["description"] == "配置自带描述"
    assert g["custom_description"] == ""
    assert g["config_description"] == "配置自带描述"

    # 自定义覆盖
    mcp_manager.save_mcp_description("git", "我的自定义介绍")
    data = mcp_manager.discover()
    g = next(x for x in data["global"] if x["name"] == "git")
    assert g["description"] == "我的自定义介绍"
    assert g["custom_description"] == "我的自定义介绍"
    assert g["config_description"] == "配置自带描述"

    # 清空自定义后回落配置描述
    mcp_manager.save_mcp_description("git", "")
    data = mcp_manager.discover()
    g = next(x for x in data["global"] if x["name"] == "git")
    assert g["description"] == "配置自带描述"
    assert g["custom_description"] == ""


def test_migrate_keeps_description_roundtrip(env, db):
    """迁移保留 description；zcode 端渲染时剔除 description（严格 schema 不写该键）。"""
    env["claude_json"].write_text(
        json.dumps(
            {"mcpServers": {"git": {"command": "npx", "args": ["-y", "@git/mcp"], "description": "Git 管理"}}}
        ),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "git"}],
        [
            {"scope": "global", "tool": "codex", "project": None},
            {"scope": "global", "tool": "dsh", "project": None},
            {"scope": "global", "tool": "zcode", "project": None},
        ],
    )
    assert result["migrated"] == 3
    assert mcp_manager.read_servers("global", "codex")[0]["description"] == "Git 管理"
    assert mcp_manager.read_servers("global", "dsh")[0]["description"] == "Git 管理"
    # zcode 严格 schema 不写 description 键：读回为空属设计使然（写了会被丢弃整个
    # server），展示层靠用户自定义描述表（save_mcp_description）补充
    assert mcp_manager.read_servers("global", "zcode")[0]["description"] == ""
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    assert "description" not in raw["mcp"]["servers"]["git"]


def test_trash_restore_keeps_description(env, db):
    """删除 → 回收站 → 恢复，description 随快照保留。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"srv": {"command": "node", "description": "描述"}}}),
        encoding="utf-8",
    )
    deleted = mcp_manager.delete_to_trash(
        db, {"scope": "global", "tool": "claude", "project": None, "name": "srv"}
    )
    assert deleted["deleted"] is True
    item = mcp_manager.list_trash(db)["items"][0]
    mcp_manager.restore_from_trash(db, item["id"])
    assert mcp_manager.read_servers("global", "claude")[0]["description"] == "描述"


def test_api_save_description_roundtrip(env):
    """API 层：PUT 保存、空串删除、GET 全量。"""
    make_claude_global(env, "srv2")
    mcp_api.mcp_description_save(schemas.McpDescriptionIn(name="srv2", description="功能说明"))
    assert mcp_api.mcp_descriptions_list() == {"srv2": "功能说明"}
    mcp_api.mcp_description_save(schemas.McpDescriptionIn(name="srv2", description="  "))
    assert mcp_api.mcp_descriptions_list() == {}


# ---------------- 自动简介（内置库 / npm 联动） ----------------


def test_builtin_description_matches_common_names():
    """常见 server 名/包名自动命中内置简介，无需手敲。"""
    cases = [
        ("github", None, None),
        ("github-mcp-server", None, None),
        ("open-websearch", None, None),
        ("playwright", None, None),
        ("chrome-devtools", None, None),
        ("postgres", None, None),
        ("unknown-xyz", None, None),
    ]
    for name, cmd, args in cases:
        text, source = mcp_manager.auto_description(name, cmd, args)
        if name == "unknown-xyz":
            assert source == ""
        else:
            assert source == "builtin"
            assert text


def test_builtin_matches_npx_package_tail():
    """npx 包名尾段也能命中内置库（如 @modelcontextprotocol/server-github）。"""
    text, source = mcp_manager.auto_description(
        "zzz-arbitrary-name", "npx", ["-y", "@modelcontextprotocol/server-github"]
    )
    assert source == "builtin"
    assert "GitHub" in text


def test_description_priority_custom_over_config_over_builtin(env):
    """取数优先级：自定义 > 配置自带 > 内置简介。"""
    env["claude_json"].write_text(
        json.dumps(
            {"mcpServers": {
                "github": {"command": "npx", "description": "配置自带简介"},
                "playwright": {"command": "python", "args": ["x.py"]},
            }}
        ),
        encoding="utf-8",
    )
    data = mcp_manager.discover()
    groups = {g["name"]: g for g in data["global"]}
    # 配置自带优先于内置
    assert groups["github"]["description"] == "配置自带简介"
    assert groups["github"]["description_source"] == "config"
    # 无配置无自定义 → 内置库
    assert groups["playwright"]["description_source"] == "builtin"
    assert groups["playwright"]["description"]
    # 自定义最优先
    mcp_manager.save_mcp_description("github", "我的 GitHub 介绍")
    data = mcp_manager.discover()
    groups = {g["name"]: g for g in data["global"]}
    assert groups["github"]["description"] == "我的 GitHub 介绍"
    assert groups["github"]["description_source"] == "custom"


def test_npm_description_fetched_and_cached(env, monkeypatch):
    """未知 npx 包自动查 npm registry 简介并入缓存。"""

    class FakeResp:
        def read(self, n=-1):
            return b'{"name": "mystery-mcp", "description": "A mystery MCP server"}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        assert "registry.npmjs.org" in url
        return FakeResp()

    monkeypatch.setattr(mcp_manager.urllib.request, "urlopen", fake_urlopen)
    text, source = mcp_manager.auto_description(
        "zzz", "npx", ["-y", "mystery-mcp"], {"left": 5}
    )
    assert source == "npm"
    assert text == "A mystery MCP server"
    # 二次调用命中缓存，不再联网
    text2, source2 = mcp_manager.auto_description(
        "zzz", "npx", ["-y", "mystery-mcp"], {"left": 5}
    )
    assert source2 == "npm"
    assert text2 == "A mystery MCP server"
    assert calls["n"] == 1


def test_npm_failure_silently_empty(env, monkeypatch):
    """npm 查询失败静默返回空简介，不打断 discover。"""

    def boom(url, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(mcp_manager.urllib.request, "urlopen", boom)
    text, source = mcp_manager.auto_description(
        "zzz", "npx", ["-y", "no-such-pkg-xyz"], {"left": 5}
    )
    assert source == ""
    assert text == ""


# ---------------- MCP 安装（新建 server） ----------------


def test_create_server_stdio_to_claude_global(env):
    """向 Claude 全局安装 stdio server：写入配置、保留其它键、可被发现。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"old": {"command": "node"}}, "topKey": "keep"}),
        encoding="utf-8",
    )
    created = mcp_manager.create_server(
        "global", "claude", None,
        {"name": "postgres", "transport": "stdio", "command": "npx",
         "args": ["-y", "@modelcontextprotocol/server-postgres"],
         "env": {"PGHOST": "127.0.0.1"}, "description": "PostgreSQL 查询"},
    )
    assert created["name"] == "postgres"
    assert created["env"] == {"PGHOST": mcp_manager.MASK}  # 返回脱敏

    raw = json.loads(env["claude_json"].read_text(encoding="utf-8"))
    assert raw["topKey"] == "keep"  # 其它顶层键保留
    assert "old" in raw["mcpServers"]  # 既有 server 不丢
    srv = raw["mcpServers"]["postgres"]
    assert srv["command"] == "npx.cmd" if mcp_manager._portable_command("npx") == "npx.cmd" else srv["command"] == "npx"
    assert srv["description"] == "PostgreSQL 查询"
    assert srv["env"] == {"PGHOST": "127.0.0.1"}

    found = mcp_manager.read_servers("global", "claude")
    assert {s["name"] for s in found} == {"old", "postgres"}


def test_create_server_http_to_zcode_no_description_key(env):
    """http server 安装到 zcode：只写规范键（严格 schema 不写 description）。"""
    created = mcp_manager.create_server(
        "global", "zcode", None,
        {"name": "remote", "transport": "http", "url": "http://localhost:8787/mcp",
         "headers": {"Authorization": "Bearer t"}, "description": "远程服务"},
    )
    assert created["transport"] == "http"
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    srv = raw["mcp"]["servers"]["remote"]
    assert srv["type"] == "http"
    assert srv["url"] == "http://localhost:8787/mcp"
    assert srv["headers"] == {"Authorization": "Bearer t"}
    assert "description" not in srv


def test_create_server_to_project_scope(env):
    """安装到项目级（claude .mcp.json / zcode .zcode/config.json）。"""
    proj = make_project(env, "proj-x")
    created = mcp_manager.create_server(
        "project", "claude", "proj-x",
        {"name": "deploy-db", "transport": "stdio", "command": "node", "args": ["db.js"]},
    )
    assert created["scope"] == "project"
    mcp = proj / ".mcp.json"
    assert json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["deploy-db"]["command"] == "node"


def test_create_server_rejects_duplicate(env):
    make_claude_global(env, "dup")
    with pytest.raises(ValueError, match="已存在同名"):
        mcp_manager.create_server(
            "global", "claude", None,
            {"name": "dup", "transport": "stdio", "command": "npx"},
        )


def test_create_server_rejects_unsupported_transport_for_tool(env):
    """传输能力矩阵：codex 仅 stdio，http 安装被拒。"""
    with pytest.raises(ValueError, match="不支持 http 传输"):
        mcp_manager.create_server(
            "global", "codex", None,
            {"name": "x", "transport": "http", "url": "http://x:1/mcp"},
        )


def test_create_server_rejects_dsh_project(env):
    with pytest.raises(ValueError, match="项目级"):
        mcp_manager.create_server(
            "project", "dsh", "proj-y",
            {"name": "x", "transport": "stdio", "command": "node"},
        )


def test_create_server_requires_command_or_url(env):
    with pytest.raises(ValueError, match="启动命令"):
        mcp_manager.create_server(
            "global", "claude", None,
            {"name": "x", "transport": "stdio", "command": ""},
        )
    with pytest.raises(ValueError, match="服务地址"):
        mcp_manager.create_server(
            "global", "zcode", None,
            {"name": "x", "transport": "sse", "url": ""},
        )


def test_api_create_route_wraps_valueerror(env):
    body = schemas.McpServerCreate(
        scope="global", tool="claude", name="api-srv",
        transport="stdio", command="npx",
    )
    created = mcp_api.mcp_server_create(body)
    assert created["name"] == "api-srv"
    with pytest.raises(HTTPException) as exc:
        mcp_api.mcp_server_create(body)  # 同名
    assert exc.value.status_code == 409


# ---------------- JSON 导入安装 ----------------


def test_parse_mcp_json_supports_multiple_formats():
    """兼容三层输入：mcpServers 顶层 / server 字典 / 单对象。"""
    a = mcp_manager.parse_mcp_json(
        '{"mcpServers": {"s1": {"command": "npx", "args": ["-y", "@x/y"], "env": {"K": "v"}}}}'
    )
    assert a[0]["name"] == "s1"
    assert a[0]["transport"] == "stdio"
    assert a[0]["args"] == ["-y", "@x/y"]
    assert a[0]["env"] == {"K": "v"}

    b = mcp_manager.parse_mcp_json('{"amap-maps": {"command": "npx", "env": {"AMAP_MAPS_API_KEY": ""}}}')
    assert b[0]["name"] == "amap-maps"
    assert b[0]["env"] == {"AMAP_MAPS_API_KEY": ""}  # 空值保留（占位）

    c = mcp_manager.parse_mcp_json('{"remote": {"type": "http", "url": "http://x:1/mcp", "headers": {"A": "1"}}}')
    assert c[0]["transport"] == "http"
    assert c[0]["headers"] == {"A": "1"}

    # 非对象条目跳过
    d = mcp_manager.parse_mcp_json('{"srv": {"command": "node"}, "note": "hello"}')
    assert [s["name"] for s in d] == ["srv"]


def test_parse_mcp_json_bad_input():
    with pytest.raises(ValueError, match="JSON 解析失败"):
        mcp_manager.parse_mcp_json("{not json")
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        mcp_manager.parse_mcp_json("[1,2]")
    with pytest.raises(ValueError, match="未解析到任何"):
        mcp_manager.parse_mcp_json('{"a": "b"}')


def test_import_servers_batch_install_and_partial_failure(env):
    """批量导入：全装成功；单条同名失败不中断其它。"""
    env["claude_json"].write_text(json.dumps({"mcpServers": {"exists": {"command": "node"}}}), encoding="utf-8")
    parsed = mcp_manager.parse_mcp_json(
        '{"new-a": {"command": "npx", "args": ["-y", "@x/a"]},'
        ' "exists": {"command": "npx"},'
        ' "new-b": {"command": "python", "args": ["b.py"]}}'
    )
    result = mcp_manager.import_servers("global", "claude", None, parsed)
    assert len(result["installed"]) == 2
    assert {x["name"] for x in result["installed"]} == {"new-a", "new-b"}
    assert len(result["failed"]) == 1
    assert result["failed"][0]["name"] == "exists"
    assert "已存在同名" in result["failed"][0]["error"]
    # 实际写入三个
    assert {s["name"] for s in mcp_manager.read_servers("global", "claude")} == {"exists", "new-a", "new-b"}


def test_api_import_route(env):
    body = schemas.McpImportIn(
        scope="global", tool="zcode",
        config_json='{"amap-maps": {"command": "npx", "args": ["-y", "@amap/amap-maps-mcp-server"], "env": {"AMAP_MAPS_API_KEY": ""}}}',
    )
    res = mcp_api.mcp_server_import(body)
    assert len(res["installed"]) == 1
    assert res["installed"][0]["name"] == "amap-maps"
    raw = json.loads(env["zcode_cli"].read_text(encoding="utf-8"))
    assert "amap-maps" in raw["mcp"]["servers"]

    # 非法 JSON → 400
    with pytest.raises(HTTPException) as exc:
        mcp_api.mcp_server_import(schemas.McpImportIn(scope="global", tool="claude", config_json="nope"))
    assert exc.value.status_code == 400


def test_dsh_normalize_names_by_id_not_servername(env):
    """dsh server 命名取 id 去掉 mcp- 前缀，而非泛化的 serverName。

    回归：dsh 条目 `id: mcp-open-websearch` + `serverName: web` 时，
    旧逻辑显示为「web」，用户在管理界面认不出它就是 open-websearch。
    """
    import yaml

    entries = yaml.safe_load(TEST_PATCH)
    entries.append(
        {
            "insert": [
                {
                    "id": "mcp-open-websearch",
                    "name": "@deepseek-ai/dsh-mcp-client",
                    "config": {"serverName": "web", "command": "node", "transport": "stdio"},
                }
            ]
        }
    )
    env["dsh_patch"].write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    servers = mcp_manager.read_servers("global", "dsh")
    assert {s["name"] for s in servers} == {"open-websearch"}
    assert servers[0]["command"] == "node"
    # round-trip：写回后 id 还原为 mcp-open-websearch
    mcp_manager.write_servers("global", "dsh", None, servers)
    entries2 = yaml.safe_load(env["dsh_patch"].read_text(encoding="utf-8"))
    items = [i for e in entries2 if isinstance(e, dict) for i in (e.get("insert") or [])]
    managed = [i for i in items if i.get("name") == "@deepseek-ai/dsh-mcp-client"]
    assert managed[0]["id"] == "mcp-open-websearch"

# ---------------- pi 端 ----------------


def make_pi_global(env, name, entry, extra_top=None):
    """向 pi 的 mcp.json 写入一个 server，保留/附带顶层键（如 autoEnableCodemode）。"""
    loaded = {}
    if env["pi_mcp"].exists():
        try:
            loaded = json.loads(env["pi_mcp"].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            loaded = {}
    loaded = loaded if isinstance(loaded, dict) else {}
    if extra_top:
        loaded.update(extra_top)
    servers = dict(loaded.get("mcpServers") or {})
    servers[name] = entry
    loaded["mcpServers"] = servers
    env["pi_mcp"].write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
    return env["pi_mcp"]


def make_pi_project(env, project, name, entry):
    """向 <项目>/.pi/mcp.json 写入一个 server。"""
    p = make_project(env, project)
    f = p / ".pi" / "mcp.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"mcpServers": {name: entry}}, ensure_ascii=False), encoding="utf-8")
    return f


def test_discover_includes_pi_global_and_project(env):
    """pi 端：全局 agent/mcp.json 与项目级 .pi/mcp.json 均被发现，能力矩阵不含 sse。"""
    make_pi_global(env, "sentry", {"url": "https://mcp.sentry.dev/mcp"}, extra_top={"autoEnableCodemode": False})
    make_claude_global(env, "sentry")
    make_pi_project(env, "proj-pi", "internal", {"command": "node", "args": ["i.js"], "exposure": "deferred"})

    data = mcp_manager.discover()
    assert data["roots"]["pi"] == str(env["pi_mcp"])
    assert data["capability"]["pi"] == ["http", "stdio"]

    groups = {g["name"]: g for g in data["global"]}
    assert groups["sentry"]["ends"] == ["claude", "pi"]

    projects = {p["project"]: p for p in data["projects"]}
    assert projects["proj-pi"]["servers"][0]["name"] == "internal"


def test_pi_read_preserves_native_keys_in_extra(env):
    """pi 自有键（exposure / toolExposure / enabled / timeout / auth）归一进 extra，不外泄明文。"""
    make_pi_global(
        env,
        "docs",
        {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "exposure": "codemode",
            "toolExposure": {"search": "direct"},
            "enabled": False,
            "timeout": 120,
            "auth": {"provider": "corporate"},
        },
    )
    servers = mcp_manager.read_servers("global", "pi")
    assert len(servers) == 1
    extra = servers[0]["extra"]
    assert extra["exposure"] == "codemode"
    assert extra["enabled"] is False
    assert extra["timeout"] == 120
    assert extra["auth"] == {"provider": "corporate"}
    masked = mcp_manager._mask_server(servers[0])
    assert masked["headers"]["Authorization"] == mcp_manager.MASK


def test_pi_write_round_trip_keeps_native_keys_and_top_level(env):
    """向 pi 安装新 server 时，既有条目的 pi 自有键与顶层 autoEnableCodemode 不被抹平。"""
    path = make_pi_global(
        env,
        "legacy",
        {"command": "node", "args": ["l.js"], "exposure": "hidden", "timeout": 30},
        extra_top={"autoEnableCodemode": False},
    )
    mcp_manager.create_server(
        "global",
        "pi",
        None,
        {
            "name": "fs",
            "transport": "stdio",
            "command": "node",
            "args": ["f.js"],
            "env": {},
            "url": None,
            "headers": {},
            "cwd": None,
            "description": "文件系统",
        },
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["autoEnableCodemode"] is False
    assert data["mcpServers"]["legacy"]["exposure"] == "hidden"
    assert data["mcpServers"]["legacy"]["timeout"] == 30
    assert data["mcpServers"]["fs"]["command"] == "node"
    assert data["mcpServers"]["fs"]["description"] == "文件系统"


def test_migrate_sse_server_to_pi_fails(tmp_path, env, db):
    """Claude 的 sse server 迁到 pi：按能力矩阵整对失败，不静默降级（pi 拒绝 legacy SSE）。"""
    make_claude_global(env, "legacy-sse")
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"legacy-sse": {"type": "sse", "url": "https://old.example/sse"}}}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "legacy-sse"}],
        [{"scope": "global", "tool": "pi", "project": None}],
    )
    assert result["migrated"] == 0 and result["failed"] == 1
    assert "不支持 sse 传输" in result["results"][0]["detail"]
    assert not env["pi_mcp"].exists()


def test_migrate_server_with_illegal_name_to_pi_fails(env, db):
    """pi 的 server 名只认字母数字与 _ -：含点号/中文的名字整对失败并留日志，不写出废条目。"""
    make_pi_global(env, "keepme", {"command": "node", "args": ["k.js"]})
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"内部工具": {"command": "node", "args": ["i.js"]}}}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "内部工具"}],
        [{"scope": "global", "tool": "pi", "project": None}],
    )
    assert result["migrated"] == 0 and result["failed"] == 1
    assert "只允许字母、数字、下划线与连字符" in result["results"][0]["detail"]
    assert json.loads(env["pi_mcp"].read_text(encoding="utf-8"))["mcpServers"].keys() == {"keepme"}


def test_create_server_pi_project_drops_auth(env):
    """pi 项目级配置不接受 auth 键：迁移/写入项目文件时剔除，避免整条被 pi 判废。"""
    make_project(env, "proj-pi")
    servers = [
        {
            "name": "docs",
            "transport": "http",
            "command": None,
            "args": [],
            "env": {},
            "url": "https://example.com/mcp",
            "headers": {},
            "cwd": None,
            "description": "",
            "tool": "pi",
            "scope": "project",
            "project": "proj-pi",
            "path": "",
            "extra": {"auth": {"provider": "corporate"}, "exposure": "direct"},
        }
    ]
    path = mcp_manager.write_servers("project", "pi", "proj-pi", servers)
    entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["docs"]
    assert "auth" not in entry
    assert entry["exposure"] == "direct"


# ---------------- Qoder 端 ----------------


def make_qoder_global(env, name, entry, extra_top=None):
    """向 Qoder 的 mcp.json 写入一个 server，保留/附带顶层键。"""
    loaded = {}
    if env["qoder_mcp"].exists():
        try:
            loaded = json.loads(env["qoder_mcp"].read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            loaded = {}
    loaded = loaded if isinstance(loaded, dict) else {}
    if extra_top:
        loaded.update(extra_top)
    servers = dict(loaded.get("mcpServers") or {})
    servers[name] = entry
    loaded["mcpServers"] = servers
    env["qoder_mcp"].write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")
    return env["qoder_mcp"]


def test_discover_includes_qoder_global(env):
    """Qoder 端：全局 ~/.qoder/mcp.json 被发现，三种传输都在能力矩阵里。"""
    make_qoder_global(env, "sentry", {"type": "http", "url": "https://mcp.sentry.dev/mcp"})
    make_claude_global(env, "sentry")

    data = mcp_manager.discover()
    assert data["roots"]["qoder"] == str(env["qoder_mcp"])
    assert data["capability"]["qoder"] == ["http", "sse", "stdio"]
    groups = {g["name"]: g for g in data["global"]}
    assert groups["sentry"]["ends"] == ["claude", "qoder"]


def test_qoder_read_preserves_native_keys_in_extra(env):
    """Qoder 自有键（disabled / timeout / authType）归一进 extra，敏感头不外泄明文。"""
    make_qoder_global(
        env,
        "docs",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "disabled": True,
            "timeout": 120,
            "authType": "oauth",
        },
    )
    servers = mcp_manager.read_servers("global", "qoder")
    assert len(servers) == 1
    assert servers[0]["transport"] == "http"
    extra = servers[0]["extra"]
    assert extra["disabled"] is True
    assert extra["timeout"] == 120
    assert extra["authType"] == "oauth"
    masked = mcp_manager._mask_server(servers[0])
    assert masked["headers"]["Authorization"] == mcp_manager.MASK


def test_qoder_write_round_trip_keeps_native_keys_and_top_level(env):
    """向 Qoder 装新 server：既有条目的 disabled / timeout 与顶层非 mcpServers 键不被抹平。"""
    path = make_qoder_global(
        env,
        "legacy",
        {"type": "stdio", "command": "node", "args": ["l.js"], "disabled": True, "timeout": 30},
        extra_top={"version": 2},
    )
    mcp_manager.create_server(
        "global",
        "qoder",
        None,
        {
            "name": "fs",
            "transport": "stdio",
            "command": "node",
            "args": ["f.js"],
            "env": {},
            "url": None,
            "headers": {},
            "cwd": None,
            "description": "文件系统",
        },
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 2
    assert data["mcpServers"]["legacy"]["disabled"] is True
    assert data["mcpServers"]["legacy"]["timeout"] == 30
    assert data["mcpServers"]["fs"]["type"] == "stdio"
    assert data["mcpServers"]["fs"]["description"] == "文件系统"


def test_migrate_claude_to_qoder_keeps_sse(env, db):
    """Claude 全局 server 迁到 Qoder：sse 条目按 Qoder 认的 type 原样写出。"""
    env["claude_json"].write_text(
        json.dumps({"mcpServers": {"old": {"type": "sse", "url": "https://a.example/sse"}}}),
        encoding="utf-8",
    )
    result = mcp_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "old"}],
        [{"scope": "global", "tool": "qoder", "project": None}],
    )
    assert result["migrated"] == 1 and result["failed"] == 0
    entry = json.loads(env["qoder_mcp"].read_text(encoding="utf-8"))["mcpServers"]["old"]
    assert entry == {"type": "sse", "url": "https://a.example/sse"}


def test_qoder_project_scope_is_rejected(env, db):
    """Qoder 的项目级 MCP 就是 Claude Code 的 <repo>/.mcp.json：不重复托管，安装与迁移都拒绝项目级。"""
    make_project(env, "proj-qoder")
    with pytest.raises(ValueError, match="Qoder 端不支持项目级"):
        mcp_manager.create_server(
            "project", "qoder", "proj-qoder", {"name": "fs", "transport": "stdio", "command": "node"}
        )
    make_claude_global(env, "sentry")
    with pytest.raises(ValueError, match="Qoder 端不支持项目级"):
        mcp_manager.migrate(
            db,
            [{"scope": "global", "tool": "claude", "project": None, "name": "sentry"}],
            [{"scope": "project", "tool": "qoder", "project": "proj-qoder"}],
        )
    assert not env["qoder_mcp"].exists()
