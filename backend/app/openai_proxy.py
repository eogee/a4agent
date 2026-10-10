"""本地翻译代理：把 Claude Code 的 Anthropic 请求翻译成 OpenAI Chat Completions。

官方 Claude Code 只支持 Anthropic 协议，不识别 OPENAI_BASE_URL 等配置。
对于 api_type=openai 的服务商，切换时本工具会启动一个本地代理
（127.0.0.1:17890+），把 /v1/messages 请求翻译为 OpenAI 格式转发给上游，
并把流式响应翻译回 Anthropic SSE，从而无需外部代理即可接入 OpenAI 兼容端点。
"""

import asyncio
import http.client
import json
import os
import secrets
import socket
import threading
import urllib.parse
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .env_compat import env_first
from .responses_translator import (
    build_payload as _build_responses_payload,
    translate_response as _translate_responses_response,
    translate_stream as _translate_responses_stream,
)

PROXY_HOST = "127.0.0.1"
PROXY_PORT_START = 17890
PROXY_PORT_END = 17899
# 新名优先，旧 A4API_PROXY_DEBUG 兼容（见 env_compat）
_DEBUG_LOG = env_first("A4AGENT_PROXY_DEBUG", "A4API_PROXY_DEBUG")

_state: dict = {
    "token": None,
    "upstream_base": None,
    "upstream_key": None,
    "server": None,
    "thread": None,
    "port": None,
    "lock": threading.Lock(),
}

proxy_app = FastAPI(title="a4agent openai proxy")


# ---------------- 生命周期 ----------------


