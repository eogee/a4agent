"""node HTTPS 客户端：TOFU 指纹校验 + 防重放头 + 响应包络解析 + 分块传输。

a4agent 与被控端 node 只通过 /v1/* 协议交互（E-03 唯一耦合面）；HTTPS 自签
证书按 TOFU 校验（§4.3）：条目已有指纹且对端不一致 → 本地拒绝（不发送任何
业务数据），错误文案以 "TOFU" 开头；条目无指纹 → 首连信任，由调用方登记。

防重放头（§3）：除 /v1/pair/* 外每个 /v1/* 请求都带 X-Timestamp / X-Nonce，
每个请求生成新 nonce。大文件 put/get 走 §13 分块协议，全程不整文件载入内存。
"""
import base64
import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time
import uuid
from urllib.parse import quote

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK = 1024 * 1024        # 下载写盘缓冲（§13.2 的 64KB–1MB 区间）
DEFAULT_TIMEOUT = 30.0

# 回环端点（/v1/pair/session|approve|reject）的 TOFU 记忆（内存，重启即重新信任；
# 回环对端是本机 node，首次信任后指纹变更视为异常）
_loopback_fps: dict = {}
_loopback_lock = threading.Lock()


class NodeError(Exception):
    """node 协议错误或连接失败。code 为契约 §5 错误码（客户端侧失败用
    UNREACHABLE / BAD_RESPONSE / TOFU_MISMATCH），status 为 HTTP 状态码。"""

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class TofuError(NodeError):
    """证书指纹与登记值不一致（§4.3.2：本地拒绝，不经 HTTP）。"""

    def __init__(self, message):
        super().__init__("TOFU_MISMATCH", message, status=None)


def _peer_fingerprint(tls_sock) -> str:
    """对端证书 DER 的 SHA-256，小写 64hex（契约 §4.1 口径）。"""
    der = tls_sock.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest()


