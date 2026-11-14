"""设备页接口（M4/B23/B33/B34 的 a4agent 半边）。

三大块：
- 运行时：本机 node 子进程守护的状态/启停/设置（node_proc）；
- 发现与配对：UDP 51889 发现池（只读线索，不落凭据）+ §23 一次确认配对编排
  （控制端 begin/confirm；被控端本机回环 session/approve/reject）；
- 操作代理：对已配对设备代理 exec/job/screen/put/get/task（协议 §6/§13/§15），
  凭据经 DPAPI 加密存储（store），TOFU 指纹校验（node_client）。

角色口径（B33 最小化）：a4agent 以固定身份名 "a4agent" 代表本机控制端；
任务历史按协议既有 identity 口径过滤（请求方本人），不做权限矩阵。
"""
import logging
import os
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ...database import get_data_dir
from ...devices import node_client, node_proc
from ...devices.pool import BROADCAST_PORT, discovery
from ...devices.store import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices")

CONTROL_IDENTITY_NAME = "a4agent"   # B33：固定身份名（配对 confirm 的 name 参数）


# --------------------------------------------------------------------------
# 错误映射
# --------------------------------------------------------------------------

def _raise_node_error(e: node_client.NodeError):
    """NodeError → HTTPException（detail 带 code，前端原样展示 message）。"""
    status = e.status if isinstance(e.status, int) and e.status >= 400 else 502
    raise HTTPException(status, {"message": e.message, "code": e.code})


# --------------------------------------------------------------------------
# 设备条目与客户端
# --------------------------------------------------------------------------

def _load_entry(name: str) -> dict:
    entry = get_store().get(name)
    if entry is None:
        raise HTTPException(404, {"message": f"设备「{name}」不存在", "code": "NOT_FOUND"})
    return entry


def _client_for(entry: dict) -> node_client.NodeClient:
    token = get_store().token_of(entry["name"])
    if not token:
        raise HTTPException(
            409, {"message": "设备凭据解密失败或为空（可能换机/换用户），请重新配对",
                  "code": "UNAUTHORIZED"})
    return node_client.NodeClient(entry["ip"], entry["port"], token=token,
                                  fingerprint=entry.get("cert_fingerprint") or None)


def _after_call(entry: dict, client: node_client.NodeClient) -> None:
    """成功调用后：首连登记 TOFU 指纹（§4.3.2 TOFU 信任）+ last_seen。"""
    if not entry.get("cert_fingerprint") and client.peer_fingerprint:
        get_store().update_fingerprint(entry["name"], client.peer_fingerprint)
    get_store().touch(entry["name"])


def _call(name: str, fn):
    """带漂移兜底的设备调用：先按存储地址；连不上时用发现池同 device_id
    线索重试一次（成功则更新存储地址，长期自动重连，C4）。"""
    entry = _load_entry(name)
    client = _client_for(entry)
    try:
        result = fn(entry, client)
        _after_call(entry, client)
        return result
    except node_client.NodeError as e:
        if e.code != "UNREACHABLE":
            _raise_node_error(e)
        # 漂移兜底：发现池里同 device_id 的新地址
        device_id = entry.get("device_id") or ""
        hint = discovery.pool.find(device_id) if device_id else None
        if not hint or (hint["ip"] == entry["ip"] and hint["port"] == entry["port"]):
            _raise_node_error(e)
        moved = dict(entry, ip=hint["ip"], port=hint["port"])
        client2 = node_client.NodeClient(
            moved["ip"], moved["port"], token=get_store().token_of(name),
            fingerprint=moved.get("cert_fingerprint") or None)
        try:
            result = fn(moved, client2)
        except node_client.NodeError as e2:
            _raise_node_error(e2)
        get_store().move(name, hint["ip"], hint["port"])
        _after_call(moved, client2)
        return result


# --------------------------------------------------------------------------
# 运行时（本机 node 守护）
# --------------------------------------------------------------------------

