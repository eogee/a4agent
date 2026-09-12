# a4agent

**本地大模型推理控制台**：把任意 `.gguf` 模型一键变成 **OpenAI 兼容的局域网 API 服务**。a4agent 是 [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server` 的 Windows 图形化封装，负责引擎本体不擅长的部分——**装哪个引擎、选哪个模型、给什么参数**，全程可视化操作。

| 能力 | 说明 |
|---|---|
| **首次配置向导** | 自动检测显卡 / 显存 / 驱动版本，推荐推理预设（上下文长度、KV 缓存量化、MTP 投机解码），并对所选模型给出显存占用风险提示 |
| **引擎自动获取** | 轻量安装包仅约 64 MB；首次运行按显卡自动下载 llama.cpp **官方预编译引擎**（Vulkan / CUDA 12.4 / CUDA 13.3 / CPU），也支持离线指定引擎目录 |
| **图形化管理** | 模型库扫描 + GGUF 元数据解析（量化 / 原生上下文 / MTP 层）、引擎参数可视化调整、运行日志、托盘常驻 |
| **局域网开放** | 设置页一键切换 `127.0.0.1` / `0.0.0.0` 监听，接入页直接给出本机局域网地址与 curl / openai SDK 调用示例 |

---

## 首次配置向导

安装完成首次启动时，向导按「获取引擎 → 检测硬件 → 选择模型目录 → 选择默认模型 → 服务端口」五步完成全部配置，之后服务即可一键启动。

### 硬件检测与预设推荐

- 通过 `nvidia-smi` 与注册表枚举显卡，列出**厂商、显存、驱动版本**；
- 依据显存大小与厂商推荐推理预设档位（上下文长度、KV 缓存级别、是否开启 MTP），并在后续选择模型时评估显存溢出风险——提示以当前预设运行该模型是否安全；
- 预设只是起点，所有参数稍后都能在「设置」页调整。

### 引擎获取（轻量版安装包）

- 向导按检测结果自动预选引擎包并标记「推荐」，点击下一步即自动下载安装，带进度显示与取消；
- 没有网络或已有引擎的用户可改选**离线安装**（指定包含 `llama-server.exe` 的目录）或**暂时跳过**（之后在设置页指定）；
- 完整版安装包（内置引擎）不会出现这一步。

---

## 引擎自动获取

### 引擎包目录

引擎包钉定在 llama.cpp 官方 Release 的**固定版本**，不自动追新——二进制可复现、升级经人工验证后再推送。当前钉定的 tag 以 `src/Core/Engine/EnginePack.cs` 的 `RepoTag` 为**单一事实来源**（升级 = 改这一个文件，同步资产名与体积即可），对应官方发布页为 `https://github.com/ggml-org/llama.cpp/releases/tag/<RepoTag>`：

| 引擎包 | 体积 | 适用 | 要求 |
|---|---|---|---|
| **Vulkan（默认推荐）** | 约 30 MB | NVIDIA / AMD / Intel 独显与核显通用 | 近年驱动即可 |
| CPU | 约 18 MB | 无可用显卡时的兜底 | — |
| CUDA 12.4 | 约 616 MB | NVIDIA | 驱动 ≥ 550 |
| CUDA 13.3 | 约 516 MB | NVIDIA | 驱动 ≥ 580 |

- NVIDIA 显卡按**驱动大版本**自动选择 CUDA 12.4 / 13.3，驱动过旧自动降级到 Vulkan；无显卡选 CPU；
- CUDA 引擎 = 主包 + cudart 运行库包，下载后自动解压**合并**到 `engine\` 目录，无需手动处理。

### 下载与安装安全

- 引擎二进制经 **HTTPS 取自 llama.cpp 官方 GitHub Release**，请勿改用不可信的镜像地址；
- 下载先写入临时目录，解压并确认 `llama-server.exe` 存在后**原子换入** `engine\` 目录——失败或中途取消不会留下半个引擎目录，也不影响已有引擎；
- 网络受限环境可手动下载后离线安装（解压后在向导 / 设置页指定引擎目录）：
  - **官方发布列表**：https://github.com/ggml-org/llama.cpp/releases ；当前钉定版本的发布页为 `https://github.com/ggml-org/llama.cpp/releases/tag/<RepoTag>`（`<RepoTag>` 以 `src/Core/Engine/EnginePack.cs` 为准）
  - 各引擎包对应的资产（zip）文件名，`<tag>` 即 `RepoTag`：
    - Vulkan：`llama-<tag>-bin-win-vulkan-x64.zip`
    - CPU：`llama-<tag>-bin-win-cpu-x64.zip`
    - CUDA 12.4：`llama-<tag>-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`
    - CUDA 13.3：`llama-<tag>-bin-win-cuda-13.3-x64.zip` + `cudart-llama-bin-win-cuda-13.3-x64.zip`
  - CUDA 引擎需把主包与 cudart 包的文件解压合并到**同一个**引擎目录；cudart 包文件名不含 tag，跟随 CUDA 大版本

---

## 模型库与默认模型

- 可添加多个模型目录，自动扫描 `.gguf` 并解析元数据：**文件大小、量化级别、原生上下文、是否含 MTP 层**（支持投机解码），解析失败的文件灰显并标注原因；
- 一键「设为默认模型」，服务启动时自动加载；模型别名取文件名；
- 目录与模型清单持久化保存，支持便携模式（exe 旁 `config.json`）。

---

## 服务运行与局域网开放

- 「状态」页实时显示运行状态（启动中 / 运行中 / 失败 / 已停止）与引擎日志；健康检查就绪后通知；
- 端口占用、引擎缺失、进程崩溃均有明确日志提示；支持定时内存裁剪（释放文件缓存）；
- **局域网开放开关**：设置页勾选「允许局域网设备访问」即切换为 `0.0.0.0` 监听，保存并重启服务生效；
- 「接入」页自动生成：Base URL、Chat Completions 完整地址、模型名（`model` 字段）、局域网调用地址（开放后显示实际局域网 IP），以及可直接复制的 **curl** 与 **openai SDK（Python）** 示例。

---

## 下载、安装与使用

### 下载安装

从 **发行版（Release）** 页面下载安装包 `a4agent-Lite-setup-*.exe`：

- **下载地址**：https://gitee.com/eogee/a4agent/releases （选择最新版本）
- **系统要求**：Windows 10/11 64 位
- 安装包约 64 MB（v0.2.0），SHA256 校验值见对应 Release 说明
- 建议核对下载页提供的 SHA256 校验值，确保文件完整未被篡改

安装包采用**每用户安装（免 UAC）**：双击运行后按向导安装到当前用户目录，全程无需管理员权限，自动创建开始菜单与桌面快捷方式。

### 运行

1. 安装完成后，从**开始菜单**或**桌面快捷方式**启动 a4agent，自动进入首次配置向导；
2. 首次安装/运行时若出现 **Windows SmartScreen 提示**，点击「更多信息 → 仍要运行」即可（应用未做商业代码签名，属正常现象，不影响功能）；
3. 引擎下载完成后添加模型目录 → 选默认模型 → 完成，服务自动启动，到「接入」页复制调用地址；
4. 主窗口关闭 = 最小化到托盘，服务继续运行；右键托盘图标可退出（会先停止服务）。

### 数据与隐私

- 配置优先使用 exe 旁的 `config.json`（便携模式，适合 U 盘 / 绿色部署），否则使用 `%APPDATA%\a4agent\config.json`；
- 引擎安装到安装目录的 `engine\` 子目录；a4agent 本身不采集任何数据，推理请求全部在本机完成。

### 常见问题

- **局域网设备连不上**：检查设置页是否已勾选「允许局域网设备访问」并重启服务；然后放行 Windows 防火墙（首次以 `0.0.0.0` 启动时系统会弹窗，点「允许」；或手动为 `engine\llama-server.exe` 添加入站规则）
- **局域网开放安全吗**：服务默认无鉴权，开放后同网段任何设备都能调用。不可信网络请在「设置 → 额外参数」加 `--api-key <你的密钥>`，客户端侧同样传入即可
- **引擎下载失败 / 太慢**：向导引擎页改选「离线安装」，手动下载官方 zip 解压后指定目录；CUDA 包体大，网络不佳建议先用 Vulkan 包
- **升级**：直接运行新版安装包覆盖安装即可，配置（含已下载的引擎）会保留
- **卸载**：在「设置 → 应用」中卸载；配置文件残留在 `%APPDATA%\a4agent\`，如需彻底清除请手动删除
- **反馈问题**：请附上主窗口「状态」页的日志文本（全选复制即可），便于定位

### 提交 Issue 要求

提交 Issue 前请确认以下信息，缺失可能导致问题无法定位：

1. **明确类型**：Bug 报告 / 功能建议 / 使用疑问
2. **环境信息（必填）**：a4agent 版本号、显卡型号与驱动版本、引擎包类型（Vulkan / CUDA / CPU）、操作系统版本
3. **模型信息**：模型名称、量化级别、文件大小
4. **复现步骤**：从打开应用到出现问题的完整操作路径，尽量写明「做了什么 → 实际结果 → 预期结果」
5. **日志与截图**：「状态」页日志的相关片段、界面报错文案与截图
6. **排查先行**：提交前先自查——重启服务、确认引擎已下载完成、确认端口未被占用、确认防火墙已放行

> 提交前请先搜索是否已有相同 Issue，避免重复提交。

---

## 自动更新与安全

应用内置自更新：发布新版后，应用在启动时静默检查，或在「设置 → 软件更新」点「检查更新」手动检查；发现新版本后确认下载，校验通过即可一键升级（安装器自动完成覆盖，配置与已下载引擎全部保留）。也可「跳过此版本」。

### 更新源与校验（防 MITM / 防伪造）

- **双源**：更新清单 `latest.json` 同时发布到 GitHub 与 Gitee，客户端并行拉取、首个通过验签的生效；安装包下载 GitHub 失败自动回退 Gitee。
- **清单签名**：发布侧用 Ed25519 私钥签名 `latest.json`（版本、更新说明、安装包 SHA256 等字段），应用内置对应公钥验签；任何字段异常或签名不符，清单直接作废、**不弹更新提示**。URL 不入签名，因此两份清单字节一致、共用同一签名，URL 指向的内容由被签名的 SHA256 绑死。
- **完整性校验**：安装包边下边算 SHA256，与签名清单比对通过才落盘；点击「安装并关闭程序」时会对磁盘文件**再次复核**才拉起安装器。
- **传输白名单**：仅 HTTPS，且重定向逐跳校验主机白名单（`github.com` / `gitee.com` / `*.githubusercontent.com` / `*.gitee.com`），拦截跳转到任意域名。
- **防降级**：候选版本需严格高于当前版本；预发布版本仅当当前运行版本也是预发布时才提示。
- **尺寸上限**：清单 512KB、安装包 300MB，超限拒绝；下载只写入应用数据目录（`%APPDATA%\a4agent\updates\`），不信任系统临时目录。

### 发布新版（维护者）

```powershell
powershell -File installer/build.ps1 -Version 0.2.1 -Lite      # 构建安装包（版本号同步写入程序集）
git tag v0.2.1; git push origin v0.2.1; git push github v0.2.1 # 推送标签
node tools/update-manifest.js --installer installer/out/a4agent-Lite-setup-v0.2.1.exe `
    --version 0.2.1 --body-file installer/out/RELEASE-NOTES-v0.2.1.md
```

发布脚本自动完成：生成并 Ed25519 签名 `latest.json` → 在两平台查找/创建 Release → 删除同名旧资产 → 上传安装包与清单 → 更新发布说明。Token 通过环境变量 `A4AGENT_GITEE_TOKEN` / `A4AGENT_GITHUB_TOKEN` 或 `--token-file` 提供。

签名私钥位于 `.claude/keys/update-signing.pem`（已 gitignore，**绝不入库**）；私钥一旦泄漏必须吊销轮换：生成新密钥 → 替换 `src/Core/Update/Updater.cs` 内置公钥 → 重新发版。协议一致性由 `Smoke --updatetest` 保证（node 签名 ↔ C# 验签跨语言互验 + 篡改拒收用例）。

---

## 安全设计

- **引擎供应链**：引擎二进制仅从 llama.cpp 官方 Release 经 HTTPS 下载，钉定固定版本、升级需显式修改目录代码；解压条目做路径逃逸防护；落盘前确认关键文件存在，安装采用临时目录 + 原子换入，杜绝半成品引擎被启动
- **默认不对外**：监听地址默认 `127.0.0.1`，开放局域网是显式的一次开关操作，且接入页与文档均给出 `--api-key` 鉴权指引
- **进程管理**：引擎以子进程方式受控启停（进程树整体结束），退出应用前自动停止服务，避免后台残留
- **单实例**：Windows 命名互斥体保证同时只有一个控制台实例

---

## 开发

环境要求：Windows 10+，[.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)

```bash
dotnet build src/App/App.csproj -c Release      # 编译
src/App/bin/Release/net8.0-windows/a4agent.exe --uitest      # 无头自检（控件树完整性）
src/Smoke/bin/Debug/net8.0-windows/Smoke.exe --updatetest    # 更新协议自检（签名互验/篡改拒收）
```

第三方组件：[BouncyCastle.Cryptography](https://github.com/bcgit/bc-csharp)（MIT 许可，用于更新清单 Ed25519 验签）。

## 打包

前置：编译安装包需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)。

```bash
powershell -File installer/build.ps1 -Version 0.2.0 -Lite   # 轻量版安装包（约 64 MB，引擎在线下载）
powershell -File installer/build.ps1 -Version 0.2.0         # 三个离线完整版（cu12.4 / cu13.3 / vulkan，需先准备 payloads）
```

版本号由 `-Version` 指定；轻量版 / 完整版由 `installer.iss` 同一模板实例化（是否传 `/DEngineSource` 决定是否内置引擎）。

## 项目结构

```
src/
  App/     WinForms 界面：主窗口（状态/模型/设置/接入）、首次配置向导、托盘
  Core/    引擎进程管理（llama-server 启停/健康轮询/内存裁剪）、命令行拼装、
           引擎包目录与下载器、GGUF 解析、GPU 检测、预设推荐
  Smoke/   冒烟测试
installer/ Inno Setup 模板与打包脚本
```
