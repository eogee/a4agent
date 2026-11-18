---
name: crossdevice
description: 控制局域网内已配对的 Windows 设备（远程命令、截屏、文件上传下载、任务历史），并查询对端设备上各宿主会话的进度状态（hook 自动上报）。当任务需要在另一台机器上执行命令、取文件、看屏幕、跑长命令，或需要了解/跟进另一台设备上正在运行的会话进度时使用。经本机 a4agent 本地 API 调用，仅限已配对设备。
---

# CrossDevice 设备控制（经 a4agent）

本机 a4agent 内嵌 CrossDevice 设备运行时：局域网内**已配对**的 Windows 设备可以被你远程操作——执行命令、截屏、上传/下载文件、查询任务历史。连接与凭据由 a4agent 托管，你只需要调本机 HTTP 接口。

## 第一步：找到 a4agent 本地接口地址

backend 只监听本机回环，端口记录在实例文件中（JSON）：

```
%APPDATA%\a4agent\desktop.json   →   {"port": <整数>, "pid": <整数>}
```

读出 `port` 后，接口基址为 `http://127.0.0.1:<port>`。文件不存在或读不出来 = a4agent 未运行，请提示用户先启动 a4agent（不要猜测端口）。

## 第二步：动态获取设备全景（每次会话先调一次）

```
GET {base}/api/v1/devices/overview
```

返回：`devices`（已配对设备清单：name / ip / port / hostname / device_id / identity）、`endpoints`（操作接口速查）、`notes`（使用边界）。**设备清单以本次返回为准**——设备会增减、上下线，不要依赖历史记忆。

## 第三步：按设备名调用操作接口

| 操作 | 调用 | 说明 |
|---|---|---|
| 设备全景 | `GET /api/v1/devices/overview` | 每次会话首调 |
| 已配对设备列表 | `GET /api/v1/devices` | `{"devices": [...]}` |
| 探活 | `POST /api/v1/devices/{name}/ping` | 返回 node 版本与存活状态 |
| 执行命令 | `POST /api/v1/devices/{name}/exec`，体 `{"cmd": "echo hello", "shell": "cmd", "timeout": 60, "cwd": "可选"}` | 异步任务，返回 `{"job_id": "..."}`（backend 已解包 node 信封；响应不含设备凭据）。**命令引号逐字生效**（node v0.5.1+）：参数含空格直接写引号，如 `tasklist /FI "IMAGENAME eq x.exe"`、`python -c "..."` |
| 取命令结果 | `GET /api/v1/devices/{name}/job/{job_id}?wait=30` | `wait` 长轮询秒数（≤120）；status ∈ running/done/timeout/failed，stdout/stderr 在 data 里 |
| 截屏 | `GET /api/v1/devices/{name}/screen` | 返回 PNG 二进制（原始分辨率） |
| 截屏并保存 | `POST /api/v1/devices/{name}/screen/save`，体 `{"save_dir": "本地目录"}` | 返回 `{"saved_path": "..."}` |
| 上传文件 | `POST /api/v1/devices/{name}/put`，multipart 字段 `file` + 表单 `remote_path`（远端绝对路径） | 4MB 分块 + 断点续传 + MD5 校验。**受被控端工作空间白名单约束**（见下方"上传被拒排查"） |
| 下载文件 | `GET /api/v1/devices/{name}/get?path=<远端绝对路径>&save_dir=<本地目录>` | 流式下载，返回 `{"saved_path": "..."}` |
| 查询可写目录 | `GET /api/v1/devices/{name}/workspaces` | 返回被控端 `workspace_roots`（允许根列表）与 `workspace_staging`（默认暂存目录）；只读，改白名单需在被控端本地配置 |
| 任务历史 | `GET /api/v1/devices/{name}/tasks?status=&limit=50&offset=0` | 按 a4agent 身份过滤 |
| 任务详情 | `GET /api/v1/devices/{name}/tasks/{task_id}` | 含 stdout/stderr |
| 会话状态汇报 | `GET /api/v1/devices/{name}/status` | 被控端宿主会话经 hook **自动上报**的最新进度（见下方「跨设备状态汇报」节）；全部设备一次拉取用 `GET /api/v1/devices/status` |

调用示例（读端口并执行命令）：

```bash
PORT=$(python -c "import json,os;print(json.load(open(os.path.expandvars(r'%APPDATA%\a4agent\desktop.json')))['port'])")
BASE=http://127.0.0.1:$PORT
curl -s "$BASE/api/v1/devices/overview"
curl -s -X POST "$BASE/api/v1/devices/pc-a/exec" \
     -H "Content-Type: application/json" -d '{"cmd":"echo hello","shell":"cmd"}'
curl -s "$BASE/api/v1/devices/pc-a/job/<job_id>?wait=30"
```

## 使用边界（务必遵守）

