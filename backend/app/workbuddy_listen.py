"""WorkBuddy 会话雷达：在提问面板出现的那一刻发提醒（不等 hook）。

为什么需要独立监听
------------------
WorkBuddy 把 AskUserQuestion 做成了宿主原生交互面板：**面板挂着的时候它不调用
hook、也不向 hook 进程写 stdin 载荷**，等用户点完选项才 spawn `a4agent.exe hook
workbuddy`。实测（2026-10-24，本机）面板出现后 3 分 12 秒内没有任何 hook 子进程，
队列文件与「答案回传」同刻落盘（相差 85ms）——所以 hook 链路拿到的永远是「已经
作答完的提问」，提醒必然是马后炮。

绕开的办法是改看宿主自己即时落盘的会话记录：AskUserQuestion 的 function_call
行在面板出现的同一刻写入 `~/.workbuddy/projects/<项目>/<会话>.jsonl`（实测
15:39:05.108，比面板渲染还早一瞬），用户作答后才追加同 callId 的
function_call_result。本模块增量读这些 jsonl，「有 call 无 result」即待作答提问，
立即入队弹横幅，延迟只取决于轮询间隔（0.4s）。

职责边界
--------
只发桌面横幅。WorkBuddy 上的手机作答没有意义（hook 拿到载荷时终端已回答完，
回填覆盖不了面板结果），因此本模块不推手机、不做答案回传——远端作答能力留给
hook 语义正常的其余六端。

容错：会话文件是宿主内部格式，随版本可能变。任何解析异常都只跳过当前行/文件，
绝不影响 hook、任务、推送等既有链路；`_offsets` 首次见到文件只记位置，不回溯
历史内容，避免重启后补弹一堆过期提醒。
"""
import json
import logging
import threading
import time
from pathlib import Path

from .hooks import deskqueue
from .hooks.transcript import extract_first_prompt
from .phone import config as phone_config
from .phone.notifier import format_notice

logger = logging.getLogger(__name__)

AGENT_NAME = "WorkBuddy"
ASK_TOOL = "AskUserQuestion"

_SCAN_SECONDS = 0.4            # 会话文件轮询间隔（≈ 提醒延迟上限）
_RELIST_SECONDS = 4.0          # 重新枚举目录的间隔，避免每轮 glob 上百文件
_ACTIVE_WINDOW_SECONDS = 30 * 60   # 只跟踪最近半小时写过的会话
_TITLE_READ_BYTES = 256 * 1024     # 取话题名称只读文件头，别整份读
_HISTORY_SKIP_MS = 60_000      # 启动前的历史提问不补弹
_REMEMBER_SECONDS = 3600       # 已提醒 callId 的记忆窗口（防重复弹）
_REMEMBER_MAX = 500            # 记忆条目上限，超出淘汰最旧
_REGISTER_TTL = 60.0           # 注册状态复查间隔
_CONFIG_TTL = 5.0              # 通知配置复查间隔

_started_ms = int(time.time() * 1000)
_offsets: dict[str, int] = {}
_reminded: dict[str, float] = {}
_stop = threading.Event()
_thread: threading.Thread | None = None
_registered_cache: tuple[float, bool] | None = None
_config_cache: tuple[float, dict] | None = None


# ---------------- 外部依赖（薄封装，便于测试替换） ----------------


def projects_dir() -> Path:
    """WorkBuddy 会话记录根目录（与 hook 注册用的 home 同源）。"""
    return Path.home() / ".workbuddy" / "projects"


def _workbuddy_registered() -> bool:
    """WorkBuddy 是否还挂着 a4agent 的 hook：卸载后雷达同步静默。"""
    global _registered_cache
    now = time.time()
    if _registered_cache is None or now - _registered_cache[0] > _REGISTER_TTL:
        from .hooks import register

        try:
            registered = register.workbuddy_registered()
        except Exception:  # noqa: BLE001 - 状态探测失败按未注册处理，不弹可疑提醒
            logger.debug("WorkBuddy 注册状态读取失败", exc_info=True)
            registered = False
        _registered_cache = (now, registered)
    return _registered_cache[1]


def _phone_cfg() -> dict:
    global _config_cache
    now = time.time()
    if _config_cache is None or now - _config_cache[0] > _CONFIG_TTL:
        _config_cache = (now, phone_config.load())
    return _config_cache[1]


# ---------------- 会话文件增量读取 ----------------


def _active_files(now: float) -> list[Path]:
    """最近半小时写过的会话文件；目录不存在（没装 WorkBuddy）返回空。"""
    root = projects_dir()
    files: list[Path] = []
    try:
        projects = list(root.iterdir())
    except OSError:
        return files
    for project in projects:
        try:
            if not project.is_dir():
                continue
            candidates = list(project.glob("*.jsonl"))
        except OSError:
            continue
        for file in candidates:
            try:
                if now - file.stat().st_mtime <= _ACTIVE_WINDOW_SECONDS:
                    files.append(file)
            except OSError:
                continue
    return files


