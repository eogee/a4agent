"""llama-server 进程管理（移植自 a4agent ServerEngine.cs / Engine.cs）。

启动/停止/健康轮询/定时内存裁剪 + 后端能力探测（MTP 写法）
+ 命令行参数拼装。状态机：stopped → starting → running / failed。
"""
import logging
import os
import platform
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _redact_args(args: list) -> str:
    """拼装命令行用于展示；--api-key 的值脱敏——这行会进日志面板，截图/共享
    屏幕时明文密钥跟着走。"""
    out = []
    redact_next = False
    for a in args:
        if redact_next:
            out.append("***")
            redact_next = False
        elif a == "--api-key":
            out.append(a)
            redact_next = True
        else:
            out.append(a)
    return " ".join(out)


class ServerEngine:
    """单个 llama-server 实例的生命周期管理，线程安全。"""

    def __init__(self, on_log=None, on_state=None):
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._poll_stop = threading.Event()
        self._trim_timer: threading.Timer | None = None
        self._on_log = on_log or (lambda line: None)
        self._on_state = on_state or (lambda s: None)
        self._state = "stopped"  # stopped / starting / running / failed
        self.last_exit_code: int | None = None
        # 启动参数回存：内存裁剪与崩溃自动重启消费配置里的运行时设置
        # （此前这三个设置只有设置页的存取往返，行为侧从未读取）
        self._trim_enabled = True
        self._trim_delay_seconds = 90
        self._restart_on_crash = False
        self._crash_restarts = 0  # 健康就绪即清零；连崩 3 次后放弃，防崩溃死循环
        self._start_params: tuple | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _set_state(self, s: str) -> None:
        with self._lock:
            self._state = s
        self._on_state(s)

    @property
    def is_busy(self) -> bool:
        return self.state in ("starting", "running")

    def mark_failed(self, reason: str = "") -> None:
        """前置校验失败时直接置为 failed（未真正拉起进程）。"""
        if reason:
            self._on_log(f"[错误] {reason}")
        self._set_state("failed")

    def start(self, engine_dir: Path, args: list, host: str, port: int,
              trim_enabled: bool = True, trim_delay_seconds: int = 90,
              restart_on_crash: bool = False) -> None:
        with self._lock:
            if self.is_busy:
                return
            exe = Path(engine_dir) / "llama-server.exe"
            if not exe.is_file():
                self._on_log(f"[错误] 未找到引擎: {exe}")
                self._on_log("[提示] 请在 设置 页指定引擎目录（含 llama-server.exe），"
                             "或在 配置向导 重新运行向导自动下载引擎")
                self._set_state("failed")
                return
            if not is_port_free(port):
                self._on_log(f"[错误] 端口 {port} 已被占用（可能已有实例在运行）")
                self._set_state("failed")
                return

            self._on_log(f"[启动] {exe}")
            self._on_log("[参数] " + _redact_args(args))
            try:
                self._proc = subprocess.Popen(
                    [str(exe)] + args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=str(engine_dir),
                    creationflags=_CREATE_NO_WINDOW,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as e:
                self._on_log(f"[错误] 启动失败: {e}")
                self._set_state("failed")
                return

            self._set_state("starting")
            self._trim_enabled = trim_enabled
            self._trim_delay_seconds = trim_delay_seconds
            self._restart_on_crash = restart_on_crash
            self._start_params = (engine_dir, args, host, port,
                                  trim_enabled, trim_delay_seconds, restart_on_crash)
            self._poll_stop.clear()
            threading.Thread(target=self._pump_output, daemon=True,
                             name="llama-log-pump").start()
            threading.Thread(
                target=self._poll_health, args=(host, port, self._proc),
                daemon=True, name="llama-health-poll").start()

    def _pump_output(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if line:
                    self._on_log(line)
        except (ValueError, OSError):
            pass  # 进程关闭时管道断裂属正常
        # stdout EOF = 进程已退出。健康检查线程就绪后即收工，这里是运行期
        # 崩溃的唯一观测点；收尾与（按设置触发的）自动重启统一在本路径
        with self._lock:
            crashed = self._state in ("starting", "running") and not self._poll_stop.is_set()
            if crashed:
                allow_restart = self._restart_on_crash and self._crash_restarts < 3
                if allow_restart:
                    self._crash_restarts += 1
        if not crashed:
            return  # 主动停止或健康检查失败路径负责收尾，这里不抢
        self.last_exit_code = proc.returncode
        self._on_log(f"[失败] 服务进程退出，exit code {proc.returncode}")
        if allow_restart:
            self._on_log(f"[自动重启] auto_restart_on_crash 已启用，2 秒后重启"
                         f"（第 {self._crash_restarts}/3 次）")
            time.sleep(2)
            with self._lock:
                if self._poll_stop.is_set():
                    self._set_state("stopped")
                    return
                self._set_state("failed")
            params = self._start_params
            if params is not None:
                self._on_log("[自动重启] 正在重启 llama-server")
                self.start(*params)
            return
        self._set_state("failed")

    def _poll_health(self, host: str, port: int, proc: subprocess.Popen) -> None:
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        url = f"http://{probe_host}:{port}/health"
        deadline = time.monotonic() + 240  # 4 分钟

        while time.monotonic() < deadline and not self._poll_stop.is_set():
            if proc.poll() is not None:
                self.last_exit_code = proc.returncode
                self._on_log(f"[失败] 服务进程退出，exit code {proc.returncode}")
                # 状态终态与自动重启由日志泵的 EOF 路径统一处理，这里不抢
                return
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    if resp.status == 200:
                        self._on_log(f"[就绪] health ok —— http://{probe_host}:{port}/")
                        self._set_state("running")
                        with self._lock:
                            self._crash_restarts = 0  # 活过一次健康检查，连崩计数归零
                        self._schedule_ram_trim(proc, self._trim_enabled,
                                                self._trim_delay_seconds)
                        return
            except Exception:
                pass  # 还没起来，继续等
            if self._poll_stop.wait(1.0):
                return

        if not self._poll_stop.is_set():
            self._on_log("[失败] 等待健康检查超时（4 分钟），请查看日志排查")
            self._set_state("failed")

    def _schedule_ram_trim(self, proc: subprocess.Popen,
                           enabled: bool = True, delay_seconds: int = 90) -> None:
        """健康就绪后定时裁剪工作集（释放文件缓存）。仅 Windows 有效。"""
        if not enabled or platform.system() != "Windows":
            return
        with self._lock:
            if self._trim_timer:
                self._trim_timer.cancel()
            delay = max(5, delay_seconds)

            def trim():
                try:
                    if proc.poll() is not None:
                        return
                    before = _working_set_gb(proc.pid)
                    if before is None:
                        return
                    if _empty_working_set(proc.pid):
                        after = _working_set_gb(proc.pid)
                        self._on_log(f"内存裁剪完成: {before:.2f}GB -> {after:.2f}GB")
                except Exception as e:
                    self._on_log("[裁剪] 失败: " + str(e))

            self._trim_timer = threading.Timer(delay, trim)
            self._trim_timer.daemon = True
            self._trim_timer.start()

    def stop(self) -> None:
        with self._lock:
            self._poll_stop.set()
            if self._trim_timer:
                self._trim_timer.cancel()
                self._trim_timer = None
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    self._on_log("[停止] 正在关闭 llama-server ...")
                    _kill_tree(proc.pid)
                    try:
                        proc.wait(8)
                    except subprocess.TimeoutExpired:
                        pass
                    self._on_log("[停止] 已关闭")
                except Exception as e:
                    self._on_log("[停止] 异常: " + str(e))
            self._proc = None
            self._set_state("stopped")


# ───────────────────────── 命令行拼装 ─────────────────────────

# 启发式 MTP（投机解码）应使用的命令行写法
MTP_NONE = "none"        # 引擎不支持投机解码
MTP_LEGACY = "legacy"    # 旧版写法：--mtp N（早期 llama.cpp）
MTP_SPEC_TYPE = "spec"   # 新版 spec 体系：--spec-type draft-mtp --spec-draft-n-max N


class BackendCapabilities:
    def __init__(self):
        self.mtp = MTP_NONE
        self.help_probe_done = False

    @property
    def supports_mtp(self) -> bool:
        return self.mtp != MTP_NONE

    def to_dict(self) -> dict:
        return {"mtp": self.mtp, "supports_mtp": self.supports_mtp,
                "help_probe_done": self.help_probe_done}


_caps_cache: "dict[str, BackendCapabilities]" = {}
_caps_lock = threading.Lock()


def detect_capabilities(engine_dir) -> BackendCapabilities:
    """跑一次 llama-server --help，探测当前后端支持哪些特性（结果按目录缓存）。

    旧版 llama.cpp 用 --mtp N；新版（b105xx 起）移除了 --mtp，
    投机解码改为 --spec-type 体系，MTP 是其中的 draft-mtp 类型。
    """
    engine_dir = str(engine_dir)
    with _caps_lock:
        cached = _caps_cache.get(engine_dir)
        if cached is not None:
            return cached
    caps = BackendCapabilities()
    exe = Path(engine_dir) / "llama-server.exe"
    try:
        if exe.is_file():
            proc = subprocess.run(
                [str(exe), "--help"],
                capture_output=True, text=True, timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
            # --help 会以非零码退出，但输出内容有效
            text = (proc.stdout or "") + (proc.stderr or "")
            if "--mtp" in text:
                caps.mtp = MTP_LEGACY
            elif "draft-mtp" in text:
                caps.mtp = MTP_SPEC_TYPE
            caps.help_probe_done = True
    except Exception as e:  # 探测失败按最保守处理
        logger.debug("llama-server --help 探测失败：%s", e)
    with _caps_lock:
        _caps_cache[engine_dir] = caps
    return caps


def build_args(infer, model_path: str, host: str, port: int,
               include_mtp: bool, mtp_style: str, mmproj: str = "") -> list:
    """把配置拼装成 llama-server 命令行参数（与 a4agent CommandLineBuilder 对齐）。

    mmproj 为经 runtime 校验过的投影模型路径，空 = 纯文本不加载。
    """
    args = [
        "-m", str(model_path),
        "--host", host,
        "--port", str(port),
        "-ngl", str(infer.ngl),
        "-c", str(infer.context_tokens),
        "--alias", Path(model_path).stem,
        "--parallel", str(max(1, infer.parallel)),
        "-fa", "on" if infer.flash_attention else "off",
        "--reasoning", "off",
        "--reasoning-budget", "0",
        "-ctk", infer.kv_level,
        "-ctv", infer.kv_level,
    ]
    if infer.ubatch > 0 and infer.ubatch != 512:
        args += ["-ub", str(infer.ubatch)]
    if infer.threads > 0:
        args += ["-t", str(infer.threads)]
    if infer.api_key.strip():
        args += ["--api-key", infer.api_key.strip()]
    if include_mtp and infer.mtp_steps > 0:
        if mtp_style == MTP_LEGACY:
            args += ["--mtp", str(infer.mtp_steps)]
        elif mtp_style == MTP_SPEC_TYPE:
            # 新版 llama.cpp：--mtp 已移除，MTP 是 --spec-type 的 draft-mtp 类型；
            # --spec-draft-n-max 控制每步草稿 token 数（即 MTP 步数）
            args += ["--spec-type", "draft-mtp"]
            args += ["--spec-draft-n-max", str(infer.mtp_steps)]
    if mmproj:
        args += ["--mmproj", mmproj]
    args += [a for a in infer.extra_args.split(" ") if a]
    return args


# ───────────────────────── 平台辅助 ─────────────────────────

def is_port_free(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            return True
        finally:
            s.close()
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    """结束整棵进程树（对应 C# Kill(entireProcessTree: true)）。"""
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=10,
                       creationflags=_CREATE_NO_WINDOW)
    else:
        os.kill(pid, 9)


def _working_set_gb(pid: int) -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return None
            return float(pmc.WorkingSetSize) / (1024 ** 3)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def _empty_working_set(pid: int) -> bool:
    try:
        import ctypes
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        PROCESS_SET_QUOTA = 0x0100
        h = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA, False, pid)
        if not h:
            return False
        try:
            return bool(psapi.EmptyWorkingSet(h))
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return False
