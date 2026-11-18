"""设备页接口（M4/B23/B33/B34 的 a4agent 半边）。

三大块：
- 运行时：本机 node 子进程守护的状态/启停/设置（node_proc）；
- 发现与配对：UDP 51889 发现池（只读线索，不落凭据）+ §23 一次确认配对编排
  （控制端 begin/confirm；被控端本机回环 session/approve/reject）；
- 操作代理：对已配对设备代理 exec/job/screen/put/get/task（协议 §6/§13/§15），
  凭据经 DPAPI 加密存储（store），TOFU 指纹校验（node_client）。

角色口径（B33 最小化）：a4agent 以固定身份名 "a4agent" 代表本机控制端；
任务历史按协议既有 identity 口径过滤（请求方本人），不做权限矩阵。

Agent 开放交互（B35/FR-16）：聚合发现端点 /overview（动态设备全景）+
内置技能包 /install-skill 三档安装（全局/项目根/自定义）——Agent 经技能
发现能力后只操作已配对设备，凭据不出 backend，审计可追溯。
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from ... import skill_manager
from ...database import get_data_dir
from ...devices import node_client, node_proc, status_store, status_watcher
from ...devices.pair_watcher import TOAST_TITLE
from ...devices.pool import BROADCAST_PORT, discovery
from ...devices.store import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/devices")

CONTROL_IDENTITY_NAME = "a4agent"   # B33：固定身份名（配对 confirm 的 name 参数）

SKILL_NAME = "crossdevice"          # B35：内置技能包目录名


# --------------------------------------------------------------------------
# B35：内置技能包定位
# --------------------------------------------------------------------------

def _skill_bundle_src() -> Path:
    """内置技能包源目录（含 SKILL.md）：打包态=_internal/resources/skills/，
    开发态=仓库 resources/skills/（build.py 已 --add-data resources;resources）。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", ""))
    else:
        base = Path(__file__).resolve().parents[4]      # api/v1/devices.py → 仓根
    return base / "resources" / "skills" / SKILL_NAME


def _backend_port() -> int | None:
    """backend 自身监听端口（desktop_recovery 实例文件，供 overview 下发）。"""
    try:
        from ... import desktop_recovery

        doc = json.loads(Path(desktop_recovery.instance_file()).read_text(encoding="utf-8"))
        return int(doc.get("port")) if doc.get("port") else None
    except Exception:  # noqa: BLE001 - 实例文件缺失/损坏时 overview 仍可返回设备
        return None


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
    status["instances"] = node_proc.get_guardian().instances_status()
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


def _ensure_instance_name(name: str) -> None:
    """实例名与 node.py --instance 同规（§16.2）：非法名直接 400。"""
    if not node_proc.INSTANCE_NAME_RE.fullmatch(name or ""):
        raise HTTPException(
            400, {"message": f"实例名非法：{name}（限 [A-Za-z0-9_-]{{1,32}}）",
                  "code": "BAD_REQUEST"})


@router.post("/runtime/instances/{name}/start")
def runtime_instance_start(name: str):
    """拉起具名实例（B40）：清单内才允许（否则 404），前置校验失败置 failed。"""
    _ensure_instance_name(name)
    try:
        return node_proc.get_guardian().start_instance(name)
    except KeyError:
        raise HTTPException(404, {"message": f"实例「{name}」不在运行时清单中",
                                  "code": "NOT_FOUND"})
    except ValueError as e:
        raise HTTPException(400, {"message": str(e), "code": "BAD_REQUEST"})


@router.post("/runtime/instances/{name}/stop")
def runtime_instance_stop(name: str):
    """停止具名实例（B40）：幂等；清单内未拉起的实例按 stopped 返回。"""
    _ensure_instance_name(name)
    try:
        return node_proc.get_guardian().stop_instance(name)
    except KeyError:
        raise HTTPException(404, {"message": f"实例「{name}」不在运行时清单中",
                                  "code": "NOT_FOUND"})
    except ValueError as e:
        raise HTTPException(400, {"message": str(e), "code": "BAD_REQUEST"})


@router.get("/runtime/config")
def runtime_config():
    return node_proc.resolve_settings(get_data_dir())


