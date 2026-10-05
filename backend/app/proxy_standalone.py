"""独立运行的 OpenAI 翻译代理进程。

工具在切换 OpenAI 类型服务商时启动本进程（开发：python -m backend.app.proxy_standalone；
打包：a4agent.exe --proxy）。应用退出后代理仍然存活，Claude Code 可继续使用。
代理轮询数据库：当前生效配置不再是 openai 类型时自动退出。
"""

import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_INTERVAL = 3.0


def _active_openai_config():
    from .database import SessionLocal
    from .config_manager import target_list
    from .models import Configuration

    db = SessionLocal()
    try:
        c = db.query(Configuration).filter(Configuration.is_active.is_(True)).first()
        if c is None or c.provider is None or c.provider.api_type != "openai":
            return None
        targets = set(target_list(c.targets))
        # 代理服务 Claude（messages 翻译）、Codex（responses 翻译）与
        # dsh（/chat/completions 透传，归一上游 tool_calls 的 null 字段）
        if not (targets & {"claude", "codex", "dsh"}):
            return None
        # 原生支持 Responses 且仅面向 Codex 的配置可直接访问上游，无需本地代理
        if targets == {"codex"} and getattr(c.provider, "native_responses", False):
            return None
        return c
    finally:
        db.close()


def _status_file():
    from .database import get_data_dir

    return get_data_dir() / "proxy.json"


def _port_alive(port) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1):
            return True
    except OSError:
        return False


def _port_owner_pid(port):
    """按监听端口反查占用进程 PID（状态文件 pid 可能过期时兜底）。"""
    if not port:
        return None
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"(Get-NetTCPConnection -LocalPort {int(port)} -State Listen "
                    "-ErrorAction SilentlyContinue | "
                    "Select-Object -First 1 -ExpandProperty OwningProcess)"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        return int(out) if out.isdigit() else None
    except Exception as e:
        logger.warning("反查端口 %s 占用进程失败：%s", port, e)
        return None


def _proxy_version(port):
    """查询端口上代理的 /v1/api/version；旧构建无此端点时返回 None。"""
    if not port:
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/v1/api/version", timeout=2
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _proxy_compatible(port) -> bool:
    """判断端口上的代理是否支持 Codex Responses 协议。

    旧构建（如 api-switch.exe 早期版本）只实现了 Anthropic /v1/messages，
    没有 /responses 端点；端口虽然活着但 Codex 会拿到 404，表现为
    “无法访问本地代理地址”。复用代理前必须校验能力，避免误用旧进程。
    """
    ver = _proxy_version(port)
    if not ver:
        return False
    return int(ver.get("version") or 0) >= 2 and "openai_responses" in (
        ver.get("features") or []
    )


def _token_owner_ok(st: dict) -> bool:
    """端口属主必须就是写这份状态文件的进程。

    否则 token 是「上一个还占着端口的代理进程」留下的：新进程写文件、旧进程占端口
    时（升级/崩溃后的孤儿），端口活着、版本兼容，但客户端拿这个 token 去请求必然
    401 invalid bearer token —— 而 is_proxy_running() 报的却是运行中。
    反查不到属主（权限不足等）时不误杀，维持原判定。
    """
    owner = _port_owner_pid(st.get("port"))
    file_pid = st.get("pid")
    if owner is None or not file_pid:
        return True
    try:
        return int(owner) == int(file_pid)
    except (TypeError, ValueError):
        return True


def _kill_owner(port) -> None:
    """强制结束占用指定端口的进程（旧代理重启前清理）。"""
    pid = _port_owner_pid(port)
    if pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("结束端口 %s 占用进程失败：%s", port, e)


