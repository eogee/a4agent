"""OpenCode 适配测试：技能身份键 / 兼容目录标注 / MCP JSONC 适配 / 产出解析。

全部通过 tmp_path + monkeypatch 隔离路径，绝不触碰真实用户目录，
也不发起真实网络请求（HTTP 部分用桩函数替换）。
"""
import json
import pathlib

import pytest

from backend.app import mcp_manager, opencode_client, skill_manager
from backend.app.models import SkillMigration, SkillTrash  # noqa: F401  # 触发建表注册


# ---------------- 基础设施 ----------------


@pytest.fixture()
def db(tmp_path):
    """独立的内存外 sqlite 会话（迁移/回收站要落库）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend.app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'opencode_test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离七端 skill 根、OpenCode 配置目录、数据目录与项目根列表。"""
    ctx = {"data": tmp_path / "data", "projects_root": tmp_path / "projects"}
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(ctx["data"]))
    for tool in ("CLAUDE", "CODEX", "DSH", "ZCODE", "PI", "QODER", "OPENCODE"):
        p = tmp_path / f"{tool.lower()}-skills"
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(f"A4AGENT_{tool}_SKILLS_PATH", str(p))
        ctx[tool.lower()] = p
    oc_cfg = tmp_path / "oc-config"
    oc_cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("A4AGENT_OPENCODE_CONFIG_DIR", str(oc_cfg))
    ctx["oc_config"] = oc_cfg
    ctx["projects_root"].mkdir(parents=True, exist_ok=True)
    skill_manager.save_project_roots([str(ctx["projects_root"])])
    return ctx


def make_skill(root, dir_name, name=None, description="描述", extra_front=""):
    d = pathlib.Path(root) / dir_name
    d.mkdir(parents=True, exist_ok=True)
    fm = ["---"]
    if name:
        fm.append(f"name: {name}")
    if description:
        fm.append(f'description: "{description}"')
    if extra_front:
        fm.append(extra_front)
    fm.append("---")
    (d / "SKILL.md").write_text("\n".join(fm) + f"\n\n# {dir_name}\n正文\n", encoding="utf-8")
    return d


def make_project(env, name):
    p = env["projects_root"] / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def global_groups(env):
    data = skill_manager.discover()
    return {g["name"]: g for g in data["global"]}


def project_groups(env, project):
    data = skill_manager.discover()
    for p in data["projects"]:
        if p["project"] == project:
            return {g["name"]: g for g in p["skills"]}
    return {}


# ---------------- 技能：身份键 ----------------


def test_opencode_global_root_is_config_dir_not_dot_opencode(env):
    """全局根是 ~/.config/opencode/skills（不是 ~/.opencode/skills），环境变量可覆盖。"""
    assert skill_manager.opencode_skills_root() == env["opencode"]
    assert skill_manager.global_skill_roots()["opencode"] == env["opencode"]


def test_opencode_project_root_uses_generic_formula(env):
    """项目级 .opencode/skills 正好命中 .{tool}/skills 通用公式，无需特判。"""
    proj = make_project(env, "p1")
    roots = skill_manager.project_skill_roots(proj)
    assert roots["opencode"] == proj / ".opencode" / "skills"
    make_skill(roots["opencode"], "deploy", "deploy")
    g = project_groups(env, "p1")["deploy"]
    assert g["ends"] == ["opencode"]


def test_opencode_aggregates_by_dir_name_not_frontmatter(env):
    """同 frontmatter name、不同目录名在 OpenCode 端是两份独立技能。

    这跟随它的真实行为：技能 ID 由路径派生，frontmatter name 只是显示名。
    若按 name 聚合，用户会看到「2 端存在」的假象，而 OpenCode 里其实是两个
    各自独立加载的技能。
    """
    make_skill(env["opencode"], "alpha", "Same Name")
    make_skill(env["opencode"], "beta", "Same Name")
    groups = global_groups(env)
    assert set(groups) == {"alpha", "beta"}
    assert groups["alpha"]["end_count"] == 1


def test_cross_end_aggregation_still_matches(env):
    """跨端同名仍聚合为一份（目录名一致时）。

    走的是「name 或 dir_name 任一命中」：Claude 端认 display name，OpenCode
    端认目录名，两边都得认才能把同一个技能并成一张卡。
    """
    make_skill(env["claude"], "deploy", "Deploy Skill")
    make_skill(env["opencode"], "deploy", "Deploy Skill")
    groups = global_groups(env)
    assert len(groups) == 1
    g = next(iter(groups.values()))
    assert g["end_count"] == 2
    assert g["ends"] == ["claude", "opencode"]


