"""应用自更新：清单拉取/验签、安装包下载/校验、安装器应用。

安全性设计：
- 传输仅 HTTPS，重定向逐跳校验 host 白名单（防跳转钓鱼）。
- 更新清单 latest.json 由发布侧用 Ed25519 私钥签名，本模块内置公钥验签；
  任何字段校验或签名不通过，清单即作废，不向用户展示更新提示。
- 安装包边下边算 SHA256，与签名过的清单比对，通过才落盘；apply 前再次重校验。
- 版本比较防降级；预发布版本除非当前运行版本也是预发布，否则跳过。
- 下载落在运行时数据目录（%APPDATA%\\a4agent\\updates\\），不信任系统临时目录。
"""
import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .database import get_data_dir
from .version import current_version

logger = logging.getLogger(__name__)

# ---- 配置常量 ----

# 发布侧签名密钥的公钥（生成方式见 .claude/skills/release/SKILL.md）：
#   openssl genpkey -algorithm ed25519 -out update-signing.pem
#   openssl pkey -in update-signing.pem -pubout -out update-signing.pub.pem
# 私钥保存在 .claude/keys/ 下，绝不入库；公钥是公开信息，随应用分发。
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAv4+TLC3OePq1OPnyOl1TtsY4T8MFDJab/fNNwfgS7rE=
-----END PUBLIC KEY-----"""

# 更新清单地址：GitHub「latest 别名」优先；GitHub 不可达时经 Gitee API 找最新 tag 回退。
GITHUB_MANIFEST_URL = "https://github.com/eogee/a4agent/releases/latest/download/latest.json"
GITEE_API_LATEST_URL = "https://gitee.com/api/v5/repos/eogee/a4agent/releases/latest"

MANIFEST_TTL = 600  # 清单缓存秒数
MANIFEST_MAX_BYTES = 512 * 1024
MAX_DOWNLOAD_SIZE = 300 * 1024 * 1024
_HTTP_TIMEOUT = 30  # 大文件（安装包）下载超时
_MANIFEST_TIMEOUT = 8  # 更新清单（GitHub/Gitee）单次请求超时；下载不经 _http_get，不受影响
_MANIFEST_ATTEMPTS = 1  # 清单双源并行，冗余来自「两源」而非「重试」
_WIN_MARGIN = 8 * 1024 * 1024  # 竞速判胜的领先量：领先 8MB 即判胜，落败方提前收手少抢带宽

_ALLOWED_HOSTS = {
    "github.com",
    "gitee.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}

# 签名载荷协议常量（与 release.js 严格一致，两端必须输出相同字节）。
# 注意：_NAMESPACE 是跨版本验签协议的一部分——改名前的 v0.3.x 客户端用它验签，
# 一旦改动所有老版本会判定清单签名无效、永远收不到更新，因此即使品牌改为
# a4agent 此值也保持不变。
_NAMESPACE = "a4api-update"
_PAYLOAD_VERSION = "1"
_SCHEMA_VERSION = "1"
_INSTALLER_PREFIX = "a4agent-setup-"
# 改名过渡期清单可能只带旧前缀资产（供 v0.3.x 老客户端升级用），查找时回退兼容
_LEGACY_INSTALLER_PREFIX = "a4api-setup-"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_MAX_ASSETS = 4

# ---- 运行状态（下载线程与请求线程共享，锁保护） ----
_lock = threading.Lock()
# 清单拉取单飞锁：网络 I/O 期间只持这把锁，不占用 _lock（避免阻塞进度轮询）
_fetch_lock = threading.Lock()
_manifest_cache: dict | None = None  # {"data": dict, "fetched_at": float}
_download: dict = {"status": "idle", "version": None, "downloaded": 0, "total": 0,
                   "path": None, "sha256_ok": False, "error": None}
_dl_thread: threading.Thread | None = None
_cancel_event: threading.Event | None = None


# ---------- 版本比较 ----------

def parse_version(v: str):
    """解析 x.y.z 为 (主, 次, 修订) 元组；非法格式返回 None。"""
    if not _VERSION_RE.match(v or ""):
        return None
    return tuple(int(p) for p in v.split("."))


def compare(a: str, b: str) -> int:
    """a > b 返回 1，a == b 返回 0，a < b 返回 -1；任一非法格式返回 0。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return 0
    return (pa > pb) - (pa < pb)


