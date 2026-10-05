"""下发前预检：引擎探测 + 服务商连通 + 端配置一致性第二跳。

预检在 POST /tasks 内同步完成（≤8s/步），任一步失败即不入队，并把自然语言
原因回给前端。注意「预检通过 ≡ 任务将以可用配置启动」并不成立：无头 CLI 读的是
自己的配置文件，与数据库里的生效方案会漂移，所以有第三步（见 §5 风险 #14）。
"""
import json
import time
import urllib.error
import urllib.request

from . import config_manager, task_engines
from .openai_proxy import PROXY_PORT_END, PROXY_PORT_START

TIMEOUT_SECONDS = 8
# 本地翻译代理的端口区间（与 openai_proxy 的钉定保持同一来源）
_PROXY_PORTS = range(PROXY_PORT_START, PROXY_PORT_END + 1)

_STEPS = (("engine", "引擎可用"), ("reachability", "服务商连通"), ("config_sync", "端配置一致"))


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


def check_engine_config(tool: str, provider, model: str) -> dict:
    """第二跳：读引擎自己的配置，确认它当前指向的就是要用的方案与模型。"""
    base = _base(provider.api_base)
    if tool == "pi":
        settings = config_manager.read_pi_settings()
        models_config = config_manager.read_pi_models_config()
        providers = models_config.get("providers") if isinstance(models_config.get("providers"), dict) else {}
        pid = str(settings.get("defaultProvider") or "")
        entry = providers.get(pid) if pid else None
        if not pid or not isinstance(entry, dict):
            return {
                "ok": False,
                "detail": "pi 尚未指向任何由 a4agent 托管的服务商，请先在「配置方案」页切换一次",
            }
        actual_base = _base(str(entry.get("baseUrl") or ""))
        ids = {str(m.get("id")) for m in (entry.get("models") or []) if isinstance(m, dict)}
        if actual_base != base:
            return {
                "ok": False,
                "detail": f"pi 当前指向 {pid}（{actual_base}），与生效方案的 {base} 不一致，请重新切换方案",
            }
        if model and ids and model not in ids:
            return {"ok": False, "detail": f"pi 的 {pid} 下没有模型 {model}，请重新切换方案"}
        return {"ok": True, "detail": f"pi 正在使用 {pid} / {model or '（未记模型）'}"}

    if tool == "dsh":
        settings = config_manager.read_dsh_settings()
        llm = settings.get(config_manager.DSH_LLM_NS) if isinstance(settings, dict) else None
        url = str((llm or {}).get("baseURL") or "") if isinstance(llm, dict) else ""
        if not url:
            return {"ok": False, "detail": "dsh 尚未写入 a4agent 的连接配置，请先在「配置方案」页切换一次"}
        host, port = _host_port(url)
        if host in ("127.0.0.1", "localhost", "::1") and port in _PROXY_PORTS and not _proxy_alive(port):
            return {"ok": False, "detail": f"dsh 依赖的本地翻译代理 :{port} 未运行，请重新切换方案以拉起代理"}
        return {"ok": True, "detail": f"dsh 连接配置为 {url}"}

    return {"ok": True, "detail": f"{tool} 暂无端配置一致性校验"}


def run(db, tool: str, provider, api_key: str, model: str) -> dict:
    """执行三步预检，返回 {ok, reason, steps}；steps 明细入库供详情展示。"""
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
        reach = check_service(provider, api_key, model)
        steps.append(
            {"name": "reachability", "label": _STEPS[1][1], "ok": reach["ok"], "detail": reach["detail"],
             "ms": int((time.time() - t0) * 1000)}
        )
        t0 = time.time()
        sync = check_engine_config(tool, provider, model)
        steps.append(
            {"name": "config_sync", "label": _STEPS[2][1], "ok": sync["ok"], "detail": sync["detail"],
             "ms": int((time.time() - t0) * 1000)}
        )

    failed = next((s for s in steps if not s["ok"]), None)
    return {
        "ok": failed is None,
        "reason": "" if failed is None else failed["detail"],
        "steps": steps,
        "ms": int((time.time() - started) * 1000),
    }
