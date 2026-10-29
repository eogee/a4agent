"""无头任务执行器：线程池 + 状态机 + 增量落盘 + 超时与取消。

下发与执行分离：API 只负责建 pending 行并 submit，本模块在后台线程里跑 CLI，
stdout 直接重定向到产出文件（前端可轮询到已产生的部分），终态落库并广播提醒。
"""
import json
import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import opencode_client, task_engines, task_notify, task_skills
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
# 执行层钳制上限 4 小时：循环任务需求（FR-3.3）允许 5 分钟 ~ 4 小时；
# 手动任务仍由 TaskCreate.timeout_seconds 的 schema（le=3600）压在 1 小时
MAX_TIMEOUT_SECONDS = 14400
# 详情接口回传的产出尾部字节上限（全文始终完整落盘）
OUTPUT_TAIL_BYTES = 200_000

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_procs: dict = {}
_procs_lock = threading.Lock()
# HTTP 引擎（OpenCode）在跑的会话：{task_id: (sessionID, base_url, password)}。
# 取消/退出时据此发 interrupt 而不是去杀进程树——服务端会话不归我们管进程。
_http_sessions: dict = {}
_http_lock = threading.Lock()
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
    """pi 的 `--mode json` 是 JSONL 事件流，按原样存；其余引擎出 markdown。

    OpenCode 的产出取自会话导出（JSON），用 .json 落盘最贴合原貌，也便于排查。
    """
    if tool in task_engines.HTTP_ENGINES:
        suffix = "json"
    elif tool == "pi":
        suffix = "jsonl"
    else:
        suffix = "md"
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

        if task.id in _cancelled:
            # cancel() 在「排队检查」与「RUNNING 落库」之间到达：此时 DB 行已被
            # cancel 落成 cancelled，worker 若继续拉起引擎会把行复活成 running
            # （两个 session 互不知情）。落终态收场，不再执行。
            task.status = CANCELLED
            task.error_summary = "任务已被取消"
            task.finished_at = datetime.now()
            db.commit()
            return

        # 技能注入：插在 _cancelled 早退之后（已取消的任务不该在工作目录里建任何
        # 东西）、output_path_for 之前，且必须早于拉起进程——各端只在进程启动时
        # 扫一次 skill（方案 §7.1 + 附录 C4.1）。mount 内部会独立 commit 一次。
        try:
            mount_info = task_skills.mount(task, db)
        except task_skills.SkillMountError as e:
            task.status = FAILED
            task.error_summary = f"技能挂载失败：{e}"
            task.finished_at = datetime.now()
            db.commit()
            return  # 注入失败不拉起进程

        path = output_path_for(task.tool, task.id)
        timeout = _clamp_timeout(task.timeout_seconds)
        if task.tool in task_engines.HTTP_ENGINES:
            parsed, exit_code, timed_out = _run_http(task, path, timeout, mount_info)
        else:
            exit_code, timed_out = _run_cli(task, path, timeout, mount_info)
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
                # OpenCode 路径在 _run_http_once 单独取收尾段；CLI 引擎的
                # parsed["text"] 本就是最终产出，直接复用
                "last_output": str(parsed.get("last_output") or parsed.get("text") or ""),
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
        try:
            # 回收点（方案 §7.3）：独立 session（不传主 session，避免受主事务回滚
            # 影响），异常自己吞并记日志——**绝不改任务状态**，删不掉留给启动 sweep。
            task_skills.unmount(task_id)
        except Exception:  # noqa: BLE001 - 回收失败不得影响已落库的终态
            logger.exception("回收任务 #%s 的技能链接异常", task_id)
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


def _run_cli(task, path: Path, timeout: int, mount_info=None) -> tuple:
    """跑一次 CLI：stdout/stderr 直接落盘，超时杀整棵进程树。返回 (退出码, 是否超时)。"""
    workdir = (task.working_dir or "").strip()
    # 默认在工作目录=用户主目录跑：留空会继承 a4agent 自己的进程目录
    # （开发时是仓库、打包后是安装目录），引擎会在那里读到不该读的项目配置
    cwd = workdir or str(Path.home())
    # 目录参数与 Popen 的 cwd 必须同值（方案 §6.3 约束 1）：两边都只看 workdir；
    # 未选工作目录时传 None，不把 home 塞进 argv（各端默认行为就是 home，显式
    # 授权主目录只会让命令更长、日志更难读）。build_argv 内部逻辑不动（约束 3）。
    # 前缀只在本次拼接，**绝不写回 task.prompt**：详情与复跑要用原文（方案 §7.2）
    prompt = _prompt_with_skills(task, mount_info)
    argv = task_engines.build_argv(task.tool, prompt, workdir or None)
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
        if task.id in _cancelled:
            # cancel() 在注册前到达（当时 _procs 里还没有句柄，只能标记）：进程
            # 一登记就补杀，否则它会跑满整个 timeout 才被状态机落成 cancelled
            _kill_tree(proc)
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


