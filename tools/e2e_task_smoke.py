"""端到端烟测：真实 pi 引擎 + 模拟 OpenAI 服务商，跑通无头任务全链路。

不依赖真实 API Key：服务商是本机一个最小 OpenAI 兼容桩服务。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MOCK_PORT = 18199
APP_PORT = 8188
REPLY = "模拟回复：任务已完成"

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# 沙箱放系统临时目录：跑完即删，不在仓库里留脏文件
SANDBOX = os.path.join(tempfile.gettempdir(), "a4agent-e2e-task")


class Mock(BaseHTTPRequestHandler):
    # 默认 HTTP/1.0 会让 pi 的 Node 客户端判为连接异常（实测 Connection error.）
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def _send(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send({"object": "list", "data": [{"id": "mock-model", "object": "model"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            chunk = {
                "id": "gen_1", "object": "chat.completion.chunk", "created": 1,
                "model": "mock-model",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY},
                             "finish_reason": "stop"}],
            }
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._send({
                "id": "gen_1", "object": "chat.completion", "created": 1, "model": "mock-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
            })


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}/api/v1{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def main():
    import shutil

    shutil.rmtree(SANDBOX, ignore_errors=True)
    os.makedirs(os.path.join(SANDBOX, "data"), exist_ok=True)
    os.makedirs(os.path.join(SANDBOX, "pi"), exist_ok=True)

    srv = HTTPServer(("127.0.0.1", MOCK_PORT), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 本机全局 pi.cmd 已损坏（路径里的 \b 被吃成退格），这里放一个可用的 shim，
    # 让烟测跑的是真实 pi 运行时，同时不去改动用户的安装
    bin_dir = os.path.join(SANDBOX, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    pi_entry = os.path.join(
        ROOT, "..", "pi", "packages", "coding-agent", "dist", "bundle", "cli.js"
    )
    pi_entry = os.path.abspath(pi_entry)
    if not os.path.exists(pi_entry):
        raise SystemExit(f"未找到 pi 入口：{pi_entry}")
    with open(os.path.join(bin_dir, "pi.cmd"), "w", encoding="ascii") as fh:
        fh.write("@echo off\r\nnode \"%s\" %%*\r\n" % pi_entry)

    env = dict(os.environ)
    env.update({
        "PATH": bin_dir + os.pathsep + env.get("PATH", ""),
        "A4AGENT_DATA_DIR": os.path.join(SANDBOX, "data"),
        "A4AGENT_PI_AGENT_DIR": os.path.join(SANDBOX, "pi"),
        "A4AGENT_SETTINGS_PATH": os.path.join(SANDBOX, "claude.json"),
        "A4AGENT_CODEX_CONFIG_PATH": os.path.join(SANDBOX, "codex.toml"),
        "A4AGENT_ZCODE_CLI_CONFIG_PATH": os.path.join(SANDBOX, "zcode-cli.json"),
        "A4AGENT_ZCODE_V2_CONFIG_PATH": os.path.join(SANDBOX, "zcode-v2.json"),
        "PYTHONIOENCODING": "utf-8",
    })
    py = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    proc = subprocess.Popen(
        [py, "-m", "uvicorn", "backend.app.main:app", "--port", str(APP_PORT), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    steps = []
    try:
        for _ in range(40):
            try:
                st, _b = api("GET", "/status")
                if st == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit("a4agent 服务未起来")

        st, eng = api("GET", "/tasks/engines")
        pi_info = [e for e in eng["engines"] if e["tool"] == "pi"][0]
        steps.append(("引擎探测", st, pi_info["installed"], pi_info.get("version") or pi_info.get("error")))

        st, prov = api("POST", "/providers", {
            "name": "E2E-mock", "api_base": f"http://127.0.0.1:{MOCK_PORT}/v1",
            "api_type": "openai", "is_custom": True})
        st, cfg = api("POST", "/configs", {
            "name": "E2E 本地模型", "provider_id": prov["id"], "api_key": "sk-mock",
            "model": "mock-model", "targets": "pi"})

        # 切换前先下发：pi 自己的 settings.json 还没有 a4agent 托管条目，
        # 预检的第二跳应当把这次下发拦下来
        st, early = api("POST", "/tasks", {"prompt": "切换前下发应被拦", "tool": "pi", "timeout_seconds": 60})
        steps.append(("未切换时预检拦截", st, (early.get("detail") or "")[:60]))

        st, sw = api("POST", f"/switch/{cfg['id']}", {"restart": False})
        steps.append(("切换方案", st, sw.get("success"), sw.get("message", "")[:60]))

        st, task = api("POST", "/tasks", {
            "prompt": "只回一句话，不要调用任何工具：" + REPLY, "tool": "pi", "timeout_seconds": 180})
        steps.append(("下发", st, task.get("id"), task.get("status")))
        tid = task.get("id")
        if tid is None:
            raise SystemExit("下发失败，看预检结果：" + json.dumps(steps, ensure_ascii=False))

        final = None
        for _ in range(90):
            st, row = api("GET", f"/tasks/{tid}")
            if row["status"] not in ("pending", "running"):
                final = row
                break
            time.sleep(2)
        steps.append(("终态", final and final["status"], final and final["exit_code"],
                      (final and final.get("error_summary") or "")[:80]))
        out = (final or {}).get("output") or ""
        # 只看「文本里出现了模拟回复」会假阳性：提示词本身就带着这句话被 pi 回显在
        # user 事件里。必须确认存在一条 stopReason=stop 的 assistant 产出。
        got = {}
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            m = ev.get("message") or {}
            if ev.get("type") == "message_end" and m.get("role") == "assistant":
                got = m
        got_text = "".join(
            c.get("text", "") for c in (got.get("content") or []) if isinstance(c, dict) and c.get("type") == "text"
        )
        steps.append((
            "最终 assistant 产出",
            got.get("stopReason"),
            REPLY in got_text,
            got_text[:60],
        ))
        steps.append(("产出文件", os.path.exists((final or {}).get("output_path") or ""),
                      (final or {}).get("output_path")))
    finally:
        proc.terminate()
        try:
            log = proc.communicate(timeout=15)[0] or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            log = ""
        print("\n===== 步骤结果 =====")
        for s in steps:
            print(s)
        tail = [l for l in log.splitlines() if "Error" in l or "Traceback" in l][:8]
        if tail:
            print("----- 服务日志关键行 -----")
            print("\n".join(tail))
        srv.shutdown()


if __name__ == "__main__":
    main()