def is_newer(candidate: str, current: str) -> bool:
    return compare(candidate, current) > 0


def is_too_old(current: str, min_version: str) -> bool:
    """当前版本低于清单 min_version 时返回 True（该更新需完整安装包，不可直接应用）。"""
    return compare(current, min_version) < 0


def _numeric_core(v: str) -> str:
    """取版本号数字核心用于比较：'0.2.0-beta' → '0.2.0'；无法剥离时原样返回。

    parse_version 只接受严格 x.y.z，预发布当前版本（0.2.0-beta）会解析失败、
    比较恒为 0 → 永远判"无更新"。比较前先剥离非数字后缀，预发布构建才能看到后续更新。
    """
    m = re.match(r"^(\d+\.\d+\.\d+)", v or "")
    return m.group(1) if m else v


# ---------- 签名载荷与清单校验 ----------

def build_payload(manifest: dict) -> bytes:
    """构造签名载荷：固定字段顺序 + 4 字节大端长度前缀，消除 JSON 序列化歧义。

    URL 不在签名范围——两份 latest.json（GitHub/Gitee）因此字节一致、可共用同一份签名；
    URL 指向的内容由被签名的 sha256 绑死，替换 URL 无法替换安装包内容。
    """
    assets = manifest.get("assets") or []

    def push(buf: bytearray, s: str) -> None:
        data = str(s).encode("utf-8")
        buf.extend(len(data).to_bytes(4, "big"))
        buf.extend(data)

    buf = bytearray()
    push(buf, _NAMESPACE)
    push(buf, _PAYLOAD_VERSION)
    push(buf, manifest["schema_version"])
    push(buf, manifest["version"])
    push(buf, manifest.get("min_version", "0.0.0"))
    push(buf, manifest.get("published_at", ""))
    push(buf, "1" if manifest.get("prerelease") else "0")
    push(buf, manifest.get("notes", ""))
    push(buf, manifest.get("notes_url", ""))
    for a in sorted(assets, key=lambda x: str(x["name"])):
        push(buf, a["name"])
        push(buf, str(a["sha256"]).lower())
        push(buf, str(a["size"]))
    return bytes(buf)


def allowed_host(host: str) -> bool:
    """下载/清单主机白名单（后缀匹配，覆盖 CDN 子域）。"""
    host = (host or "").lower()
    if not host:
        return False
    if host in _ALLOWED_HOSTS:
        return True
    return host.endswith(".githubusercontent.com") or host.endswith(".gitee.com")


def validate_manifest(manifest: dict) -> bool:
    """严格校验清单字段类型与取值。不合法一律返回 False（视为签名无效）。"""
    try:
        if str(manifest.get("schema_version")) != _SCHEMA_VERSION:
            return False
        if "version" not in manifest or not _VERSION_RE.match(str(manifest["version"])):
            return False
        if not _VERSION_RE.match(str(manifest.get("min_version", "0.0.0"))):
            return False
        if not isinstance(manifest.get("prerelease", False), bool):
            return False

        assets = manifest.get("assets")
        if not isinstance(assets, list) or not assets:
            return False
        if len(assets) > _MAX_ASSETS:
            return False
        seen: set = set()
        for a in assets:
            if not isinstance(a, dict):
                return False
            name = a.get("name")
            if not isinstance(name, str) or not name:
                return False
            url = a.get("url")
            if not isinstance(url, str) or not allowed_host(_host_of(url)):
                return False
            key = (name, url)
            if key in seen:  # 拒绝「同名同 URL」的重复资产；同名不同镜像允许
                return False
            seen.add(key)
            sha = a.get("sha256")
            if not isinstance(sha, str) or not _SHA256_RE.match(sha.strip()):
                return False
            size = a.get("size")
            if not isinstance(size, int) or size <= 0 or size > MAX_DOWNLOAD_SIZE:
                return False
        return True
    except Exception:
        return False