def test_opencode_name_is_display_only(env):
    """display_name 保留 frontmatter name，聚合键是目录名。"""
    make_skill(env["opencode"], "git-release", "Git Release")
    g = global_groups(env)["git-release"]
    assert g["name"] == "git-release"
    assert g["display_name"] == "Git Release"


def test_opencode_notice_warns_missing_description(env):
    """缺 description 会导致技能不被推荐给模型——这是真实影响，要提示。"""
    make_skill(env["opencode"], "nod", "nod", description="")
    notice = skill_manager.opencode_skill_notice(env["opencode"] / "nod")
    assert "description" in notice


def test_opencode_notice_warns_name_dir_mismatch(env):
    """ID 取目录名，name 只作显示名：不一致时要说清用户该用哪个。"""
    make_skill(env["opencode"], "real-id", "Pretty Name")
    notice = skill_manager.opencode_skill_notice(env["opencode"] / "real-id")
    assert "real-id" in notice and "显示名" in notice


def test_opencode_notice_respects_autoinvoke_off(env):
    make_skill(env["opencode"], "quiet", "quiet",
                extra_front="metadata:\n  opencode/autoinvoke: false")
    notice = skill_manager.opencode_skill_notice(env["opencode"] / "quiet")
    assert "autoinvoke" in notice


def test_opencode_notice_empty_for_compliant_skill(env):
    make_skill(env["opencode"], "fine", "fine")
    assert skill_manager.opencode_skill_notice(env["opencode"] / "fine") == ""


# ---------------- 技能：兼容目录重叠 ----------------


def test_compat_dir_entries_marked_opencode_visible(env):
    """落在 .claude/skills 的条目标注「OpenCode 已可见」，但不计入 opencode 端。

    这是实测踩到的真实现象：本项目的技能放在 .agents/skills（ZCode/Qoder 的
    跨工具目录），OpenCode 会自动发现。不标注的话界面会漏报 OpenCode 端的存在，
    「一键适配」还会白迁一份。
    """
    proj = make_project(env, "compat")
    make_skill(proj / ".claude" / "skills", "shared", "shared")
    g = project_groups(env, "compat")["shared"]
    assert g["opencode_visible"] is True
    assert "opencode" not in g["ends"]
    assert g["opencode_via"].endswith(".claude\\skills")


def test_agents_only_project_not_in_a4agent_view(env):
    """只存在于 .agents/skills 的项目不入 a4agent 视图（它不是任何端的托管目录）。"""
    proj = make_project(env, "agents-only")
    make_skill(proj / ".agents" / "skills", "hidden", "hidden")
    assert project_groups(env, "agents-only") == {}


def test_own_opencode_dir_not_marked_visible(env):
    """托管在 .opencode/skills 的条目不算「兼容目录可见」，避免误标。"""
    proj = make_project(env, "own")
    make_skill(proj / ".opencode" / "skills", "mine", "mine")
    g = project_groups(env, "own")["mine"]
    assert g["opencode_visible"] is False
    assert g["ends"] == ["opencode"]


# ---------------- 技能：冲突判定 ----------------


def test_opencode_conflict_only_by_dir_name(env, db):
    """仅 frontmatter name 相同、目录名不同的既有技能，不该被当成冲突销毁。

    OpenCode 的身份是 ID（目录名），两者是两个各自工作的技能。
    """
    make_skill(env["claude"], "deploy", "Deploy", "新版本")
    old = make_skill(env["opencode"], "deploy-old", "Deploy", "旧版本但ID不同")
    result = skill_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "Deploy"}],
        [{"scope": "global", "tool": "opencode", "project": None}],
    )
    assert result["conflicts_trashed"] == 0
    assert old.exists(), "仅显示名相同的既有技能被误删"
    assert (env["opencode"] / "deploy" / "SKILL.md").is_file()


def test_opencode_conflict_same_dir_name_is_trashed(env, db):
    """目录名相同即同一技能：旧版照例进回收站。"""
    make_skill(env["claude"], "deploy", "Deploy", "新版本")
    old = make_skill(env["opencode"], "deploy", "Deploy", "旧版本")
    result = skill_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "Deploy"}],
        [{"scope": "global", "tool": "opencode", "project": None}],
    )
    assert result["conflicts_trashed"] == 1
    # 旧版被移走后又在原路径写入了新副本，所以目录仍在、内容已是新版；
    # 旧版本体进了回收站（由 conflicts_trashed 与回收站条目共同证明）
    assert (env["opencode"] / "deploy" / "SKILL.md").read_text(encoding="utf-8").find("新版本") > 0
    trashed = db.query(SkillTrash).all()
    assert [t.dir_name for t in trashed] == ["deploy"]