1. **只能操作 overview 列出的已配对设备**。配对（建立新信任）是设备所有者在 a4agent 设备页人工确认的动作——**你没有发起配对的能力，也不要尝试**。
2. **不要索要凭据**。设备令牌与证书指纹由 a4agent 加密托管，不会出现在任何接口响应中。
3. **命令会被审计记录**。远程命令在被控端有完整审计（谁、何时、执行了什么），只执行任务所需范围内的命令；`shell=powershell` 时同样如此。
4. 设备可能离线：调用失败时先重新拉 overview 或对该设备 `ping`，不要盲目重试。
5. 大文件传输（put/get）自带分块与校验，无需你手写分块逻辑。
6. **写方向操作受被控端工作空间白名单约束**（put、写/删/建目录/移动）：默认仅 **node 暂存目录**可写。上传的脚本/临时产物先 put 到暂存目录再 exec 执行；需要写入其他位置时，把期望路径告知设备所有者在其被控端 `node.json` 的 `workspace` 键中添加（a4agent 无法也不应远程修改白名单）。

## 上传被拒排查（403 SANDBOX_DENIED）

- `put`/写接口返回 403 `SANDBOX_DENIED` = 目标路径不在被控端白名单内，**不是 bug**。
- 报错 `message` 会列出当前允许的根，`detail.allowed_roots`（node v0.5.1+）为机读数组；也可主动调 `GET /api/v1/devices/{name}/workspaces` 查询。
- 默认白名单 = 仅暂存目录（被控端 node 数据目录下 `tmp`，如 `%APPDATA%\a4agent\node\tmp`）；`workspace_staging` 字段给出确切路径。
- 扩大范围：设备所有者编辑被控端 `%APPDATA%\a4agent\node\node.json`，加 `"workspace": ["C:\\某目录"]` 后重启 node（或以 `--workspace "C:\\某目录"` 启动参数）。

## 大 payload / 长脚本投递

- **不要把长脚本或大段内容塞进 `cmd`**：cmd 单条命令行上限 8191 字符，多层引号转义也易出错。长内容一律走文件通道。
- 推荐两步组合：
  1. **写入暂存目录**：`POST /api/v1/devices/{name}/put`（multipart 上传，分块且无大小上限），`remote_path` 填暂存目录下的绝对路径——先调 `GET /api/v1/devices/{name}/workspaces` 拿 `workspace_staging` 的确切路径。
  2. **exec 执行该文件**：如 `{"cmd": "python C:\\...\\tmp\\task.py", "shell": "cmd"}`（命令引号逐字生效，见上表）；长输出用 `job_id` 轮询取回，不要反复重新发起。
- PowerShell 长脚本同理：上传后 `{"cmd": "powershell -File C:\\...\\tmp\\task.ps1", "shell": "cmd"}`；`shell=powershell` 走 `-EncodedCommand` 语义，内嵌引号同样可靠。
- 大数据文件（非脚本）同样先 put 上传、再在 exec 命令里引用该路径处理，产物用 `get` 下载回来。
- 暂存目录恒可写（被控端 node 数据目录下 `tmp`），是这套组合的默认落点；产物清理可在 exec 里执行 `del <路径>`，或交由被控端自行管理。

## 宏（xd macro rec/run）——控制端 CLI 能力（非 a4agent HTTP 接口）

`xd macro rec/run` 是 CrossDevice **控制端 CLI（xd.py）** 的本地子命令（契约 V1.7 新增，纯控制端本地功能，协议零变更）：把本机（控制端）录下的一段键鼠操作存成宏文件，再按原节奏回放到任意已注册设备。它须在**装有 CrossDevice 控制端的机器上本地执行**（如在本机 shell 直接运行 `py xd.py macro ...`）；a4agent 本地 HTTP 接口**不提供调宏端点**（上表没有，也不存在），不要尝试经 a4agent HTTP API 调宏。

- **宏 = 白名单动作类型序列**——**不允许任意脚本**（D30 红线：不支持循环、条件、变量、表达式、外部命令）；宏文件只在控制端本地解析与严格校验，不进入被控端执行面，加载校验失败即整体拒绝、不执行任何动作。
- **回放**（`xd macro run <设备> <宏文件> [--speed <倍率 0.01–100，默认 1.0>]`）：按宏内延迟**分段**经既有 `/v1/input` 发送，**全程持输入租约**；他人持有时返回 `INPUT_LEASE_TAKEN` 且不执行；**Ctrl+C 或异常中断会立即 best-effort 释放输入租约**（FR-06"中断即释放"），正常回放结束也会自动释放（不等 60 秒过期）。
- **录制**（`xd macro rec <输出宏文件> [--seconds <N> 默认 10，Ctrl+C 提前结束]`）：经 Windows 低级键鼠钩子（WH_MOUSE_LL/WH_KEYBOARD_LL，纯标准库 ctypes）捕获**控制端本机**键鼠；忽略注入事件（回放不会被自录进去）；钩子安装失败会明确报错。录制完成后**不自动回放**。

