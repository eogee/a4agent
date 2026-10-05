# a4agent

**六端 AI 编程工具管理台 + 本地大模型推理控制台**：为 **Claude Code、Codex、dsh（DeepSeek Harness）、ZCode（智谱 Agentic 开发环境）、pi（本地 pi coding agent）与 Qoder（AI IDE）** 提供统一的**技能管理（Skill）**、**MCP 管理**与 **API 服务商切换**（Qoder 端托管技能与 MCP，不参与 API 切换，原因见「API 服务商切换」），并内置 **llama.cpp 本地模型推理**（原 a4agent 能力完整合并）。所有操作通过可视化界面完成，无需手动编辑配置文件。

| 能力 | 说明 |
|---|---|
| **技能管理** | 六端全局/项目级 skill 自动发现、聚合标注、跨端迁移、回收站恢复，一键把项目 skill 补齐到所有缺失的端 |
| **MCP 管理** | 六端 MCP server 自动发现与**一键安装**、跨端迁移（按传输能力矩阵校验）、快照回收站、自动/自定义**功能介绍**，密钥全程脱敏/加密 |
| **API 切换** | 五端不同服务商、模型、API Key 一键切换，配置自动备份、原子写入，密钥 DPAPI 加密存储 |
| **本地模型** | 把任意 `.gguf` 模型一键变成 OpenAI 兼容的本地/局域网 API 服务：引擎按显卡自动获取、模型库扫描、显存风险评估，一键接入五端配置方案 |
| **任务下发** | 让各端 CLI 以**无头模式**后台执行任务：下发即返回、下发前自动预检（引擎 / 服务商连通 / 端配置一致）、队列轮询、产出留档、取消与超时、关窗不杀任务 |

---

## 技能管理

四个工具均使用同一套技能格式（`<skill-name>/SKILL.md` 目录 + frontmatter `name`/`description`），因此 a4agent 可以把它们当作一种资源统一管理。

### 发现与聚合

- 自动扫描六端**全局**与**项目级**技能目录，以 frontmatter `name` 为唯一标识做**聚合标注**：同名技能跨端存在时自动标记「已在 N 端存在」。
- 项目根目录列表可配置（默认扫描 `C:\ProgramMine`），只收录含至少一个 skill 的项目。
- Codex 全局的保留目录（`.system/` 等点开头目录）自动跳过，不视为用户技能。

### 跨端迁移与「一键适配六端」

- 任意一端的 skill 可迁移到任意目标端（全局或任意项目），迁移为**非破坏复制**：源端永久保留。
- 迁移目标支持**自选项目文件夹**：目标项目无需事先存在任何 skill，`.{tool}/skills` 目录缺失时自动创建；桌面端直接调用系统目录选择框，浏览器端退化为输入路径。
- 目标端已存在同名技能（frontmatter name 或目录名任一命中）时，旧版自动移入回收站而非静默覆盖。
- **「一键适配六端」**：一键把项目内全部 skill 按其缺失的端一次补齐，先弹计划清单再执行，带进度条并临时锁定页面，防止中途误操作。
- 每次迁移写入**迁移日志**（时间、Skill、源、目标、结果），全程可追溯。

### 回收站

- 删除的 skill 移入回收站，**30 天内**可恢复原位或彻底删除；恢复时原位置被占用会明确报错，绝不覆盖。
- 过期条目在下次访问回收站时惰性清理，并在返回结果中提示本次清理数量。

### 六端技能目录

| 工具 | 全局（用户级） | 项目级 |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `<项目>/.claude/skills/` |
| Codex | `~/.codex/skills/` | `<项目>/.codex/skills/` |
| dsh | `~/.dsh/skills/`（`$DSH_HOME` 可覆盖） | `<项目>/.dsh/skills/` |
| ZCode | `~/.zcode/skills/` | `<项目>/.zcode/skills/` |
| pi | `~/.pi/agent/skills/`（`$PI_CODING_AGENT_DIR` 可覆盖） | `<项目>/.pi/skills/` |
| Qoder | `~/.qoder/skills/`（`$A4AGENT_QODER_HOME` 可覆盖） | `<项目>/.qoder/skills/` |