def test_migration_to_opencode_appends_notice(env, db):
    """迁到 OpenCode 时若命中真实使用问题，提示里要带上。"""
    make_skill(env["claude"], "nod", "nod", description="")
    result = skill_manager.migrate(
        db,
        [{"scope": "global", "tool": "claude", "project": None, "name": "nod"}],
        [{"scope": "global", "tool": "opencode", "project": None}],
    )
    detail = result["results"][0]["detail"]
    assert "OpenCode" in detail and "description" in detail


# ---------------- MCP：配置路径 ----------------


def test_global_config_prefers_existing_jsonc(env):
    """已有 jsonc 绝不降级写成 json（否则用户手写注释会被格式化掉）。"""
    base = env["oc_config"]
    assert mcp_manager.opencode_mcp_path().name == "opencode.jsonc"  # 都不存在时按首选新建
    (base / "opencode.json").write_text("{}", encoding="utf-8")
    assert mcp_manager.opencode_mcp_path().name == "opencode.json"
    (base / "opencode.jsonc").write_text("{}", encoding="utf-8")
    assert mcp_manager.opencode_mcp_path().name == "opencode.jsonc"


def test_project_config_follows_opencode_precedence(env):
    """项目级按**生效优先级**探测：.opencode 覆盖根级同名键。

    顺序错了会把配置写进被覆盖的文件——写完 OpenCode 不读，等于静默失效。
    """
    root = env["projects_root"] / "proj"
    root.mkdir(parents=True, exist_ok=True)
    assert mcp_manager.opencode_project_mcp_path(root) == root / ".opencode" / "opencode.jsonc"
    (root / "opencode.json").write_text("{}", encoding="utf-8")
    assert mcp_manager.opencode_project_mcp_path(root) == root / "opencode.json"
    (root / ".opencode").mkdir(exist_ok=True)
    (root / ".opencode" / "opencode.json").write_text("{}", encoding="utf-8")
    assert mcp_manager.opencode_project_mcp_path(root) == root / ".opencode" / "opencode.json"


def test_transport_and_scope_capabilities(env):
    assert mcp_manager.TRANSPORT_CAPABILITY["opencode"] == {"stdio", "http"}
    assert mcp_manager.DASH_SCOPE_CAPABILITY["opencode"] == ("global", "project")


# ---------------- MCP：JSONC 解析 ----------------


def test_loads_jsonc_handles_comments_and_trailing_commas():
    from backend.app.mcp_manager import loads_jsonc

    assert loads_jsonc('{\n // c\n "a": 1,\n}') == {"a": 1}
    assert loads_jsonc('{ /* b */ "a": [1,2,], }') == {"a": [1, 2]}


def test_loads_jsonc_preserves_urls_and_comment_like_strings():
    """配置里出现 https:// 与 /* 属常态：字符串字面量不能被当成注释剥掉。"""
    from backend.app.mcp_manager import loads_jsonc

    assert loads_jsonc('{"url": "https://mcp.example.com/mcp"}') == {
        "url": "https://mcp.example.com/mcp"
    }
    assert loads_jsonc('{"note": "a /* not a comment */ b"}') == {
        "note": "a /* not a comment */ b"
    }
    assert loads_jsonc('{"note": "he said \\"hi\\" // x"}') == {
        "note": 'he said "hi" // x'
    }


def test_read_opencode_servers_tolerates_jsonc(env):
    path = env["oc_config"] / "opencode.jsonc"
    path.write_text(
        '{\n  // 用户注释\n  "model": "x/y",\n  "mcp": {"servers": {"z": '
        '{"type": "local", "command": ["node", "s.js"]}}},\n}\n',
        encoding="utf-8",
    )
    servers = mcp_manager.read_opencode_servers(path)
    assert [s["name"] for s in servers] == ["z"]
    assert servers[0]["command"] == "node"


def test_write_opencode_aborts_on_broken_json(env):
    """配置损坏时中止写入，不按空配置重建（否则清空用户的 model/agents）。"""
    path = env["oc_config"] / "opencode.jsonc"
    original = '{"mcp": {"servers": {'  # 故意不闭合
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="中止写入"):
        mcp_manager.write_servers("global", "opencode", None, [])
    assert path.read_text(encoding="utf-8") == original