def _find_free_port():
    for port in range(PROXY_PORT_START, PROXY_PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((PROXY_HOST, port))
                return port
            except OSError:
                continue
    return None


def start(upstream_base: str, upstream_key: str, token: str | None = None) -> dict:
    """启动（或更新）翻译代理，返回 settings.json 需要的 base_url 与 token。

    代理已在运行时 token 保持不变（避免使已写出的 settings.json 失效）；
    仅更新上游地址与密钥请使用 update_upstream()。
    """
    with _state["lock"]:
        if _state["server"] is None:
            port = _find_free_port()
            if port is None:
                raise RuntimeError(
                    f"本地翻译代理端口 {PROXY_PORT_START}-{PROXY_PORT_END} 均被占用"
                )
            server = uvicorn.Server(
                uvicorn.Config(proxy_app, host=PROXY_HOST, port=port, log_level="warning")
            )
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            _state["server"] = server
            _state["thread"] = thread
            _state["port"] = port
        _state["token"] = token or _state["token"] or secrets.token_urlsafe(24)
        _state["upstream_base"] = upstream_base
        _state["upstream_key"] = upstream_key
        return {
            # 注意：Claude Code 会把 ANTHROPIC_BASE_URL 当作基础地址，
            # 自动追加 /v1/messages，因此这里不能带 /v1 后缀
            "base_url": f"http://{PROXY_HOST}:{_state['port']}",
            "token": _state["token"],
            "port": _state["port"],
        }


def update_upstream(upstream_base: str, upstream_key: str) -> None:
    """更新上游地址与密钥，不更换 token（已写出的 settings.json 保持有效）。"""
    with _state["lock"]:
        _state["upstream_base"] = upstream_base
        _state["upstream_key"] = upstream_key


def stop() -> None:
    """停止代理并清空上游配置。"""
    with _state["lock"]:
        server = _state["server"]
        if server is not None:
            server.should_exit = True
        _state.update(
            token=None,
            upstream_base=None,
            upstream_key=None,
            server=None,
            thread=None,
            port=None,
        )


# ---------------- 请求/响应翻译 ----------------


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _clean_text(value) -> str:
    """折叠空白/换行，避免 llama.cpp 把描述嵌进 grammar 注释时被打断。"""
    return " ".join(str(value or "").split())


def _clean_schema_descriptions(schema) -> Any:
    """递归清理 schema 内所有 description 字段中的换行。"""
    if isinstance(schema, dict):
        out: dict = {}
        for k, v in schema.items():
            # 仅当 description 的值确实是字符串时做空白折叠；
            # 否则（如工具参数里恰好有一个叫 description 的属性，值是 schema 字典）
            # 继续递归处理，避免把整个 schema 误转成字符串导致上游 400。
            if k == "description" and isinstance(v, str):
                out[k] = _clean_text(v)
            else:
                out[k] = _clean_schema_descriptions(v)
        return out
    if isinstance(schema, list):
        return [_clean_schema_descriptions(v) for v in schema]
    return schema


_LLAMA_DROP_KEYS = {
    "$schema", "pattern", "format", "maxLength", "minLength", "maxItems",
    "minItems", "multipleOf", "minimum", "maximum", "const", "default",
    "additionalProperties", "$defs", "$ref", "examples", "title",
}


def _sanitize_schema_for_llama(schema) -> Any:
    """把工具 schema 净化成 llama.cpp 可解析的简化结构。

    llama.cpp 的工具解析器很脆弱：Claude Code 的复杂 schema（$schema、pattern、
    无 type 的属性、additionalProperties 等）组合起来会让它报
    "failed to parse grammar" 或 "Unable to generate parser"。这里只保留
    type/properties/required/items/enum 等核心结构。
    """
    if isinstance(schema, dict):
        out: dict = {}
        for k, v in schema.items():
            if k in _LLAMA_DROP_KEYS:
                continue
            if k == "description" and isinstance(v, str):
                v = "".join(c for c in " ".join(v.split()) if ord(c) < 128)
            if k == "type" and isinstance(v, list):
                v = v[0] if v else "string"
            out[k] = _sanitize_schema_for_llama(v)
        if "properties" in out and "type" not in out:
            out["type"] = "object"
        return out
    if isinstance(schema, list):
        return [_sanitize_schema_for_llama(v) for v in schema]
    return schema


def _sanitize_tools(body: dict) -> list:
    """用净化后的 schema 重新生成工具列表（保留名称与描述）。"""
    tools = []
    for t in body.get("tools", []):
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": _clean_text(t.get("description", "")),
                    "parameters": _sanitize_schema_for_llama(
                        t.get("input_schema", {"type": "object", "properties": {}})
                    ),
                },
            }
        )
    return tools


