"""node 设备运行时守护（M4/B23/B33/D29；B40 起支持具名多实例）。

a4agent 以 ``[python, node.py, --config <a4agent 管理的配置目录>]`` 拉起本机
被控端 node 并守护：进程退出自动拉起（连续快速崩溃 5 次后停止守护并置
failed，防死循环）；启动前证书缺失自动调 gen_cert.ps1（PowerShell 一次性
工具，与 node.py 同源可配）。

B40 多实例（契约 §16）：设置清单里的每个具名实例以
``[python, node.py, --instance <名>]`` 独立拉起并各自守护（--instance 与
--config 互斥；LOCALAPPDATA 覆盖到托管 node 目录，使 node-<名>.json、
audit-<名>/、tasks-<名>.db、暂存目录都落 a4agent 托管目录；TLS 经
--cert/--key 显式指向共享的 <node 配置目录>/tls，不写 node 配置文件）。

只拉起守护，不改 node 代码；不读 node 配置文件里的令牌——端口按约定从
node.json 的 port 键读取（node 绑定成功后自写，契约 §9.1），令牌只来自
配对签发（devices.store）。

node.py 路径可配置（解析顺序：运行时设置文件 → 环境变量 → 安装目录内嵌副本
→ 开发态 CrossDevice 仓兄弟目录）；打包形态由 SA-3 的安装器内嵌
``node_runtime/node.py + gen_cert.ps1``，并内嵌 python-embed 解释器到
``node_runtime/python/``（C8 方案 A：打包态解释器最优先候选，全新机零依赖，
SA-3 集成点闭合）。
"""
import json
import logging
import os
import re
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