def verify_signature(manifest: dict) -> bool:
    """Ed25519 验签：先字段严格校验，再重建签名载荷比对。失败即拒。"""
    if not validate_manifest(manifest):
        return False
    sig_b64 = manifest.get("signature")
    if not isinstance(sig_b64, str) or not sig_b64:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        signature = base64.b64decode(sig_b64, validate=True)
        pub = serialization.load_pem_public_key(PUBLIC_KEY_PEM.encode("ascii"))
        if not isinstance(pub, Ed25519PublicKey):
            return False
        pub.verify(signature, build_payload(manifest))
        return True
    except Exception as e:
        logger.warning("更新清单验签失败：%s", e)
        return False


# ---------- 状态持久化（忽略版本 / 已下载待安装） ----------

def state_path() -> Path:
    return get_data_dir() / "update_state.json"


def read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(state: dict) -> None:
    """原子写状态文件，避免中途崩溃损坏。"""
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".update_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def is_ignored(version: str) -> bool:
    return version in read_state().get("ignored", [])


def ignore_version(version: str) -> None:
    state = read_state()
    ignored = state.get("ignored", [])
    if version not in ignored:
        ignored.append(version)
        state["ignored"] = ignored
        write_state(state)


def _mark_downloaded(version: str, path: Path, sha256: str = "", size: int = 0) -> None:
    """持久化「已下载待安装」标记，含校验值（apply 离线时仍可复核）。"""
    state = read_state()
    entry = {"version": version, "path": str(path)}
    if sha256:
        entry["sha256"] = sha256
    if size:
        entry["size"] = int(size)
    state["downloaded"] = entry
    write_state(state)


def _clear_downloaded_marker() -> None:
    state = read_state()
    if "downloaded" in state:
        state.pop("downloaded", None)
        write_state(state)


# ---------- 网络拉取（含逐跳 host 白名单） ----------

def _host_of(url: str) -> str:
    return (urlparse(url or "").netloc or "").split(":")[0].lower()