> ZCode 官方还识别跨工具兼容目录 `~/.agents/skills` 与 `<项目>/.agents/skills`；a4agent 统一托管到 `.zcode` 前缀，与其它端保持一致。迁移到 ZCode 的 skill 会被 ZCode 客户端真实读取。
>
> pi 的全局技能在 **agent 子目录**下（`~/.pi/agent/skills`，不是 `~/.pi/skills`）。pi 对 skill 的校验比其它端严格：`name` 只能是小写字母、数字与连字符（≤64 字符，不得以连字符首尾或含连续连字符），`description` 必填（≤1024 字符），不合规的 skill **pi 会直接不加载**。迁移到 pi 时 a4agent 会按 pi 的规则复查一次，不合规则在迁移结果里明确警告，而不是留下「迁成功却找不到」的状态。
>
> Qoder 的细节：Qoder 除 `~/.qoder/skills` 外还识别跨工具目录 `~/.agents/skills` 与 `<项目>/.agents/skills`（优先级低于前者），a4agent 统一托管到 `.qoder/skills` 前缀，与其它端一致，项目级因此无需特判。配置目录名随发行版而变（国际版 `.qoder`、国内版 `.qoder-cn`），默认探测实际存在的那个，也可用 `A4AGENT_QODER_HOME` 直接指定。

---

## MCP 管理

六端的 MCP server 都写在各自的配置文件里，a4agent 把它们归一为统一视图（`name` / `transport` / `command` / `args` / `env` / `url` / `headers`）管理。

### 发现与聚合

- 自动发现六端全局与项目级 MCP server，同名 server 跨端聚合标注（「已在 N 端存在」）。
- 详情中 `env` / `headers` **一律脱敏**（只回显键名），API 永不回传明文密钥。
- 每张卡片显示该 server 的**功能介绍**：自动识别常用 server（内置简介库 + npx 包实时查询 npm registry，均有缓存与失败兜底），也支持点「介绍」手工维护说明（本地持久化、跨端共享、留空即清除）。
- 项目级自动识别：Claude Code `.mcp.json`、Codex `.codex/config.toml`、ZCode `.zcode/config.json`、pi `.pi/mcp.json`（Qoder 的项目级就是 Claude Code 的 `.mcp.json`，归 Claude 端托管，不重复发现）。

### 安装 MCP 服务

- 点「安装 MCP」可在任意应用**从零新建** server：选择目标应用（Claude Code / Codex / dsh / ZCode / pi / Qoder，写入该应用全局配置）+ 传输类型（stdio / http / sse，按目标端能力自动过滤，如 Codex 仅 stdio、pi 无 sse），填写命令/参数/环境变量或地址/请求头。
- 也支持**粘贴 JSON 批量导入**：把已有的 MCP 配置片段直接粘进来（兼容「mcpServers」顶层、名称作键的 server 字典、单对象三种格式），一次装多个，逐条独立——单条失败（同名冲突、目标端不支持该传输等）不中断其余，并逐条报告成功/失败。
- 安装走各端原生渲染与原子写（目标同名已存在会明确拒绝，可改用迁移或先删除），既有 server 与其它配置键原样保留；安装后立即出现在卡片列表，并自动匹配功能介绍。

### 跨端迁移与传输能力矩阵

- 任意端的 server 可迁移到任意目标端；目标端已有同名 server 时**先快照进回收站再写入**，写入前自动备份目标配置文件。
- 迁移按**传输能力矩阵**严格校验，不兼容的组合整对失败并留日志，**不静默降级**：

| 目标端 | 支持的传输 | 配置文件 |
|---|---|---|
| Claude Code | stdio / sse / http | `~/.claude.json`（全局）、`<项目>/.mcp.json`（项目） |
| Codex | stdio | `~/.codex/config.toml`（`[mcp_servers.*]`） |
| dsh | stdio / streamable-http | `~/.dsh/profiles/<profile>/cordis.patch.yml` |
| ZCode | stdio / sse / http | `~/.zcode/cli/config.json`、`<项目>/.zcode/config.json`（`mcp.servers`） |
| pi | stdio / streamable-http | `~/.pi/agent/mcp.json`（全局）、`<项目>/.pi/mcp.json`（项目，需该项目被 pi 信任） |
| Qoder | stdio / sse / http | `~/.qoder/mcp.json`（全局；项目级复用 Claude Code 的 `<项目>/.mcp.json`） |

