"""node 设备运行时守护（M4/B23/B33/D29）。

a4agent 以 ``[python, node.py, --config <a4agent 管理的配置目录>]`` 拉起本机
被控端 node 并守护：进程退出自动拉起（连续快速崩溃 5 次后停止守护并置
failed，防死循环）；启动前证书缺失自动调 gen_cert.ps1（PowerShell 一次性
工具，与 node.py 同源可配）。

只拉起守护，不改 node 代码；不读 node 配置文件里的令牌——端口按约定从
node.json 的 port 键读取（node 绑定成功后自写，契约 §9.1），令牌只来自
配对签发（devices.store）。

node.py 路径可配置（解析顺序：运行时设置文件 → 环境变量 → 安装目录内嵌副本
→ 开发态 CrossDevice 仓兄弟目录）；打包形态由 SA-3 的安装器内嵌
``node_runtime/node.py + gen_cert.ps1``。
"""
import json
import logging
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..config_io import atomic_write_doc, read_doc

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RUNTIME_FILE = "device_runtime.json"
NODE_CONFIG_DIR = "node"            # a4agent 数据目录下的 node 配置目录
NODE_CONFIG_FILE = "node.json"      # node 自己的配置文件（由 node 维护，a4agent 不写）
DEFAULT_PORT = 51888
PROBE_TIMEOUT_SECONDS = 90          # 启动探活等待窗
RESTART_DELAY_SECONDS = 2.0
CRASH_UPTIME_SECONDS = 10.0         # 存活短于此视为快速崩溃
MAX_CRASH_STREAK = 5

SETTINGS_KEYS = ("auto_start", "node_path", "python", "cert_tool", "extra_args")


def _default_node_candidates():
    """node.py 候选路径（按优先级）。"""
    out = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            out.append(Path(meipass) / "node_runtime" / "node.py")
        out.append(Path(sys.executable).resolve().parent / "node_runtime" / "node.py")
    else:
        repo = Path(__file__).resolve().parent.parent.parent.parent
        out.append(repo.parent / "CrossDevice" / "node" / "node.py")
    return out


def _cert_tool_candidates(node_path: Path):
    """gen_cert.ps1 候选：与 node.py 同源的仓内 tools/ 或同目录扁平布局。"""
    return [node_path.parent.parent / "tools" / "gen_cert.ps1",
            node_path.parent / "gen_cert.ps1"]


def _default_python():
    """拉起 node 用的解释器：优先真 python（venv base_prefix / 系统 PATH），
    跳过 Windows 商店的 python.exe 存根（它在 PATH 里靠前，实际执行报
    9009"未安装"）。打包态不含解释器时由安装器环境提供（SA-3 集成点）。"""
    candidates = []
    base = Path(sys.base_prefix) / "python.exe"
    if base.is_file():
        candidates.append(base)
    for name in ("python", "python3"):
        which = shutil.which(name)
        if which and "windowsapps" not in which.lower():
            candidates.append(Path(which))
    candidates.append(Path(sys.executable))
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return str(candidates[-1])


def resolve_settings(data_dir: Path) -> dict:
    """运行时设置解析（设置文件 → 环境变量 → 默认候选）。返回解析后全量配置。"""
    cfg = read_doc(Path(data_dir) / RUNTIME_FILE, "json")
    node_path = str(cfg.get("node_path") or "").strip() \
        or os.environ.get("A4AGENT_NODE_PATH", "").strip()
    if not node_path:
        for cand in _default_node_candidates():
            if cand.is_file():
                node_path = str(cand)
                break
    python = str(cfg.get("python") or "").strip() \
        or os.environ.get("A4AGENT_NODE_PYTHON", "").strip() or _default_python()
    cert_tool = str(cfg.get("cert_tool") or "").strip() \
        or os.environ.get("A4AGENT_CERT_TOOL", "").strip()
    if node_path:
        node_path_p = Path(node_path)
        if not cert_tool:
            for cand in _cert_tool_candidates(node_path_p):
                if cand.is_file():
                    cert_tool = str(cand)
                    break
    extra = cfg.get("extra_args")
    extra = [str(a) for a in extra] if isinstance(extra, list) else []
    return {
        "auto_start": bool(cfg.get("auto_start", False)),
        "node_path": node_path,
        "python": python,
        "cert_tool": cert_tool,
        "extra_args": extra,
    }


def save_settings(data_dir: Path, patch: dict) -> dict:
    """更新运行时设置（未提供的键保持原值），返回保存后的文件内容。"""
    path = Path(data_dir) / RUNTIME_FILE
    cfg = read_doc(path, "json")
    for key in SETTINGS_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "extra_args":
            cfg[key] = [str(a) for a in value] if isinstance(value, list) else []
        elif key == "auto_start":
            cfg[key] = bool(value)
        else:
            cfg[key] = str(value or "").strip()
    atomic_write_doc(path, cfg, "json")
    return cfg