# ---------------- MCP：归一化与渲染 ----------------


def test_normalize_opencode_splits_command_array():
    """官方把可执行与参数合成 command 数组，归一层要拆回 command + args。"""
    raw = {"type": "local", "command": ["npx", "-y", "@mcp/x"], "environment": {"K": "v"}}
    s = mcp_manager.normalize_opencode("ctx", raw, pathlib.Path("x"), "opencode", "global", None)
    assert s["command"] == "npx"
    assert s["args"] == ["-y", "@mcp/x"]
    assert s["env"] == {"K": "v"}
    assert s["transport"] == "stdio"


def test_normalize_opencode_remote_and_sse_fallback():
    remote = mcp_manager.normalize_opencode(
        "d", {"type": "remote", "url": "https://x/mcp"}, pathlib.Path("x"),
        "opencode", "global", None)
    assert remote["transport"] == "http"
    # 未知 type 归 stdio 且不保留 sse：OpenCode 配置里没有 sse 的位置，
    # 静默降级会写出 OpenCode 根本读不懂的条目
    sse = mcp_manager.normalize_opencode(
        "s", {"type": "sse", "url": "https://x"}, pathlib.Path("x"),
        "opencode", "global", None)
    assert sse["transport"] == "stdio"


def test_normalize_opencode_keeps_owned_keys_in_extra():
    raw = {"type": "local", "command": ["x"], "codemode": False,
           "timeout": {"startup": 1000}, "disabled": False, "protocol": "auto"}
    s = mcp_manager.normalize_opencode("x", raw, pathlib.Path("x"), "opencode", "global", None)
    assert s["extra"]["codemode"] is False
    assert s["extra"]["timeout"] == {"startup": 1000}
    assert s["extra"]["disabled"] is False
    assert s["extra"]["protocol"] == "auto"


def test_render_opencode_roundtrip_preserves_owned_keys():
    raw = {"type": "local", "command": ["npx", "-y", "@mcp/x"], "environment": {"K": "v"},
           "codemode": False, "timeout": {"startup": 1000}, "disabled": True}
    s = mcp_manager.normalize_opencode("ctx", raw, pathlib.Path("x"), "opencode", "global", None)
    out = mcp_manager.render_opencode(s)
    assert out["type"] == "local"
    assert out["command"] == ["npx", "-y", "@mcp/x"]
    assert out["environment"] == {"K": "v"}
    assert out["codemode"] is False
    assert out["timeout"] == {"startup": 1000}
    assert out["disabled"] is True


def test_render_opencode_does_not_add_cmd_suffix():
    """不套 _portable_command：OpenCode 自己 spawn，npx.cmd 与用户写法不一致。"""
    s = mcp_manager.normalize_opencode(
        "x", {"type": "local", "command": ["npx", "-y"]}, pathlib.Path("x"),
        "opencode", "global", None)
    assert mcp_manager.render_opencode(s)["command"] == ["npx", "-y"]


def test_render_opencode_never_writes_description():
    """OpenCode schema 严格，未知键会被丢弃——description 只作本地卡片介绍。"""
    s = mcp_manager.normalize_opencode(
        "x", {"type": "local", "command": ["npx"]}, pathlib.Path("x"),
        "opencode", "global", None)
    s["description"] = "手工填写的介绍"
    assert "description" not in mcp_manager.render_opencode(s)


def test_write_opencode_preserves_other_top_level_keys(env):
    path = env["oc_config"] / "opencode.jsonc"
    path.write_text(json.dumps({
        "model": "x/y",
        "permissions": [{"action": "shell", "resource": "*", "effect": "ask"}],
        "mcp": {"servers": {"a": {"type": "local", "command": ["n"]}}},
    }), encoding="utf-8")
    servers = mcp_manager.read_opencode_servers(path)
    mcp_manager.write_servers("global", "opencode", None, servers)
    back = json.loads(path.read_text(encoding="utf-8"))
    assert back["model"] == "x/y"
    assert back["permissions"][0]["action"] == "shell"
    assert len(back["mcp"]["servers"]) == 1


def test_write_opencode_backs_up_before_modifying(env):
    path = env["oc_config"] / "opencode.jsonc"
    path.write_text('{"mcp": {"servers": {"a": {"type": "local", "command": ["n"]}}}}',
                    encoding="utf-8")
    servers = mcp_manager.read_opencode_servers(path)
    mcp_manager.write_servers("global", "opencode", None, servers + [servers[0] | {"name": "b"}])
    from backend.app import config_manager

    backups = list(config_manager.backup_dir().glob("mcp.opencode.jsonc.*.bak"))
    assert backups, "写入前未产生备份"