> dsh 与 ZCode 的细节：dsh 的 MCP server 挂在 `@deepseek-ai/dsh-mcp-client` 插件条目下，重写时保留其它非管理条目；dsh 无项目级 MCP。ZCode 配置 schema 严格（未知键会被丢弃），a4agent 只写其规范字段（`type`/`command`/`args`/`cwd`/`env`/`url`/`headers`/`enabled`/`timeoutMs`），迁移到 ZCode 的 server 会被客户端自动连接。
>
> pi 的细节：pi 的 `mcp.json` 与 Claude Code 同构（顶层 `mcpServers`），但**明确拒绝 legacy SSE**（`type: "sse"` 会被判废），迁移到 pi 的 sse 条目会整对失败并留日志。pi 另有自有键 `exposure` / `toolExposure` / `enabled` / `timeout` / `auth` / `oauth` 决定工具如何暴露给模型，a4agent 读写时**无损保留**这些键与顶层 `autoEnableCodemode`。pi 的 server 名只接受字母、数字、`_`、`-`，含中文或点号的名字会在迁移/安装时被明确拒绝（写出即废条目）；项目级文件不接受 `auth` 键，写入时自动剔除并记录日志。
>
> Qoder 的细节：Qoder 的 `~/.qoder/mcp.json` 同样与 Claude Code 同构（顶层 `mcpServers`，`type` 认 stdio / sse / http / streamable-http），自有键是 `disabled`（在 Qoder 界面里关掉某个 server 的开关，注意**不是** `enabled`）、`timeout`、`authType`，a4agent 读写时一并**无损保留**。Qoder 的**项目级 MCP 与 Claude Code 共用同一个 `<项目>/.mcp.json`**（它按 `.mcp.json` → `mcp.json` 的顺序查找），所以 a4agent 不让 Qoder 端再接管项目级文件——同一文件被两端各写一次只会互相踩；迁到 Claude 项目级即 Qoder 项目级同时生效。Qoder 的运行时镜像 `%APPDATA%\Qoder\SharedClientCache\extension\local\mcp.json` 与 `~/.qoder/mcp-router.json` 是进程内状态，a4agent 既不读也不写、不纳入备份。
>
> **Qoder 视图「看不到已连接 MCP」是正常的**：a4agent 管理的是**用户自定义**那一份 `~/.qoder/mcp.json`；而 Qoder 会话里实际连着的多数连接器并不来自这个文件——内置连接器（浏览器控制、node REPL 等）随 Qoder 安装包分发，插件自带的连接器写在 `~/.qoder/plugins/cache/<来源>/<插件>/mcp.json` 里，两者都不属于用户配置文件，因此不会出现在 a4agent 的 Qoder 卡片上。判断依据很简单：`~/.qoder/mcp.json` 为空、而 Qoder 里已连着若干 MCP，二者并不矛盾。想让某个 server 出现在 Qoder 端视图，就用「安装 MCP」或跨端迁移把它写进这个文件。

### 回收站与安全

- 被替换/删除的 server 配置片段**快照进回收站**（30 天内可恢复）；快照中的 `env` / `headers` 用 **DPAPI 加密**落盘，恢复时解密写回。
- 每次迁移写入日志；发现、预览、回收站接口对敏感字段全程脱敏。

---

## API 服务商切换

在以上管理能力之外，a4agent 也可为五端一键切换服务商、模型与 API Key：

- **Claude Code**：Anthropic 协议直连，或经内置**本地翻译代理**把请求实时翻译为 OpenAI Chat Completions 转发给 OpenAI 兼容服务商（代理仅监听 `127.0.0.1`、随机 token 鉴权，工具退出后仍存活）。
- **Codex**：OpenAI Responses 协议写入 `~/.codex/config.toml`；上游原生支持 Responses（如 DeepSeek）时直连，否则经本地代理翻译转发。
- **dsh**：经本地代理 `/chat/completions` 透传连接上游（顺带归一上游流式分片中的 `null` 字段，规避 dsh 适配器把工具名覆盖为空的问题），配置热加载、新会话即生效。
- **ZCode**：原生支持 Anthropic / OpenAI 两种协议，**直连**写入 CLI 与桌面端两份配置（provider 条目以 `a4a_p<id>` 托管、保留手工条目），无需本地代理。
- **pi**：原生支持 `anthropic-messages` / `openai-completions` / `openai-responses` 三种协议，**直连**写入 `~/.pi/agent/models.json`（provider 条目同样以 `a4a_p<id>` 托管）与 `settings.json` 的 `defaultProvider` / `defaultModel`，无需本地代理；pi 自己的凭证库 `auth.json`（可能存有 OAuth 登录态）不参与切换，下次启动 pi 会话生效。
- **Qoder 不参与 API 切换**：Qoder 有 BYOK / 自定义服务商能力，但它把服务商与模型目录落在 `~/.qoder/.models/<uid>/` 下（`catalog-v6`、`customs`、`external-providers/catalog-v12-*`），内容经其自带 WASM 原生模块按机器码加密，算法专有且随版本变化，外部无法像其它端那样写明文配置生效。因此 a4agent 只在**技能与 MCP** 两栏托管 Qoder；需要换服务商时请在 Qoder 界面内填写。