def _prompt_with_skills(task, mount_info) -> str:
    """按注入结果给 prompt 拼技能前缀（只用于本次调用，不落 task.prompt）。

    - 真的挂进工作目录了（含「同名已有、本次让路」）→ 用 prompt_prefix 的标准措辞
    - 没有工作目录（no_workdir，方案 §5.3 不注入）→ 降级措辞，说明只是建议
    - 其它情况（未选技能 / 全被归一化跳过）→ 不加前缀，保持原文
    """
    if not mount_info:
        return task.prompt or ""
    names = [str(item.get("name") or "").strip() for item in mount_info.get("created") or []]
    # skipped_existing 的目标目录确实存在（用户自己装的那份），对模型是可用技能
    names += [str(name or "").strip() for name in mount_info.get("skipped_existing") or []]
    names = [name for name in names if name]
    if names:
        return task_skills.prompt_prefix([{"name": name} for name in names]) + (task.prompt or "")
    if str(mount_info.get("skipped_reason") or "") == "no_workdir":
        wanted = _skill_names(task)
        if wanted:
            return (
                f"本次建议使用的技能：{'、'.join(wanted)}"
                "（未注入到工作目录，可能不会被自动加载）。\n\n"
            ) + (task.prompt or "")
    return task.prompt or ""


def _skill_names(task) -> list:
    """读 task.skills 快照里的技能名（不依赖注入结果，no_workdir 降级措辞要用）。"""
    raw = getattr(task, "skills", "") or ""
    if isinstance(raw, str):
        if not raw.strip():
            return []
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    names = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("dir_name") or "").strip()
            if name:
                names.append(name)
    return names


def _looks_like_empty_rejection(messages: list) -> bool:
    """空会话失败：只有 user 原文 + idle{failed}，没有任何 assistant 消息。

    这是实测到的「模型名不被接受」的表现形态——服务端在建会话阶段就拒了，
    所以既没有产出也没有 error 字段。
    """
    has_assistant = False
    idle_failed = False
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "assistant":
            has_assistant = True
        elif m.get("type") == "idle" and m.get("outcome") == "failed":
            idle_failed = True
    return idle_failed and not has_assistant


def _model_rejected(parsed: dict, messages: list | None = None) -> bool:
    """失败原因是否指向「模型本身不可用」（值得换一个模型重试）。

    两条依据：错误文案里的关键词，或空会话失败形态（见 _looks_like_empty_rejection）——
    后者是实测到的服务端行为，光看文案会漏判。
    """
    err = str(parsed.get("error") or "").lower()
    if any(m in err for m in opencode_client._REJECTED_MODEL_MARKERS):
        return True
    return messages is not None and _looks_like_empty_rejection(messages)


def _run_http_once(task, path: Path, timeout: int, model: dict | None,
                   base: str, password: str, mount_info=None) -> tuple[dict, str, int, bool, list]:
    """跑一次 HTTP 会话任务。

    返回 (parsed, sessionID, exit_code, timed_out, messages)；messages 一并回传
    是为了让调用方能识别「空会话失败」这一形态（换模型重试需要它）。

    OpenCode 的 prompt 组装点是 create_session（会话建好后 prompt 就固定了，
    与 CLI 一样没有第二次机会），所以技能前缀在这里按与 _run_cli 完全相同的
    口径拼：同样不写回 task.prompt。
    """
    directory = (task.working_dir or "").strip() or None
    timed_out = False
    sid = ""
    try:
        sid = opencode_client.create_session(
            _prompt_with_skills(task, mount_info), directory=directory, model=model,
            base_url=base, password=password,
        )
    except opencode_client.OpenCodeError as e:
        path.write_text(f"# OpenCode 任务下发失败\n\n会话创建失败：{e}\n", encoding="utf-8")
        return ({"ok": False, "text": "", "error": f"OpenCode 会话创建失败：{e}",
                 "usage": {}}, "", 0, False, [])

    with _http_lock:
        _http_sessions[task.id] = (sid, base, password)
    # 登记 a4agent 自建会话：事件订阅对同一条终局不再重复推通知
    opencode_client.mark_dispatched(sid)

    try:
        cancelled = task.id in _cancelled
        # 超时上限给足：wait 是阻塞端点，超时由我们自己兜底而非服务端
        idle = (not cancelled) and opencode_client.wait_idle(
            sid, base, password, timeout=float(timeout))
        if cancelled:
            opencode_client.interrupt(sid, base, password)
            opencode_client.wait_idle(sid, base, password, timeout=15)
        elif not idle and not opencode_client.wait_idle(sid, base, password, timeout=15):
            timed_out = True
            opencode_client.interrupt(sid, base, password)

        try:
            session = opencode_client.get_session(sid, base, password)
        except opencode_client.OpenCodeError:
            session = {}
        try:
            transcript = opencode_client.export_session(sid, base, password)
        except opencode_client.OpenCodeError:
            transcript = json.dumps({"session": session}, ensure_ascii=False, indent=2)
        path.write_text(transcript, encoding="utf-8")

        messages = opencode_client.list_messages(sid, base, password)
        parsed = opencode_client.parse_output(messages, session, exit_code=0,
                                              timed_out=timed_out)
        # 多轮会话的 text 是全部文本块拼接，通知要的「AI 最后输出」单取收尾那段
        parsed["last_output"] = opencode_client.last_assistant_text(messages)
        # 模型名在会话创建阶段就被拒时，服务端可能**只给一个空 failed 会话**：
        # 实测不存在的模型 → 消息流里既无 assistant 消息也无 error 字段，只有
        # idle{failed} + 原始 user 文本。这种情况下错误原因只能从会话日志取，
        # 否则用户只会看到「OpenCode 会话失败（无文本产出）」而不知道是模型名错。
        if not parsed.get("ok") and model and _looks_like_empty_rejection(messages):
            parsed["error"] += f"（请求的模型 {model['providerID']}/{model['id']} 未被接受）"
        return (parsed, sid, 0, timed_out, messages)
    finally:
        with _http_lock:
            _http_sessions.pop(task.id, None)
        # 会话清掉，避免任务历史在 OpenCode 侧堆积；失败只记日志（任务结果优先）
        if not opencode_client.delete_session(sid, base, password):
            logger.info("OpenCode 会话 %s 未删除（可能已不存在）", sid)