def _decode_node_line(raw: bytes) -> str:
    """node 子进程输出行解码：优先 UTF-8（新版 node 管道输出），失败回退 GBK
    （Windows 控制台默认编码，兼容旧实例/异常路径的中文输出），
    避免守护日志窗出现替换符乱码。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


RUNTIME_FILE = "device_runtime.json"
NODE_CONFIG_DIR = "node"            # a4agent 数据目录下的 node 配置目录
NODE_CONFIG_FILE = "node.json"      # node 自己的配置文件（由 node 维护，a4agent 不写）
DEFAULT_PORT = 51888
PROBE_TIMEOUT_SECONDS = 90          # 启动探活等待窗
RESTART_DELAY_SECONDS = 2.0
CRASH_UPTIME_SECONDS = 10.0         # 存活短于此视为快速崩溃
MAX_CRASH_STREAK = 5

SETTINGS_KEYS = ("auto_start", "node_path", "python", "cert_tool", "extra_args",
                 "instances")

# B40 多实例：实例名与 node.py --instance 同规（契约 §16.2）；
# INSTANCE_DIR 是 LOCALAPPDATA 覆盖后 node 自己解析出的实例根目录名
INSTANCE_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
INSTANCE_DIR = "CrossDevice"


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


def _embed_python_candidates():
    """安装器内嵌解释器候选（打包态）：build.py 构建时下载 python-embed 并经
    installer.iss 安装到 {app}\\node_runtime\\python\\python.exe，参照
    _default_node_candidates 的 frozen/meipass 双候选写法（meipass 命中 onefile
    附带形态，exe 同目录命中 onedir 安装形态）；dev 态无此目录自然为空。"""
    out = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            out.append(Path(meipass) / "node_runtime" / "python" / "python.exe")
        out.append(Path(sys.executable).resolve().parent
                   / "node_runtime" / "python" / "python.exe")
    return out


def _default_python():
    """拉起 node 用的解释器（按优先级）：

    1. 打包态安装目录内嵌解释器（node_runtime/python/python.exe，由安装器
       内嵌 python-embed 提供，全新机零依赖——C8 方案 A，SA-3 集成点闭合，
       不再依赖目标机恰有匹配 Python）；
    2. 真 python（venv base_prefix / 系统 PATH），跳过 Windows 商店的
       python.exe 存根（它在 PATH 里靠前，实际执行报 9009"未安装"）；
    3. sys.executable 兜底。
    """
    candidates = _embed_python_candidates()
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


def _norm_instances(value, strict: bool = False) -> list:
    """实例名清单规整（B40）：字符串化、去空、去重保序。

    strict=True（save_settings 写盘前把关）时非清单/非法名抛 ValueError；
    否则直接过滤（resolve 读取兜底，脏数据不外泄）。
    """
    if not isinstance(value, list):
        if strict:
            raise ValueError("instances 必须是实例名字符串列表")
        return []
    out = []
    for item in value:
        name = str(item).strip()
        if not name:
            continue
        if not INSTANCE_NAME_RE.fullmatch(name):
            if strict:
                raise ValueError(f"实例名非法：{name}（限 [A-Za-z0-9_-]{{1,32}}）")
            continue
        if name not in out:
            out.append(name)
    return out


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
        "instances": _norm_instances(cfg.get("instances")),
    }


def save_settings(data_dir: Path, patch: dict) -> dict:
    """更新运行时设置（未提供的键保持原值），返回保存后的文件内容。

    instances 校验失败（非法实例名/非清单）抛 ValueError，文件保持原样。
    """
    path = Path(data_dir) / RUNTIME_FILE
    cfg = read_doc(path, "json")
    for key in SETTINGS_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "extra_args":
            cfg[key] = [str(a) for a in value] if isinstance(value, list) else []
        elif key == "instances":
            cfg[key] = _norm_instances(value, strict=True)
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


def _read_port_from(config_file: Path):
    """node 配置文件里的 port 键（node 绑定成功后自写，a4agent 只读不写）"""
    doc = read_doc(Path(config_file), "json")
    port = doc.get("port")
    try:
        return int(port) if port else None
    except (TypeError, ValueError):
        return None


class _InstanceSlot:
    """具名实例的守护状态槽位（B40）：独立进程/守护循环/日志环/退出码，
    口径逐项镜像默认实例。"""

    def __init__(self, name: str, config_dir: Path):
        self.name = name
        self.config_dir = Path(config_dir)     # LOCALAPPDATA 覆盖后的实例根
        self.config_file = self.config_dir / f"node-{name}.json"
        self.state = "stopped"                 # stopped / starting / running / failed
        self.proc = None
        self.stop_requested = threading.Event()
        self.started_at = 0.0
        self.crash_streak = 0
        self.exit_code = None
        self.logs = deque(maxlen=200)


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
        self._instances = {}        # 实例名 -> _InstanceSlot（B40）

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
        """node.json 里的 port 键（node 绑定成功后自写；a4agent 只读不写）。"""
        return _read_port_from(self.config_file)

    def read_device_id(self):
        """node.json 的 device_id 键（node 首启生成并持久化；node 未启动过为 None）。

        供发现池排除本机条目：设备页已有"本机被控端"区块，广播经回环/
        本机网卡回到自己的监听端口时不应重复出现在"发现的设备"列表。
        """
        doc = read_doc(self.config_file, "json")
        did = str(doc.get("device_id") or "").strip()
        return did or None

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
                bufsize=0,
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

    def _pump_output(self, proc, slot=None):
        try:
            for raw in proc.stdout:
                line = _decode_node_line(raw).rstrip("\r\n")
                if line:
                    with self._lock:
                        (slot.logs if slot is not None else self._logs).append(
                            f"{time.strftime('%H:%M:%S')} {line}")
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
        """停止 node 并终止守护（B40 起连同全部具名实例，供退出收尾复用）。"""
        with self._lock:
            for slot in list(self._instances.values()):
                self._stop_slot_locked(slot)
            self._instances.clear()
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

    # ---------------- 具名实例（B40 多实例） ----------------

    def _slot_log(self, slot: _InstanceSlot, line):
        with self._lock:
            slot.logs.append(f"{time.strftime('%H:%M:%S')} {line}")
        logger.info("node 实例 %s：%s", slot.name, line)

    def _slot_status(self, name: str, slot) -> dict:
        """单实例状态摘要；未拉起的实例也按清单给出 stopped 基线。"""
        config_dir = self.config_dir / INSTANCE_DIR
        config_file = config_dir / f"node-{name}.json"
        cert_ready = (self.config_dir / "tls" / "cert.pem").is_file()
        if slot is None:
            return {
                "name": name, "state": "stopped", "pid": None,
                "exit_code": None, "port": _read_port_from(config_file),
                "config_dir": str(config_dir), "config_file": str(config_file),
                "cert_ready": cert_ready, "log": [],
            }
        with self._lock:
            return {
                "name": name, "state": slot.state,
                "pid": slot.proc.pid if slot.proc else None,
                "exit_code": slot.exit_code,
                "port": _read_port_from(slot.config_file),
                "config_dir": str(slot.config_dir),
                "config_file": str(slot.config_file),
                "cert_ready": cert_ready,
                "log": list(slot.logs)[-80:],      # 截断摘要，口径镜像默认实例
            }

    def instances_status(self) -> list:
        """具名实例状态清单（以设置清单为准；默认无实例 = []）。"""
        names = resolve_settings(self.data_dir).get("instances") or []
        with self._lock:
            return [self._slot_status(name, self._instances.get(name))
                    for name in names]

    def start_instance(self, name: str) -> dict:
        """拉起具名实例（幂等）。前置校验口径镜像默认实例 start。

        契约 §16.2：spawn 只带 --instance（与 --config 互斥）、不带 extra_args
        （保持实例确定性）；LOCALAPPDATA 覆盖到托管 node 目录，实例配置/审计/
        任务库/暂存目录都落托管目录；TLS 经 --cert/--key 显式指向共享的
        config_dir/tls（CLI 优先级最高，a4agent 不写 node 配置文件）。
        """
        with self._lock:
            if not INSTANCE_NAME_RE.fullmatch(name or ""):
                raise ValueError(f"实例名非法：{name}（限 [A-Za-z0-9_-]{{1,32}}）")
            settings = resolve_settings(self.data_dir)
            if name not in (settings.get("instances") or []):
                raise KeyError(name)
            slot = self._instances.get(name)
            if slot is None:
                slot = _InstanceSlot(name, self.config_dir / INSTANCE_DIR)
                self._instances[name] = slot
            if slot.state in ("starting", "running"):
                return self._slot_status(name, slot)
            if not settings["node_path"] or not Path(settings["node_path"]).is_file():
                self._slot_log(slot, f"[错误] node.py 不存在："
                               f"{settings['node_path'] or '未找到'}")
                slot.state = "failed"
                return self._slot_status(name, slot)
            if not settings["python"] or not Path(settings["python"]).is_file():
                self._slot_log(slot, f"[错误] 未找到可用的 Python 解释器："
                               f"{settings['python'] or '未配置'}")
                slot.state = "failed"
                return self._slot_status(name, slot)
            if not ensure_certs(settings["cert_tool"], self.config_dir / "tls",
                                lambda line: self._slot_log(slot, line)):
                slot.state = "failed"
                return self._slot_status(name, slot)
            if self._spawn_instance_locked(settings, slot) is None:
                return self._slot_status(name, slot)
            slot.stop_requested.clear()
            threading.Thread(target=self._guard_loop_instance, args=(slot,),
                             daemon=True,
                             name=f"a4agent-node-guard-{name}").start()
            return self._slot_status(name, slot)

    def _spawn_instance_locked(self, settings, slot: _InstanceSlot):
        """创建具名实例子进程（持锁调用）。失败置 failed 并返回 None。"""
        args = [settings["python"], settings["node_path"],
                "--instance", slot.name,
                "--cert", str(self.config_dir / "tls" / "cert.pem"),
                "--key", str(self.config_dir / "tls" / "key.pem")]
        env = dict(os.environ)
        env["LOCALAPPDATA"] = str(self.config_dir)
        slot.config_dir.mkdir(parents=True, exist_ok=True)
        self._slot_log(slot, "[启动] " + " ".join(args))
        try:
            slot.proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(slot.config_dir),
                env=env,
                creationflags=_CREATE_NO_WINDOW,
                bufsize=0,
            )
        except OSError as e:
            self._slot_log(slot, f"[错误] 启动失败：{e}")
            slot.state = "failed"
            return None
        slot.state = "starting"
        slot.started_at = time.time()
        slot.exit_code = None
        threading.Thread(target=self._pump_output, args=(slot.proc, slot),
                         daemon=True,
                         name=f"a4agent-node-log-{slot.name}").start()
        threading.Thread(target=self._probe_loop_instance, args=(slot,),
                         daemon=True,
                         name=f"a4agent-node-probe-{slot.name}").start()
        return slot.proc

    def _probe_loop_instance(self, slot: _InstanceSlot):
        """实例启动探活：端口取 node-<名>.json 的 port 键，就绪置 running。"""
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            with self._lock:
                state = slot.state
                proc = slot.proc
            if state != "starting" or proc is None or proc.poll() is not None:
                return  # 已停止/已失败/进程退出，交给守护线程收尾
            port = _read_port_from(slot.config_file)
            if port and probe_alive(port):
                with self._lock:
                    if slot.state == "starting":
                        slot.state = "running"
                        slot.crash_streak = 0
                self._slot_log(slot, f"[就绪] 实例已在端口 {port} 提供服务（探活通过）")
                return
            time.sleep(1.0)
        with self._lock:
            if slot.state == "starting":
                slot.state = "failed"
                self._slot_log(slot, f"[失败] 等待实例就绪超时（{PROBE_TIMEOUT_SECONDS} 秒），"
                               "请查看日志")

    def _guard_loop_instance(self, slot: _InstanceSlot):
        """实例守护循环：退出自动拉起；快速崩溃连击达上限停止守护。"""
        proc = slot.proc
        while proc is not None:
            rc = proc.wait()
            slot.exit_code = rc
            with self._lock:
                if slot.stop_requested.is_set():
                    slot.state = "stopped"
                    self._slot_log(slot, f"[停止] 实例已退出（exit={rc}）")
                    return
                uptime = time.time() - slot.started_at
                if uptime < CRASH_UPTIME_SECONDS:
                    slot.crash_streak += 1
                else:
                    slot.crash_streak = 0
                if slot.crash_streak >= MAX_CRASH_STREAK:
                    slot.state = "failed"
                    self._slot_log(slot, f"[失败] 实例连续 {slot.crash_streak} 次快速崩溃，"
                                   f"停止自动重启（exit={rc}），请查看日志排查")
                    return
                self._slot_log(slot, f"[守护] 实例退出（exit={rc}），"
                               f"{RESTART_DELAY_SECONDS:.0f} 秒后自动重启")
            slot.stop_requested.wait(RESTART_DELAY_SECONDS)
            if slot.stop_requested.is_set():
                with self._lock:
                    slot.state = "stopped"
                return
            with self._lock:
                if slot.state in ("stopped", "failed"):
                    return
                settings = resolve_settings(self.data_dir)
                if slot.name not in (settings.get("instances") or []):
                    slot.state = "stopped"      # 清单已移除：不再重启
                    return
                proc = self._spawn_instance_locked(settings, slot)

    def stop_instance(self, name: str) -> dict:
        """停止具名实例（幂等）：清单内未拉起的实例按 stopped 返回。"""
        with self._lock:
            if not INSTANCE_NAME_RE.fullmatch(name or ""):
                raise ValueError(f"实例名非法：{name}（限 [A-Za-z0-9_-]{{1,32}}）")
            if name not in (resolve_settings(self.data_dir).get("instances") or []):
                raise KeyError(name)
            slot = self._instances.get(name)
            if slot is not None:
                self._stop_slot_locked(slot)
            return self._slot_status(name, slot)

    def sync_instances(self, names) -> None:
        """设置清单对账（B40）：移除的实例停进程并清理守护状态；新增的不自动拉起。"""
        wanted = _norm_instances(names)
        with self._lock:
            stale = [n for n in list(self._instances) if n not in wanted]
            for name in stale:
                slot = self._instances.pop(name)
                self._stop_slot_locked(slot)
                self._slot_log(slot, "[移除] 实例已移出清单，守护状态已清理")

    def _stop_slot_locked(self, slot: _InstanceSlot):
        """停单个具名实例进程并收尾守护状态（持锁调用）。"""
        slot.stop_requested.set()
        proc = slot.proc
        if proc is not None and proc.poll() is None:
            self._slot_log(slot, "[停止] 正在关闭实例 ...")
            try:
                _kill_tree(proc.pid)
            except Exception as e:  # noqa: BLE001 - 停止失败不阻断状态收尾
                self._slot_log(slot, f"[停止] 关闭异常：{e}")
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                pass
        slot.proc = None
        slot.state = "stopped"


guardian = None   # 惰性单例（绑定数据目录）


def get_guardian() -> NodeGuardian:
    global guardian
    if guardian is None:
        from ..database import get_data_dir
        guardian = NodeGuardian(get_data_dir())
    return guardian