@router.get("/runtime")
def runtime_status():
    status = node_proc.get_guardian().status()
    status["listener"] = {"bound": discovery.listener.bound,
                          "port": BROADCAST_PORT,
                          "error": discovery.listener.bind_error}
    return status


@router.post("/runtime/start")
def runtime_start():
    return node_proc.get_guardian().start()


@router.post("/runtime/stop")
def runtime_stop():
    return node_proc.get_guardian().stop()


@router.get("/runtime/config")
def runtime_config():
    return node_proc.resolve_settings(get_data_dir())


@router.put("/runtime/config")
def runtime_save_config(patch: dict):
    unknown = [k for k in patch if k not in node_proc.SETTINGS_KEYS]
    if unknown:
        raise HTTPException(400, {"message": f"未知设置项：{', '.join(unknown)}",
                                  "code": "BAD_REQUEST"})
    return node_proc.save_settings(get_data_dir(), patch)


# --------------------------------------------------------------------------
# 发现池（广播线索 + 手动目标；绝不落凭据，D38）
# --------------------------------------------------------------------------

@router.get("/pool")
def pool_snapshot():
    return discovery.snapshot()


@router.post("/pool/manual")
def pool_manual_add(body: dict):
    ip = str(body.get("ip") or "").strip()
    port = body.get("port") or 51888
    try:
        key = discovery.manual.add(ip, port, label=str(body.get("label") or ""))
    except (ValueError, OSError) as e:
        raise HTTPException(400, {"message": f"地址无效：{e}", "code": "BAD_REQUEST"})
    return {"added": key}


@router.delete("/pool/manual")
def pool_manual_remove(body: dict):
    ip = str(body.get("ip") or "").strip()
    try:
        port = int(body.get("port") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, {"message": "端口需要是整数", "code": "BAD_REQUEST"})
    removed = discovery.manual.remove(ip, port)
    if not removed:
        raise HTTPException(404, {"message": "该手动目标不存在", "code": "NOT_FOUND"})
    return {"removed": True}


# --------------------------------------------------------------------------
# 配对编排（§23）：控制端 begin/confirm；被控端本机回环确认
# --------------------------------------------------------------------------