def translate_request(body: dict, include_tools: bool = True) -> dict:
    """Anthropic /v1/messages 请求体 -> OpenAI chat.completions 请求体。

    include_tools=False 时丢弃工具定义：部分本地推理服务（llama.cpp/LM Studio）
    无法把复杂工具 schema 转成 grammar，会报 "failed to parse grammar"。
    """
    model = body.get("model") or "gpt-4o"
    messages: list = []
    system_parts: list = []

    system = body.get("system")
    if system:
        if isinstance(system, list):
            system_parts.extend(
                b.get("text", "")
                for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            )
        elif system:
            system_parts.append(system)

    for m in body.get("messages", []):
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, list):
            blocks = content
        elif content:
            blocks = [{"type": "text", "text": content}]
        else:
            blocks = []

        if role == "user":
            parts = []
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                btype = b.get("type")
                if btype == "text":
                    parts.append({"type": "text", "text": b.get("text", "")})
                elif btype == "image":
                    src = b.get("source", {})
                    media = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    parts.append(
                        {"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}}
                    )
                elif btype == "tool_result":
                    tcontent = b.get("content")
                    if isinstance(tcontent, list):
                        tcontent = "".join(
                            x.get("text", "")
                            for x in tcontent
                            if isinstance(x, dict) and x.get("type") == "text"
                        )
                    text = str(tcontent or "")
                    if b.get("is_error"):
                        text = f"[错误] {text}" if text else "[调用出错]"
                    messages.append(
                        {"role": "tool", "tool_call_id": b.get("tool_use_id", ""), "content": text}
                    )
            if parts:
                if len(parts) == 1 and parts[0]["type"] == "text":
                    messages.append({"role": "user", "content": parts[0]["text"]})
                else:
                    messages.append({"role": "user", "content": parts})

        elif role == "assistant":
            text_parts = [
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            tool_calls = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": b.get("name", ""),
                                "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                            },
                        }
                    )
            if not text_parts and not tool_calls:
                continue
            msg: dict = {"role": "assistant"}
            if text_parts:
                msg["content"] = "".join(text_parts)
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        elif role == "system":
            # Claude Code 新版本会把部分系统提示放在 messages 里
            if isinstance(content, list):
                system_parts.extend(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            elif content:
                system_parts.append(content)

    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(p for p in system_parts if p)})

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    if body.get("max_tokens"):
        payload["max_tokens"] = int(body["max_tokens"])
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]

    if include_tools:
        tools = []
        for t in body.get("tools", []):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": _clean_text(t.get("description", "")),
                        "parameters": _clean_schema_descriptions(
                            t.get("input_schema", {"type": "object", "properties": {}})
                        ),
                    },
                }
            )
        if tools:
            payload["tools"] = tools

        tool_choice = body.get("tool_choice")
        if tool_choice:
            ttype = tool_choice.get("type")
            if ttype == "any":
                payload["tool_choice"] = "required"
            elif ttype == "tool":
                payload["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tool_choice.get("name", "")},
                }
            else:
                payload["tool_choice"] = "auto"

    if payload["stream"]:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _map_finish(reason):
    if reason == "tool_calls":
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    return "end_turn" if reason else None


def translate_response(data: dict, model: str) -> dict:
    """OpenAI 非流式响应 -> Anthropic /v1/messages 响应体。"""
    choices = data.get("choices") or []
    usage = data.get("usage") or {}
    if not choices:
        raise ValueError("上游响应缺少 choices")
    msg = choices[0].get("message") or {}
    content = []
    if msg.get("content"):
        content.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
        content.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": args,
            }
        )
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": _map_finish(choices[0].get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def translate_stream(chunks, model: str):
    """把 OpenAI 流式分片（dict 迭代器）转换为 Anthropic SSE 事件字符串序列。"""
    yield _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    text_started = False
    tool_open = {}  # OpenAI tool index -> Anthropic block index
    tool_buf: dict = {}  # OpenAI tool index -> {id, name}
    next_block_index = 1
    output_tokens = 0
    finish = None

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        usage = chunk.get("usage")
        if usage:
            output_tokens = usage.get("completion_tokens", output_tokens) or 0
        for ch in chunk.get("choices") or []:
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
            delta = ch.get("delta") or {}

            content = delta.get("content")
            if content:
                if not text_started:
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": 0,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    text_started = True
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": content},
                    },
                )

            for tc in delta.get("tool_calls") or []:
                t_idx = tc.get("index", 0)
                buf = tool_buf.setdefault(t_idx, {"id": None, "name": ""})
                if tc.get("id"):
                    buf["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    buf["name"] = fn["name"]
                args_frag = fn.get("arguments")

                if t_idx not in tool_open:
                    block_idx = next_block_index
                    next_block_index += 1
                    tool_open[t_idx] = block_idx
                    yield _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": block_idx,
                            "content_block": {
                                "type": "tool_use",
                                "id": buf["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                                "name": buf["name"],
                                "input": {},
                            },
                        },
                    )
                if args_frag:
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": tool_open[t_idx],
                            "delta": {"type": "input_json_delta", "partial_json": args_frag},
                        },
                    )

    if text_started:
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
    for t_idx, block_idx in sorted(tool_open.items()):
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})

    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": _map_finish(finish), "stop_sequence": None},
            "usage": {"input_tokens": 0, "output_tokens": output_tokens},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


# ---------------- 上游 HTTP ----------------