def _spawn() -> None:
    """以独立进程方式启动代理（应用退出后仍然存活）。"""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--proxy"]
        cwd = None
    else:
        root = Path(__file__).resolve().parents[2]
        cmd = [sys.executable, "-m", "backend.app.proxy_standalone"]
        cwd = str(root)
    subprocess.Popen(
        cmd,
        cwd=cwd,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _refresh_upstream(port: int) -> None:
    """通知代理按数据库当前生效配置立即刷新上游，避免切换后短暂使用旧上游。"""
    if not port:
        return
    try:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/proxy/refresh",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as e:
        logger.warning("刷新代理上游失败：%s", e)


def _remove_status_file(status_file) -> None:
    """尽力删除状态文件，进程崩溃残留时避免下次复用陈旧信息。"""
    try:
        status_file.unlink()
    except OSError:
        pass


def ensure_proxy_running() -> dict:
    """确保翻译代理进程在运行，返回 {"base_url", "token"} 供写 settings.json。"""
    status_file = _status_file()
    if status_file.exists():
        try:
            st = json.loads(status_file.read_text(encoding="utf-8"))
            if _port_alive(st.get("port")) and st.get("token"):
                if _proxy_compatible(st.get("port")) and _token_owner_ok(st):
                    _refresh_upstream(st.get("port"))
                    return {
                        "base_url": f"http://127.0.0.1:{st['port']}",
                        "token": st["token"],
                    }
                # 端口活着但进程是旧构建（不支持 Responses）或不是写这份 token 的
                # 那个进程：杀掉重启，否则客户端会拿到没人认的 token
                _kill_owner(st.get("port"))
            # 端口已死、token 缺失或进程不兼容：清理陈旧状态文件后重新拉起
            _remove_status_file(status_file)
        except (OSError, ValueError, KeyError) as e:
            logger.warning("读取代理状态文件失败，清理后重新启动：%s", e)
            _remove_status_file(status_file)

    _spawn()
    deadline = time.time() + 20
    while time.time() < deadline:
        if status_file.exists():
            try:
                st = json.loads(status_file.read_text(encoding="utf-8"))
                if (
                    _port_alive(st.get("port"))
                    and st.get("token")
                    and _proxy_compatible(st.get("port"))
                ):
                    _refresh_upstream(st.get("port"))
                    return {
                        "base_url": f"http://127.0.0.1:{st['port']}",
                        "token": st["token"],
                    }
            except (OSError, ValueError, KeyError):
                pass
        time.sleep(0.5)
    raise RuntimeError("本地翻译代理启动失败，请查看日志")


def is_proxy_running() -> dict:
    """返回代理运行状态：{"running": bool, "port": int|None, "pid": int|None}。"""
    status_file = _status_file()
    port = None
    pid = None
    if status_file.exists():
        try:
            st = json.loads(status_file.read_text(encoding="utf-8"))
            port = st.get("port")
            pid = st.get("pid")
        except (OSError, ValueError):
            pass
    if port and _port_alive(port):
        # 以实际占用端口的进程为准，防止状态文件 pid 过期
        return {"running": True, "port": port, "pid": _port_owner_pid(port) or pid}
    return {"running": False, "port": None, "pid": None}


def stop_proxy() -> dict:
    """停止本地翻译代理进程；即使状态文件已陈旧也会顺手清理。"""
    st = is_proxy_running()
    status_file = _status_file()
    if not st["running"]:
        _remove_status_file(status_file)
        return {"stopped": False, "detail": "代理未在运行"}
    pids = set()
    if st.get("pid"):
        pids.add(str(st["pid"]))
    owner = _port_owner_pid(st.get("port"))
    if owner:
        pids.add(str(owner))
    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/PID", pid, "/F"],
                capture_output=True,
                timeout=10,
            )
        except Exception as e:
            logger.warning("结束代理进程 %s 失败：%s", pid, e)
    _remove_status_file(status_file)
    return {"stopped": True, "detail": f"已停止代理进程 {sorted(pids)}"}


def main() -> None:
    from . import openai_proxy
    from .crypto import decrypt_text
    from .logging_config import setup_logging

    setup_logging()

    status_file = _status_file()
    previous = {}
    if status_file.exists():
        try:
            previous = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("读取旧代理状态文件失败：%s", e)
            previous = {}
    token = previous.get("token") or None

    first = True
    last_signature = None
    while True:
        cfg = _active_openai_config()
        if cfg is None:
            if not first:
                break
            time.sleep(POLL_INTERVAL)
            continue

        key = decrypt_text(cfg.api_key_encrypted)
        if not key:
            logger.error("当前配置「%s」的 API Key 解密失败，代理无法启动", cfg.name)
            break
        signature = (cfg.id, cfg.provider.id, cfg.provider.api_base, cfg.model)
        if signature != last_signature:
            info = openai_proxy.start(cfg.provider.api_base, key, token=token)
            token = info["token"]
            status_file.write_text(
                json.dumps({"pid": os.getpid(), "port": info["port"], "token": token}),
                encoding="utf-8",
            )
            last_signature = signature

        first = False
        time.sleep(POLL_INTERVAL)

    openai_proxy.stop()
    _remove_status_file(status_file)


if __name__ == "__main__":
    main()
