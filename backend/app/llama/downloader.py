"""引擎包下载安装（移植自 a4agent EngineDownloader.cs）。

把 llama.cpp 官方 Release zip（CUDA 含 cudart 运行库包）下载、
解压合并到目标 engine 目录。全程先写临时目录，成功后原子换入，
失败/取消不会留下半个 engine 目录。后台线程执行，进度走轮询。
"""
import logging
import shutil
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

from . import catalog

logger = logging.getLogger(__name__)

_CHUNK = 1 << 16


def is_engine_present(engine_dir) -> bool:
    return (Path(engine_dir) / "llama-server.exe").is_file()


class EngineDownloader:
    """同一时刻仅一个下载任务；进度/结果通过 progress() 轮询读取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state = {
            "running": False,
            "stage": "",
            "received_bytes": 0,
            "total_bytes": 0,
            "indeterminate": False,
            "error": "",
            "done": False,
        }

    def progress(self) -> dict:
        with self._lock:
            return dict(self._state)

    def cancel(self) -> dict:
        self._cancel.set()
        return self.progress()

    def start(self, pack: catalog.EnginePack, engine_dir) -> dict:
        with self._lock:
            if self._state["running"]:
                return {"ok": False, "error": "已有下载任务在进行中"}
            self._cancel.clear()
            self._state = {
                "running": True,
                "stage": f"准备下载：{pack.label}",
                "received_bytes": 0,
                "total_bytes": pack.total_bytes,
                "indeterminate": False,
                "error": "",
                "done": False,
            }
            self._thread = threading.Thread(
                target=self._run, args=(pack, str(engine_dir)),
                name="llama-engine-dl", daemon=True,
            )
            self._thread.start()
        return {"ok": True}

    # ───────────────────────── 后台执行 ─────────────────────────

    def _run(self, pack: catalog.EnginePack, engine_dir: str) -> None:
        try:
            self._download(pack, engine_dir)
        except _Cancelled:
            self._finish(error="已取消下载")
        except Exception as e:
            logger.warning("引擎下载失败：%s", e)
            self._finish(error=str(e))

    def _download(self, pack: catalog.EnginePack, engine_dir: str) -> None:
        root = str(Path(engine_dir).parent)
        Path(root).mkdir(parents=True, exist_ok=True)

        tmp_root = Path(root) / f".engine-dl-{uuid.uuid4().hex}"
        staging = Path(root) / f".engine-new-{uuid.uuid4().hex}"
        tmp_root.mkdir()
        staging.mkdir()

        try:
            grand_total = pack.total_bytes
            done_bytes = 0
            zips = []

            for i, asset in enumerate(pack.zips):
                if self._cancel.is_set():
                    raise _Cancelled()
                dest = tmp_root / f"{i}.zip"
                label = f"下载 {i + 1}/{len(pack.zips)}: {asset}"
                self._report(stage=label, received=done_bytes, total=grand_total)
                done_bytes += self._download_file(
                    catalog.pack_url(pack, i), dest, label, grand_total)
                zips.append(dest)

            self._report(stage="解压安装中…", received=done_bytes,
                         total=grand_total, indeterminate=True)
            for z in zips:
                _extract_flat(z, staging, self._cancel)

            # 换入前先确认关键文件在；再把旧目录整体改名保底，新目录就位后
            # 才删旧的。直接 rmtree 旧目录意味着 rename 一旦失败（杀软锁文件
            # 等）引擎就全没了，只能重下
            if not is_engine_present(staging):
                raise IOError("解压后未找到 llama-server.exe，引擎包内容异常")

            old = Path(engine_dir)
            old_bak = old.with_name(old.name + ".bak")
            if old_bak.exists():
                shutil.rmtree(old_bak, ignore_errors=True)
            if old.exists():
                old.rename(old_bak)
            try:
                Path(staging).rename(old)
            except Exception:
                if old_bak.exists() and not old.exists():
                    old_bak.rename(old)  # 换入失败就还回去，保住旧引擎
                raise
            shutil.rmtree(old_bak, ignore_errors=True)

            self._report(stage="✔ 引擎安装完成", received=grand_total,
                         total=grand_total, done=True)
            self._finish()
        finally:
            _try_delete(tmp_root)
            _try_delete(staging)

    def _download_file(self, url: str, dest: Path, label: str, grand_total: int) -> int:
        req = urllib.request.Request(url, headers={"User-Agent": "a4agent"})
        last_report = 0.0
        received = 0
        with urllib.request.urlopen(req, timeout=60) as resp, \
                open(dest, "wb") as fp:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                if self._cancel.is_set():
                    raise _Cancelled()
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                fp.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_report > 0.2:  # 限频，避免状态字典写爆
                    last_report = now
                    self._report(stage=label, received=received,
                                 total=total or grand_total)
        if total and received != total:
            # 断流/截断在此刻就报错，而不是等用户白等完整包下载后
            # 在解压阶段才收到 BadZipFile
            raise IOError(f"下载不完整（{received}/{total} 字节），请重试")
        return received

    def _report(self, stage="", received=None, total=None,
                indeterminate=None, done=None) -> None:
        with self._lock:
            if stage:
                self._state["stage"] = stage
            if received is not None:
                self._state["received_bytes"] = received
            if total is not None:
                self._state["total_bytes"] = total
            if indeterminate is not None:
                self._state["indeterminate"] = indeterminate
            if done is not None:
                self._state["done"] = done

    def _finish(self, error: str = "") -> None:
        with self._lock:
            self._state["running"] = False
            self._state["error"] = error
            if not error and not self._state["done"]:
                self._state["done"] = True


class _Cancelled(Exception):
    pass


def _extract_flat(zip_path: Path, dest: Path, cancel: threading.Event) -> None:
    """zip 内容全部平铺进 dest（llama.cpp 官方包内部结构是平的，
    CUDA 主包与 cudart 包文件名不重叠）。跳过试图逃出 dest 的恶意条目。"""
    dest = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for entry in zf.infolist():
            if cancel.is_set():
                raise _Cancelled()
            if entry.filename.endswith("/"):
                continue  # 目录条目
            target = (dest / entry.filename).resolve()
            if not target.is_relative_to(dest):
                continue  # 路径穿越，丢弃
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                continue  # 先到先得，主包优先
            with zf.open(entry) as src, open(target, "wb") as fp:
                shutil.copyfileobj(src, fp)


def _try_delete(path: Path) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
    except OSError as e:  # 清理失败不影响主流程
        logger.debug("临时目录清理失败：%s", e)
