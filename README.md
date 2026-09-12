# a4agent

本地大模型推理控制台：一键把 `.gguf` 模型变成 **OpenAI 兼容的局域网 API 服务**。

a4agent 是 [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` 的 Windows 图形化封装，
负责它不擅长的那部分——**装哪个引擎、选哪个模型、给什么参数**：

- 🧭 **首次配置向导**：自动检测显卡/显存/驱动，推荐推理预设（上下文、KV 量化、MTP 投机解码），
  模型显存占用风险提示
- 📦 **轻量安装包**：安装器只含启动器（约 30 MB），首次运行按显卡自动下载
  llama.cpp 官方引擎（Vulkan / CUDA 12.4 / CUDA 13.3 / CPU），也支持离线指定引擎目录
- 🖥️ **图形化管理**：模型库扫描与 GGUF 元数据解析、引擎参数可视化调整、运行日志、托盘常驻
- 🌐 **局域网开放开关**：设置页一键切换 `127.0.0.1` / `0.0.0.0`，接入页直接给出
  curl 与 openai SDK 示例
- 🩹 **细节打磨**：崩溃与端口占用检测、健康检查轮询、定时内存裁剪（EmptyWorkingSet）

## 快速开始

1. 从 [Releases](https://gitee.com/eogee/a4agent/releases) 下载 `a4agent-Lite-setup`（推荐）或离线完整版；
2. 安装后自动打开配置向导：选引擎包（默认按显卡推荐）→ 等待下载 → 添加模型目录 → 选默认模型；
3. 完成后服务自动启动，接入页有 Base URL 与调用示例：

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "你的模型名", "messages": [{"role": "user", "content": "你好"}]}'
```

> 引擎包来自 llama.cpp 官方 GitHub Release。网络受限时可手动下载对应 zip，
> 解压后在向导/设置页指定引擎目录（包含 `llama-server.exe`）。

## 从源码构建

```powershell
# 编译（需要 .NET 8 SDK）
dotnet build src/App/App.csproj -c Release

# 无头自检（验证控件树完整性）
src/App/bin/Release/net8.0-windows/a4agent.exe --uitest

# 打包：轻量版安装器（约 30 MB，需 Inno Setup）
powershell -File installer/build.ps1 -Version 0.1.0 -Lite

# 打包：三个离线完整版（cu12.4 / cu13.3 / vulkan，需先准备 payloads）
powershell -File installer/build.ps1 -Version 0.1.0
```

## 架构

```
src/
  App/    WinForms 界面：主窗口（状态/模型/设置/接入）、首次配置向导、托盘
  Core/   引擎进程管理（llama-server 启停/健康轮询）、命令行拼装、
          引擎包目录与下载器、GGUF 解析、GPU 检测、预设推荐
  Smoke/  冒烟测试
```

配置文件优先用 exe 旁的 `config.json`（便携模式），否则用 `%APPDATA%\a4agent\config.json`。

## 引擎下载说明

- 引擎包钉定在 llama.cpp 的固定版本（见 `src/Core/Engine/EnginePack.cs` 的 `RepoTag`），
  升级时修改该文件中的 tag、资产名与体积即可；
- CUDA 引擎 = 主包 + cudart 运行库包，下载后自动解压合并到 `engine\` 目录；
- 下载先写入临时目录，校验 `llama-server.exe` 存在后原子换入，失败不会污染现有目录；
- 安全提示：引擎二进制经 HTTPS 取自 llama.cpp 官方 Release，请勿改用不可信的镜像地址。

## License

[MIT](LICENSE) — 引擎二进制归 [llama.cpp](https://github.com/ggml-org/llama.cpp) 项目所有，遵循其自身许可协议。