def _pair_target(body: dict):
    ip = str(body.get("ip") or "").strip()
    try:
        port = int(body.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not ip or not 0 < port < 65536:
        raise HTTPException(400, {"message": "需要有效的 ip 与 port",
                                  "code": "BAD_REQUEST"})
    return ip, port


@router.post("/pair/begin")
def pair_begin(body: dict):
    """控制端发起配对：对被控端 node 调 POST /v1/pair/begin（免鉴权族）。"""
    ip, port = _pair_target(body)
    try:
        return node_client.pair_begin(ip, port)
    except node_client.NodeError as e:
        _raise_node_error(e)


@router.post("/pair/confirm")
def pair_confirm(body: dict):
    """控制端输码确认：POST /v1/pair/confirm → TOFU 校验 → DPAPI 登记。"""
    ip, port = _pair_target(body)
    code = str(body.get("code") or "").strip()
    if not code:
        raise HTTPException(400, {"message": "请输入被控端展示的 6 位配对码",
                                  "code": "BAD_REQUEST"})
    # B33：固定身份名代表本机控制端（node 侧冲突时自动后缀）
    name = CONTROL_IDENTITY_NAME
    try:
        data = node_client.pair_confirm(ip, port, code, name)
    except node_client.NodeError as e:
        _raise_node_error(e)
    device_id = data.get("device_id") or ""
    hint = discovery.pool.find(device_id) if device_id else None
    hostname = (hint or {}).get("hostname", "")
    entry = get_store().register(
        name=str(body.get("display_name") or "").strip() or None,
        device_id=device_id, ip=ip, port=port,
        identity=data.get("identity") or "default",
        token=data.get("token") or "",
        cert_fingerprint=data.get("cert_fingerprint") or "",
        hostname=hostname, info={"node_version": data.get("node_version", "")})
    return entry


@router.get("/pair/incoming")
def pair_incoming():
    """被控端确认数据源：查本机 node 的 /v1/pair/session（回环，内存 TOFU）。

    node 未运行或不可达时返回 state=idle + available=false（前端据此跳过弹窗）。
    """
    guardian = node_proc.get_guardian()
    port = guardian.read_port()
    if guardian.state not in ("starting", "running") or not port:
        return {"available": False, "state": "idle"}
    try:
        data, _fp = node_client.pair_request(
            "127.0.0.1", port, "GET", "/v1/pair/session", loopback_tofu=True)
    except node_client.NodeError as e:
        return {"available": False, "state": "idle", "error": e.message}
    data["available"] = True
    return data


@router.post("/pair/approve")
def pair_approve():
    """被控端本地确认：POST /v1/pair/approve（回环）→ 返回 6 位码与时效。"""
    guardian = node_proc.get_guardian()
    port = guardian.read_port()
    if not port:
        raise HTTPException(409, {"message": "本机 node 未运行，无法确认配对",
                                  "code": "BAD_REQUEST"})
    try:
        data, _fp = node_client.pair_request(
            "127.0.0.1", port, "POST", "/v1/pair/approve", body={},
            loopback_tofu=True)
    except node_client.NodeError as e:
        _raise_node_error(e)
    return data


@router.post("/pair/reject")
def pair_reject():
    """被控端拒绝：POST /v1/pair/reject（回环）→ 会话作废，连接不建立。"""
    guardian = node_proc.get_guardian()
    port = guardian.read_port()
    if not port:
        raise HTTPException(409, {"message": "本机 node 未运行，无法处理配对",
                                  "code": "BAD_REQUEST"})
    try:
        data, _fp = node_client.pair_request(
            "127.0.0.1", port, "POST", "/v1/pair/reject", body={},
            loopback_tofu=True)
    except node_client.NodeError as e:
        _raise_node_error(e)
    return data


@router.post("/pair/reset-loop-fingerprint")
def pair_reset_loop_fp():
    """重置本机 node 回环指纹记忆（node 重装/换证书后重连用）。"""
    guardian = node_proc.get_guardian()
    node_client.reset_loopback_fingerprint(guardian.read_port()
                                           or node_proc.DEFAULT_PORT)
    return {"reset": True}


# --------------------------------------------------------------------------
# 已配对设备列表与管理
# --------------------------------------------------------------------------

@router.get("")
def device_list():
    return {"devices": get_store().list()}


@router.post("/{name}/forget")
def device_forget(name: str):
    if not get_store().remove(name):
        raise HTTPException(404, {"message": f"设备「{name}」不存在",
                                  "code": "NOT_FOUND"})
    return {"removed": True}


@router.post("/{name}/trust/reset")
def device_trust_reset(name: str):
    """重置 TOFU 指纹（等价 xd --trust 场景：设备重装/换证书后重连）。"""
    _load_entry(name)
    get_store().update_fingerprint(name, "")
    return {"reset": True}


@router.post("/{name}/ping")
def device_ping(name: str):
    """探活 + 版本信息（/v1/health）。"""
    def _go(entry, client):
        return client.request("GET", "/v1/health", timeout=10)
    try:
        data = _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
    return data


# --------------------------------------------------------------------------
# 操作代理：exec / job / screen / put / get / tasks
# --------------------------------------------------------------------------

@router.post("/{name}/exec")
def device_exec(name: str, body: dict):
    cmd = str(body.get("cmd") or "").strip()
    if not cmd:
        raise HTTPException(400, {"message": "命令不能为空", "code": "BAD_REQUEST"})
    payload = {"cmd": cmd,
               "shell": body.get("shell") or "cmd",
               "timeout": int(body.get("timeout") or 60)}
    if body.get("cwd"):
        payload["cwd"] = str(body["cwd"])

    def _go(entry, client):
        return client.request("POST", "/v1/exec", body=payload, timeout=30)
    try:
        return _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)