class _HostCheckRedirectHandler(HTTPRedirectHandler):
    """在每一跳重定向前校验目标 host，拦截跳转到非白名单主机。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = _host_of(newurl)
        if not allowed_host(host):
            raise URLError(f"redirect blocked: {host}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, max_bytes: int, timeout: float) -> bytes:
    """HTTPS GET + 逐跳 host 白名单 + 最终 URL 复检 + 大小上限。timeout 为该请求 socket 超时。"""
    opener = build_opener(_HostCheckRedirectHandler())
    req = Request(url, headers={"User-Agent": "a4agent-updater/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        if not allowed_host(_host_of(resp.geturl())):
            raise URLError(f"blocked host: {_host_of(resp.geturl())}")
        total = 0
        chunks = []
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise URLError("response too large")
            chunks.append(chunk)
    return b"".join(chunks)


def _fetch_url_retry(url: str, max_bytes: int, timeout: float, attempts: int = 1) -> bytes:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return _http_get(url, max_bytes, timeout)
        except (HTTPError, URLError, OSError, ValueError) as e:
            last = e
            logger.warning("拉取失败 %s：%s", url, e)
            time.sleep(1)
    if last is not None:
        raise last
    raise URLError(url)


def _gitee_latest_manifest_url() -> str:
    """Gitee 无 latest 别名，用公开 API 取最新 release 的 tag 再拼清单地址。"""
    data = json.loads(_fetch_url_retry(
        GITEE_API_LATEST_URL, MANIFEST_MAX_BYTES,
        timeout=_MANIFEST_TIMEOUT, attempts=_MANIFEST_ATTEMPTS,
    ).decode("utf-8"))
    tag = data.get("tag_name", "")
    if not tag:
        raise URLError("gitee latest release has no tag")
    return f"https://gitee.com/eogee/a4agent/releases/download/{tag}/latest.json"


def _fetch_github_manifest() -> dict:
    """从 GitHub「latest 别名」拉取并解析清单。"""
    raw = _fetch_url_retry(
        GITHUB_MANIFEST_URL, MANIFEST_MAX_BYTES,
        timeout=_MANIFEST_TIMEOUT, attempts=_MANIFEST_ATTEMPTS,
    )
    return json.loads(raw.decode("utf-8"))


def _fetch_gitee_manifest() -> dict:
    """从 Gitee 拉取并解析清单（先经公开 API 拿最新 tag，再拼清单地址）。"""
    url = _gitee_latest_manifest_url()
    raw = _fetch_url_retry(
        url, MANIFEST_MAX_BYTES,
        timeout=_MANIFEST_TIMEOUT, attempts=_MANIFEST_ATTEMPTS,
    )
    return json.loads(raw.decode("utf-8"))


def _fetch_from_remote() -> dict:
    """并行拉取 GitHub / Gitee 清单，首个成功解析的胜出；全部失败抛异常。

    国内访问 GitHub 慢/不通，并行让快源（通常 Gitee）先返回，避免顺序等待
    GitHub 超时再回退。_fetch_lock 单飞语义由调用方 fetch_manifest 持锁，不变。
    返回后不等待慢源：shutdown(wait=False) 让仍卡在 socket 超时的线程自行收尾，
    避免阻塞响应路径等一个必然失败的请求。
    """
    ex = ThreadPoolExecutor(max_workers=2)
    futures = {ex.submit(_fetch_github_manifest), ex.submit(_fetch_gitee_manifest)}
    last_err: Exception | None = None
    try:
        for fut in as_completed(futures):
            try:
                return fut.result()
            except Exception as e:
                last_err = e
                logger.warning("更新清单源拉取失败：%s", e)
        raise last_err if last_err is not None else URLError("update manifest unreachable")
    finally:
        # 不用 with ThreadPoolExecutor()：其 __exit__ 会 shutdown(wait=True)，
        # 阻塞在挂起的 GitHub 请求上重新引入延迟。
        ex.shutdown(wait=False, cancel_futures=True)


def fetch_manifest() -> dict:
    """拉取并缓存更新清单，返回已通过验签的清单；失败抛异常。

    GitHub 优先，GitHub 不可达时回退 Gitee。
    网络 I/O 在 _fetch_lock 内、_lock 外执行：单飞（同一时刻只拉一次），
    且不阻塞 progress / start_download / apply 等需要 _lock 的请求。
    """
    global _manifest_cache
    # 快速路径：缓存未过期直接返回（只取 _lock，不碰网络）
    with _lock:
        cached = _manifest_cache
        if cached and time.time() - cached["fetched_at"] < MANIFEST_TTL:
            return cached["data"]

    with _fetch_lock:  # 单飞：并发调用只有一个真正拉取，其余等待后复用结果
        with _lock:  # 双检：等 _fetch_lock 期间其它线程可能已填充缓存
            cached = _manifest_cache
            if cached and time.time() - cached["fetched_at"] < MANIFEST_TTL:
                return cached["data"]
        data = _fetch_from_remote()
        if not verify_signature(data):
            raise ValueError("manifest signature invalid")
        with _lock:
            _manifest_cache = {"data": data, "fetched_at": time.time()}
        return data


# ---------- 更新检查 ----------

def _installer_name(version: str) -> str:
    return f"{_INSTALLER_PREFIX}{version}.exe"


def _installer_names(version: str) -> list[str]:
    """该版本安装包的候选资产名：新前缀优先，旧前缀兜底（过渡期清单可能只带旧名）。"""
    return [f"{_INSTALLER_PREFIX}{version}.exe", f"{_LEGACY_INSTALLER_PREFIX}{version}.exe"]


def _asset_for_version(manifest: dict, version: str) -> dict:
    for name in _installer_names(version):
        for a in manifest.get("assets", []):
            if a.get("name") == name:
                return a
    raise ValueError(f"更新清单中找不到版本 {version} 的安装包")


def _urls_for_version(manifest: dict, version: str) -> list[str]:
    names = set(_installer_names(version))
    return [a["url"] for a in manifest.get("assets", []) if a.get("name") in names]


def _cleanup_applied_downloads(current: str) -> None:
    """当前版本已 ≥ 已下载版本时，清理该下载目录与状态（防残留占空间）。"""
    state = read_state()
    dl = state.get("downloaded")
    if not isinstance(dl, dict) or not dl.get("version"):
        return
    version = dl["version"]
    if parse_version(version) is None or compare(version, current) > 0:
        return
    try:
        d = get_data_dir() / "updates" / version
        for p in d.glob("*"):
            p.unlink(missing_ok=True)
        d.rmdir()
    except OSError:
        pass
    state.pop("downloaded", None)
    write_state(state)


def _pending_verified_download(manifest: dict) -> dict | None:
    """返回「已下载且仍校验通过」的安装包信息；无效则清理状态。

    校验优先用持久化的 SHA256（下载时已与签名清单比对），离线也能判定；
    持久化标记缺失（旧版本留下的）时回退用签名清单核对。
    """
    state = read_state()
    dl = state.get("downloaded")
    if not isinstance(dl, dict) or not dl.get("path") or not dl.get("version"):
        return None
    path = Path(dl["path"])
    version = dl["version"]
    if not path.is_file():
        _clear_downloaded_marker()
        return None
    try:
        if dl.get("sha256"):
            valid = _sha256_of(path).lower() == str(dl["sha256"]).lower()
            if valid and dl.get("size"):
                valid = path.stat().st_size == int(dl["size"])
        else:
            asset = _asset_for_version(manifest, version)
            valid = (_sha256_of(path).lower() == asset["sha256"].lower()
                     and path.stat().st_size == int(asset["size"]))
        if valid:
            return {"version": version, "path": str(path)}
    except Exception:
        pass
    _clear_downloaded_marker()
    return None


def check(force: bool = False) -> dict:
    """检查是否有可用更新，返回给前端的状态对象。"""
    global _manifest_cache
    current = current_version()
    # 当前版本可能是预发布（如 0.2.0-beta）：比较用数字核心，展示仍用完整版本号。
    # 否则 parse_version('0.2.0-beta') 失败 → compare 恒 0 → 永远"无更新"（含稳定版）。
    cmp_current = _numeric_core(current)
    if force:
        with _lock:
            _manifest_cache = None
    try:
        manifest = fetch_manifest()
    except Exception as e:
        return {"status": "error", "current_version": current, "error": str(e)}

    _cleanup_applied_downloads(cmp_current)

    candidate = manifest.get("version", "")
    prerelease = bool(manifest.get("prerelease"))
    # 预发布版本：仅当当前运行版本本身带非数字标记时才提示
    current_is_prerelease = not bool(_VERSION_RE.match(current))

    result = {
        "status": "error",
        "current_version": current,
        "latest_version": candidate,
        "notes": manifest.get("notes", ""),
        "notes_url": manifest.get("notes_url", ""),
        "prerelease": prerelease,
    }

    if not is_newer(candidate, cmp_current):
        result["status"] = "up_to_date"
        return result
    if is_too_old(cmp_current, str(manifest.get("min_version", "0.0.0"))):
        result["status"] = "too_old"
        result["error"] = f"当前版本过旧，请直接下载完整安装包更新到 {candidate}"
        return result
    if prerelease and not current_is_prerelease:
        result["status"] = "up_to_date"
        return result
    if is_ignored(candidate):
        result["status"] = "ignored"
        return result

    result["status"] = "update_available"
    # 上次已下载未安装且仍有效 → 直接提示应用，避免重复下载
    pending = _pending_verified_download(manifest)
    if pending and pending["version"] == candidate:
        result["downloaded"] = True
        result["downloaded_version"] = candidate
    else:
        _clear_downloaded_marker()
    return result


# ---------- 下载 ----------

def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_file(url: str, dest: Path, expected_sha256: str, expected_size: int,
                   cancel: threading.Event, on_progress=None) -> None:
    """下载单个 URL 到 dest，边下边算 SHA256；尺寸/哈希不符抛异常，不留 .part。

    cancel 只要求实现 is_set()（竞速时传入组合判定对象，不限于 threading.Event）；
    on_progress(received_bytes) 每收到一个 chunk 调用一次，供竞速上报领先进度。
    """
    opener = build_opener(_HostCheckRedirectHandler())
    req = Request(url, headers={"User-Agent": "a4agent-updater/1.0"})
    with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
        if not allowed_host(_host_of(resp.geturl())):
            raise URLError(f"blocked host: {_host_of(resp.geturl())}")
        hasher = hashlib.sha256()
        received = 0
        with open(dest, "wb") as f:
            while True:
                if cancel.is_set():
                    raise InterruptedError("cancelled")
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                received += len(chunk)
                if received > MAX_DOWNLOAD_SIZE:
                    raise URLError("download too large")
                f.write(chunk)
                if on_progress is not None:
                    on_progress(received)
    if received != expected_size:
        raise ValueError(f"size mismatch: expected {expected_size}, got {received}")
    if hasher.hexdigest() != expected_sha256.lower():
        raise ValueError("sha256 mismatch")


def _race_download(urls: list[str], update_dir: Path, asset_name: str,
                   expected_sha: str, expected_size: int,
                   user_cancel: threading.Event) -> int | None:
    """全部镜像并行竞速下载，返回首个通过校验的 url 下标；取消/全败返回 None。

    之前按清单顺序串行回退：慢源只要「能连上」就永远不会抛异常触发回退
    （国内实测 GitHub 资产 ~0.1 MB/s、Gitee ~2 MB/s），整个安装包都磨在慢源上。
    改为全部镜像同时开下：快源自然先过校验胜出，慢源/挂源自动落败；
    领先超过 _WIN_MARGIN 时提前判胜，落败方即刻收手，少抢快源的带宽。

    状态机：done 由调用方在拿到胜者后写入；cancelled/failed 在此写入。
    进度取各线程中的领先者（进度条按安装包大小归一，不会出现 200%）。
    """
    parts = [update_dir / f"{asset_name}.part{i}" for i in range(len(urls))]
    received = [0] * len(urls)  # 仅各工作线程写自己的槽位，读不加锁（进度展示容许滞后）
    errors: list[str | None] = [None] * len(urls)
    winner: list[int | None] = [None]
    decided = threading.Event()  # 胜负已定（或用户取消）：落败线程尽快退出
    winner_done = threading.Event()  # 胜者已完成校验：主流程无需等落败线程收尾

    def lost(i: int) -> bool:
        return decided.is_set() and winner[0] != i

    def report(i: int, n: int) -> None:
        received[i] = n
        with _lock:
            if _download.get("status") != "downloading":
                return
            _download["downloaded"] = max(received)
            if winner[0] is None and not decided.is_set():
                alive = [j for j in range(len(urls)) if errors[j] is None]
                if len(alive) > 1:
                    lead = max(alive, key=lambda j: received[j])
                    rest = min(received[j] for j in alive if j != lead)
                    if received[lead] - rest >= _WIN_MARGIN:
                        winner[0] = lead
                        decided.set()

    def worker(i: int) -> None:
        # 每线程独立取消判定：用户取消，或别家已胜出
        class _Cancel:
            @staticmethod
            def is_set() -> bool:
                return user_cancel.is_set() or lost(i)

        try:
            if lost(i):
                return
            _download_file(urls[i], parts[i], expected_sha, expected_size,
                           _Cancel(), on_progress=lambda n, i=i: report(i, n))
            # 无条件记录（而非仅首个）：早判胜的胜者随后校验失败时，此处的
            # 真正完成者才是 main 应落盘的 .part
            winner[0] = i
            decided.set()
            winner_done.set()
        except InterruptedError:
            parts[i].unlink(missing_ok=True)
            received[i] = 0
            if user_cancel.is_set():
                decided.set()  # 让其余线程尽快退出
                with _lock:
                    if _download.get("status") == "downloading":
                        _download.update(status="cancelled", error="已取消下载", path=None)
            # 竞速落败：静默清理退出
        except Exception as e:
            parts[i].unlink(missing_ok=True)
            received[i] = 0
            errors[i] = str(e)
            logger.warning("下载 %s 失败：%s", urls[i], e)

    ex = ThreadPoolExecutor(max_workers=len(urls))
    futures = [ex.submit(worker, i) for i in range(len(urls))]
    try:
        # 胜者过验即返回，不等落败线程收尾（其可能卡在 connect 超时上，
        # 最长 _HTTP_TIMEOUT 秒后自行清理退出，不持任何锁）
        while not winner_done.is_set() and not all(f.done() for f in futures):
            time.sleep(0.05)
        w = winner[0]
        # 已过校验的胜者优先落盘：校验完成的瞬间即使点了取消，也不再丢弃下载成果
        if w is not None and parts[w].is_file():
            return w
        if user_cancel.is_set():
            return None  # cancelled 状态已由工作线程写入
        with _lock:
            if _download.get("status") == "downloading":
                detail = "；".join(f"{urls[i]}: {errors[i]}" for i in range(len(urls))
                                  if errors[i])
                _download.update(status="failed", path=None,
                                 error=detail or "全部镜像下载失败")
        return None
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def _run_download(version: str) -> None:
    """下载线程主体：全部镜像并行竞速，首个通过校验的落盘。"""
    try:
        manifest = fetch_manifest()
        asset = _asset_for_version(manifest, version)
        urls = _urls_for_version(manifest, version)
        expected_sha = asset["sha256"].lower()
        expected_size = int(asset["size"])

        update_dir = get_data_dir() / "updates" / version
        update_dir.mkdir(parents=True, exist_ok=True)
        final_path = update_dir / asset["name"]

        # 已存在且校验通过 → 直接复用
        if final_path.is_file():
            if _sha256_of(final_path) == expected_sha and final_path.stat().st_size == expected_size:
                with _lock:
                    _download.update(status="done", version=version, path=str(final_path),
                                     downloaded=expected_size, total=expected_size, sha256_ok=True,
                                     error=None)
                _mark_downloaded(version, final_path, expected_sha, expected_size)
                return
            final_path.unlink(missing_ok=True)

        # 清理历史残留（旧串行方案的 .part 与竞速中断留下的 .partN）
        for p in update_dir.glob(asset["name"] + ".part*"):
            p.unlink(missing_ok=True)

        with _lock:
            _download.update(status="downloading", version=version, url=urls[0],
                             downloaded=0, total=expected_size, path=None,
                             sha256_ok=False, error=None)
        w = _race_download(urls, update_dir, asset["name"], expected_sha, expected_size,
                           _cancel_event)
        if w is None:
            return  # 取消/全败：状态已由 _race_download 写入
        os.replace(update_dir / f"{asset['name']}.part{w}", final_path)
        with _lock:
            _download.update(status="done", version=version, path=str(final_path),
                             downloaded=expected_size, total=expected_size, sha256_ok=True,
                             error=None)
        _mark_downloaded(version, final_path, expected_sha, expected_size)
    except Exception as e:
        logger.error("更新下载失败：%s", e)
        with _lock:
            _download.update(status="failed", path=None, error=str(e))


def start_download(version: str) -> dict:
    """启动下载线程。同版本进行中复用进度；不同版本冲突返回错误。"""
    global _dl_thread, _download, _cancel_event
    with _lock:
        status = _download.get("status")
        cur = _download.get("version")
        if status in ("downloading", "queued") and cur == version:
            return {"started": False, "already_running": True}
        if status in ("downloading", "queued") and cur != version:
            return {"started": False, "error": "已有下载任务进行中"}
        _cancel_event = threading.Event()
        _download = {"status": "queued", "version": version, "url": None,
                     "downloaded": 0, "total": 0, "path": None,
                     "sha256_ok": False, "error": None}
        _dl_thread = threading.Thread(target=_run_download, args=(version,), daemon=True)
        _dl_thread.start()
    return {"started": True}


def progress() -> dict:
    with _lock:
        return dict(_download)


def cancel_download() -> dict:
    with _lock:
        if _cancel_event is not None:
            _cancel_event.set()
    return {"cancelled": True}


# ---------- 应用更新 ----------

def apply() -> dict:
    """应用更新：重校验磁盘安装包 → 停后台代理 → spawn 自身副本启动安装器。

    校验以「下载时已与签名清单比对并持久化的 SHA256」为准，离线也能应用；
    若此时能拉到更新清单再做一次交叉核对（可选增强，拉不到不阻断）。
    安装包定位：优先内存态（刚下载完成），重启后回退到持久化的 downloaded 标记。

    调用方（update 路由的 BackgroundTasks）在响应 flush 后执行 os._exit(0)，
    让 GUI 进程退出并释放 AppMutex，安装器才可干净覆盖。
    """
    with _lock:
        dl = dict(_download)
    state = read_state()
    marker = state.get("downloaded")

    # 定位安装包：内存态 > 持久化标记（覆盖"下载后重启再点立即更新"的场景）
    installer_path = dl.get("path") if dl.get("status") == "done" else None
    version = dl.get("version") if installer_path else None
    expected_sha = expected_size = None
    if isinstance(marker, dict) and marker.get("path"):
        installer_path = installer_path or marker.get("path")
        version = version or marker.get("version")
        expected_sha = marker.get("sha256")
        expected_size = marker.get("size")
    if not installer_path or not version:
        raise ValueError("没有已下载并校验通过的安装包，请先下载更新")
    installer = Path(installer_path)
    if not installer.is_file():
        raise ValueError("安装包文件不存在，请重新下载")

    # 1) 本地复核：以持久化校验值为准（下载时已与签名清单比对，离线可信）
    if expected_sha and _sha256_of(installer).lower() != str(expected_sha).lower():
        raise ValueError("安装包校验失败，请重新下载")
    if expected_size and installer.stat().st_size != int(expected_size):
        raise ValueError("安装包大小不符，请重新下载")

    # 2) 可选交叉核对：能拉到签名清单就再核一次；拉不到不阻断（离线可更新）
    try:
        manifest = fetch_manifest()
        asset = _asset_for_version(manifest, version)
        if _sha256_of(installer) != asset["sha256"].lower():
            raise ValueError("安装包校验失败，请重新下载")
        if installer.stat().st_size != int(asset["size"]):
            raise ValueError("安装包大小不符，请重新下载")
    except ValueError:
        raise
    except Exception as e:
        logger.warning("应用更新时拉取清单失败，按已持久化的校验值继续：%s", e)

    # 停后台翻译代理，避免其持有 _internal 的 DLL 句柄阻塞覆盖安装
    from .proxy_standalone import stop_proxy

    stop_proxy()

    if getattr(sys, "frozen", False):
        argv = [sys.executable, "--apply-update", str(installer)]
    else:
        root = Path(__file__).resolve().parent.parent.parent
        argv = [sys.executable, str(root / "desktop.py"), "--apply-update", str(installer)]

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(argv, creationflags=creationflags, close_fds=True)
    _clear_downloaded_marker()
    logger.info("已启动更新安装器：%s", installer.name)
    return {"applied": True}
