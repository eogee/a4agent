"""无头任务执行器：线程池 + 状态机 + 增量落盘 + 超时与取消。

下发与执行分离：API 只负责建 pending 行并 submit，本模块在后台线程里跑 CLI，
stdout 直接重定向到产出文件（前端可轮询到已产生的部分），终态落库并广播提醒。
"""
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import task_engines, task_notify
from .database import SessionLocal, get_data_dir
from .models import AgentTask

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
PRECHECK_FAILED = "precheck_failed"
TERMINAL_STATUSES = {SUCCESS, FAILED, TIMEOUT, CANCELLED, PRECHECK_FAILED}

DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 3600
# 详情接口回传的产出尾部字节上限（全文始终完整落盘）
OUTPUT_TAIL_BYTES = 200_000

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_procs: dict = {}
_procs_lock = threading.Lock()
_cancelled: set = set()
# 应用退出抢先收走进程时，把原因交给 worker 落库，避免两边抢写同一行
_shutdown_reason: dict = {}
# 一旦置位，队列里还没开跑的任务不再拉起（回收与新下发都按「未执行」落库）；
# 调用方在 os._exit 前调用，置位后不再复位
_shutting_down = threading.Event()


def max_workers() -> int:
    """并发上限：默认 5（决议），A4AGENT_TASK_CONCURRENCY 可调，钳在 1–8。"""
    raw = os.environ.get("A4AGENT_TASK_CONCURRENCY", "").strip()
    try:
        value = int(raw) if raw else 5
    except ValueError:
        value = 5
    return max(1, min(value, 8))


def _ensure_pool() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=max_workers(), thread_name_prefix="a4agent-task")
        return _pool


def output_dir() -> Path:
    d = get_data_dir() / "task_outputs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def output_path_for(tool: str, task_id: int) -> Path:
    """pi 的 `--mode json` 是 JSONL 事件流，按原样存；其余引擎出 markdown。"""
    suffix = "jsonl" if tool == "pi" else "md"
    return output_dir() / f"{task_id}.{suffix}"


def submit(task_id: int) -> None:
    _ensure_pool().submit(_execute, task_id)


def running_count() -> int:
    with _procs_lock:
        return len(_procs)


def _execute(task_id: int) -> None:
    db = SessionLocal()
    try:
        task = db.get(AgentTask, task_id)
        if task is None or task.status != PENDING:
            return
        if _shutting_down.is_set():
            # 应用正在退出：不再拉起新的引擎进程，如实落库（应用退出后 os._exit
            # 会带走线程，这里不写就是一条永远停在排队中的僵尸行）
            task.status = FAILED
            task.error_summary = "应用退出，任务未执行"
            task.finished_at = datetime.now()
            db.commit()
            return
        task.status = RUNNING
        task.started_at = datetime.now()
        db.commit()

        path = output_path_for(task.tool, task.id)
        timeout = _clamp_timeout(task.timeout_seconds)
        exit_code, timed_out = _run_cli(task, path, timeout)
        content = _read_tail(path)
        parsed = task_engines.parse_output(task.tool, content, exit_code)

        forced = _shutdown_reason.pop(task.id, None)
        if forced:
            # 应用退出抢在 worker 之前收走进程：按退出原因落库，别报成「引擎无产出」
            status, error = FAILED, f"{forced}，任务已终止"
        elif task.id in _cancelled:
            status, error = CANCELLED, "任务已被取消"
        elif timed_out:
            status, error = TIMEOUT, f"超过 {timeout} 秒未完成，已终止引擎进程"
        elif parsed["ok"]:
            status, error = SUCCESS, ""
        else:
            status, error = FAILED, parsed["error"]

        task.status = status
        task.exit_code = exit_code
        task.error_summary = error
        task.output_path = str(path)
        task.finished_at = datetime.now()
        db.commit()
        task_notify.notify(
            {
                "id": task.id,
                "tool": task.tool,
                "status": status,
                "error": error,
                "prompt": (task.prompt or "")[:80],
            }
        )
        logger.info("无头任务 #%s（%s）结束：%s", task.id, task.tool, status)
    except Exception as e:  # noqa: BLE001 - 后台线程兜底，异常落库而不是静默丢线程
        logger.exception("无头任务 #%s 执行异常", task_id)
        db.rollback()
        task = db.get(AgentTask, task_id)
        if task is not None:
            task.status = FAILED
            task.error_summary = f"执行异常：{e}"
            task.finished_at = datetime.now()
            db.commit()
    finally:
        _cancelled.discard(task_id)
        db.close()


