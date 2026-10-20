# a4agent v0.6.4

## English

### Engine management: one engine per backend, switchable from Settings

- Engines now install into `%LOCALAPPDATA%\Programs\a4agent-<backend>\engine`, one directory per backend. Re-downloading a backend only replaces its own copy and never overwrites an engine another backend is using.
- The backend actually running is detected from the DLLs present in the directory (`ggml-cuda` > `ggml-vulkan` > `ggml-cpu`), instead of being inferred from the package you once downloaded.
- When several engines coexist, an NVIDIA card prefers the CUDA one — backend-vs-GPU match first, then modification time — rather than whichever files are newest. The title bar shows the backend that is really running.
- Changing the backend only rewrites the pointer in the config, never touches engine files: you can switch while the service is running, and it takes effect on the next start. Switching is refused while a download is in progress, so it cannot race the download callback.
- Engines are pinned to llama.cpp **b11719** (2026-10-18 nightly); asset names and sizes of the four packs were re-checked against the GitHub API.

### Tray and startup recovery

- Closing the window no longer means quitting: a4agent stays resident in the tray, left-click brings the window back, and the context menu can exit directly.
- A second launch that cannot take the single-instance lock no longer dies with a bare "already running" box. It tries to wake the hidden window first, then falls back to a clean restart; if even that instance cannot take the lock, it tells you what to do in Task Manager instead of two instances pulling at each other.
- Shutdown now collects the tray first, then the local inference engine and headless task child processes, so nothing is left running in the background.

### Project discovery, search and UI

- The default scan range is now every visible drive letter on the machine, instead of a folder hard-coded at build time. Projects also surface from the history the tools already keep — Claude Code's project list, Codex session working directories, recent VS Code workspaces — so projects at any depth appear with no setup.
- "Project root" is replaced by a "Project sources" dialog: auto-discovered entries can be ignored one by one or restored to defaults, and scan ranges can still be added by hand. Same-name projects in different paths get a parent-folder suffix so migration can never write into the wrong directory, and disconnected network drives no longer stall discovery (3s timeout per scan).
- Skill and MCP search in the project view now also matches the project folder name and root path. When a global search returns nothing but the keyword does hit a project folder, it reports how many and offers a one-click switch to the project view.
- Progress bars now use the same layui style as the download dialog, and the 45° stripe animation is gone. DSH / Pi display names are uppercased, the ZCode and OpenCode badges became a grey light/dark pair, and the README gains an XERJ Corpus Hub contributor badge.

### Compatibility

- Windows 10/11 64-bit.
- Engines already installed in older directories are still detected, so nothing has to be re-downloaded; pick which one to use from the new "Engine backend" row in Settings.
- Config and data stay in `%APPDATA%\a4agent` and survive the upgrade.

### Upgrade

Run `a4agent-setup-0.6.4.exe` over the existing installation.

---

## 中文

### 引擎管理：一个后端一套引擎，设置页可切换

- 引擎改为装进 `%LOCALAPPDATA%\Programs\a4agent-<后端>\engine`，一个后端一个目录。重下某个后端只替换它自己那份，不再覆盖另一个后端正在用的引擎。
- 实际在跑哪个后端，改成按目录里真实存在的 DLL 识别（`ggml-cuda` > `ggml-vulkan` > `ggml-cpu`），不再靠「你当初下的是哪个包」去猜。
- 多套引擎共存时，NVIDIA 卡按「后端与显卡匹配优先、其次修改时间」排序，CUDA 排在 Vulkan 之前，不再是谁的文件新就用谁。顶栏显示真正在跑的后端。
- 切换后端只改写配置里的指针，不动引擎文件：服务运行中也能切，下次启动生效；下载进行中禁止切换，避免和下载完成的回调互相覆盖。
- 引擎钉到 llama.cpp **b11719**（2026-10-18 nightly），四个引擎包的资产名与体积按 GitHub API 重新核对。

### 托盘常驻与启动自恢复

- 关窗不再等于退出：应用常驻托盘，左键唤回窗口，右键菜单可直接退出。
- 二次启动抢不到单例锁时，不再只弹一句「a4agent 已在运行」把用户晾在门口：先尝试唤回被隐藏的窗口，唤不到就清障重启；连兜底实例都抢不到锁时，明确告诉你去任务管理器结束进程，而不是两个实例互相唤醒。
- 退出顺序改为先收托盘，再回收本地推理引擎与无头任务子进程，后台不留残留。

### 项目发现、搜索与界面

- 默认扫描范围改为本机所有可见盘符，不再是打包时写死的那个目录。项目还会从各工具本来就记着的历史里自动浮现：Claude Code 的项目表、Codex 会话的工作目录、VS Code 最近工作区——任意深度的项目零操作出现。
- 「项目根目录」改为「项目来源」弹窗：自动发现的部分可逐条忽略或恢复默认，扫描范围仍可手动添加。同名不同路径的项目会加父目录后缀消歧，迁移目标不可能再写错目录；断开的网络盘不再拖死发现接口（单次扫描 3 秒超时）。
- 项目视图的技能与 MCP 搜索扩展为同时匹配项目文件夹名与根路径；全局搜索零结果、但关键词其实命中了项目文件夹时，直接告诉你命中几个并可一键切过去。
- 进度条统一为「下载更新」弹窗那套 layui 样式，45° 斜纹动画移除。DSH / Pi 显示名统一大写，ZCode 与 OpenCode 徽章改为灰系深浅对，README 增加 XERJ Corpus Hub 贡献者徽章。

### 兼容性

- Windows 10/11 64 位。
- 旧目录里的引擎仍会被识别，不必重新下载；想换用哪个后端，在设置页的「引擎后端」里选。
- 配置与数据仍在 `%APPDATA%\a4agent`，升级全程保留。

### 升级

直接运行 `a4agent-setup-0.6.4.exe` 覆盖安装即可。

## 校验 / Checksum

```
CA62D00C7E68CEBCD8B9B3D0963AFF83C559FEF64570D6E2356CDDAA7B5D6693  a4agent-setup-0.6.4.exe
```
