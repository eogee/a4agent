"""node 协议客户端测试（B23/E-03）：包络解析、错误码映射、TOFU、防重放头、
分块上传/下载（配假 node HTTP 服务；TOFU 另有真 TLS 用例）。"""
import base64
import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.app.devices import node_client
from backend.app.devices.node_client import (NodeClient, NodeError,
                                             TofuError, _read_envelope)

FAKE_FP = "a" * 64
TOKEN = "cd" * 32


# ---------------- 假 node（明文 HTTP，_open_tls 打桩为明文连接） ----------------

class FakeNodeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):     # 静默
        pass

    # --- 断言辅助 ---
    def record(self, extra=None):
        item = {"method": self.command, "path": self.path,
                "headers": dict(self.headers)}
        if extra:
            item.update(extra)
        type(self).requests.append(item)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def send_envelope(self, data, status=200):
        payload = json.dumps({"ok": True, "data": data}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_envelope(self, code, message, status):
        payload = json.dumps(
            {"ok": False, "error": {"code": code, "message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        type(self).requests.append({"method": self.command, "path": self.path,
                                    "headers": dict(self.headers)})
        if self.path.startswith("/v1/health"):
            if self.headers.get("X-Auth-Token") != TOKEN:
                return self.send_error_envelope("UNAUTHORIZED", "令牌不匹配", 401)
            return self.send_envelope({"status": "up", "node_version": "0.5.0"})
        if self.path.startswith("/v1/screen"):
            if not self.headers.get("X-Auth-Token"):
                return self.send_error_envelope("UNAUTHORIZED", "缺令牌", 401)
            png = b"\x89PNG-fake-screen"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self.wfile.write(png)
            return
        if self.path.startswith("/v1/fs/download"):
            if not self.headers.get("X-Auth-Token"):
                return self.send_error_envelope("UNAUTHORIZED", "缺令牌", 401)
            data = type(self).download_payload
            rng = self.headers.get("Range")
            self.send_response(206 if rng else 200)
            total = len(data)
            if rng:
                start = int(rng.split("bytes=")[1].split("-")[0])
                chunk = data[start:]
                self.send_header("Content-Range", f"bytes {start}-{total-1}/{total}")
            else:
                chunk = data
            self.send_header("X-File-Size", str(total))
            self.send_header("X-File-MD5", hashlib.md5(data).hexdigest())
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)
            return
        if self.path.startswith("/v1/task/list"):
            return self.send_envelope({"entries": [], "total": 0})
        return self.send_error_envelope("NOT_FOUND", "未知接口", 404)

    def do_POST(self):
        type(self).requests.append({"method": self.command, "path": self.path,
                                    "headers": dict(self.headers)})
        body = self.body_json()
        if self.path == "/v1/pair/begin":
            assert not self.headers.get("X-Auth-Token")     # pair 族免令牌
            assert not self.headers.get("X-Timestamp")      # pair 族免防重放头
            return self.send_envelope(
                {"session_id": "s1", "state": "pending", "expires_at": 123})
        if self.path == "/v1/pair/confirm":
            if body.get("code") != "123456":
                return self.send_error_envelope(
                    "PAIR_CODE_MISMATCH", "配对码不匹配，剩余可尝试次数：4", 401)
            return self.send_envelope(
                {"token": "ee" * 32, "identity": "a4agent",
                 "device_id": "a1b2c3d4e5f6",
                 "cert_fingerprint": type(self).confirm_fp, "node_version": "0.5.0"})
        if self.path == "/v1/exec":
            return self.send_envelope({"job_id": "job0001aaaa"})
        if self.path == "/v1/fs/upload/begin":
            type(self).upload = {"data": b"", "size": body["size"]}
            return self.send_envelope({"upload_id": "u1", "chunk_size": 4,
                                       "resume_offset": 0})
        if self.path == "/v1/fs/upload/chunk":
            raw = base64.b64decode(body["data_b64"])
            assert body["offset"] == len(type(self).upload["data"])
            type(self).upload["data"] += raw
            return self.send_envelope({"received": len(type(self).upload["data"]),
                                       "next_offset": len(type(self).upload["data"])})
        if self.path == "/v1/fs/upload/commit":
            got = type(self).upload["data"]
            return self.send_envelope({"path": "/tmp/x", "size": len(got),
                                       "md5": hashlib.md5(got).hexdigest()})
        return self.send_error_envelope("NOT_FOUND", "未知接口", 404)


@pytest.fixture()
def fake_node(monkeypatch):
    """起一个假 node（明文 HTTP），并把 _open_tls 打桩成明文连接 + 固定指纹。"""
    FakeNodeHandler.requests = []
    FakeNodeHandler.confirm_fp = FAKE_FP
    FakeNodeHandler.download_payload = b"0123456789abcdef"
    FakeNodeHandler.upload = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeNodeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    real_open = node_client._open_tls

    def fake_open_tls(host, port, timeout, expected_fp=None):
        if expected_fp and expected_fp.lower() != FAKE_FP:
            raise TofuError(f"TOFU 证书指纹变更：期望 {expected_fp[:16]}…")
        sock = socket.create_connection((host, port), timeout=timeout)
        return sock, FAKE_FP

    monkeypatch.setattr(node_client, "_open_tls", fake_open_tls)
    yield {"port": server.server_address[1], "handler": FakeNodeHandler,
           "real_open": real_open}
    server.shutdown()
    server.server_close()
    monkeypatch.setattr(node_client, "_open_tls", real_open)


def make_client(port, **kw):
    return NodeClient("127.0.0.1", port, token=TOKEN, fingerprint=FAKE_FP, **kw)


# ---------------- 包络 / 鉴权头 / 错误映射 ----------------

def test_request_sends_auth_and_replay_headers(fake_node):
    port = fake_node["port"]
    make_client(port).request("GET", "/v1/health")
    req = fake_node["handler"].requests[-1]
    assert req["headers"]["X-Auth-Token"] == TOKEN
    assert req["headers"]["X-Nonce"]
    assert req["headers"]["X-Timestamp"]
    # nonce 每请求独立（§3.3）
    make_client(port).request("GET", "/v1/health")
    nonces = {r["headers"].get("X-Nonce")
              for r in fake_node["handler"].requests if r["path"] == "/v1/health"}
    assert len(nonces) == 2


def test_request_missing_replay_headers_rejected_by_node(fake_node):
    """防重放头缺失 → node 按 §3.2 拒绝（假 node 以 401 代拟，只验包络通路）。"""
    port = fake_node["port"]
    client = make_client(port)
    client.token = ""
    with pytest.raises(NodeError) as ei:
        client.request("GET", "/v1/health")
    assert ei.value.status == 401
    assert ei.value.code == "UNAUTHORIZED"


def test_error_envelope_maps_code_and_message(fake_node):
    port = fake_node["port"]

    class _H(FakeNodeHandler):
        pass
    # 借 confirm 的 401 路径验证错误映射
    with pytest.raises(NodeError) as ei:
        node_client.pair_confirm("127.0.0.1", port, "000000", "a4agent")
    assert ei.value.code == "PAIR_CODE_MISMATCH"
    assert "剩余可尝试次数" in ei.value.message
    assert ei.value.status == 401


def test_screen_returns_png_bytes(fake_node):
    port = fake_node["port"]
    png = make_client(port).screen()
    assert png.startswith(b"\x89PNG")


def test_unreachable_maps_to_node_error(monkeypatch):
    monkeypatch.setattr(node_client, "_open_tls",
                        lambda *a, **k: (_ for _ in ()).throw(
                            NodeError("UNREACHABLE", "无法连接")))
    client = NodeClient("127.0.0.1", 1, token=TOKEN)
    with pytest.raises(NodeError) as ei:
        client.request("GET", "/v1/health")
    assert ei.value.code == "UNREACHABLE"


# ---------------- 配对族 ----------------

def test_pair_begin_no_auth_headers(fake_node):
    port = fake_node["port"]
    data = node_client.pair_begin("127.0.0.1", port)
    assert data["state"] == "pending"
    req = fake_node["handler"].requests[-1]
    assert req["headers"].get("X-Auth-Token") is None
    assert req["headers"].get("X-Timestamp") is None


def test_pair_confirm_fingerprint_consistency(fake_node):
    port = fake_node["port"]
    data = node_client.pair_confirm("127.0.0.1", port, "123456", "a4agent")
    assert data["token"] == "ee" * 32
    assert data["cert_fingerprint"] == FAKE_FP


def test_pair_confirm_aborts_on_fingerprint_mismatch(fake_node):
    port = fake_node["port"]
    fake_node["handler"].confirm_fp = "b" * 64   # 响应指纹与实际连接不符
    with pytest.raises(TofuError):
        node_client.pair_confirm("127.0.0.1", port, "123456", "a4agent")


# ---------------- 分块传输（§13） ----------------

def test_download_streams_and_verifies(fake_node, tmp_path):
    port = fake_node["port"]
    dest = tmp_path / "down.bin"
    out = make_client(port).download("/v1/whatever", str(dest))
    assert dest.read_bytes() == b"0123456789abcdef"
    assert out["size"] == 16
    assert out["md5"] == hashlib.md5(b"0123456789abcdef").hexdigest()
    assert not dest.with_suffix(dest.suffix + ".crosspart").exists()


def test_download_resume_from_breakpoint(fake_node, tmp_path):
    port = fake_node["port"]
    dest = tmp_path / "down.bin"
    bp = tmp_path / "down.bin.crosspart"
    bp.write_bytes(b"01234567")     # 前半已收
    out = make_client(port).download("/v1/whatever", str(dest))
    assert dest.read_bytes() == b"0123456789abcdef"
    reqs = [r for r in fake_node["handler"].requests
            if r["path"].startswith("/v1/fs/download")]
    assert reqs[-1]["headers"].get("Range") == "bytes=8-"
    assert out["size"] == 16


def test_upload_chunks_in_order(fake_node, tmp_path):
    port = fake_node["port"]
    src = tmp_path / "up.bin"
    src.write_bytes(b"abcdefgh")    # chunk_size=4 → 两块
    out = make_client(port).upload(str(src), "/tmp/x")
    assert out["size"] == 8
    assert out["md5"] == hashlib.md5(b"abcdefgh").hexdigest()
    assert fake_node["handler"].upload["data"] == b"abcdefgh"


# ---------------- 真 TLS 的 TOFU（临时自签证书，发完即清） ----------------

def test_open_tls_real_tofu(tmp_path):
    """真 TLS 连接：指纹口径（DER SHA-256 小写）与 TOFU 拒绝路径。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    import ssl

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "node-test")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    cert_pem = tmp_path / "cert.pem"
    key_pem = tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    der = cert.public_bytes(serialization.Encoding.DER)
    expect_fp = hashlib.sha256(der).hexdigest()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_pem), str(key_pem))
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        try:
            conn, _ = server.accept()
            tls = ctx.wrap_socket(conn, server_side=True)
            tls.recv(1024)
            tls.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
            tls.close()
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        # 1) 未登记指纹 → 信任并返回实际指纹
        tls, actual = node_client._open_tls("127.0.0.1", port, 5, None)
        assert actual == expect_fp
        tls.close()
        # 2) 已登记一致 → 通过
        threading.Thread(target=serve, daemon=True).start()
        tls, actual = node_client._open_tls("127.0.0.1", port, 5, expect_fp)
        tls.close()
        # 3) 已登记不一致 → 本地拒绝（TofuError，文案以 TOFU 开头）
        threading.Thread(target=serve, daemon=True).start()
        with pytest.raises(TofuError) as ei:
            node_client._open_tls("127.0.0.1", port, 5, "f" * 64)
        assert ei.value.message.startswith("TOFU")
    finally:
        server.close()


def test_read_envelope_rejects_non_envelope():
    class R:
        status = 200
        def read(self):
            return b"<html>oops</html>"
    with pytest.raises(NodeError) as ei:
        _read_envelope(R())
    assert ei.value.code == "BAD_RESPONSE"
