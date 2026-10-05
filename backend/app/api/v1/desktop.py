"""桌面壳接口：二次启动唤回已有实例的窗口。

服务只监听 127.0.0.1（见 desktop.py），唤起动作也仅限本机；钩子由桌面壳在
启动时注册，纯 API 形态（uvicorn 直跑）下未注册时返回未接管而不是报错。
"""
from fastapi import APIRouter

router = APIRouter(prefix="/desktop")

_wake_hook = None


def set_wake_hook(hook) -> None:
    global _wake_hook
    _wake_hook = hook


@router.post("/wake")
def wake():
    """把已隐藏的主窗口重新显示并置于前台。"""
    if _wake_hook is None:
        return {"restored": False, "detail": "当前不是桌面窗口形态"}
    try:
        _wake_hook()
    except Exception as e:  # noqa: BLE001 - 唤回失败只影响这一次点击
        return {"restored": False, "detail": f"唤回窗口失败：{e}"}
    return {"restored": True, "detail": "已唤回 a4agent 窗口"}