def _open_upstream(payload: dict, upstream_base: str, upstream_key: str):
    base = (upstream_base or "").rstrip("/")
    if not base:
        raise RuntimeError("翻译代理未配置上游地址")
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path + "/chat/completions"
    if not path.startswith("/"):
        path = "/" + path
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    conn = (
        http.client.HTTPSConnection(host, port, timeout=180)
        if parsed.scheme == "https"
        else http.client.HTTPConnection(host, port, timeout=180)
    )
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {upstream_key or ''}",
    }
    conn.request(
        "POST",
        path,
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    return conn, conn.getresponse()


def _iter_openai_chunks(resp):
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _normalize_tool_call_nulls(chunk: dict) -> None:
    """把流式分片里 tool_calls 的 null 字段归一为「省略键」。

    opencode zen 等上游把后续 tool_call 分片的 id/name 置为 null（而非省略），
    dsh-llm-deepseek 适配器用 `x !== void 0` 判断（lib/index.js:321-322），
    null 会通过检查并把已解析出的工具名/ID 覆盖为空，导致 harness 报
    `unknown tool ""`。这里把 null 键删除，等价于上游省略该字段，
    客户端（含 dsh、a4agent 自身）将保留首个分片解析出的 id/name。
    """
    for choice in chunk.get("choices") or []:
        delta = choice.get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            if tc.get("id") is None:
                tc.pop("id", None)
            fn = tc.get("function")
            if isinstance(fn, dict) and fn.get("name") is None:
                fn.pop("name", None)


def _iter_openai_chunks_from_lines(lines):
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


def _debug_log(payload: dict, status: int, raw_body: bytes) -> None:
    if not _DEBUG_LOG:
        return
    try:
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write("=== request ===\n")
            f.write(json.dumps(payload, ensure_ascii=False)[:4000] + "\n")
            f.write(f"=== upstream status {status} ===\n")
            f.write(raw_body.decode("utf-8", "replace")[:8000] + "\n")
    except OSError:
        pass


def _error_body(message: str, etype: str = "api_error") -> dict:
    return {"type": "error", "error": {"type": etype, "message": message}}


def _upstream_status(status: int) -> int:
    """上游 4xx 保留原状态码，5xx 统一映射为 502（Bad Gateway）。

    429/408 等可退避状态透传给客户端后，客户端会读取 Retry-After 并退避重试，
    而不是在 502 下无间隔猛打重连风暴、进一步撞上游限流。
    """
    return status if 400 <= status < 500 else 502


def _retry_after_headers(resp) -> dict:
    """透传上游 Retry-After，供客户端在 429/503 时按建议时间退避。"""
    ra = resp.getheader("Retry-After")
    return {"Retry-After": ra} if ra else {}


def _is_grammar_error(status: int, detail: str) -> bool:
    """识别 llama.cpp 等本地推理服务的工具 schema 解析失败。"""
    return status == 400 and any(
        s in detail
        for s in (
            "grammar",
            "samplers",
            "Unable to generate parser",
            "Unrecognized schema",
            "JSON schema conversion failed",
        )
    )


# ---------------- FastAPI 端点 ----------------


@proxy_app.post("/v1/messages")
async def messages(request: Request):
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write("=== incoming ===\n")
                f.write(json.dumps(dict(request.headers), ensure_ascii=False) + "\n")
        except OSError:
            pass
    token = request.headers.get("x-api-key")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not _state["token"] or token != _state["token"]:
        return JSONResponse(
            _error_body("invalid x-api-key", "authentication_error"),
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error_body("invalid JSON body", "invalid_request_error"), status_code=400)
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG + ".req.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False))
        except OSError:
            pass

    with _state["lock"]:
        upstream_base, upstream_key = _state["upstream_base"], _state["upstream_key"]
    if not upstream_base or not upstream_key:
        return JSONResponse(_error_body("proxy is not configured"), status_code=503)

    try:
        payload = translate_request(body)
    except Exception as e:
        return JSONResponse(_error_body(str(e), "invalid_request_error"), status_code=400)

    model = body.get("model") or ""
    if body.get("stream"):
        def gen():
            conn = None
            try:
                payload_used = payload
                conn, resp = _open_upstream(payload_used, upstream_base, upstream_key)
                if resp.status != 200:
                    detail = resp.read().decode("utf-8", "replace")
                    if _is_grammar_error(resp.status, detail) and payload_used.get("tools"):
                        # 第一级降级：净化工具 schema 后重试（保留工具调用能力）
                        conn.close()
                        conn = None
                        payload_used = translate_request(body)
                        payload_used["tools"] = _sanitize_tools(body)
                        conn, resp = _open_upstream(payload_used, upstream_base, upstream_key)
                        detail = (
                            "" if resp.status == 200
                            else resp.read().decode("utf-8", "replace")
                        )
                    if _is_grammar_error(resp.status, detail) and payload_used.get("tools"):
                        # 第二级降级：仍无法解析则去掉工具
                        conn.close()
                        conn = None
                        payload_used = translate_request(body, include_tools=False)
                        conn, resp = _open_upstream(payload_used, upstream_base, upstream_key)
                        detail = (
                            "" if resp.status == 200
                            else resp.read().decode("utf-8", "replace")
                        )
                    if resp.status != 200:
                        yield _sse(
                            "error",
                            _error_body(f"upstream HTTP {resp.status}: {detail[:500]}"),
                        )
                        return
                if _DEBUG_LOG:
                    raw_body = resp.read()
                    _debug_log(payload_used, resp.status, raw_body)
                    chunks = _iter_openai_chunks_from_lines(
                        raw_body.decode("utf-8", "replace").splitlines()
                    )
                else:
                    chunks = _iter_openai_chunks(resp)
                for ev in translate_stream(chunks, model):
                    yield ev
            except Exception as e:
                yield _sse("error", _error_body(str(e)))
            finally:
                if conn is not None:
                    conn.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    conn = None
    try:
        payload_used = payload
        conn, resp = await asyncio.to_thread(
            _open_upstream, payload_used, upstream_base, upstream_key
        )
        if resp.status != 200:
            detail = resp.read().decode("utf-8", "replace")
            if _is_grammar_error(resp.status, detail) and payload_used.get("tools"):
                # 第一级降级：净化工具 schema 后重试（保留工具调用能力）
                conn.close()
                conn = None
                payload_used = translate_request(body)
                payload_used["tools"] = _sanitize_tools(body)
                conn, resp = await asyncio.to_thread(
                    _open_upstream, payload_used, upstream_base, upstream_key
                )
                detail = (
                    "" if resp.status == 200
                    else resp.read().decode("utf-8", "replace")
                )
            if _is_grammar_error(resp.status, detail) and payload_used.get("tools"):
                # 第二级降级：仍无法解析则去掉工具
                conn.close()
                conn = None
                payload_used = translate_request(body, include_tools=False)
                conn, resp = await asyncio.to_thread(
                    _open_upstream, payload_used, upstream_base, upstream_key
                )
                detail = (
                    "" if resp.status == 200
                    else resp.read().decode("utf-8", "replace")
                )
            if resp.status != 200:
                return JSONResponse(
                    _error_body(f"upstream HTTP {resp.status}: {detail[:500]}"),
                    status_code=_upstream_status(resp.status),
                    headers=_retry_after_headers(resp),
                )
        data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return JSONResponse(_error_body(str(e)), status_code=502)
    finally:
        if conn is not None:
            conn.close()
    if _DEBUG_LOG:
        _debug_log(payload, resp.status, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    try:
        return JSONResponse(translate_response(data, model))
    except Exception as e:
        return JSONResponse(_error_body(str(e)), status_code=502)


@proxy_app.api_route("/v1/api/hello", methods=["GET", "HEAD", "POST"])
async def _hello():
    return JSONResponse({"ok": True})


@proxy_app.get("/v1/api/version")
async def _version():
    """返回代理能力版本，供宿主工具校验当前代理是否支持 Codex Responses。"""
    return {
        "name": "a4agent-proxy",
        "version": 2,
        "features": ["anthropic_messages", "openai_responses"],
    }


@proxy_app.post("/v1/proxy/refresh")
async def _proxy_refresh(request: Request):
    """按数据库当前生效配置刷新上游地址与密钥，避免切换后代理仍在用旧上游。

    与其余端点同权要求 token：本端点会把生效配置解密后装进代理，不能对本机
    任意进程/网页开放（响应还会带出 upstream_base 与模型名）。
    """
    token = request.headers.get("x-api-key")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not _state["token"] or token != _state["token"]:
        return JSONResponse(
            _error_body("invalid bearer token", "authentication_error"),
            status_code=401,
        )
    try:
        from .config_manager import target_list
        from .crypto import decrypt_text
        from .database import SessionLocal
        from .models import Configuration
    except Exception as e:
        return JSONResponse(_error_body(f"refresh unavailable: {e}"), status_code=500)
    db = SessionLocal()
    try:
        c = db.query(Configuration).filter(Configuration.is_active.is_(True)).first()
        if c is None or c.provider is None or c.provider.api_type != "openai":
            return JSONResponse(_error_body("no active openai provider"), status_code=409)
        if not (set(target_list(c.targets)) & {"claude", "codex", "dsh"}):
            return JSONResponse(_error_body("active config does not need proxy"), status_code=409)
        key = decrypt_text(str(c.api_key_encrypted))
        if not key:
            return JSONResponse(_error_body("api key decrypt failed"), status_code=500)
        update_upstream(c.provider.api_base, key)
        return {"ok": True, "upstream_base": c.provider.api_base, "model": c.model}
    finally:
        db.close()


@proxy_app.api_route("/chat/completions", methods=["POST"])
@proxy_app.api_route("/v1/chat/completions", methods=["POST"])
async def chat_completions(request: Request):
    """OpenAI chat/completions 透明透传入口（供 dsh 等原生 OpenAI 客户端走本地代理）。

    与 /v1/messages、/responses 不同，这里不做协议翻译，直接把请求转发给上游；
    仅对**流式响应**中 tool_calls 分片的 null 字段做归一（见
    _normalize_tool_call_nulls），让 dsh-llm-deepseek 等对 null 敏感的客户端
    能正确保留工具名/ID，其余字段原样透传。非流式响应原样透传。
    """
    token = request.headers.get("x-api-key")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not _state["token"] or token != _state["token"]:
        return JSONResponse(
            _error_body("invalid bearer token", "authentication_error"),
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error_body("invalid JSON body", "invalid_request_error"), status_code=400)
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG + ".chat.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False))
        except OSError:
            pass

    with _state["lock"]:
        upstream_base, upstream_key = _state["upstream_base"], _state["upstream_key"]
    if not upstream_base or not upstream_key:
        return JSONResponse(_error_body("proxy is not configured"), status_code=503)

    try:
        conn, resp = await asyncio.to_thread(
            _open_upstream, body, upstream_base, upstream_key
        )
    except Exception as e:
        return JSONResponse(_error_body(str(e)), status_code=502)

    if resp.status != 200:
        detail = resp.read().decode("utf-8", "replace")
        conn.close()
        if _DEBUG_LOG:
            _debug_log(body, resp.status, detail.encode("utf-8"))
        return JSONResponse(
            _error_body(f"upstream HTTP {resp.status}: {detail[:500]}"),
            status_code=_upstream_status(resp.status),
            headers=_retry_after_headers(resp),
        )

    if body.get("stream"):
        def gen():
            try:
                if _DEBUG_LOG:
                    raw_body = resp.read()
                    _debug_log(body, resp.status, raw_body)
                    chunks = _iter_openai_chunks_from_lines(
                        raw_body.decode("utf-8", "replace").splitlines()
                    )
                else:
                    chunks = _iter_openai_chunks(resp)
                for chunk in chunks:
                    _normalize_tool_call_nulls(chunk)
                    yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                yield _sse("error", _error_body(str(e)))
            finally:
                conn.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        conn.close()
        return JSONResponse(_error_body(str(e)), status_code=502)
    finally:
        conn.close()
    if _DEBUG_LOG:
        _debug_log(body, resp.status, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return JSONResponse(data)


@proxy_app.post("/responses")
@proxy_app.post("/v1/responses")
async def responses(request: Request):
    """Codex Responses 协议入口：翻译为上游 chat/completions 并翻译回 Responses。"""
    token = request.headers.get("x-api-key")
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not _state["token"] or token != _state["token"]:
        return JSONResponse(
            _error_body("invalid bearer token", "authentication_error"),
            status_code=401,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error_body("invalid JSON body", "invalid_request_error"), status_code=400)
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG + ".responses.json", "w", encoding="utf-8") as f:
                f.write(json.dumps(body, ensure_ascii=False))
        except OSError:
            pass

    with _state["lock"]:
        upstream_base, upstream_key = _state["upstream_base"], _state["upstream_key"]
    if not upstream_base or not upstream_key:
        return JSONResponse(_error_body("proxy is not configured"), status_code=503)

    try:
        payload = _build_responses_payload(body)
    except Exception as e:
        return JSONResponse(_error_body(str(e), "invalid_request_error"), status_code=400)

    model = body.get("model") or ""
    stream = bool(body.get("stream"))
    try:
        conn, resp = await asyncio.to_thread(
            _open_upstream, payload, upstream_base, upstream_key
        )
    except Exception as e:
        return JSONResponse(_error_body(str(e)), status_code=502)

    if resp.status != 200:
        detail = resp.read().decode("utf-8", "replace")
        conn.close()
        if _DEBUG_LOG:
            _debug_log(payload, resp.status, detail.encode("utf-8"))
        return JSONResponse(
            _error_body(f"upstream HTTP {resp.status}: {detail[:500]}"),
            status_code=_upstream_status(resp.status),
            headers=_retry_after_headers(resp),
        )

    if stream:
        def gen():
            try:
                if _DEBUG_LOG:
                    raw_body = resp.read()
                    _debug_log(payload, resp.status, raw_body)
                    chunks = _iter_openai_chunks_from_lines(
                        raw_body.decode("utf-8", "replace").splitlines()
                    )
                else:
                    chunks = _iter_openai_chunks(resp)
                for event, data in _translate_responses_stream(chunks, model):
                    yield _sse(event, data)
            except Exception as e:
                yield _sse("error", _error_body(str(e)))
            finally:
                conn.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    try:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        conn.close()
        return JSONResponse(_error_body(str(e)), status_code=502)
    finally:
        conn.close()
    if _DEBUG_LOG:
        _debug_log(payload, resp.status, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    try:
        return JSONResponse(_translate_responses_response(data, model))
    except Exception as e:
        return JSONResponse(_error_body(str(e)), status_code=502)


@proxy_app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def _catch_all(request: Request, path: str):
    if _DEBUG_LOG:
        try:
            with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"=== unhandled {request.method} /{path} ===\n")
                f.write(json.dumps(dict(request.headers), ensure_ascii=False) + "\n")
                if request.method in ("POST", "PUT", "PATCH"):
                    try:
                        raw = await request.body()
                        f.write(raw.decode("utf-8", "replace")[:4000] + "\n")
                    except Exception:
                        pass
        except OSError:
            pass
    return JSONResponse(
        {"type": "error", "error": {"type": "not_found", "message": f"unknown endpoint: {path}"}},
        status_code=404,
    )