def _run_http(task, path: Path, timeout: int, mount_info=None) -> tuple[dict, int, bool]:
    """HTTP 引擎（OpenCode）：建会话 → 等空闲 → 导出产出，必要时换模型重试。

    与 _run_cli 的差异只有一处：没有子进程，所以「取消」不是杀进程树，而是
    对服务端会话发 interrupt（cancel 路径已单独处理）。产出、终态与提醒的
    落库逻辑完全复用 CLI 分支，因此状态机对用户是一致的。

    **换模型重试**：实测服务端默认模型可能已废弃（清单里仍标 enabled=true，
    真跑才报 provider.invalid-request）。这类失败是瞬时的（不烧 token），
    换服务端清单里的下一个候选重试一次，比直接判任务失败更符合用户预期；
    候选封顶 3 个，避免「全部模型都坏」时无谓连跑。

    返回 (parsed, exit_code, timed_out)；exit_code 对 HTTP 引擎没有进程语义，
    固定 0（成败已由 parsed 表达）。
    """
    base, password = opencode_client.resolve_connection()
    parsed, _sid, exit_code, timed_out, messages = _run_http_once(
        task, path, timeout, None, base, password, mount_info)
    if parsed.get("ok") or not _model_rejected(parsed, messages):
        return (parsed, exit_code, timed_out)
    # 用户已取消 / 已超时 → 换模型重跑没有意义
    if task.id in _cancelled or timed_out:
        return (parsed, exit_code, timed_out)

    try:
        candidates = opencode_client.candidate_models(base, password)
    except opencode_client.OpenCodeError:
        candidates = []
    for model in candidates:
        logger.info("OpenCode 任务 #%s：默认模型不可用，换 %s 重试",
                    task.id, f"{model['providerID']}/{model['id']}")
        parsed, _sid, exit_code, timed_out, messages = _run_http_once(
            task, path, timeout, model, base, password, mount_info)
        if parsed.get("ok") or not _model_rejected(parsed, messages):
            return (parsed, exit_code, timed_out)
    return (parsed, exit_code, timed_out)


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
    """取消运行中的任务：杀进程树 + 标记 cancelled（pending 任务直接落终态）。

    HTTP 引擎（OpenCode）没有本地进程可杀，改为给服务端会话发 interrupt；
    worker 检测到 _cancelled 后会走 interrupt → 落 cancelled 的收尾路径。
    """
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
    with _http_lock:
        session = _http_sessions.get(task_id)
    if session is not None:
        sid, base, password = session
        opencode_client.interrupt(sid, base, password)
        return {"cancelled": True, "detail": "已中断 OpenCode 会话，任务标记为取消"}
    return {"cancelled": True, "detail": "已标记为取消"}


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
        "loop_id": task.loop_id,
        "trigger_type": task.trigger_type or "",
        "skills": _skills_of(task),
    }
    if include_output:
        text = _read_tail(Path(task.output_path)) if task.output_path else ""
        data["output"] = text
        data["output_truncated"] = len(text) >= OUTPUT_TAIL_BYTES
        # 详情额外给回收依据摘要，便于排查残留（方案 §8）；列表不带
        data["skill_links"] = _links_of(task)
    return data


def _skills_of(task) -> list:
    """任务行的 skills 快照（下发时写入的 JSON 数组）；坏数据按「没选技能」处理。"""
    raw = task.skills or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _links_of(task) -> dict:
    """任务行的 skill_links（回收依据 JSON）；空串 = 当前没有需要回收的条目。"""
    raw = task.skill_links or ""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


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