切换前自动备份目标配置文件（滚动保留最近 5 份）并原子写入；API Key 使用 Windows DPAPI 加密存储，接口永不回显明文。

### 预置服务商模板

启动时自动写入并按模板定义同步，可增删改：

| 服务商 | API 地址 | 协议 | 原生 Responses |
|--------|----------|------|----------------|
| DeepSeek-anthropic | `https://api.deepseek.com/anthropic` | Anthropic | — |
| 智谱-anthropic | `https://open.bigmodel.cn/api/anthropic` | Anthropic | — |
| DeepSeek-openai | `https://api.deepseek.com/` | OpenAI | ✅ 直连 |
| 智谱-openai | `https://open.bigmodel.cn/api/paas/v4` | OpenAI | — |
| OpenRouter-openai | `https://openrouter.ai/api/v1` | OpenAI | — |
| OpenCodeGo-openai | `https://opencode.ai/zen/go/v1` | OpenAI | — |
| 本地llmstudio-openai | `http://127.0.0.1:1234/v1` | OpenAI | — |

> 模板命名遵循「服务商-协议」约定：同一服务商可能同时提供 Anthropic 与 OpenAI 兼容两套接口，因此预置两条记录（如 `DeepSeek-anthropic` / `DeepSeek-openai`）。**原生 Responses**：勾选后 Codex 直接连接上游 `/responses` 接口，无需本地翻译代理；DeepSeek 官方原生支持 OpenAI Responses（仅 `deepseek-v4-flash` 模型）。内置模板在升级时会按模板定义自动同步，自定义服务商不受影响。

---

## 本地模型（llama.cpp 推理控制台）

