"""循环任务调度器：进程内守护线程，每 20 秒扫描到期任务并交给任务执行器。

两条决策（2026-10-25 与用户确认）：
- **只在应用运行时调度**：调度器是 FastAPI 进程里的守护线程，窗口关掉就停；
  停机期间错过的触发按任务勾选处理（默认只记一条 missed，勾了 catch_up 才补跑一次）。
- **运行记录复用任务表**：每轮执行插一条带 loop_id 的 AgentTask 再交给
  task_runner，超时/日志/取消/通知/详情全部走任务链，这里只管「什么时候、谁来、做什么」。

扫描精度 20 秒（需求要求分钟级即可）；SQLite 短事务，写完再投递，
避免 worker 抢先读到还没落库的行。
"""
import json
import logging
import threading
from datetime import datetime

from . import loop_schedule, task_engines, task_precheck, task_runner
from .crypto import decrypt_text
from .database import SessionLocal
from .models import AgentTask, Loop

logger = logging.getLogger(__name__)

SCAN_INTERVAL = 20.0
RUNNING_STATUSES = ("pending", "running")


def _local_precheck(loop: Loop, db) -> tuple[bool, str]:
    """发射前的本地快检：CLI 在不在、配置方案与 Key 读不读得到。

    与手动下发同一道闸的前两步；第三步「真实连通」（跑一次最小无头调用，
    预算最长 5 分钟）只给自己动手的「立即执行」用——调度线程每 20 秒
    扫一轮，不能被连通预算堵住，无人值守的轮次靠运行记录里的失败原因反馈。
    """
    info = task_engines.probe(loop.tool)
    if not info["installed"]:
        return False, f"{info.get('label') or loop.tool} 未安装或不在 PATH 中"
    if loop.tool in task_engines.HTTP_ENGINES:
        return True, ""  # OpenCode 自带模型与凭据体系，不读 a4agent 的配置方案
    from . import crud

    active = crud.get_active_config(db)
    if active is None or active.provider is None:
        return False, "还没有生效的配置方案，请先在「配置方案」页切换一次"
    if not decrypt_text(active.api_key_encrypted):
        return False, "该配置方案的 API Key 解密失败或为空，请到「配置方案」页重新保存一次 Key"
    return True, ""

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()


# ---------------- 单轮扫描（可注入 db/submit，便于单测） ----------------

def _config(loop: Loop) -> dict:
    raw = loop.schedule_config or "{}"
    if isinstance(raw, dict):
        return dict(raw)
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("循环任务 #%s 的触发计划不是合法 JSON，按一次性处理", loop.id)
        return {"run_at": (loop.next_run_at or datetime.now()).strftime("%Y-%m-%d %H:%M")}


def _active_run(db, loop_id: int) -> AgentTask | None:
    """该循环是否还有没跑完的一轮 → 防重入（FR-2.3）。"""
    return (db.query(AgentTask)
            .filter(AgentTask.loop_id == loop_id,
                    AgentTask.status.in_(RUNNING_STATUSES))
            .first())


def _record(loop: Loop, status: str, detail: str, now: datetime) -> AgentTask:
    """构造一条不发进程的标记记录（skipped / missed），让运行历史看得到原因。"""
    return AgentTask(
        prompt=loop.task_content or "",
        tool=loop.tool,
        working_dir=loop.workdir or "",
        timeout_seconds=loop.timeout_seconds or 1800,
        status=status,
        loop_id=loop.id,
        trigger_type="auto",
        error_summary=detail,
        finished_at=now,
    )


def _advance(loop: Loop, now: datetime, config: dict, ran: bool) -> None:
    """推进 next_run_at；一次性任务执行/跳过后收尾为 completed。"""
    if loop.schedule_type == "once":
        loop.status = "completed"
        loop.next_run_at = None
        return
    loop.next_run_at = loop_schedule.next_run(
        loop.schedule_type, config, now,
        last_run_at=now if ran else loop.last_run_at,
        created_at=loop.created_at,
    )


