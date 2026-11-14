# a4agent v0.7.1（安装即用：内嵌设备运行时解释器 / Install-and-go: embedded runtime interpreter）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.7.1.exe——八端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）

Windows: a4agent-setup-0.7.1.exe — the eight-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway").

**v0.7.1 更新内容：**

安装即用（C8 修复）——安装包内嵌 Python 运行时（python 3.12 embeddable，约 11MB）到 `node_runtime\python\`，设备解释器解析顺序将其置于最前：**全新机器无需预装任何 Python 即可拉起本机被控端**，彻底消除"目标机无匹配解释器导致设备页运行时失败"的缺口；无内嵌场景（开发态/手工构建）仍按原有候选链兜底

Agent 开放交互（B35/FR-16）——新增 `GET /api/v1/devices/overview` 聚合发现端点（一次调用返回已配对设备全景 + backend 地址 + 操作接口速查，设备令牌与指纹不出 backend）与 `POST /api/v1/devices/install-skill` 内置技能三档安装（全局技能库/已注册项目根/自定义目录，同名不覆盖）；内置 `crossdevice` 技能包（能力概述、动态发现流程、API 速查、边界声明），设备页一键安装——AI 编程 Agent 在全新工作空间即可发现并使用局域网设备控制能力，配对确认仍是设备页人工动作

设备页修复——「发现的设备」列表排除本机行（node.json device_id 识别）并自动清理离线 10 分钟的死设备残影；node 守护日志 utf-8→GBK 双编码解码兜底（修复日志窗中文乱码）；已配对设备卡片单列全宽布局；跨技能文案 i18n 中英登记

质量门槛——后端全量 763 通过；CrossDevice 侧四道发布门槛全绿（8/8、10/10、10/10、14/14）并完成双机实测收口（安装即互见/一次确认配对/防滥用全负例/双端重启免确认重连，证据见 CrossDevice 验收记录 V1.2 §5）；tag v0.5.0 已随独立包发布

---

**What's new in v0.7.1:**

Install-and-go (C8 fix) — the installer now embeds a Python runtime (python 3.12 embeddable, ~11 MB) at `node_runtime\python\`, placed first in the device interpreter resolution order: **a brand-new machine needs no pre-installed Python to start the local controlled-device node**, closing the gap where a missing interpreter broke the Devices page runtime. Non-embedded scenarios (development / manual builds) keep the original candidate chain as fallback

Agent-open interaction (B35/FR-16) — new `GET /api/v1/devices/overview` aggregate endpoint (one call returns the paired-device panorama plus backend address and an API cheat-sheet; device tokens and fingerprints never leave the backend) and `POST /api/v1/devices/install-skill` three-scope skill installation (global library / registered project roots / custom directory, no overwrite on conflict). A built-in `crossdevice` skill (capabilities, dynamic discovery flow, API cheat-sheet, boundary statement) can be installed with one click from the Devices page — AI coding agents can now discover and use LAN device control from a fresh workspace, while pairing confirmation remains a manual action on the Devices page

Devices page fixes — the "Discovered Devices" list now excludes the local machine (via node.json device_id) and auto-cleans dead-device ghosts after 10 minutes offline; node guardian logs decode as UTF-8 with GBK fallback (fixes garbled Chinese in the log pane); paired-device cards use a full-width single-column layout; cross-skill strings registered in both i18n languages

Quality gates — 763 backend tests passing; all four CrossDevice release gates green (8/8, 10/10, 10/10, 14/14) with the dual-machine acceptance closed (install-and-discover, one-confirm pairing, full anti-abuse negatives, both-sides restart reconnection — evidence in CrossDevice acceptance record V1.2 §5); CrossDevice tag v0.5.0 shipped as a standalone package

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94
