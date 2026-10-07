"""下发前预检：引擎可用 → 配置就绪 → 真实连通。

预检在 POST /tasks 内同步完成，任一步失败即不入队，并把自然语言
原因回给前端。

v0.5.2 的关键转变：第二、三步不再比对 a4agent 自己写入的托管配置
（a4a_p* 条目 / llm-deepseek 段），改为读取**用户自己在应用内配置的内容**，
并用一次真实的最小无头调用来验证「能不能真跑」。

这样做的原因：dsh / ZCode / pi / Qoder 都自带完整供应商界面，用户在那边
配的任意组合本工具无从预知（也无需预知）。字段比对只会把「配置得和我
不一样」误判成不可用；而真实调用则不管怎么配都能给出诚实答案。

注意「预检通过 ≡ 任务将以该配置执行」依然不成立：预检跑的是一次独立调用，
正式任务读的是引擎自己的实时配置（用户可能中途改了）。措辞因此只承诺
「已检测引擎可用」。
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config_manager, task_engines
from .openai_proxy import PROXY_PORT_END, PROXY_PORT_START

TIMEOUT_SECONDS = 8
# 本地翻译代理的端口区间（与 openai_proxy 的钉定保持同一来源）
_PROXY_PORTS = range(PROXY_PORT_START, PROXY_PORT_END + 1)

_STEPS = (("engine", "引擎可用"), ("config_sync", "配置就绪"), ("reachability", "真实连通"))


def _base(url: str) -> str:
    return str(url or "").rstrip("/")


def _host_port(url: str) -> tuple:
    from urllib.parse import urlparse

    p = urlparse(str(url or ""))
    return (p.hostname or "").lower(), p.port


def _http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple:
    """返回 (状态码, 响应文本)；网络层异常抛 urllib.error.URLError 由调用方归类。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            return int(resp.status), (resp.read() or b"").decode("utf-8", "replace")[:400]
    except urllib.error.HTTPError as e:
        return int(e.code), (e.read() or b"").decode("utf-8", "replace")[:400]