宏文件格式、去抖/聚合阈值与示例见 CrossDevice README"宏（macro rec/run）"节与接口契约 §11。

## 跨设备状态汇报（B→A hook 自动上报）——查对端会话进度

被控端（B 侧）上的宿主会话在**关键节点自动向控制端（A 侧）上报状态**，无需任务提示词写任何上报逻辑：a4agent 的 hook 注册器已在宿主配置中挂好 hook（ZCode＝`~/.zcode/cli/config.json` 的 `hooks.events.{Stop, PreToolUse:AskUserQuestion, PermissionRequest}` → `a4agent.exe hook zcode`；覆盖七端，幂等注册，详见 a4agent `backend/app/hooks/register.py`）。hook 触发时把一条状态原子写入被控端 `node/tmp/status/<session_id>.json`（不含令牌/证书/对话正文，workspace 只留目录名），A 侧 backend **每 15 秒轮询拉取**入库。

- **查询**：`GET /api/v1/devices/{name}/status`（单台）或 `GET /api/v1/devices/status`（全部设备一次拉）。响应 `{device, hostname, last_error, sessions: [...]}`；`sessions` 每会话只留最新一条、按时间倒序，字段：`session_id`／`event`／`state`／`agent`／`workspace`／`summary`（≤200 字人读摘要）／`ts`／`age_seconds`／`stale`（>45 秒未更新）及中文标签。
- **关键事件**（会触发 A 侧人读提醒：桌面弹窗/手机推送）：`hook.stop`＝回合完成、`hook.permission`＝权限申请、`hook.question`＝提问等待；其余事件只入库不打扰人。状态按会话保留 7 天、每设备上限 1000 行。

**Agent 使用纪律**：

1. 在对端发起长程任务（如粘贴长任务提示词、启动长会话）后，**不要在任务提示词里写轮询/汇报要求**——宿主级 hook 对该端所有会话自动生效；你要做的是事后用 status 端点查进度，而不是设计上报机制。
2. 经 `/exec`、`/screen` 做的自动化操作**不产生 hook 事件**（它们走 a4agent 通道，不是宿主会话）——查会话进度用 status 端点，不要反复截屏当轮询器。
3. `sessions` 为空 ≠ 设备离线或链路故障：目标会话可能还在长回合中（Stop 尚未触发）。判定顺序：先 `ping` 设备 → 再看响应里 `last_error` → 再下结论。
4. status 只覆盖**会话级**节点（回合完成/提问/权限申请）；批次级里程碑进度不在本通道，仍在项目自身的文档与 git 里——两层互补，不要期待 status 携带任务内容。
5. 若发现某宿主未注册 hook（宿主配置无对应段），提示用户在 a4agent 中执行 hook 注册（幂等、覆盖七端），**不要手写宿主 hook 配置**。
6. **验证/调试 hook 链路时**：向 `a4agent.exe hook <agent>` 喂测试载荷须用 `Start-Process -RedirectStandardInput <载荷文件> -Wait -PassThru`（GUI 子系统 exe 经 PowerShell/cmd 管道直喂会丢 stdin 且进程树挂住直到超时——表现为载荷为空不写状态、`$LASTEXITCODE` 为空，**极易误判"链路不通"**）；exe 冷启动数十秒属正常；合成测试用独立标注的 session_id（如 `a4hook-chain-test-<日期>`），验证后 B/A 两侧痕迹保留 7 天自动清理，无需手删。

### 远程批办（**规划中，未实现——勿调用不存在端点**）

「A 侧替用户处置对端提问/提权」已立项（见 a4agent 仓 `docs/需求识别-远程批办.md`，2026-11-19 拍板）：拟新增 B 侧 pending 队列＋hook 双通道等待（本地注入与 ntfy 先到先得）＋A 侧 `GET /devices/{name}/pending` 与 `POST /devices/{name}/pending/{id}/respond` 两端点＋设备页批办卡；三红线＝**默认关闭（B 侧所有者显式开启 assist 档）／工具白名单（危险类永不远程批）／逐条审计＋身份标注（agent 代答与用户点按区分并写回 B 侧会话）**。**实现入库前，本节所述端点不存在，不要尝试调用**；现状请依本节上文「跨设备状态汇报」的可见性通道，处置仍由 B 侧本机 UI 或手机（out 档）完成。

## 错误形态

业务失败返回 HTTP 4xx/5xx，`{"detail": {"code": "<错误码>", "message": "<中文描述>"}}`；常见码：`NOT_FOUND`（设备名不存在）、`BAD_REQUEST`（参数错误）、`SANDBOX_DENIED`（写路径越白名单，见上节）、`PAIR_*`（配对相关——你不应触发）。长命令记得用 `job_id` 轮询而不是反复发起。