def _open_tls(host, port, timeout, expected_fp=None):
    """建立 TLS 连接并按 TOFU 校验对端指纹。

    返回 (tls socket, 实际指纹)。expected_fp 非空且不一致 → 关闭连接并抛
    TofuError（发送任何业务数据之前，§4.3.2）。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE      # 信任由 TOFU 指纹承担（§4）
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
        tls = ctx.wrap_socket(raw, server_hostname=host)
    except (OSError, ssl.SSLError) as e:
        raise NodeError("UNREACHABLE", f"无法连接 {host}:{port}（{e}）") from e
    try:
        actual = _peer_fingerprint(tls)
    except (OSError, ssl.SSLError) as e:
        tls.close()
        raise NodeError("UNREACHABLE", f"TLS 握手失败（{e}）") from e
    if expected_fp:
        expected = expected_fp.lower()
        if actual != expected:
            tls.close()
            raise TofuError(
                f"TOFU 证书指纹变更：期望 {expected[:16]}…，实际 {actual[:16]}…。"
                f"若确为该设备重装/换证书，可在设备页重置指纹后重连")
    return tls, actual


def _http_over(tls_sock, host, port, timeout):
    """在既有 TLS socket 上跑 http.client（单请求一连接，用后即关）。"""
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.sock = tls_sock
    return conn


def _read_envelope(resp):
    """解析响应包络（§1.3）。成功返回 data；失败抛 NodeError。"""
    body = resp.read()
    try:
        doc = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise NodeError("BAD_RESPONSE",
                        f"响应不是有效包络（HTTP {resp.status}）", resp.status)
    if not isinstance(doc, dict):
        raise NodeError("BAD_RESPONSE",
                        f"响应不是有效包络（HTTP {resp.status}）", resp.status)
    if doc.get("ok") is True:
        return doc.get("data")
    err = doc.get("error") or {}
    raise NodeError(str(err.get("code", "INTERNAL")),
                    str(err.get("message", "未知错误")), resp.status)


def _md5_of_file(path) -> "hashlib._Hash":
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(DOWNLOAD_CHUNK)
            if not block:
                break
            md5.update(block)
    return md5


class NodeClient:
    """单设备的 node 协议客户端（每次调用新建 TLS 连接，TOFU 指纹校验）。

    fingerprint 为设备条目登记的 TOFU 指纹（None = 未登记，首连信任）；
    调用后 peer_fingerprint 属性为本轮实际对端指纹，供调用方登记。
    """

    def __init__(self, ip, port, token="", fingerprint=None):
        self.ip = str(ip)
        self.port = int(port)
        self.token = token or ""
        self.fingerprint = (fingerprint or "").lower() or None
        self.peer_fingerprint = None

    # ---------- 基础请求 ----------

    def _headers(self, with_auth=True, extra=None):
        headers = dict(extra or {})
        if with_auth:
            if not self.token:
                raise NodeError("UNAUTHORIZED", "设备尚未配对（无令牌）", 401)
            headers["X-Auth-Token"] = self.token
            # 防重放头（§3）：每个请求新 nonce + 当前时间戳
            headers["X-Timestamp"] = repr(time.time())
            headers["X-Nonce"] = uuid.uuid4().hex
        return headers

    def request(self, method, path, body=None, timeout=DEFAULT_TIMEOUT,
                with_auth=True):
        """JSON 业务请求：返回包络 data；失败抛 NodeError。"""
        payload = None
        headers = self._headers(with_auth=with_auth)
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        tls, actual = _open_tls(self.ip, self.port, timeout, self.fingerprint)
        self.peer_fingerprint = actual
        try:
            conn = _http_over(tls, self.ip, self.port, timeout)
            try:
                conn.request(method, path, body=payload, headers=headers)
                resp = conn.getresponse()
                return _read_envelope(resp)
            except NodeError:
                raise
            except (http.client.HTTPException, OSError, socket.timeout) as e:
                raise NodeError("UNREACHABLE", f"请求 {path} 失败（{e}）") from e
        finally:
            try:
                tls.close()
            except OSError:
                pass

    # ---------- 二进制端点（§6.6 screen / §13.2 download） ----------

    def screen(self, monitor=-1, max_width=0, timeout=60) -> bytes:
        """截屏：成功返回 PNG 原始字节；失败抛 NodeError。"""
        path = f"/v1/screen?monitor={int(monitor)}&max_width={int(max_width)}"
        headers = self._headers()
        tls, actual = _open_tls(self.ip, self.port, timeout, self.fingerprint)
        self.peer_fingerprint = actual
        try:
            conn = _http_over(tls, self.ip, self.port, timeout)
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                ctype = resp.getheader("Content-Type", "")
                if resp.status == 200 and "image/png" in ctype:
                    return resp.read()
                _read_envelope(resp)   # 非 PNG 一律是包络错误
                raise NodeError("BAD_RESPONSE", "截屏响应缺少 PNG 内容", 200)
            except NodeError:
                raise
            except (http.client.HTTPException, OSError, socket.timeout) as e:
                raise NodeError("UNREACHABLE", f"截屏失败（{e}）") from e
        finally:
            try:
                tls.close()
            except OSError:
                pass

    def download(self, remote_path, local_path, fresh=False, progress=None,
                 timeout=600) -> dict:
        """流式下载（§13.2/§18.1）：1MB 循环拉取 + 断点续拉 + MD5 校验。

        断点文件 <local>.crosspart：存在且短于总长 → 从该偏移以 Range 续拉；
        校验一致后 os.replace 为目标文件；不一致保留断点文件可重试
        （fresh=True 先丢弃断点重新下载）。返回 {path, size, md5}。
        """
        local_path = os.path.abspath(local_path)
        bp_path = local_path + ".crosspart"
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        if fresh and os.path.exists(bp_path):
            try:
                os.unlink(bp_path)
            except OSError:
                pass
        range_offset = os.path.getsize(bp_path) if os.path.exists(bp_path) else 0
        qpath = quote(remote_path)
        range_header = f"bytes={range_offset}-" if range_offset > 0 else None

        tls, actual = _open_tls(self.ip, self.port, timeout, self.fingerprint)
        self.peer_fingerprint = actual
        conn = None
        try:
            conn = _http_over(tls, self.ip, self.port, timeout)
            try:
                headers = self._headers(
                    extra={"Range": range_header} if range_header else None)
                conn.request("GET", f"/v1/fs/download?path={qpath}",
                             headers=headers)
                resp = conn.getresponse()
                if resp.status == 400 and range_header:
                    # 断点 ≥ 总长（异常残留）→ 丢弃断点重来（§18.1 语义）；
                    # 先收掉当前连接再递归，避免旧连接悬挂到整段下载结束
                    try:
                        os.unlink(bp_path)
                    except OSError:
                        pass
                    conn.close()
                    conn = None
                    return self.download(remote_path, local_path, fresh=True,
                                         progress=progress, timeout=timeout)
                if resp.status not in (200, 206):
                    _read_envelope(resp)   # 抛包络错误
                    raise NodeError("BAD_RESPONSE", "下载响应异常", resp.status)
                total = int(resp.getheader("X-File-Size", "0") or 0)
                expect_md5 = (resp.getheader("X-File-MD5", "") or "").lower()
                if resp.status == 206:
                    if not os.path.exists(bp_path):
                        raise NodeError("BAD_RESPONSE", "断点文件缺失，请重试",
                                        resp.status)
                    md5 = _md5_of_file(bp_path)     # 已收前缀的 MD5 基线
                    received = range_offset
                    out = open(bp_path, "ab")
                else:
                    md5 = hashlib.md5()
                    received = 0
                    out = open(bp_path, "wb")
                try:
                    while True:
                        chunk = resp.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        md5.update(chunk)
                        received += len(chunk)
                        if progress:
                            progress(received, total)
                    out.flush()
                finally:
                    out.close()
                actual_md5 = md5.hexdigest().lower()
                if total and received != total:
                    raise NodeError(
                        "BAD_RESPONSE",
                        f"下载不完整（已收 {received}/{total}），断点已保留可重试",
                        resp.status)
                if expect_md5 and actual_md5 != expect_md5:
                    raise NodeError(
                        "BAD_RESPONSE",
                        "下载校验失败（MD5 与服务端不一致），断点已保留可重试",
                        resp.status)
                os.replace(bp_path, local_path)
                return {"path": remote_path, "size": received,
                        "md5": actual_md5}
            except NodeError:
                raise
            except (http.client.HTTPException, OSError, socket.timeout) as e:
                raise NodeError("UNREACHABLE", f"下载失败（{e}）") from e
        finally:
            if conn is not None:
                try:
                    conn.close()   # 连同底层 TLS socket 一并关闭
                except OSError:
                    pass

    # ---------- 分块上传（§13.3 三段式） ----------

    def upload(self, local_path, remote_path, progress=None,
               timeout=600) -> dict:
        """流式上传：begin → 分块 chunk（断点续传）→ commit。

        返回 commit 的 {path, size, md5}。全程不整文件载入内存。
        """
        local_path = os.path.abspath(local_path)
        size = os.path.getsize(local_path)
        md5 = _md5_of_file(local_path).hexdigest().lower()
        begin = self.request("POST", "/v1/fs/upload/begin",
                             body={"path": remote_path, "size": size, "md5": md5},
                             timeout=timeout)
        upload_id = begin.get("upload_id")
        chunk_size = int(begin.get("chunk_size", 4194304))
        sent = int(begin.get("resume_offset", 0) or 0)
        if not upload_id:
            raise NodeError("BAD_RESPONSE", "上传会话创建失败", 200)
        with open(local_path, "rb") as f:
            if sent:
                f.seek(sent)
            offset = sent
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                body = {"upload_id": upload_id, "offset": offset,
                        "data_b64": base64.b64encode(chunk).decode("ascii")}
                self.request("POST", "/v1/fs/upload/chunk", body=body,
                             timeout=timeout)
                offset += len(chunk)
                if progress:
                    progress(offset, size)
        if offset != size:
            # 本地读取与声明不符：放弃会话，不让 commit 报"块未收全"
            try:
                self.request("POST", "/v1/fs/upload/abort",
                             body={"upload_id": upload_id}, timeout=timeout)
            except NodeError:
                pass
            raise NodeError("BAD_RESPONSE", "本地文件读取不完整，已放弃上传", 0)
        return self.request("POST", "/v1/fs/upload/commit",
                            body={"upload_id": upload_id}, timeout=timeout)


# --------------------------------------------------------------------------
# 配对端点族（§23：免鉴权 + 免防重放头；guard 变体在 node 侧）
# --------------------------------------------------------------------------

def pair_request(ip, port, method, path, body=None, timeout=15,
                 expected_fp=None, loopback_tofu=False):
    """调 /v1/pair/* 端点（无令牌、无防重放头）。

    返回 (data, 实际指纹)。loopback_tofu=True（本机被控端回环确认）用内存
    TOFU 记忆：首连记录指纹，之后不一致报错。
    """
    if loopback_tofu:
        with _loopback_lock:
            expected_fp = _loopback_fps.get((ip, port))
    tls, actual = _open_tls(ip, port, timeout, expected_fp)
    if loopback_tofu:
        with _loopback_lock:
            if _loopback_fps.get((ip, port)) is None:
                _loopback_fps[(ip, port)] = actual
    try:
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn = _http_over(tls, ip, port, timeout)
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            data = _read_envelope(resp)
            return data, actual
        except NodeError:
            raise
        except (http.client.HTTPException, OSError, socket.timeout) as e:
            raise NodeError("UNREACHABLE", f"请求 {path} 失败（{e}）") from e
    finally:
        try:
            tls.close()
        except OSError:
            pass


def pair_begin(ip, port, timeout=15):
    """POST /v1/pair/begin。返回 {session_id, state, expires_at}。"""
    data, _fp = pair_request(ip, port, "POST", "/v1/pair/begin", body={},
                             timeout=timeout)
    return data


def pair_confirm(ip, port, code, name, timeout=15):
    """POST /v1/pair/confirm（新连接），并做配对确认指纹一致性校验（§23.3.7）。

    成功返回 {token, identity, device_id, cert_fingerprint, node_version}；
    响应 cert_fingerprint 与本次连接实际对端指纹不一致视为中间异常，
    放弃登记并抛 TofuError。
    """
    data, actual = pair_request(ip, port, "POST", "/v1/pair/confirm",
                                body={"code": str(code), "name": str(name)},
                                timeout=timeout)
    claimed = str(data.get("cert_fingerprint", "")).lower()
    if not claimed or claimed != actual:
        raise TofuError(
            "TOFU 配对确认指纹与实际连接不一致，已放弃登记（请重试配对）")
    return data


def reset_loopback_fingerprint(port):
    """重置本机 node 的回环指纹记忆（node 重装/换证书后重连用）。"""
    with _loopback_lock:
        for key in [k for k in _loopback_fps if k[1] == port]:
            del _loopback_fps[key]