@router.put("/runtime/config")
def runtime_save_config(patch: dict):
    unknown = [k for k in patch if k not in node_proc.SETTINGS_KEYS]
    if unknown:
        raise HTTPException(400, {"message": f"未知设置项：{', '.join(unknown)}",
                                  "code": "BAD_REQUEST"})
    try:
        out = node_proc.save_settings(get_data_dir(), patch)
    except ValueError as e:
        raise HTTPException(400, {"message": str(e), "code": "BAD_REQUEST"})
    if "instances" in patch:
        # B40 清单对账：移除的实例停进程并清理守护状态（新增的不自动拉起）
        node_proc.get_guardian().sync_instances(out.get("instances") or [])
    return out


# --------------------------------------------------------------------------
# 发现池（广播线索 + 手动目标；绝不落凭据，D38）
# --------------------------------------------------------------------------

@router.get("/pool")
def pool_snapshot():
    # 排除本机 node 条目：设备页"本机被控端"区块已单独展示本机，
    # 广播经回环/本机网卡回到自己时不应重复出现在"发现的设备"列表
    # （node 未启动过时 device_id 为 None，不过滤——此时本机也无广播帧）
    did = node_proc.get_guardian().read_device_id()
    return discovery.snapshot(exclude_device_id=did)


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


_APPROVE_TOAST_MESSAGE = "配对请求已确认，请在设备互联页查看配对码"


def _notify_pair(message: str) -> None:
    """配对系统 toast（B39）：尽力而为，任何失败绝不阻断配对响应。

    文案只含固定提示，不含 IP/配对码/令牌等敏感数据；与前端轮询无关——
    请求到达的提醒由 devices.pair_watcher 常驻线程负责，这里只管确认回执。
    """
    try:
        from ... import win_toast
        if not win_toast.AVAILABLE:
            logger.info("系统 toast 不可用，跳过配对提醒")
            return
        win_toast.show(TOAST_TITLE, message)
    except Exception:  # noqa: BLE001 - 提醒失败绝不影响配对流程
        logger.exception("配对提醒 toast 发送失败")


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
    try:
        _notify_pair(_APPROVE_TOAST_MESSAGE)
    except Exception:  # noqa: BLE001 - 提醒回调异常绝不阻断 approve 响应
        logger.exception("配对提醒回调异常")
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


# --------------------------------------------------------------------------
# Agent 开放交互（B35/FR-16）：聚合发现 + 技能安装
# --------------------------------------------------------------------------

def _overview_endpoints() -> dict:
    """操作接口速查（与 SKILL.md 表格一致；文本形态供 Agent 直接读取）。"""
    return {
        "list_devices": "GET /api/v1/devices",
        "overview": "GET /api/v1/devices/overview",
        "ping": "POST /api/v1/devices/{name}/ping",
        "exec": "POST /api/v1/devices/{name}/exec {\"cmd\", \"shell\": \"cmd|powershell\", \"timeout\": 60, \"cwd\": 可选}",
        "job_result": "GET /api/v1/devices/{name}/job/{job_id}?wait=30",
        "screen": "GET /api/v1/devices/{name}/screen（PNG 二进制）",
        "screen_save": "POST /api/v1/devices/{name}/screen/save {\"save_dir\"}",
        "put": "POST /api/v1/devices/{name}/put（multipart: file + remote_path）",
        "get": "GET /api/v1/devices/{name}/get?path=<远端路径>&save_dir=<本地目录>",
        "workspaces": "GET /api/v1/devices/{name}/workspaces（查询被控端可写根：工作空间白名单 + 默认暂存目录）",
        "status": "GET /api/v1/devices/{name}/status（跨设备状态汇报：事件/状态/时间/陈旧标记）",
        "tasks": "GET /api/v1/devices/{name}/tasks?status=&limit=50&offset=0",
        "task_detail": "GET /api/v1/devices/{name}/tasks/{task_id}",
    }


@router.get("/{name}/workspaces")
def device_workspaces(name: str):
    """查询被控端工作空间白名单（B42/契约 V1.6：put 撞墙后允许根可发现）。

    live 调 node `/v1/info`（§6.2 V1.6 新增 workspace_roots / workspace_staging）。
    白名单属被控端本地配置（D24）：本端点只读，不提供任何远程修改能力。
    """
    def _go(entry, client):
        return client.request("GET", "/v1/info", timeout=15)
    try:
        info = _call(name, _go)
    except node_client.NodeError as e:
        _raise_node_error(e)
    return {"device": name,
            "workspace_roots": info.get("workspace_roots") or [],
            "workspace_staging": info.get("workspace_staging"),
            "node_version": info.get("node_version")}


def _status_payload(entry: dict) -> dict:
    name = entry.get("name") or ""
    return {
        "device": name,
        "hostname": entry.get("hostname") or name,
        "last_error": status_watcher.get_watcher().errors().get(name),
        "sessions": status_store.list_sessions(name),
    }


