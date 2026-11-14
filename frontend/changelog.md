# a4agent v0.7.0（设备互联：一次确认配对跨机免手工 / Device networking: one-confirm pairing）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.7.0.exe——八端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）。本版起安装包内嵌 CrossDevice 设备运行时，安装即具备跨设备控制能力

Windows: a4agent-setup-0.7.0.exe — the eight-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway"). This release embeds the CrossDevice device runtime, so cross-device control works right after install.

**v0.7.0 更新内容：**

新增「设备」页——CrossDevice 设备运行时（MIT 协议）随安装包交付到安装目录 `node_runtime/`，应用首启自动以独立子进程拉起本机被控端并守护：退出自动重启、连续崩溃熔断、TLS 证书缺失自动生成；也可在设备页手动指定 node.py 路径（开发态可直接指向 CrossDevice 仓）

局域网自动发现——双端安装 a4agent 后，同网段设备经 UDP 广播自动出现在「发现的设备」列表（约 10 秒心跳、30 秒无帧判离线、同源限频去重）；广播只提供"看得见"，**不自动建立信任**；未广播的设备可手动添加 IP

一次确认配对——控制端点「发起配对」→ 被控端弹出确认窗（显示来源 IP，可拒绝）→ 确认后同屏展示一次性 6 位配对码 + 5 分钟倒计时 → 控制端输码即建立连接，此后该设备对长期免手工；配对码一次性、猜错达上限（默认 5 次）即作废本次会话、发起端受 10 秒限频保护；配对成功/失败/拒绝全程落被控端审计

配对后免手工操作——远程命令（异步任务与任务历史）、全屏截屏（原始分辨率 PNG 本地保存）、文件上传与下载（4MB 分块 + 断点续传 + MD5 校验）、任务历史按身份过滤；已配对设备的凭据（令牌与证书指纹）经 Windows DPAPI 加密落盘，接口与文件层均不回传明文；证书指纹变更会被拒绝，可在设备页重置指纹后重新连接

边界如实声明——未配对设备对被控端的请求一律 401；非私网来源一律拒绝；**不提供同网段静默互信**（每台新设备都需要被控端本地一次确认）；被控端拒绝则连接不建立

质量门槛——后端新增测试 67 条、全量 753 通过；CrossDevice 侧配对冒烟 14/14、四道发布门槛（8/8、10/10、10/10、14/14）全绿；安装包内嵌 node 运行时与 CrossDevice MIT LICENSE 文本

---

**What's new in v0.7.0:**

New "Devices" page — the CrossDevice device runtime (MIT licensed) ships inside the installer at `node_runtime/`; on first launch a4agent starts the local controlled-device node as an independent supervised subprocess: auto-restart on exit, crash circuit-breaker, and automatic TLS certificate generation when missing. You can also point the page at a custom node.py path (in development, the CrossDevice repo works directly)

LAN auto-discovery — with a4agent installed on both machines, devices in the same subnet appear automatically under "Discovered Devices" via UDP broadcast (roughly a 10-second heartbeat, 30 seconds without frames marks offline, per-source rate limiting and dedup). The broadcast only makes devices visible and **never grants trust by itself**; non-broadcasting devices can be added by IP

One-confirm pairing — click "Pair" on the controller → the controlled side pops a confirmation window (showing the source IP, with a Reject button) → once confirmed, a one-time 6-digit pairing code with a 5-minute countdown is shown on screen → enter the code on the controller and the pair is established for good, no more manual setup. Codes are single-use, guessing past the limit (5 by default) voids the session, and the initiating side is rate-limited (10 s). Every success, failure and rejection lands in the controlled side's audit log

Hands-free operation after pairing — remote commands (async jobs with history), full-screen screenshots (original-resolution PNG saved locally), file upload/download (4 MB chunks + resumable transfer + MD5 verification), and task history filtered by identity. Credentials of paired devices (tokens and certificate fingerprints) are stored locally encrypted with Windows DPAPI and never returned in plain text; a fingerprint change is rejected and can be reset from the Devices page to reconnect

Honest boundaries — requests from unpaired devices are always 401; non-private-network sources are always rejected; **no silent same-subnet trust** (every new device needs one local confirmation on the controlled side); rejection means no connection

Quality gates — 67 backend tests added, 753 passing; CrossDevice pairing smoke 14/14 with all four release gates green (8/8, 10/10, 10/10, 14/14); the installer embeds the node runtime plus the CrossDevice MIT LICENSE text

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94

## 校验 / Checksum

```
6a0fb6b955003feea13d27bf9fc8c3c377c5bb65ba1620ac15b8e8eea5532781  a4agent-setup-0.7.0.exe
```