# ---------------- 产出解析 ----------------


def _messages(text="结论", finish="stop", error=None):
    msgs = [{"type": "user", "payload": {"text": "问题"}}]
    parts = [{"type": "text", "text": text}] if text else []
    msgs.append({"type": "assistant", "content": parts, "finish": finish})
    if error:
        msgs[-1]["error"] = error
    return msgs


def test_parse_output_success_reads_outcome_and_usage():
    msgs = _messages("干完了") + [{"type": "idle", "outcome": "succeeded"}]
    session = {"outcome": "succeeded", "cost": 0.25,
               "tokens": {"input": 100, "output": 20, "reasoning": 5,
                          "cache": {"read": 900, "write": 0}}}
    r = opencode_client.parse_output(msgs, session)
    assert r["ok"] is True
    assert r["text"] == "干完了"
    assert r["usage"]["input"] == 100
    assert r["usage"]["output"] == 20
    assert r["usage"]["totalTokens"] == 120
    assert r["usage"]["cost"] == 0.25


def test_parse_output_failure_uses_structured_error_message():
    """失败原因直接取 error.type + message，不靠退出码猜。"""
    msgs = _messages("", finish="error",
                     error={"type": "provider.internal",
                            "message": "Streaming response failed: [504] timeout"})
    msgs.append({"type": "idle", "outcome": "failed"})
    r = opencode_client.parse_output(msgs, {"outcome": "failed"})
    assert r["ok"] is False
    assert "provider.internal" in r["error"]
    assert "504" in r["error"]


def test_parse_output_interrupted_is_cancelled():
    msgs = _messages("半截") + [{"type": "idle", "outcome": "interrupted"}]
    r = opencode_client.parse_output(msgs, {"outcome": "interrupted"})
    assert r["ok"] is False
    assert "中断" in r["error"]


def test_parse_output_timeout_flag_wins():
    msgs = _messages("跑到一半") + [{"type": "idle", "outcome": "succeeded"}]
    r = opencode_client.parse_output(msgs, {"outcome": "succeeded"}, timed_out=True)
    assert r["ok"] is False
    assert "超时" in r["error"]


def test_parse_output_without_outcome_falls_back_to_text():
    """拿不到 outcome（wait 端点缺失走轮询）时，有文本即视为成功。"""
    r = opencode_client.parse_output(_messages("有产出"), {})
    assert r["ok"] is True


def test_parse_output_empty_is_failure():
    r = opencode_client.parse_output(_messages(""), {"outcome": "succeeded"})
    assert r["ok"] is False
    assert "无文本产出" in r["error"]


# ---------------- 连接配置 ----------------


def test_resolve_connection_prefers_explicit(monkeypatch):
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "local")
    base, pwd = opencode_client.resolve_connection({"base_url": "http://h:1234/",
                                                   "password": "given"})
    assert base == "http://h:1234"
    assert pwd == "given"


def test_resolve_connection_falls_back_to_local(monkeypatch):
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "auto")
    base, pwd = opencode_client.resolve_connection({})
    assert base == opencode_client.DEFAULT_BASE_URL
    assert pwd == "auto"


def test_read_local_password_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(opencode_client, "service_json_path",
                        lambda: tmp_path / "nope.json")
    assert opencode_client.read_local_password() == ""


def test_read_local_password_broken_file(monkeypatch, tmp_path):
    bad = tmp_path / "service.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(opencode_client, "service_json_path", lambda: bad)
    assert opencode_client.read_local_password() == ""


def test_candidate_models_filters_and_caps(monkeypatch):
    def fake_api(method, path, body=None, base_url="", password="", timeout=0, params=None):
        return {"data": [
            {"providerID": "p1", "id": "a", "enabled": True},
            {"providerID": "p1", "id": "disabled", "enabled": False},
            {"providerID": "p2", "id": "b", "enabled": True},
            {"providerID": "", "id": "no-provider", "enabled": True},
            {"providerID": "p3", "id": "c", "enabled": True},
            {"providerID": "p4", "id": "d", "enabled": True},
        ]}

    monkeypatch.setattr(opencode_client, "api", fake_api)
    got = opencode_client.candidate_models("http://x", "pw", limit=3)
    assert [m["id"] for m in got] == ["a", "b", "c"]


