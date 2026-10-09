"""浏览器开发调试入口：用已安装版的同一份数据启动 Web 服务。

与桌面版（desktop.py）的区别：
  - 不启动 pywebview 窗口，直接在浏览器访问（原生对话框不可用：任务下发的选目录
    走后端列目录的内置弹窗，技能迁移/本地模型仍退化为手动输入路径）
  - 默认共享已安装版的数据目录（%APPDATA%\\a4agent），配置方案 / 供应商 /
    本地模型设置全部实时同步
  - 跑的是仓库源码最新代码，改完前端刷新浏览器即可，无需重新打包安装

服务就绪后会自动打开浏览器，无需手动刷新等待。

用法：
    uv run python dev_server.py             # http://127.0.0.1:18900
    uv run python dev_server.py 18901       # 自定义端口
"""
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 必须在导入 backend.app.* 之前设置：database.py 在导入期读取该变量。
# 指向已安装版数据目录，实现浏览器调试与桌面版数据完全同步。
os.environ.setdefault(
    "A4AGENT_DATA_DIR",
    str(Path(os.environ.get("APPDATA", Path.home())) / "a4agent"),
)

DEFAULT_PORT = 18900


def _write_instance(port: int) -> None:
    """记录运行端口，供进程内插件发现本服务。

    dsh 的 a4agent-dsh-hook 靠这个文件找到回调地址（它跑在 dsh 进程内，
    无法像外部 hook 那样直接调 a4agent hook 子命令）。桌面版由 desktop.py
    的 write_instance 负责；开发版不写的话，插件会判定「a4agent 未运行」
    而静默跳过通知——测试全绿、真机不响的典型来源。
    """
    try:
        from backend.app.database import get_data_dir

        path = get_data_dir() / "desktop.json"
        path.write_text(json.dumps({"port": port, "pid": os.getpid()}),
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - 写不进去不该拦住服务启动
        print(f"⚠ 无法写入 desktop.json（dsh hook 将无法回调本服务）：{exc}")


def _clear_instance() -> None:
    try:
        from backend.app.database import get_data_dir

        (get_data_dir() / "desktop.json").unlink(missing_ok=True)
    except OSError:
        pass


def _warn_if_desktop_running() -> None:
    """已安装版与调试版同时写同一份数据可能偶发 SQLite 锁冲突，仅提示不阻止。

    改名过渡期新旧可执行文件并存：a4agent.exe（新版）与 a4api.exe（≤v0.3.3）都查。
    """
    try:
        out = subprocess.run(
            ["tasklist"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        ).stdout
        running = [name for name in ("a4agent.exe", "a4api.exe") if name in out]
        if running:
            print(f"⚠ 检测到已安装版（{' / '.join(running)}）正在运行。")
            print("  两边共享同一份数据，同时改动可能偶发写入冲突；建议先关闭已安装版再调试。")
    except Exception:
        pass  # 探测失败不影响启动


def _open_browser_when_ready(port: int) -> None:
    """轮询直到服务真正可访问，再打开浏览器（避免页面先于服务打开报拒绝连接）。"""
    import urllib.request

    url = f"http://127.0.0.1:{port}"
    for _ in range(120):  # 最多等 60 秒
        try:
            urllib.request.urlopen(url, timeout=1)
            print(f"✓ 服务已就绪，正在打开浏览器：{url}")
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.5)


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT

    import uvicorn

    from backend.app.main import app

    print(f"a4agent 浏览器调试版数据目录（与已安装版共享）：{os.environ['A4AGENT_DATA_DIR']}")
    print("Ctrl+C 停止服务")
    _warn_if_desktop_running()

    threading.Thread(
        target=_open_browser_when_ready, args=(port,), daemon=True
    ).start()

    _write_instance(port)
    try:
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        uvicorn.Server(config).run()
    finally:
        # 端口失效后必须清掉，否则插件会连到一个已不存在的服务并静默等待
        _clear_instance()


if __name__ == "__main__":
    main()
