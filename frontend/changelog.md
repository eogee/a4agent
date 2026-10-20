# a4agent v0.6.4（引擎后端可选与托盘常驻 / Switchable engine backends and a resident tray）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.6.4.exe——八端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）

Windows: a4agent-setup-0.6.4.exe — the eight-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway").

**v0.6.4 更新内容 / What's new in v0.6.4：**

引擎按后端分目录安装——一个后端一套引擎（`Programs\a4agent-<后端>\engine`），重下某个后端不再覆盖另一个后端正在用的引擎；实际在跑哪个后端按目录里的 DLL 实测识别（`ggml-cuda` > `ggml-vulkan` > `ggml-cpu`），顶栏显示真实后端

Engines install per backend — one engine per backend directory under `Programs\a4agent-<backend>\engine`, so re-downloading one never overwrites an engine another backend is using; the running backend is detected from the DLLs present (`ggml-cuda` > `ggml-vulkan` > `ggml-cpu`) and shown in the title bar

设置页可切换引擎后端——新增「引擎后端」行（自动 / CUDA / Vulkan / CPU），只列已安装的后端；切换只改配置指针、不动引擎文件，服务运行中也能切、下次启动生效，下载进行中禁止切换

Engine backend switchable in Settings — a new "Engine backend" row (Auto / CUDA / Vulkan / CPU) lists only installed backends; switching only rewrites the config pointer, works while the service is running and takes effect on the next start, and is refused during a download

引擎升级到 llama.cpp b11719（2026-10-18 nightly）——四套引擎包的资产名与体积按 GitHub API 重新核对

Engines bumped to llama.cpp b11719 (2026-10-18 nightly) — asset names and sizes of the four packs were re-checked against the GitHub API

托盘常驻——关窗不再等于退出，托盘图标常驻，左键唤回窗口，右键菜单直接退出；退出时先收托盘，再回收引擎与任务子进程

Tray residency — closing the window no longer quits; the tray icon stays, left-click brings the window back, the menu exits directly, and shutdown collects the tray before the engine and task child processes

启动自恢复——二次启动抢不到单例锁时先唤回隐藏窗口，唤不到就清障重启，兜底实例仍失败才提示到任务管理器结束进程，不再只弹一句「已在运行」

Startup recovery — a second launch that cannot take the single-instance lock wakes the hidden window first, then falls back to a clean restart, and only then tells you what to do in Task Manager

项目自动发现——默认扫描本机所有盘符，并读取 Claude Code 项目表、Codex 会话工作目录、VS Code 最近工作区，任意深度的项目零操作出现；「项目根目录」改为「项目来源」弹窗，可逐条忽略或恢复默认

Automatic project discovery — scans every drive letter and reads the history the tools already keep (Claude Code's project list, Codex session directories, recent VS Code workspaces), so projects at any depth appear with no setup; the old root-folder dialog becomes a "Project sources" dialog you can prune or reset

搜索与界面——项目视图搜索同时匹配项目文件夹名与根路径，全局零结果时可一键切到项目视图；进度条统一为下载弹窗样式，DSH / Pi 显示名统一大写，ZCode 与 OpenCode 徽章改为灰系深浅对

Search and UI — project-view search matches project folder names and root paths too, with a one-click switch when a global search finds nothing; progress bars use the download-dialog style, DSH / Pi names are uppercased, and the ZCode / OpenCode badges became a grey light/dark pair

兼容性——Windows 10/11 64 位；旧目录里的引擎仍会被识别，无需重下；配置与数据仍在 `%APPDATA%\a4agent`，升级全程保留

Compatibility — Windows 10/11 64-bit; engines in older directories are still detected so nothing needs re-downloading; config and data stay in `%APPDATA%\a4agent` and survive the upgrade

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94
