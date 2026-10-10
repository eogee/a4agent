"""手机通知接口：配置读写、话题重置、测试推送。

测试推送是同步请求：成功/失败原因原样回给前端前置弹窗（项目约定：失败要
可见，不静默）；任务终态的常规推送才走 notifier 的异步线程。
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from ... import schemas
from ...hooks import dsh as dsh_hook
from ...hooks import register as hook_register
from ...phone import config as phone_config
from ...phone import ntfy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/phone")


def _out(cfg: dict) -> schemas.PhoneConfigOut:
    return schemas.PhoneConfigOut(
        enabled=cfg["enabled"],
        server=cfg["server"],
        topic=cfg["topic"],
        token_set=bool(cfg["token"]),
        events=cfg["events"],
        subscribe_url=phone_config.subscribe_url(cfg),
        qr_payload=phone_config.qr_payload(cfg),
        desktop=cfg.get("desktop", True),
    )


@router.get("/config", response_model=schemas.PhoneConfigOut)
def get_config():
    cfg = phone_config.ensure_topic(phone_config.load())
    return _out(cfg)


@router.put("/config", response_model=schemas.PhoneConfigOut)
def put_config(body: schemas.PhoneConfigIn):
    cfg = phone_config.load()
    data = body.model_dump(exclude_none=True)
    events_in = data.pop("events", None)
    if events_in:
        cfg["events"] = {**cfg["events"], **events_in}
    for key in ("enabled", "server", "topic", "token", "desktop"):
        if key in data:
            cfg[key] = data[key]
    if err := phone_config.validate(cfg["server"], cfg["topic"]):
        raise HTTPException(422, err)
    cfg = phone_config.ensure_topic(cfg)
    return _out(phone_config.save(cfg))


@router.post("/topic/regenerate", response_model=schemas.PhoneConfigOut)
def regenerate_topic():
    """重置话题：手机丢失等场景换新话题，旧话题立即作废。"""
    cfg = phone_config.load()
    cfg["topic"] = phone_config.new_topic()
    return _out(phone_config.save(cfg))


@router.post("/test", response_model=schemas.PhoneTestOut)
def test_push():
    cfg = phone_config.ensure_topic(phone_config.load())
    if err := phone_config.validate(cfg["server"], cfg["topic"]):
        raise HTTPException(422, err)
    ok, reason = ntfy.publish(cfg, "a4agent 测试通知",
                              "收到这条说明手机通知已打通")
    return schemas.PhoneTestOut(sent=ok, detail=reason)


@router.post("/test/desktop", response_model=schemas.PhoneTestOut)
def test_desktop():
    """测试桌面横幅：同步调 win_toast 直发，不经队列（用户在场，立即反馈）。

    与 hook 的桌面弹窗走不同路径——那条要经请求文件由常驻进程代发，
    这里就是当前进程自己弹，失败原因原样回显。
    """
    from ... import win_toast

    cfg = phone_config.load()
    if not cfg.get("desktop", True):
        return schemas.PhoneTestOut(
            sent=False, detail="桌面横幅开关已关闭，请先在上方勾选「桌面横幅」并保存")
    if not win_toast.AVAILABLE:
        return schemas.PhoneTestOut(sent=False, detail="仅 Windows 支持系统通知横幅")
    try:
        sent = win_toast.show("a4agent 测试通知", "收到这条说明桌面横幅已打通")
    except Exception as exc:  # noqa: BLE001 - 失败要可见但不抛栈给前端
        logger.exception("桌面横幅测试异常")
        return schemas.PhoneTestOut(sent=False, detail=str(exc))
    return schemas.PhoneTestOut(
        sent=sent,
        detail="" if sent else "系统通知被拒绝，可能被勿扰模式或组策略禁用")


# ---------------- 会话交互（hook） ----------------

def _hook_status_out(cfg: dict) -> schemas.PhoneHookStatusOut:
    return schemas.PhoneHookStatusOut(
        mode=cfg["hook"]["mode"],
        timeout=cfg["hook"]["timeout"],
        plan_timeout=cfg["hook"]["plan_timeout"],
        topic_ready=bool(cfg["topic"]),
        hook_command=hook_register.default_hook_command(),
        engines=hook_register.registration_status(),
        desktop=cfg.get("desktop", True),
    )


@router.get("/hook/status", response_model=schemas.PhoneHookStatusOut)
def hook_status():
    return _hook_status_out(phone_config.load())


@router.put("/hook/config", response_model=schemas.PhoneHookStatusOut)
def put_hook_config(body: schemas.PhoneHookIn):
    cfg = phone_config.load()
    data = body.model_dump(exclude_none=True)
    cfg["hook"] = {**cfg["hook"], **data}
    return _hook_status_out(phone_config.save(cfg))


@router.post("/hook/register")
def hook_register_engine(body: schemas.HookEngineIn):
    result = hook_register.register_engine(body.engine)
    result["engines"] = hook_register.registration_status()
    return result


@router.post("/hook/unregister")
def hook_unregister(body: schemas.HookEngineIn):
    result = hook_register.unregister_engine(body.engine)
    result["engines"] = hook_register.registration_status()
    return result


# ---------------- DSH 事件（进程内插件回调） ----------------
# DSH 没有外部 hook 命令协议：插件跑在 dsh 进程内，只能反向 HTTP 回调本机
# 常驻服务。请求会挂住直到手机作答或超时（长轮询），因此耗时可达 timeout 秒
# ——插件侧读超时要大于此值，否则插件先断、服务端还在等，白等一轮。
#
# 这些端点只监听回环（服务本身绑 127.0.0.1），且不接受跨站调用：DSH 插件以
# 本机进程身份直连，故不引入鉴权；CSRF 侧由「非简单表单 + 仅回环可达」兜住。

async def _dsh_payload(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, "载荷不是合法 JSON")
    return data if isinstance(data, dict) else {}


@router.post("/dsh/task-complete")
async def dsh_task_complete(request: Request):
    """任务完成：桌面弹窗 + 手机推送，无需响应体（插件不等待）。"""
    payload = await _dsh_payload(request)
    try:
        dsh_hook.handle_task_complete(payload)
    except Exception:  # noqa: BLE001 - 通知失败绝不能影响 DSH 会话
        logger.exception("DSH 任务完成处理异常")
    return {"ok": True}


@router.post("/dsh/question")
async def dsh_question(request: Request):
    """提问作答：长轮询至手机作答或超时。

    返回 {answers: [...]} 表示手机已作答，插件用它替换原生提问；
    返回 {} 表示放行（终端优先 / 推送失败 / 超时），由 DSH 走原生交互。
    """
    payload = await _dsh_payload(request)
    cfg = phone_config.load()
    try:
        result = await _run_with_timeout(
            dsh_hook.handle_ask_user_question, payload,
            dsh_hook.http_timeout(cfg))
    except Exception:  # noqa: BLE001 - 回退原生，不能把异常抛给插件
        logger.exception("DSH 提问处理异常")
        return {}
    return result or {}


@router.post("/dsh/permission")
async def dsh_permission(request: Request):
    """权限审批：长轮询至手机点选或超时。

    返回 {outcome: 'allowed-once'|'rejected'} 表示手机已决策；
    返回 {} 表示放行原生审批链。
    """
    payload = await _dsh_payload(request)
    cfg = phone_config.load()
    try:
        outcome = await _run_with_timeout(
            dsh_hook.handle_permission_request, payload,
            dsh_hook.http_timeout(cfg))
    except Exception:  # noqa: BLE001 - 回退原生
        logger.exception("DSH 审批处理异常")
        return {}
    return {"outcome": outcome} if outcome else {}


async def _run_with_timeout(func, payload: dict, timeout: int):
    """在线程池里跑同步阻塞的等待逻辑（response.wait_for_response 是阻塞轮询，
    直接在事件循环里调会卡住整个服务）。线程超时后不强制中断——放弃等待并
    放行原生交互即可，后台线程自然到期退出。"""
    import asyncio

    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(None, func, payload), timeout=timeout)