def _fire(loop: Loop, now: datetime, db, submit, precheck=None) -> str:
    """处理一条到期任务，返回 fired / skipped / precheck_failed。"""
    config = _config(loop)
    if _active_run(db, loop.id) is not None:
        db.add(_record(loop, "skipped", "上一轮运行尚未结束，本次触发已跳过", now))
        _advance(loop, now, config, ran=False)
        db.commit()  # 与 fired 分支一致：tick 自建的 session 关掉即回滚
        return "skipped"

    ok, reason = (precheck or _local_precheck)(loop, db)
    if not ok:
        # 预检不过不拉进程：记一条带原因的运行记录，用户看得到为什么没跑
        db.add(_record(loop, task_runner.PRECHECK_FAILED, f"预检未通过：{reason}", now))
        _advance(loop, now, config, ran=False)
        db.commit()
        return task_runner.PRECHECK_FAILED

    task = AgentTask(
        prompt=loop.task_content or "",
        tool=loop.tool,
        working_dir=loop.workdir or "",
        timeout_seconds=loop.timeout_seconds or 1800,
        status="pending",
        loop_id=loop.id,
        trigger_type="auto",
    )
    db.add(task)
    db.flush()  # 先拿到 id
    loop.last_run_at = now
    _advance(loop, now, config, ran=True)
    db.commit()  # worker 用另一个 session，必须先落库
    submit(task.id)
    logger.info("循环任务 #%s（%s）已投递，任务 #%s", loop.id, loop.name, task.id)
    return "fired"


def _due_loops(db, now: datetime):
    return (db.query(Loop)
            .filter(Loop.enabled.is_(True),
                    Loop.status == "active",
                    Loop.next_run_at.isnot(None),
                    Loop.next_run_at <= now)
            .order_by(Loop.next_run_at)
            .all())


def tick(now: datetime | None = None, db=None, submit=None, precheck=None) -> dict:
    """扫描一轮到期任务并投递，返回 {fired, skipped, precheck_failed} 计数。"""
    now = now or datetime.now()
    own_session = db is None
    if own_session:
        db = SessionLocal()
    submit = submit or task_runner.submit
    counts = {"fired": 0, "skipped": 0}
    try:
        for loop in _due_loops(db, now):
            try:
                outcome = _fire(loop, now, db, submit, precheck)
            except Exception:  # noqa: BLE001 - 单条失败不能拖垮整轮扫描
                db.rollback()
                logger.exception("循环任务 #%s 投递失败", loop.id)
                continue
            counts[outcome] = counts.get(outcome, 0) + 1
    finally:
        if own_session:
            db.close()
    return counts


# ---------------- 启动时的错过处理（FR-2.2） ----------------

def startup_sweep(now: datetime | None = None, db=None, submit=None,
                  precheck=None) -> dict:
    """应用启动时处理停机期间错过的触发。

    勾了「启动时补跑」的任务补执行一次（不按错过次数叠加），其余只记
    missed 并推进到下一次。
    """
    now = now or datetime.now()
    own_session = db is None
    if own_session:
        db = SessionLocal()
    submit = submit or task_runner.submit
    counts = {"fired": 0, "missed": 0}
    try:
        for loop in _due_loops(db, now):
            config = _config(loop)
            try:
                if loop.catch_up:
                    outcome = _fire(loop, now, db, submit, precheck)
                    counts["fired" if outcome == "fired" else "missed"] += 1
                    continue
                db.add(_record(loop, "missed", "应用未运行，本次触发已错过", now))
                _advance(loop, now, config, ran=False)
                db.commit()
                counts["missed"] += 1
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("循环任务 #%s 错过处理失败", loop.id)
        if counts["missed"] or counts["fired"]:
            logger.info("循环调度启动补算：补跑 %s，错过 %s",
                        counts["fired"], counts["missed"])
    finally:
        if own_session:
            db.close()
    return counts


# ---------------- 线程生命周期 ----------------

def _loop() -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001 - 调度异常不能拖垮常驻线程
            logger.exception("循环调度扫描异常")
        _stop.wait(SCAN_INTERVAL)


def start() -> None:
    """启动调度线程（幂等）；先跑一次错过处理，再进入周期扫描。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        try:
            startup_sweep()
        except Exception:  # noqa: BLE001 - 补算失败不影响调度启动
            logger.exception("循环调度启动补算失败")
        _thread = threading.Thread(target=_loop, name="a4agent-loop-scheduler", daemon=True)
        _thread.start()
        logger.info("循环调度器已启动（每 %.0f 秒扫描一次）", SCAN_INTERVAL)


def stop() -> None:
    """停止调度线程（幂等）；运行中的 CLI 子进程由 task_runner.shutdown_all 收口。"""
    global _thread
    with _lock:
        _stop.set()
        thread, _thread = _thread, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


def running() -> bool:
    """调度线程是否存活（给 API 的健康指示用）。"""
    return _thread is not None and _thread.is_alive()
