"""无头引擎适配层测试：探测缓存、命令矩阵与 pi 的 JSONL 产出解析。"""
import json

import pytest

from backend.app import task_engines


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    task_engines._probe_cache.clear()
    yield
    task_engines._probe_cache.clear()


# ---------------- 命令解析与探测 ----------------


def test_resolve_command_rejects_unknown_tool(monkeypatch):
    with pytest.raises(ValueError):
        task_engines.resolve_command("zcode")


def test_probe_reports_missing_engine(monkeypatch):
    monkeypatch.setattr(task_engines.shutil, "which", lambda name: None)
    info = task_engines.probe("pi")
    assert info["installed"] is False
    assert "未探测到" in info["error"]


def test_probe_captures_version_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(task_engines.shutil, "which", lambda name: r"C:\npm\pi.cmd")

    def run(argv, **kwargs):
        calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "1.0.2\n", "stderr": ""})()

    monkeypatch.setattr(task_engines.subprocess, "run", run)
    first = task_engines.probe("pi")
    assert first["installed"] is True
    assert first["version"] == "1.0.2"
    assert calls[0][1] == "--version"

    # 命中缓存：不再重复执行
    task_engines.probe("pi")
    assert len(calls) == 1
    task_engines.probe("pi", refresh=True)
    assert len(calls) == 2


# ---------------- 命令矩阵 ----------------


def test_build_argv_matrix(monkeypatch):
    monkeypatch.setattr(task_engines.shutil, "which", lambda name: name)
    # 别让测试去读真实的 ~/.dsh profile
    monkeypatch.setattr(task_engines, "foreign_plugin_ids", lambda path=None: [])
    pi = task_engines.build_argv("pi", "写个测试")
    assert pi == ["pi", "-p", "写个测试", "--mode", "json", "--no-session"]
    dsh = task_engines.build_argv("dsh", "写个测试")
    assert dsh == ["dsh", "--profile", "headless", "写个测试"]


# ---------------- dsh headless 外部插件屏蔽 ----------------


def _profile_patch(tmp_path, body):
    path = tmp_path / "cordis.patch.yml"
    path.write_text(body, encoding="utf-8")
    return path


def test_foreign_plugin_ids_only_picks_relative_paths(tmp_path):
    path = _profile_patch(tmp_path, """
- insert:
    - id: dsh-hook
      name: "../../../../../ProgramMine/a4phone/dsh/lib/index.js"
- insert:
    - id: timer
      name: "@deepseek-ai/cordis-plugin-timer"
""")
    assert task_engines.foreign_plugin_ids(path) == ["dsh-hook"]


def test_foreign_plugin_ids_tolerates_missing_or_broken_file(tmp_path):
    assert task_engines.foreign_plugin_ids(tmp_path / "nope.yml") == []
    path = _profile_patch(tmp_path, "\t- 坏缩进: [")
    assert task_engines.foreign_plugin_ids(path) == []


def test_dsh_argv_carries_suppress_overlay(tmp_path, monkeypatch):
    """profile 里有外部 hook 时，下发命令带 --patch 覆盖层且只禁用那些条目。"""
    profile = _profile_patch(
        tmp_path,
        '- insert:\n    - id: dsh-hook\n      name: "../../../ProgramMine/a4phone/dsh/lib/index.js"\n',
    )
    monkeypatch.setattr(task_engines, "dsh_profile_patch_path", lambda: profile)
    monkeypatch.setattr(task_engines.shutil, "which", lambda name: "dsh")
    monkeypatch.setenv("A4AGENT_DATA_DIR", str(tmp_path / "data"))

    import yaml

    argv = task_engines.build_argv("dsh", "跑一下测试")
    assert argv[:3] == ["dsh", "--profile", "headless"]
    assert argv[-1] == "跑一下测试"
    overlay = argv[argv.index("--patch") + 1]
    assert yaml.safe_load(open(overlay, encoding="utf-8")) == [{"id": "dsh-hook", "disabled": True}]
    assert "a4agent 自动生成" in open(overlay, encoding="utf-8").read()


def test_build_argv_refuses_uninstalled_engine(monkeypatch):
    monkeypatch.setattr(task_engines.shutil, "which", lambda name: None)
    with pytest.raises(ValueError):
        task_engines.build_argv("pi", "x")


# ---------------- pi JSONL 解析 ----------------


def _pi_line(**kw):
    return json.dumps(kw, ensure_ascii=False)


def test_parse_pi_success_with_usage():
    stdout = "\n".join(
        [
            _pi_line(type="session", version=3, id="abc", cwd="C:/tmp"),
            _pi_line(type="agent_start"),
            _pi_line(
                type="message_end",
                message={
                    "role": "assistant",
                    "content": [{"type": "text", "text": "任务已完成"}],
                    "stopReason": "stop",
                    "usage": {"input": 120, "output": 30, "totalTokens": 150, "cost": {"total": 0.002}},
                },
            ),
            _pi_line(type="turn_end", message={"role": "assistant", "content": []}),
            "not-json noise line",
        ]
    )
    out = task_engines.parse_output("pi", stdout, 0)
    assert out["ok"] is True
    assert out["text"] == "任务已完成"
    assert out["usage"]["output"] == 30
    assert out["usage"]["cost"] == pytest.approx(0.002)


