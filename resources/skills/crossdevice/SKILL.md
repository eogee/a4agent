---
name: crossdevice
description: 控制局域网内已配对的 Windows 设备（远程命令、截屏、文件上传下载、任务历史）。当任务需要在另一台机器上执行命令、取文件、看屏幕或跑长命令时使用。经本机 a4agent 本地 API 调用，仅限已配对设备。
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
| 执行命令 | `POST /api/v1/devices/{name}/exec`，体 `{"cmd": "echo hello", "shell": "cmd", "timeout": 60, "cwd": "可选"}` | 异步任务，返回 `{"job_id": "..."}`（backend 已解包 node 信封；响应不含设备凭据） |
| 取命令结果 | `GET /api/v1/devices/{name}/job/{job_id}?wait=30` | `wait` 长轮询秒数（≤120）；status ∈ running/done/timeout/failed，stdout/stderr 在 data 里 |
| 截屏 | `GET /api/v1/devices/{name}/screen` | 返回 PNG 二进制（原始分辨率） |
| 截屏并保存 | `POST /api/v1/devices/{name}/screen/save`，体 `{"save_dir": "本地目录"}` | 返回 `{"saved_path": "..."}` |
| 上传文件 | `POST /api/v1/devices/{name}/put`，multipart 字段 `file` + 表单 `remote_path`（远端绝对路径） | 4MB 分块 + 断点续传 + MD5 校验 |
| 下载文件 | `GET /api/v1/devices/{name}/get?path=<远端绝对路径>&save_dir=<本地目录>` | 流式下载，返回 `{"saved_path": "..."}` |
| 任务历史 | `GET /api/v1/devices/{name}/tasks?status=&limit=50&offset=0` | 按 a4agent 身份过滤 |
| 任务详情 | `GET /api/v1/devices/{name}/tasks/{task_id}` | 含 stdout/stderr |

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

## 错误形态

业务失败返回 HTTP 4xx/5xx，`{"detail": {"code": "<错误码>", "message": "<中文描述>"}}`；常见码：`NOT_FOUND`（设备名不存在）、`BAD_REQUEST`（参数错误）、`PAIR_*`（配对相关——你不应触发）。长命令记得用 `job_id` 轮询而不是反复发起。