@router.get("/status")
def device_status_all():
    """全部已配对设备的最新状态（设备互联页一次拉取）。"""
    return {"devices": [_status_payload(e) for e in get_store().list()]}


@router.get("/{name}/status")
def device_status(name: str):
    """单台设备的最新状态；未配对返回 404。"""
    return _status_payload(_load_entry(name))


@router.get("/overview")
def devices_overview():
    """聚合发现（B35/FR-16）：Agent 每次会话首调，动态获取已配对设备全景。

    只列已配对设备（配对是设备页人工动作，Agent 无权发起新配对）；
    凭据（令牌/指纹）不出 backend——响应不含任何 DPAPI 存储内容。
    """
    port = _backend_port()
    return {
        "backend_base": f"http://127.0.0.1:{port}" if port else None,
        "devices": [
            {
                "name": d.get("name", ""),
                "ip": d.get("ip", ""),
                "port": d.get("port", 0),
                "hostname": d.get("hostname", ""),
                "device_id": d.get("device_id", ""),
                "identity": d.get("identity") or "default",
            }
            for d in get_store().list()
        ],
        "endpoints": _overview_endpoints(),
        "notes": [
            "只能操作本清单中的已配对设备；配对确认是设备互联页人工动作，Agent 无权发起新配对。",
            "设备令牌与证书指纹由 a4agent 加密托管，不会出现在任何接口响应中。",
            "远程命令在被控端有完整审计记录，只执行任务所需范围内的命令。",
            "文件上传（put/写方向接口）受被控端工作空间白名单约束：默认仅 node 暂存目录可写；"
            "403 SANDBOX_DENIED 的 message 会列出允许根，也可用 workspaces 端点主动查询（B42）。",
            "上传的自动化脚本/临时产物建议先 put 到暂存目录再 exec 执行，exec 命令引号逐字透传"
            "（node v0.5.1 起），参数含空格直接写引号即可（B43）。",
            "backend 端口记录于 %APPDATA%\\a4agent\\desktop.json；设备可能离线，调用失败先 ping。",
        ],
    }


@router.post("/install-skill")
def install_skill(body: dict):
    """安装内置 crossdevice 技能（B35/FR-16）：三档范围，已存在不覆盖、如实报告。

    scope=global → skill_manager.global_skill_roots() 九端全局根；
    scope=project → project_path 下各端项目级子目录（skill_manager.project_skill_roots）；
    scope=custom → body.custom_dir 指定目录（写入 <目录>/crossdevice/SKILL.md）。
    """
    scope = str(body.get("scope") or "").strip()
    if scope not in ("global", "project", "custom"):
        raise HTTPException(400, {"message": "scope 必须是 global / project / custom",
                                  "code": "BAD_REQUEST"})

    src = _skill_bundle_src()
    src_file = src / skill_manager.SKILL_FILE
    if not src_file.is_file():
        raise HTTPException(500, {"message": f"内置技能包缺失：{src}",
                                  "code": "INTERNAL"})

    if scope == "global":
        roots = skill_manager.global_skill_roots()
        targets = {tool: root / SKILL_NAME for tool, root in roots.items()}
    elif scope == "project":
        base = Path(str(body.get("project_path") or "")).expanduser()
        if not base.is_dir():
            raise HTTPException(400, {"message": "项目目录不存在",
                                      "code": "BAD_REQUEST"})
        roots = skill_manager.project_skill_roots(base)
        targets = {tool: root / SKILL_NAME for tool, root in roots.items()}
    else:
        custom = Path(str(body.get("custom_dir") or "")).expanduser()
        if not custom.is_absolute():
            raise HTTPException(400, {"message": "自定义目录必须是绝对路径",
                                      "code": "BAD_REQUEST"})
        targets = {"custom": custom / SKILL_NAME}

    installed, skipped = [], []
    for tool, dest_dir in targets.items():
        dest_file = dest_dir / skill_manager.SKILL_FILE
        if dest_file.exists():
            skipped.append({"tool": tool, "path": str(dest_file),
                            "reason": "已存在同名技能，未覆盖"})
            continue
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_file, dest_file)
            installed.append({"tool": tool, "path": str(dest_file)})
        except OSError as e:
            skipped.append({"tool": tool, "path": str(dest_file),
                            "reason": f"写入失败：{e}"})

    return {"installed": installed, "skipped": skipped,
            "total": len(targets)}