def _child_env(tool: str):
    """引擎子进程的环境变量；无特殊需求返回 None（沿用继承的环境）。

    pi 读自己的 `PI_CODING_AGENT_DIR`：显式指向 a4agent 刚写过配置的目录，
    否则两边可能落在不同位置（`A4AGENT_PI_AGENT_DIR` 只影响 a4agent 这一侧），
    任务就会用旧配置跑——端到端实测正是这样先失败的。
    qoder 复用桌面端内置内核时必须以 ELECTRON_RUN_AS_NODE=1 跑 Qoder.exe，
    否则拉起的是整个 IDE 而不是无头引擎。
    """
    if tool == "pi":
        from . import config_manager

        env = dict(os.environ)
        env["PI_CODING_AGENT_DIR"] = str(config_manager.pi_agent_dir())
        return env
    extra = task_engines.runtime_extra_env(tool)
    if not extra:
        return None
    env = dict(os.environ)
    env.update(extra)
    return env


def _run_cli(task, path: Path, timeout: int) -> tuple:
    """跑一次 CLI：stdout/stderr 直接落盘，超时杀整棵进程树。返回 (退出码, 是否超时)。"""
    argv = task_engines.build_argv(task.tool, task.prompt)
    # 默认在工作目录=用户主目录跑：留空会继承 a4agent 自己的进程目录
    # （开发时是仓库、打包后是安装目录），引擎会在那里读到不该读的项目配置
    cwd = (task.working_dir or "").strip() or str(Path.home())
    timed_out = False
    env = _child_env(task.tool)
    with path.open("wb") as sink:
        proc = subprocess.Popen(
            argv,
            stdout=sink,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            creationflags=_no_window_flags(),
        )
        with _procs_lock:
            _procs[task.id] = proc
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # 极端情况：强杀兜底
                proc.kill()
        finally:
            with _procs_lock:
                _procs.pop(task.id, None)
    return (int(proc.returncode or 0) if proc.returncode is not None else -1), timed_out