「本地模型」页签把 [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server` 封装为一键启动的 **OpenAI 兼容 API 服务**，负责引擎本体不擅长的部分——装哪个引擎、选哪个模型、给什么参数，全程可视化操作（原 a4agent 项目能力完整合并于此）。

### 首次配置向导

按「获取引擎 → 检测硬件 → 选择模型目录 → 选择默认模型 → 服务端口」五步完成全部配置：

- **硬件检测与预设推荐**：通过 `nvidia-smi` 与注册表枚举显卡，列出厂商、显存、驱动版本；依据显存档位推荐推理预设（上下文长度、KV 缓存量化、MTP 投机解码），并在选择模型时给出显存溢出风险提示
- **引擎自动获取**：按显卡自动下载 llama.cpp **官方预编译引擎**（Vulkan / CUDA 12.4 / CUDA 13.3 / CPU，钉定固定版本、二进制可复现），也支持**离线安装**（指定含 `llama-server.exe` 的目录）或暂时跳过；CUDA 引擎 = 主包 + cudart 运行库包，下载后自动解压合并
- 已装有旧版 a4agent 时自动**接管其引擎目录**，无需重复下载

### 模型库与服务运行

- 添加多个模型目录，自动扫描 `.gguf` 并解析元数据：**大小、量化级别、原生上下文、是否含 MTP 层**（支持投机解码）；一键「设为默认模型」
- 「启动服务」实时显示运行状态（启动中 / 运行中 / 失败 / 已停止）与引擎日志；健康检查就绪后通知；端口占用、引擎缺失、进程崩溃均有明确日志提示；支持定时内存裁剪
- 推理参数可视化调整：上下文长度、KV 缓存级别（f16/q8_0/q4_0）、Flash Attention、GPU 层数、MTP 步数、API Key 鉴权、附加参数逃生舱

### 局域网开放与一键接入

- 「接入」卡片直接给出 Base URL、Chat Completions 地址、模型名（`model` 字段）与可直接复制的 **curl / openai SDK** 示例；推理设置切换为 `0.0.0.0` 监听后显示实际局域网调用地址
- **「接入配置方案」**一键创建指向本地服务的配置方案，弹窗里勾选要写入的应用（Claude Code / Codex / dsh / ZCode / pi），到「配置方案」页切换即可让对应应用使用本地模型，与云上 API 无缝互切；接入 pi 时会把服务的实际上下文窗口（`-c`）一并写入模型条目，避免 pi 按默认 128K 提前截断

---

## 任务下发（无头 Agent 任务）

「任务下发」页签把各端的**无头（headless）模式**产品化：在界面里写一句任务描述、选一个引擎，a4agent 就在后台把 CLI 拉起来跑完、把产出留档，你可以继续干别的事（机制见使用文档《让 Agent 后台自主执行任务》）。

### 支持的引擎

| 引擎 | 无头命令 | 产出形态 |
|---|---|---|
| **pi** | `pi -p "<任务>" --mode json --no-session` | JSONL 事件流，按 `stopReason` 定成败、按事件取文本 |
| **dsh** | `dsh --profile headless "<任务>"` | stdout 直出（能解析为 JSON 时抽正文字段） |

- 引擎自动探测（安装状态 / 版本 / 可执行路径），**未安装的在界面置灰**并给出原因；Claude Code / Codex / ZCode 属后续批次
- 所有命令都先把命令名解析成绝对路径：Windows 上这些 CLI 都是 npm 的 `.cmd` 壳，直接把命令名交给 `subprocess` 会启动失败

### 下发前自动预检（不通就不入队）

点「下发任务」会先同步做三步检查，任一步失败就**不启动引擎**，并用人话告诉你为什么：

1. **引擎可用**：目标 CLI 已安装且真能执行（结果缓存 5 分钟）
2. **服务商连通**：用当前生效方案打一次最小请求（Anthropic 走 `max_tokens=1`，OpenAI 走 `/models`）；401/403 → 「密钥无效或已过期」，超时/断连 → 「服务商暂不可达」；服务商是**本地模型**时改为探活，失败提示「本地推理服务未启动」，不要求 API Key
3. **端配置一致**：无头 CLI 读的是自己的配置文件，与数据库里的生效方案可能漂移，所以预检会读目标端的实际配置比对（pi 读 `settings.json` 的 `defaultProvider` + `models.json` 的 `baseUrl` / 模型；dsh 读 `settings.yaml` 的连接地址并检查本地翻译代理是否活着）。不一致就拦下并提示「重新切换方案」，避免任务用错模型/错服务商跑完

被拦下的任务仍会留一条 `precheck_failed` 记录，在队列里可以看到失败在哪一步、耗时多少。

### 队列与产出

- **下发即返回**：接口只建任务行并交给后台线程池（默认并发 5，`A4AGENT_TASK_CONCURRENCY` 可调），不等结果
- 状态机：`排队中 → 执行中 → 已完成 / 失败 / 超时 / 已取消 / 预检未通过`，界面 2 秒轮询，全部空闲自动停表
- 「查看产出」弹窗显示引擎产出全文（长产出只回传末尾部分，文件里始终完整）与预检各步耗时
- 支持**取消**（杀整棵进程树，node 壳会派生子进程）、任务历史、删除（同步清理产出文件）
- 超时默认 10 分钟（可选 5–60 分钟），到点强杀并标记超时
- 产出落盘 `%APPDATA%\a4agent\task_outputs\<任务号>.jsonl|md`，与其它运行数据同目录

### 关窗不杀任务

- **关闭主窗口 = 转入后台常驻**，任务继续执行；再次启动 a4agent 会自动唤回原实例的窗口而不是新起一个进程
- 真正的退出入口是顶栏「**退出**」，会先告知还有几个任务在跑并二次确认；退出时如实把运行中任务标记为「应用退出」并终止其进程树
- 任务到终态时：窗口开着就在页面内提示；窗口已隐藏则闪烁任务栏图标（零新增依赖，系统通知与托盘留给后续版本）

---

## 下载、安装与使用

### 下载安装

从 **发行版（Release）** 页面下载安装包 `a4agent-setup-*.exe`：

- **下载地址**：https://github.com/eogee/a4agent/releases （选择最新版本）
- **系统要求**：Windows 10/11 64 位
- 建议核对下载页提供的 SHA256 校验值，确保文件完整未被篡改

安装包采用**每用户安装（免 UAC）**：双击运行后按向导安装到当前用户目录，全程无需管理员权限。安装完成后自动创建开始菜单与桌面快捷方式，并可在「设置 → 应用」中卸载。

### 运行

1. 安装完成后，从**开始菜单**或**桌面快捷方式**启动 a4agent
2. 首次安装/运行时若出现 **Windows SmartScreen 提示**，点击「更多信息 → 仍要运行」即可（应用未做商业代码签名，属正常现象，不影响功能）
3. 界面六个页签：配置方案（API 切换）、供应商管理、**技能管理**、**MCP 管理**、**本地模型**（llama.cpp 推理控制台）、**任务下发**（无头 Agent 任务）

### 数据与隐私

- 运行时数据（数据库、配置备份）写入 `%APPDATA%\a4agent\`，日志写入 `~/.a4agent/logs/`
- 无头任务的引擎产出写入 `%APPDATA%\a4agent\task_outputs\`（与配置备份同级、仅本机用户可访问）；产出是引擎原始输出，可能包含代码与路径等敏感内容，「问题反馈」**不会**自动附带任务产出，删除任务会同步删除其产出文件
- API Key 使用 Windows DPAPI 加密存储，与当前 Windows 用户绑定
- 修改前自动备份原配置文件（`~/.claude/settings.json` / `~/.codex/config.toml` / `~/.dsh/settings.yaml` / `~/.dsh/.credentials.yaml` / `~/.zcode/cli/config.json` / `~/.zcode/v2/config.json` / `~/.pi/agent/models.json` / `~/.pi/agent/settings.json` 等，滚动保留最近 5 份）

### 常见问题

- **杀毒软件报毒**：PyInstaller 打包的程序偶被安全软件误报，请添加信任或排除；可将样本提交给对应厂商申诉误报
- **升级**：直接运行新版 `a4agent-setup-*.exe` 覆盖安装即可，数据与配置（`%APPDATA%\a4agent\`）会保留；升级前会自动停止后台翻译代理并清理旧文件
- **从 a4api 升级（v0.3.x → v0.4.0+）**：产品已更名为 a4agent，直接安装新版即可——首次启动会把 `%APPDATA%\a4api\` 数据自动迁入 `%APPDATA%\a4agent\`，安装器会把程序目录从 `Programs\a4api` 迁到 `Programs\a4agent` 并清理旧快捷方式；此前写入 Claude Code / Codex / dsh / zcode 的 `a4api_p*` 托管条目会在下次切换时自动替换为 `a4a_p*`，无需手工处理。v0.3.x 的「检查更新」也能直接升级到新版
- **卸载**：在「设置 → 应用」中卸载；程序文件会移除，运行数据（数据库、配置备份）保留在 `%APPDATA%\a4agent\`，如需彻底清除请手动删除该目录
- **任务下发**：关闭主窗口**不等于退出**——a4agent 会转入后台把任务跑完，再次启动程序即唤回原窗口；要真正结束请用顶栏「退出」（会先告知还有几个任务在跑）。任务到终态时，窗口已隐藏则闪烁任务栏图标提醒
- **反馈问题**：推荐用应用内入口——页脚「问题反馈」直接提交，支持截图（≤10 张 × ≤1MB）、自动附带环境信息与可选日志，直达开发者邮箱 eogee@qq.com；也可附上 `~/.a4agent/logs/a4agent.log` 日志片段提 Issue

### 提交 Issue 要求

Issue 已配置结构化模板（**Bug 报告 / 功能需求**），按表单填写即可；应用内「问题反馈」则直接邮件送达并自动完成第 2、4 项信息，无需走 Issue。手动提交前请确认以下信息，缺失可能导致问题无法定位：

1. **明确类型**：Bug 报告 / 功能建议 / 使用疑问，选择对应标签，便于分流处理
2. **环境信息（必填）**：
   - a4agent 版本号（发行版页面标注的版本）
   - 目标应用：Claude Code / Codex / dsh / ZCode / 其他
   - 服务商与模型：如 DeepSeek、智谱 GLM 等
3. **复现步骤**：从打开应用到出现问题的完整操作路径，越具体越好；尽量写明「做了什么 → 实际结果 → 预期结果」
4. **日志**：附上 `~/.a4agent/logs/a4agent.log` 的**相关片段**（不要整份粘贴，可截取报错前后内容）
5. **报错信息与截图**：界面报错文案、终端输出、异常截图一并附上
6. **隐私红线**：**切勿在 Issue 中粘贴 API Key、模型密钥等敏感信息**；如日志可能含敏感内容，请先脱敏
7. **排查先行**：提交前先自查——重启应用、确认 API Key 有效、确认本机能访问上游服务、确认无代理/杀软干扰

> 提交前请先搜索是否已有相同 Issue，避免重复提交。

---

## 自动更新与安全

发布新版本到 GitHub/Gitee 后，应用会在**启动时静默检查**或点顶部「检查更新」时发现更新，经你确认后下载安装包并显示进度，下载校验通过后再次确认即可运行安装器完成升级；也可选择「忽略此版本」。

### 更新源与校验（防 MITM / 防伪造）

- **双源竞速下载**：安装包从 GitHub 与 Gitee 两个镜像**同时下载、快者胜出**（同一 SHA256 绑定两个镜像地址），慢源/不可达源自动落败并提前收手；领先超过 8MB 即判胜，进度条始终显示领先者。不再串行等待慢源（国内访问 GitHub 资产常只有 ~0.1 MB/s，竞速后自动落到 Gitee 的 ~2 MB/s）。
- **更新清单签名**：发布侧用 Ed25519 私钥签名 `latest.json`（版本、更新说明、安装包 SHA256 等字段），应用内置对应公钥验签；任何字段异常或签名不符，清单直接作废、**不弹更新提示**。URL 不入签名，因此 GitHub/Gitee 两份清单字节一致、共用同一签名，URL 指向的内容由被签名的 SHA256 绑死。
- **完整性校验**：安装包边下边算 SHA256，与签名过的清单比对通过才落盘（存于 `%APPDATA%\a4agent\updates\<版本>\`）；点击「立即更新」时会对磁盘文件**再次校验**才启动安装器。
- **传输白名单**：仅 HTTPS，且每次重定向逐跳校验主机白名单（`github.com` / `gitee.com` / 两个 `*.githubusercontent.com` 对象存储域 / `*.gitee.com`），拦截跳转到任意域名。
- **防降级**：候选版本需严格高于当前版本；低于清单 `min_version`（过旧需完整安装包）时拒绝；预发布版本仅当当前运行版本也是预发布时才提示。
- **尺寸上限**：清单 512KB、安装包 300MB，超限拒绝；下载只写入用户数据目录，不信任系统临时目录。

### 更新流程（一次点击走完）

1. 检测到新版本 → 弹窗展示版本号与更新说明（说明内容由签名清单携带，篡改即拒收）。
2. 确认 → 后台下载，前端实时进度；下载中可取消。
3. 校验通过 → 提示「立即更新 / 稍后」。点击立即更新：应用先停掉本地翻译代理、自动退出并释放单实例锁，随后拉起 Inno Setup 安装向导；安装完成后启动的是新版本，配置、数据库与密钥完整保留。

---

## 安全设计

- **密钥加密存储**：所有 API Key 入库前经 Windows DPAPI（直接调用 `crypt32.dll`，无第三方依赖）加密，密文 base64 存入 SQLite，与当前 Windows 用户绑定；API 响应永不回显明文。
- **本地翻译代理鉴权**：仅绑定 `127.0.0.1`、端口限定 `17890–17899`；每次启动生成随机鉴权 token，请求必须匹配否则 `401`；只在「OpenAI 兼容 + 目标含 Claude/Codex/dsh」时运行，密钥从数据库按当前生效配置解密，不硬编码。
- **配置写入与备份**：修改任何目标配置文件前自动备份（滚动保留最近 5 份）；全部采用**原子写**（临时文件 + `fsync` + `os.replace`），崩溃不损坏配置；写入为**合并式**，用户已有的 hooks / permissions / 其它 env / provider 原样保留。
- **后端 API 防护**：CORS 白名单仅放行 `localhost` / `127.0.0.1` / `[::1]`；请求体经 Pydantic 严格校验；桌面形态下服务仅暴露本机。
- **数据与文件权限**：打包后数据写入 `%APPDATA%\a4agent\`；非 Windows 环境收紧 `700`/`600` 权限；`.gitignore` 排除数据库与运行时数据。
- **并发与一致性**：配置激活用进程内互斥锁串行化，异常事务回滚；SQLite 开启外键约束，删除服务商前校验其下配置方案。
- **进程与单实例**：Windows 命名互斥体保证单实例运行；重启 Claude Code 前用 CIM 精确匹配进程，避免误杀。
- **日志与隐私**：默认不记录任何请求/响应内容；代理调试日志仅当显式设置 `A4AGENT_PROXY_DEBUG` 时开启。

### 自动化测试

- `test_skill_manager.py`：六端 skill 发现聚合、跨端迁移、同名冲突回收、删除→恢复往返、30 天过期清理。
- `test_mcp_manager.py`：六端 MCP 发现聚合、安装与 JSON 批量导入（同名/传输能力/项目级/格式兼容校验）、跨端迁移（含传输能力矩阵约束）、快照回收、env/headers 脱敏与 DPAPI 加密、功能介绍（自定义/配置/内置简介库/npm 联动）。
- `test_switch.py` / `test_config_manager.py`：五端切换写入、协议约束、原子写入不留临时文件。
- `test_crypto.py`：DPAPI 加解密往返、非法密文返回空。
- `test_openai_proxy.py`：Anthropic ⇄ OpenAI 协议翻译正确性，含工具 schema 处理回归用例。
- `test_updater.py`：签名载荷 golden 基准、验签/篡改拒绝、版本比较与防降级、SHA256/尺寸校验、URL 白名单、双源清单竞速与 TTL 缓存、本地下载/取消/镜像竞速（快者胜、慢者清理、领先判胜、全败报错）、状态原子读写。
- `test_task_engines.py`：引擎探测与缓存、npm 壳路径解析、坏壳（node 崩溃但退出码 0）识别、pi/dsh 命令矩阵、pi 的 JSONL 解析（含「中途重试出错、最后一次成功」不得误判失败）。
- `test_task_precheck.py`：Anthropic/OpenAI/本地模型/本地代理四类连通分支、密钥失效与不可达文案、端配置第二跳（未指向 / base 漂移 / 模型缺失 / 一致放行）、引擎失败时的短路。
- `test_task_runner.py`：真实子进程当引擎跑通状态机、产出落盘与终态广播、超时杀进程树、运行中取消、应用退出的回收竞态。
- `test_tasks_api.py`：无生效方案拒绝、下发即返回并入队、预检失败留 `precheck_failed` 行、列表与详情产出、取消与删除约束。

---

## 开发

环境要求：Windows 10+，Python 3.10，[uv](https://docs.astral.sh/uv/)

```bash
uv sync                     # 安装依赖
uv run python desktop.py    # 启动桌面应用
# 或后端单独调试
uv run uvicorn backend.app.main:app --port 8000
```

## 打包

前置：编译安装包需要 [Inno Setup 6](https://jrsoftware.org/isinfo.php)（`winget install --id JRSoftware.InnoSetup -e --accept-source-agreements`）。

```bash
uv run python build.py                 # 生成 dist/a4agent/（文件夹版 onedir）
uv run python build.py --installer     # 文件夹版 + 编译安装包 dist/a4agent-setup-<版本>.exe
uv run python build.py --onefile       # 可选：生成单 exe（临时分发用）
```

版本号自动取自 `pyproject.toml`（`[project].version`），也可用 `--version` 覆盖；ISCC.exe 自动探测（`--iscc` 指定路径）。

## 项目结构

```
backend/app/       FastAPI 后端（模型、CRUD、配置读写、加密、进程管理）
backend/app/llama/ 本地模型推理控制台（引擎目录、GGUF 解析、硬件检测、进程管理）
backend/app/task_*.py 无头任务：引擎适配 / 下发前预检 / 执行器 / 完成提醒
frontend/          LayUI 前端（js/llama.js 本地模型页、js/tasks.js 任务下发页）
desktop.py         pywebview 桌面入口（关窗后台常驻、二次启动唤回窗口）
build.py           打包脚本
tools/e2e_task_smoke.py 无头任务端到端烟测（真实引擎 + 模拟服务商）
```

运行时数据（数据库、配置备份、llama 配置与引擎）写入 `backend/database/`（开发）或 `%APPDATA%\a4agent\`（打包后）。

---

## 联系方式

- **官网**：<https://eogee.com>
- **使用文档**：<https://eogee.com/article/83>
- **QQ**：3886370035 ｜ **微信**：eogee2022
- **问题反馈**：推荐应用内页脚「问题反馈」直接提交（支持截图），直达开发者邮箱 eogee@qq.com；也可按上方「提交 Issue 要求」提 Issue