@router.get("/{name}/job/{job_id}")
def device_job(name: str, job_id: str, wait: int = Query(0, ge=0, le=120)):
    def _go(entry, client):
        return client.request("GET", f"/v1/job/{job_id}?wait={int(wait)}",
                              timeout=max(30, int(wait) + 30))
    try:
        return _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)


@router.get("/{name}/screen")
def device_screen(name: str, monitor: int = -1, max_width: int = 0):
    def _go(entry, client):
        return client.screen(monitor=monitor, max_width=max_width)
    try:
        png = _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@router.post("/{name}/screen/save")
def device_screen_save(name: str, body: dict):
    """截屏并保存到本地目录（截屏"下载保存"的最小实现）。"""
    save_dir = str(body.get("save_dir") or "").strip()
    folder = Path(save_dir).expanduser() if save_dir else None
    if not folder or not folder.is_dir():
        raise HTTPException(400, {"message": "请先选择保存目录", "code": "BAD_REQUEST"})
    monitor = int(body.get("monitor", -1))
    max_width = int(body.get("max_width", 0))

    def _go(entry, client):
        return client.screen(monitor=monitor, max_width=max_width)
    try:
        png = _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
    dest = folder / f"screen-{name}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    dest.write_bytes(png)
    return {"saved_path": str(dest), "size": len(png)}


@router.post("/{name}/put")
async def device_put(name: str, file: UploadFile = File(...),
                     remote_path: str = Form(...)):
    """上传文件：multipart 流式落临时文件 → §13 分块上传（不整载内存）。"""
    if not remote_path.strip():
        raise HTTPException(400, {"message": "请填写远端目标路径",
                                  "code": "BAD_REQUEST"})
    tmp_dir = get_data_dir() / "device_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=tmp_dir, suffix=".upload")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
        if size == 0:
            raise HTTPException(400, {"message": "不能上传空文件", "code": "BAD_REQUEST"})

        def _go(entry, client):
            return client.upload(tmp_name, remote_path.strip())
        try:
            data = _call(name, _go)
        except node_client.NodeError as e:
            _raise_node_error(e)
        return data
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


@router.get("/{name}/get")
def device_get(name: str, path: str, save_dir: str, fresh: bool = False):
    """下载远端文件：§13.2 流式下载（断点续拉 + MD5 校验）保存到本地目录。"""
    if not path.strip():
        raise HTTPException(400, {"message": "请填写远端文件路径",
                                  "code": "BAD_REQUEST"})
    folder = Path(save_dir).expanduser()
    if not save_dir.strip() or not folder.is_dir():
        raise HTTPException(400, {"message": "请先选择保存目录", "code": "BAD_REQUEST"})
    basename = Path(path.replace("\\", "/")).name or "download.bin"
    dest = folder / basename

    def _go(entry, client):
        return client.download(path.strip(), str(dest), fresh=fresh)
    try:
        data = _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
    data["saved_path"] = str(dest)
    return data


@router.get("/{name}/tasks")
def device_tasks(name: str, status: str = "", limit: int = Query(50, ge=1, le=200),
                 offset: int = Query(0, ge=0)):
    """任务历史（协议 §15.2 identity 口径：不传 identity = 按请求者身份过滤）。"""
    qs = f"/v1/task/list?limit={int(limit)}&offset={int(offset)}"
    if status:
        qs += f"&status={status}"

    def _go(entry, client):
        return client.request("GET", qs, timeout=30)
    try:
        return _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)


@router.get("/{name}/tasks/{task_id}")
def device_task_detail(name: str, task_id: str):
    def _go(entry, client):
        return client.request("GET", f"/v1/task/{task_id}", timeout=30)
    try:
        return _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