def ensure_certs(cert_tool: str, tls_dir: Path, log) -> bool:
    """证书缺失时调 gen_cert.ps1 生成（等价 xd cert init 的底层命令）。

    已有 cert.pem/key.pem 时直接返回 True；工具缺失或 PowerShell 失败
    返回 False（由调用方置 failed 并提示）。
    """
    if (tls_dir / "cert.pem").is_file() and (tls_dir / "key.pem").is_file():
        return True
    if not cert_tool or not Path(cert_tool).is_file():
        log(f"[错误] 未找到证书生成工具 gen_cert.ps1（期望 {cert_tool or '未配置'}），"
            f"无法生成 TLS 证书")
        return False
    ps = shutil.which("powershell")
    if not ps:
        log("[错误] 未找到 PowerShell，无法生成证书")
        return False
    tls_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(cert_tool),
           "-OutDir", str(tls_dir)]
    log("[证书] 生成自签证书：" + " ".join(cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180,
                              creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"[错误] 证书生成失败：{e}")
        return False
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if output:
        for line in output.splitlines()[-6:]:
            log("[证书] " + line)
    if proc.returncode != 0:
        log(f"[错误] 证书生成失败（exit={proc.returncode}）")
        return False
    ok = (tls_dir / "cert.pem").is_file() and (tls_dir / "key.pem").is_file()
    if not ok:
        log("[错误] 证书生成后仍未找到 cert.pem/key.pem")
    return ok


def probe_alive(port, timeout=2.0) -> bool:
    """探活（watchdog 同款口径）：TLS 握手 + 任意 HTTP 状态行（401 即存活）；
    TLS 失败再试明文（兼容 --debug-plaintext）。"""
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            tls = ctx.wrap_socket(raw, server_hostname="127.0.0.1")
            tls.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = tls.recv(64)
            tls.close()
            return data.startswith(b"HTTP/")
        except (OSError, ssl.SSLError):
            pass  # 明文调试模式兜底
        raw2 = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            raw2.sendall(b"GET / HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            return raw2.recv(64).startswith(b"HTTP/")
        finally:
            raw2.close()
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=_CREATE_NO_WINDOW)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


class NodeGuardian:
    """本机 node 子进程守护（线程安全；单实例全局单例）。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.config_dir = self.data_dir / NODE_CONFIG_DIR
        self.config_file = self.config_dir / NODE_CONFIG_FILE
        self._lock = threading.RLock()
        self._state = "stopped"     # stopped / starting / running / failed
        self._proc = None
        self._stop_requested = threading.Event()
        self._started_at = 0.0
        self._crash_streak = 0
        self._exit_code = None
        self._logs = deque(maxlen=200)
        self._guard_thread = None

    # ---------- 日志 ----------

    def _log(self, line):
        with self._lock:
            self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        logger.info("node 运行时：%s", line)

    # ---------- 状态 ----------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def read_port(self):
        """node.json 的 port 键（node 绑定成功后自写；a4agent 只读不写）。"""
        doc = read_doc(self.config_file, "json")
        port = doc.get("port")
        try:
            return int(port) if port else None
        except (TypeError, ValueError):
            return None

    def status(self) -> dict:
        with self._lock:
            settings = resolve_settings(self.data_dir)
            return {
                "state": self._state,
                "pid": self._proc.pid if self._proc else None,
                "exit_code": self._exit_code,
                "port": self.read_port(),
                "config_dir": str(self.config_dir),
                "config_file": str(self.config_file),
                "node_path": settings["node_path"],
                "python": settings["python"],
                "cert_tool": settings["cert_tool"],
                "auto_start": settings["auto_start"],
                "cert_ready": (self.config_dir / "tls" / "cert.pem").is_file(),
                "log": list(self._logs)[-80:],
            }

    # ---------- 启动 / 停止 ----------

    def start(self) -> dict:
        """拉起 node（幂等）。前置校验失败直接置 failed（未真正拉起）。"""
        with self._lock:
            if self._state in ("starting", "running"):
                return self.status()
            settings = resolve_settings(self.data_dir)
            if not settings["node_path"]:
                self._log("[错误] 未找到 node.py：安装形态应在安装目录 node_runtime/ 下，"
                          "或在运行时设置中指定路径")
                self._state = "failed"
                return self.status()
            if not Path(settings["node_path"]).is_file():
                self._log(f"[错误] node.py 不存在：{settings['node_path']}")
                self._state = "failed"
                return self.status()
            if not settings["python"] or not Path(settings["python"]).is_file():
                self._log(f"[错误] 未找到可用的 Python 解释器：{settings['python'] or '未配置'}")
                self._state = "failed"
                return self.status()
            self.config_dir.mkdir(parents=True, exist_ok=True)
            if not ensure_certs(settings["cert_tool"],
                                self.config_dir / "tls", self._log):
                self._state = "failed"
                return self.status()
            proc = self._spawn_locked(settings)
            if proc is None:
                return self.status()
            self._stop_requested.clear()
            self._guard_thread = threading.Thread(
                target=self._guard_loop, args=(proc,), daemon=True,
                name="a4agent-node-guard")
            self._guard_thread.start()
            return self.status()

    def _spawn_locked(self, settings):
        """创建 node 子进程（持锁调用）。失败置 failed 并返回 None。"""
        args = [settings["python"], settings["node_path"],
                "--config", str(self.config_file)] + settings["extra_args"]
        self._log("[启动] " + " ".join(args))
        try:
            self._proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(self.config_dir),
                creationflags=_CREATE_NO_WINDOW,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as e:
            self._log(f"[错误] 启动失败：{e}")
            self._state = "failed"
            return None
        self._state = "starting"
        self._started_at = time.time()
        self._exit_code = None
        threading.Thread(target=self._pump_output, args=(self._proc,),
                         daemon=True, name="a4agent-node-log").start()
        threading.Thread(target=self._probe_loop, daemon=True,
                         name="a4agent-node-probe").start()
        return self._proc

    def _pump_output(self, proc):
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line:
                    with self._lock:
                        self._logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        except (ValueError, OSError):
            pass  # 进程退出时管道断裂属正常

    def _probe_loop(self):
        """启动探活：端口取 node.json 的 port 键（node 绑定后写入），就绪置 running。"""
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                state = self._state
                proc = self._proc
            if state != "starting" or proc is None or proc.poll() is not None:
                return  # 已停止/已失败/进程退出，交给守护线程收尾
            port = self.read_port()
            if port and probe_alive(port):
                with self._lock:
                    if self._state == "starting":
                        self._state = "running"
                        self._crash_streak = 0
                self._log(f"[就绪] node 已在端口 {port} 提供服务（探活通过）")
                return
            time.sleep(1.0)
        with self._lock:
            if self._state == "starting":
                self._state = "failed"
                self._log(f"[失败] 等待 node 就绪超时（{PROBE_TIMEOUT_SECONDS} 秒），请查看日志")

    def _guard_loop(self, proc):
        """守护循环：进程退出自动拉起（D29）；快速崩溃连击达上限停止守护。"""
        while proc is not None:
            rc = proc.wait()
            self._exit_code = rc
            with self._lock:
                if self._stop_requested.is_set():
                    self._state = "stopped"
                    self._log(f"[停止] node 已退出（exit={rc}）")
                    return
                uptime = time.time() - self._started_at
                if uptime < CRASH_UPTIME_SECONDS:
                    self._crash_streak += 1
                else:
                    self._crash_streak = 0
                if self._crash_streak >= MAX_CRASH_STREAK:
                    self._state = "failed"
                    self._log(f"[失败] node 连续 {self._crash_streak} 次快速崩溃，"
                              f"停止自动重启（exit={rc}），请查看日志排查")
                    return
                self._log(f"[守护] node 退出（exit={rc}），"
                          f"{RESTART_DELAY_SECONDS:.0f} 秒后自动重启")
            self._stop_requested.wait(RESTART_DELAY_SECONDS)
            if self._stop_requested.is_set():
                with self._lock:
                    self._state = "stopped"
                return
            with self._lock:
                if self._state in ("stopped", "failed"):
                    return
                settings = resolve_settings(self.data_dir)
                proc = self._spawn_locked(settings)

    def stop(self) -> dict:
        """停止 node 并终止守护。"""
        with self._lock:
            self._stop_requested.set()
            proc = self._proc
            if proc is not None and proc.poll() is None:
                self._log("[停止] 正在关闭 node ...")
                try:
                    _kill_tree(proc.pid)
                except Exception as e:  # noqa: BLE001 - 停止失败不阻断状态收尾
                    self._log(f"[停止] 关闭异常：{e}")
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    pass
            self._proc = None
            self._state = "stopped"
            return self.status()


guardian = None   # 惰性单例（绑定数据目录）


def get_guardian() -> NodeGuardian:
    global guardian
    if guardian is None:
        from ..database import get_data_dir
        guardian = NodeGuardian(get_data_dir())
    return guardian