def _clamp_timeout(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_TIMEOUT_SECONDS
    return max(30, min(seconds, MAX_TIMEOUT_SECONDS))


def _read_tail(path: Path) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > OUTPUT_TAIL_BYTES:
            fh.seek(-OUTPUT_TAIL_BYTES, os.SEEK_END)
        return fh.read().decode("utf-8", "replace")


def _no_window_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _kill_tree(proc) -> None:
    """node / npm 壳会派生子进程，只杀外壳会留下干活的那一个。"""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=_no_window_flags(),
                timeout=20,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    proc.kill()


def cancel(task_id: int, db) -> dict:
    """取消运行中的任务：杀进程树 + 标记 cancelled（pending 任务直接落终态）。"""
    task = db.get(AgentTask, task_id)
    if task is None:
        raise ValueError("任务不存在")
    if task.status not in (RUNNING, PENDING):
        return {"cancelled": False, "detail": f"任务当前为「{task.status}」，无法取消"}
    if task.status == PENDING:
        _cancelled.add(task_id)
        task.status = CANCELLED
        task.error_summary = "任务在下发前被取消"
        task.finished_at = datetime.now()
        db.commit()
        return {"cancelled": True, "detail": "已从队列取消"}
    with _procs_lock:
        proc = _procs.get(task_id)
    _cancelled.add(task_id)
    if proc is not None:
        _kill_tree(proc)
    return {"cancelled": True, "detail": "已终止引擎进程，任务标记为取消"}


def shutdown_all(reason: str = "应用退出") -> int:
    """退出前回收：杀掉仍在跑的子进程、把没开跑的排队任务如实落库，不留僵尸。

    先登记原因让 worker 自己落库（它会 pop），杀完等进程表收敛；杀的期间
    worker 还可能从队列捞起任务 spawn 新进程，因此收到进程表为空为止
    （至多三轮，防杀不掉的死循环）。最后对库再做一次兜底：非终态行
    （running / pending）统一标记，pending 行是「未执行」而非「已终止」。
    置位的 _shutting_down 不复位：之后线程池里残余的 worker 即使被调度到，
    也会把任务按「未执行」落库而不是真的拉起引擎。
    """
    _shutting_down.set()
    killed = 0
    for _ in range(3):
        with _procs_lock:
            items = list(_procs.items())
        if not items:
            break
        for task_id, proc in items:
            _cancelled.discard(task_id)
            _shutdown_reason[task_id] = reason
            _kill_tree(proc)
        killed += len(items)
        deadline = time.time() + 3.0
        while time.time() < deadline and running_count():
            time.sleep(0.05)

    db = SessionLocal()
    try:
        rows = (
            db.query(AgentTask)
            .filter(AgentTask.status.in_((PENDING, RUNNING)))
            .all()
        )
        swept_pending = 0
        for task in rows:
            was_running = task.status == RUNNING
            task.status = FAILED
            task.error_summary = f"{reason}，任务已终止" if was_running else f"{reason}，任务未执行"
            task.finished_at = datetime.now()
            swept_pending += 0 if was_running else 1
        db.commit()
        return killed + swept_pending
    finally:
        db.close()


def outstanding_count() -> int:
    """在跑的引擎进程数 + 仍在排队没开跑的任务行数（退出确认框用）。"""
    n = running_count()
    db = SessionLocal()
    try:
        n += db.query(AgentTask).filter(AgentTask.status == PENDING).count()
    finally:
        db.close()
    return n


def task_payload(task, include_output: bool = False) -> dict:
    """统一的任务序列化：列表不带产出，详情按需附带尾部文本。"""
    data = {
        "id": task.id,
        "prompt": task.prompt,
        "tool": task.tool,
        "tool_label": task_engines.ENGINE_LABELS.get(task.tool, task.tool),
        "working_dir": task.working_dir or "",
        "timeout_seconds": _clamp_timeout(task.timeout_seconds),
        "status": task.status,
        "model": task.model or "",
        "config_id": task.config_id,
        "exit_code": task.exit_code,
        "error_summary": task.error_summary or "",
        "output_path": task.output_path or "",
        "precheck": _precheck_of(task),
        "created_at": _fmt(task.created_at),
        "started_at": _fmt(task.started_at),
        "finished_at": _fmt(task.finished_at),
        "duration_seconds": _duration(task),
    }
    if include_output:
        text = _read_tail(Path(task.output_path)) if task.output_path else ""
        data["output"] = text
        data["output_truncated"] = len(text) >= OUTPUT_TAIL_BYTES
    return data


def _precheck_of(task) -> dict:
    import json

    raw = task.precheck_result or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {"ok": True, "steps": [], "reason": ""}


def _fmt(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def _duration(task) -> int | None:
    if not task.started_at:
        return None
    end = task.finished_at or datetime.now()
    return max(0, int((end - task.started_at).total_seconds()))


def wait_for_idle(timeout: float = 5.0) -> bool:
    """等后台线程收敛（测试与退出前用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if running_count() == 0:
            return True
        time.sleep(0.05)
    return running_count() == 0
