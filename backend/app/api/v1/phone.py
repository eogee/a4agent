"""手机通知接口：配置读写、话题重置、测试推送。

测试推送是同步请求：成功/失败原因原样回给前端前置弹窗（项目约定：失败要
可见，不静默）；任务终态的常规推送才走 notifier 的异步线程。
"""
import logging

from fastapi import APIRouter, HTTPException

from ... import schemas
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
                              "✅ 收到这条说明手机通知已打通")
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
        sent = win_toast.show("a4agent 测试通知", "✅ 收到这条说明桌面横幅已打通")
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