def test_parse_pi_detects_model_error_despite_zero_exit():
    """pi 连接失败仍可能退出码 0：成败以最后一条 assistant 消息的 stopReason 为准。"""
    events = [
        _pi_line(
            type="message_end",
            message={"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Connection error."},
        ),
        _pi_line(type="auto_retry_start", attempt=1, maxAttempts=3, delayMs=2000),
        _pi_line(type="auto_retry_start", attempt=2, maxAttempts=3, delayMs=4000),
        _pi_line(
            type="message_end",
            message={"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Connection error."},
        ),
        _pi_line(type="auto_retry_end", success=False, attempt=3, finalError="Connection error."),
        _pi_line(type="agent_settled"),
    ]
    out = task_engines.parse_output("pi", "\n".join(events), 0)
    assert out["ok"] is False
    assert out["error"] == "Connection error."


def test_parse_pi_recovers_after_intermediate_retry_error():
    """重试环里每条消息都带 errorMessage：某次成功后必须以最后一条为准，不能误判失败。"""
    events = [
        _pi_line(
            type="message_end",
            message={"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "Connection error."},
        ),
        _pi_line(type="auto_retry_start", attempt=1, maxAttempts=3, delayMs=2000),
        _pi_line(
            type="message_end",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "重试后成功"}],
                "stopReason": "stop",
                "errorMessage": "Connection error.",
            },
        ),
        _pi_line(type="auto_retry_end", success=True, attempt=1),
    ]
    out = task_engines.parse_output("pi", "\n".join(events), 0)
    assert out["ok"] is True
    assert out["text"] == "重试后成功"
    assert out["error"] == ""


def test_parse_pi_aborted_is_failure():
    out = task_engines.parse_output(
        "pi",
        _pi_line(type="message_end", message={"role": "assistant", "content": [], "stopReason": "aborted"}),
        0,
    )
    assert out["ok"] is False and out["error"]


def test_parse_pi_collects_multi_turn_text():
    stdout = "\n".join(
        [
            _pi_line(
                type="message_end",
                message={"role": "assistant", "content": [{"type": "text", "text": "先看目录"}], "stopReason": "toolUse"},
            ),
            _pi_line(type="message_end", message={"role": "user", "content": [{"type": "text", "text": "工具结果"}]}),
            _pi_line(
                type="message_end",
                message={"role": "assistant", "content": [{"type": "text", "text": "结论：没问题"}], "stopReason": "stop"},
            ),
        ]
    )
    out = task_engines.parse_output("pi", stdout, 0)
    assert out["ok"] is True
    assert out["text"] == "先看目录\n\n结论：没问题"


def test_parse_pi_empty_output_is_failure():
    out = task_engines.parse_output("pi", "", 0)
    assert out["ok"] is False
    assert "无文本产出" in out["error"]


# ---------------- 直出型引擎解析 ----------------


def test_parse_flat_plain_text_and_json_field():
    ok = task_engines.parse_output("dsh", "跑完了，一切正常", 0)
    assert ok["ok"] is True and ok["text"] == "跑完了，一切正常"

    js = json.dumps({"response": "结构化结果", "other": 1}, ensure_ascii=False)
    parsed = task_engines.parse_output("dsh", js, 0)
    assert parsed["text"] == "结构化结果"

    failed = task_engines.parse_output("dsh", "", 2)
    assert failed["ok"] is False and "退出码 2" in failed["error"]


def test_parse_flat_crash_summary_uses_real_error_line():
    """本机 dsh 真实失败：摘要要带上它自己的 Error 行，而不是只说「退出码 1」。"""
    raw = (
        'file:///C:/Users/x/dsh-app-boot/lib/index.js:1187\n'
        '\t\tthrow new Error(`${binName}: ${stage}: ${detail}${stack}`, { cause });\n'
        '      ^\n\n'
        'Error: dsh: plugin tree failed to load: dsh: 1 entry did not activate\n'
        '../../ProgramMine/a4phone/dsh/lib/index.js: pending (waiting for service: workspaceRegistry)\n'
        '    at boot (file:///C:/Users/x/dsh-app-boot/lib/index.js:1187:9)\n'
    )
    out = task_engines.parse_output("dsh", raw, 1)
    assert out["ok"] is False
    assert "plugin tree failed to load" in out["error"]
    assert "node:internal" not in out["error"]


def test_parse_flat_summary_falls_back_to_first_line():
    """无英文特征词的一行式失败（本机 dsh 实测）也要进摘要。"""
    raw = '\ndsh: AUTH: upstream HTTP 403: {"error_type":"governance.dots_platform_key_not_allowed"}\n'
    out = task_engines.parse_output("dsh", raw, 1)
    assert out["ok"] is False
    assert "dots_platform_key_not_allowed" in out["error"]


def test_healthy_probe_output_is_not_treated_as_crash():
    """版本号里没有错误特征词也不能被当成崩溃——摘要用的宽松回退不能污染探测。"""
    done = type("R", (), {"stdout": "1.0.2\n", "stderr": ""})()
    assert task_engines._crash_line(done) == ""
    assert task_engines._failure_summary(["1.0.2"]) == "1.0.2"