def _read_new_rows(path: Path) -> list[dict]:
    """读出文件自上次扫描以来的完整行；半行留在文件里等下次。"""
    key = str(path)
    try:
        size = path.stat().st_size
    except OSError:
        return []
    prev = _offsets.get(key)
    if prev is None:
        _offsets[key] = size  # 首次见面只记位置：历史内容不回溯
        return []
    if size < prev:  # 文件被截断/重写（同一会话换实现）：从头再来
        prev = 0
    if size == prev:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(prev)
            chunk = fh.read(size - prev)
    except OSError:
        return []
    if not chunk.endswith(b"\n"):
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return []  # 只有半行：等宿主写完再读，offset 不前进
        chunk = chunk[:cut + 1]
    _offsets[key] = prev + len(chunk)

    rows: list[dict] = []
    for line in chunk.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue  # 非 JSON 行（宿主内部其它格式）跳过
        if isinstance(row, dict):
            rows.append(row)
    return rows


# ---------------- 提醒 ----------------


def _topic(path: Path) -> str:
    """话题名称：会话第一条真实用户消息，与 hook 侧 session_topic 同源。"""
    return extract_first_prompt(str(path), max_bytes=_TITLE_READ_BYTES) or ""


def _emit(path: Path) -> None:
    deskqueue.queue_notify(*format_notice(_topic(path), "问题作答", AGENT_NAME))


def _remember(call_id: str, now: float) -> None:
    _reminded[call_id] = now
    if len(_reminded) > _REMEMBER_MAX:
        cutoff = now - _REMEMBER_SECONDS
        for key, ts in list(_reminded.items()):
            if ts < cutoff:
                del _reminded[key]
        for key, _ in sorted(_reminded.items(), key=lambda kv: kv[1])[:50]:
            del _reminded[key]


def scan_once(files: list[Path] | None = None, now: float | None = None) -> int:
    """扫一轮会话文件，返回本次发出的提醒条数（测试可直接调用）。"""
    now = time.time() if now is None else now
    targets = _active_files(now) if files is None else files
    sent = 0
    for path in targets:
        rows = _read_new_rows(path)
        if not rows:
            continue
        calls: list[tuple[str, str, int]] = []  # (callId, 行 id, 时间戳)
        replied_calls: set[str] = set()
        replied_parents: set[str] = set()
        for row in rows:
            kind = row.get("type")
            if kind == "function_call" and row.get("name") == ASK_TOOL:
                call_id = str(row.get("callId") or row.get("id") or "")
                if call_id:
                    try:
                        ts = int(row.get("timestamp") or 0)
                    except (TypeError, ValueError):
                        ts = 0
                    calls.append((call_id, str(row.get("id") or ""), ts))
            elif kind == "function_call_result":
                replied_id = row.get("callId")
                if replied_id:
                    replied_calls.add(str(replied_id))
                parent_id = row.get("parentId")
                if parent_id:
                    replied_parents.add(str(parent_id))
        for call_id, row_id, ts in calls:
            # 同一批增量里已经作答（用户手速极快）：不必再提醒
            if call_id in replied_calls or (row_id and row_id in replied_parents):
                continue
            if ts and ts < _started_ms - _HISTORY_SKIP_MS:
                continue  # 启动前的历史提问不补弹
            if call_id in _reminded:
                continue
            _remember(call_id, now)
            try:
                _emit(path)
            except Exception:  # noqa: BLE001 - 单条提醒失败不影响后续会话
                logger.warning("WorkBuddy 提问提醒失败：%s", path.name, exc_info=True)
                continue
            sent += 1
            logger.info("WorkBuddy 提问提醒已入队（会话 %s）", path.stem)
    return sent


# ---------------- 生命周期 ----------------


def reset_state() -> None:
    """清空增量位置与去重记忆（测试与重新启动用）。"""
    _offsets.clear()
    _reminded.clear()


def start() -> None:
    """应用启动时调用：拉起雷达线程（幂等）。永不抛错——提醒是尽力而为。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    reset_state()
    _stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="a4agent-wb-radar")
    _thread.start()


def stop() -> None:
    """应用退出时调用（幂等）。"""
    _stop.set()


def _loop() -> None:
    files: list[Path] = []
    last_list = 0.0
    while not _stop.wait(_SCAN_SECONDS):
        if not (_phone_cfg().get("desktop", True) and _workbuddy_registered()):
            continue  # 开关关着或该端已卸载：纯待机，不碰磁盘
        now = time.time()
        if now - last_list >= _RELIST_SECONDS:
            files = _active_files(now)
            last_list = now
            # 掉出活跃窗口的会话不再跟踪，增量位置表跟着瘦身（长期常驻不涨内存）
            alive = {str(f) for f in files}
            for key in [k for k in _offsets if k not in alive]:
                del _offsets[key]
        try:
            scan_once(files, now)
        except Exception:  # noqa: BLE001 - 雷达任何异常都不能拖累主服务
            logger.exception("WorkBuddy 会话雷达异常")