def test_candidate_models_empty_on_bad_payload(monkeypatch):
    monkeypatch.setattr(opencode_client, "api", lambda *a, **k: {"data": None})
    assert opencode_client.candidate_models("http://x", "pw") == []


def test_model_rejected_markers_cover_observed_error():
    """实测错误文本：'Model exo-free has been deprecated.'"""
    err = "provider.invalid-request：Model exo-free has been deprecated."
    assert any(m in err.lower() for m in opencode_client._REJECTED_MODEL_MARKERS)


def test_model_rejected_ignores_unrelated_failure():
    err = "OpenCode 会话创建失败：连接被拒绝"
    assert not any(m in err.lower() for m in opencode_client._REJECTED_MODEL_MARKERS)


def test_probe_cache_is_keyed_by_connection(monkeypatch):
    """缓存必须按 (地址, 密码) 分键：从本机切到远程不能复用旧结果。

    否则切换保存后，界面会显示一个根本没连过的地址「已连接」。
    """
    monkeypatch.setattr(opencode_client, "_probe_cache", {})
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "local-pw")
    seen = []

    def fake_info(base, pwd):
        seen.append((base, pwd))
        return {"version": "2.0.26"}

    monkeypatch.setattr(opencode_client, "server_info", fake_info)
    opencode_client.probe({"base_url": "http://127.0.0.1:49374"})
    opencode_client.probe({"base_url": "http://remote:9999", "password": "pw2"})
    assert len(seen) == 2, "不同连接复用了同一份探测缓存"
    # 同参数再探一次应命中缓存
    opencode_client.probe({"base_url": "http://remote:9999", "password": "pw2"})
    assert len(seen) == 2


def test_probe_cache_key_does_not_store_plaintext(monkeypatch):
    monkeypatch.setattr(opencode_client, "_probe_cache", {})
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "topsecret")
    monkeypatch.setattr(opencode_client, "server_info", lambda b, p: {"version": "x"})
    opencode_client.probe()
    dumped = repr(opencode_client._probe_cache)
    assert "topsecret" not in dumped


def test_probe_without_password_reports_credential_problem(monkeypatch):
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "")
    monkeypatch.setattr(opencode_client, "_probe_cache", {})
    info = opencode_client.probe()
    assert info["installed"] is False
    assert "密码" in info["error"]


def test_probe_reports_version_and_capabilities(monkeypatch):
    monkeypatch.setattr(opencode_client, "_probe_cache", {})
    monkeypatch.setattr(opencode_client, "read_local_password", lambda: "pw")

    def fake_info(base, pwd):
        return {"version": "2.0.26", "capabilities": {"persistentPty": False}}

    monkeypatch.setattr(opencode_client, "server_info", fake_info)
    info = opencode_client.probe()
    assert info["installed"] is True
    assert info["version"] == "2.0.26"
    assert info["capabilities"]["persistentPty"] is False


# ---------------- 事件映射 ----------------


def test_event_status_mapping_covers_terminal_events():
    """终态事件 → a4agent 终态词。

    直接读事件映射表所在模块的模块级常量，但不 import backend.app.main
    （那会在 import 期建库并连接真实数据目录）。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[2] / "app" / "main.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    mapping = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_OC_STATUS_BY_EVENT" for t in node.targets
        ):
            mapping = ast.literal_eval(node.value)
    assert mapping, "未找到 _OC_STATUS_BY_EVENT"
    assert mapping["session.execution.succeeded"] == "success"
    assert mapping["session.execution.failed"] == "failed"
    assert "session.execution.interrupted" in mapping


def test_event_stream_parses_type_from_data_payload():
    """实测：SSE 的 event: 字段为空，类型在 data JSON 的 type 里。"""
    seen = []
    stream = opencode_client.EventStream("http://x", "pw", seen.append)
    stream._handle_block('data: {"id":"e1","type":"session.execution.succeeded"}')
    assert len(seen) == 1
    assert seen[0]["type"] == "session.execution.succeeded"


def test_event_stream_ignores_non_data_and_bad_json():
    seen = []
    stream = opencode_client.EventStream("http://x", "pw", seen.append)
    stream._handle_block("event: ping")
    stream._handle_block("data: not-json")
    assert seen == []


def test_event_stream_survives_callback_error():
    """单条回调抛异常不能拖垮订阅线程（否则一次坏事件就永久失去通知）。"""
    def boom(_):
        raise RuntimeError("回调炸了")

    stream = opencode_client.EventStream("http://x", "pw", boom)
    stream._handle_block('data: {"type":"session.execution.failed"}')