def check_service(provider, api_key: str, model: str = "") -> dict:
    """服务商连通性：云端最小请求；本地服务与翻译代理只做探活。

    model 必须用生效方案的真实模型名：Anthropic 协议对缺模型名直接回 400，
    写 None/占位值会把「连通」误判成「暂不可达」。
    """
    base = _base(provider.api_base)
    host, port = _host_port(base)
    loopback = host in ("127.0.0.1", "localhost", "::1")

    if loopback and port in _PROXY_PORTS:
        running = _proxy_alive(port)
        return {
            "ok": running,
            "detail": f"本地翻译代理 :{port} " + ("运行中" if running else "未运行，请先切换一次配置方案"),
        }
    if loopback:
        # 本地模型（llama-server）：不要求 Key，探 /v1/models 即可
        try:
            status, _ = _http("GET", f"{base}/models", {"Authorization": f"Bearer {api_key or 'none'}"})
        except (urllib.error.URLError, OSError) as e:
            return {"ok": False, "detail": f"本地推理服务未启动（{base}：{getattr(e, 'reason', e)}）"}
        return {
            "ok": status // 100 == 2,
            "detail": f"本地推理服务 {base} 响应 {status}" + ("" if status // 100 == 2 else "，请确认服务已启动"),
        }

    if provider.api_type == "anthropic":
        url, method, body = (
            f"{base}/v1/messages",
            "POST",
            {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
        )
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    else:
        url, method, body, headers = f"{base}/models", "GET", None, {"Authorization": f"Bearer {api_key}"}

    try:
        status, text = _http(method, url, headers, body)
    except urllib.error.URLError as e:
        return {"ok": False, "detail": f"服务商暂不可达（{getattr(e, 'reason', e)}）"}
    except OSError as e:
        return {"ok": False, "detail": f"服务商暂不可达（{e}）"}

    if status in (401, 403):
        return {"ok": False, "detail": "密钥无效或已过期，请在「供应商管理」里更新 API Key"}
    if status // 100 != 2 and provider.api_type != "anthropic":
        # 部分 OpenAI 兼容服务不实现 /models：退一次最小 chat 请求再判
        try:
            body = {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]}
            status, text = _http("POST", f"{base}/chat/completions", headers, body)
        except (urllib.error.URLError, OSError) as e:
            return {"ok": False, "detail": f"服务商暂不可达（{getattr(e, 'reason', e)}）"}
    if status in (401, 403):
        return {"ok": False, "detail": "密钥无效或已过期，请在「供应商管理」里更新 API Key"}
    if status // 100 != 2:
        return {"ok": False, "detail": f"服务商暂不可达（HTTP {status}）"}
    return {"ok": True, "detail": f"{provider.name} 响应正常（HTTP {status}）"}


def _proxy_alive(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", int(port))) == 0


def _read_user_config_signal(tool: str) -> tuple[bool, str]:
    """读用户自己配的配置，判断「是否已配好服务商与模型」。

    与 v0.5.2 之前的关键差异：这里读的是**用户自己在应用内配的配置**，
    而不是 a4agent 写入的托管条目（a4a_p* / llm-deepseek 段）。因此本步骤
    只做粗粒度的就绪判断，不比对任何具体服务商或模型名——那是用户自己的
    自由度，本工具无权也不必去评判。

    返回 (是否读到配置, 补充说明)。读不到明文配置时返回 (False, 说明)，
    交由调用方如实告知用户「无法校验」，而不是假装通过。
    """
    from . import task_engines

    hint = task_engines.CONFIG_HINTS.get(tool, "")
    try:
        if tool == "claude":
            settings = config_manager.read_settings()
            env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
            ok = bool(env.get("ANTHROPIC_BASE_URL") and env.get("ANTHROPIC_AUTH_TOKEN"))
            return ok, ("已在 ~/.claude/settings.json 配置" if ok else f"请先在 {hint}")
        if tool == "codex":
            cfg = config_manager.read_codex_settings()
            ok = bool(cfg.get("model_provider") and cfg.get("model"))
            return ok, (f"当前模型 {cfg.get('model')}" if ok else f"请先在 {hint}")
        if tool == "zcode":
            cli = config_manager.read_zcode_cli_config()
            providers = cli.get("provider") if isinstance(cli.get("provider"), dict) else {}
            ok = bool(cli.get("model") and providers)
            return ok, (f"当前模型 {cli.get('model')}" if ok else f"请先在 {hint}")
        if tool == "pi":
            models = config_manager.read_pi_models_config()
            settings = config_manager.read_pi_settings()
            providers = models.get("providers") if isinstance(models.get("providers"), dict) else {}
            pid = str(settings.get("defaultProvider") or "")
            entry = providers.get(pid) if pid else None
            ok = isinstance(entry, dict) and bool(entry.get("baseUrl"))
            return ok, (
                f"当前 {pid or '（未选）'} / {settings.get('defaultModel') or '（未选）'}"
                if ok else f"请先在 {hint}"
            )
        if tool == "dsh":
            # 只看 dsh 自己界面配的正规命名空间 llm-pi-ai；
            # llm-deepseek 是旧版 a4agent 写入的侧门路径，已在v0.5.2 移除
            settings = config_manager.read_dsh_settings()
            ns = settings.get("llm-pi-ai") if isinstance(settings, dict) else None
            providers = ns.get("providers") if isinstance(ns, dict) else None
            ok = isinstance(providers, dict) and bool(providers)
            return ok, (f"已配 {len(providers)} 个供应商" if ok else f"请先在 {hint}")
        if tool == "qoder":
            # Qoder 的模型配置在 ~/.qoder/.models/<uid>/ 下经 WASM 按机器码
            # 加密，本工具读不到内容，因此不做就绪判断，交由第三步实测兜底
            return False, "Qoder 模型配置经专有加密，无法读取，请以实测结果为准"
    except Exception as e:  # 读配置失败不应让整个预检崩掉
        return False, f"读取 {tool} 配置失败（{e}），请以实测结果为准"
    return False, f"请先在 {hint}"


def smoke_run(tool: str, timeout: int = 90) -> dict:
    """真实跑一次最小无头调用，验证「能不能真用」。

    这是解耦后的核心：不再比对配置字段，而是直接验证引擎能否用用户自己的
    配置完成一次真实往返。配置对不对、密钥有效没、模型名拼写、网络通不通
    ——一次调用全都会暴露出来，比任何字段比对都可靠。

    cwd 固定为用户主目录：留空会继承 a4agent 自己的进程目录（开发时是仓库、
    打包后是安装目录），引擎会在那里读到不该读的项目配置。command 侧不再
    重复传工作目录，与 task_runner._run_cli 保持同一种处理方式。
    """
    from . import task_engines

    try:
        argv = task_engines.build_argv(tool, "回复 ok 两个字即可")
    except ValueError as e:
        return {"ok": False, "detail": str(e)}

    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            input="",
            cwd=str(Path.home()),
            env=dict(os.environ),
            creationflags=_no_window(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": f"实测超时（{timeout}s），引擎可能卡在等待输入"}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "detail": f"无法启动引擎：{e}"}

    # stderr 必须一起喂给解析器：引擎的失败原因（401/404/模型不存在等）
    # 通常打在 stderr，只给 stdout 会让用户只看到「退出码 1」而不知原因。
    # task_runner._run_cli 也是把两者合并落盘后再解析，两处保持一致。
    merged = done.stdout or ""
    if (done.stderr or "").strip():
        merged = f"{merged}\n{done.stderr}"
    parsed = task_engines.parse_output(tool, merged, done.returncode)
    if parsed["ok"]:
        return {"ok": True, "detail": "实测通过，引擎可用"}
    return {"ok": False, "detail": f"实测未通过：{parsed['error']}"}


def _no_window() -> int:
    """桌面应用绝不能弹黑窗（与 task_engines 同一套做法）。"""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(db, tool: str, provider, api_key: str, model: str) -> dict:
    """执行预检，返回 {ok, reason, steps}；steps 明细入库供详情展示。

    v0.5.2 后的三步：引擎可用 → 配置就绪（读用户自己的配置）→ 真实连通
    （跑一次最小无头调用）。第三步是关键：它验证的是「能不能真跑」，而
    不是「配置字段是否等于我们写的值」，因此用户在自己应用里配的任意组合
    都能被支持，也不依赖 a4agent 的托管配置。
    """
    started = time.time()
    steps = []

    t0 = time.time()
    info = task_engines.probe(tool)
    steps.append(
        {
            "name": "engine",
            "label": _STEPS[0][1],
            "ok": bool(info["installed"]),
            "detail": (f"{info['label']} {info['version']}".strip() if info["installed"] else info["error"]),
            "ms": int((time.time() - t0) * 1000),
        }
    )

    if info["installed"]:
        t0 = time.time()
        ready, ready_detail = _read_user_config_signal(tool)
        steps.append(
            {"name": "config_sync", "label": _STEPS[1][1], "ok": ready,
             "detail": ready_detail, "ms": int((time.time() - t0) * 1000)}
        )
        # 就绪判断失败时不再实测（配置都没配好，跑必然失败，徒增等待）
        if ready:
            t0 = time.time()
            live = smoke_run(tool)
            steps.append(
                {"name": "reachability", "label": _STEPS[2][1], "ok": live["ok"],
                 "detail": live["detail"], "ms": int((time.time() - t0) * 1000)}
            )

    failed = next((s for s in steps if not s["ok"]), None)
    return {
        "ok": failed is None,
        "reason": "" if failed is None else failed["detail"],
        "steps": steps,
        "ms": int((time.time() - started) * 1000),
    }
