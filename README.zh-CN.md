# a4agent

[English](README.md) | 简体中文

**七端 AI 编程工具管理台 + 本地大模型推理控制台**：为 **Claude Code、Codex、dsh（DeepSeek Harness）、ZCode（智谱 Agentic 开发环境）、pi（本地 pi coding agent）、Qoder（AI IDE）与 OpenCode** 提供统一的技能管理与 MCP 管理（七端全覆盖），并为其中没有图形配置界面的两个 CLI 工具（Claude Code、Codex）提供 API 服务商切换，同时内置 llama.cpp 本地模型推理。所有操作通过可视化界面完成，无需手动编辑配置文件。

| 能力 | 说明 |
|---|---|
| **技能管理** | 七端全局/项目级 skill 自动发现、聚合标注、跨端迁移、回收站恢复，一键把项目 skill 补齐到所有缺失的端 |
| **MCP 管理** | 七端 MCP server 自动发现与**一键安装**、跨端迁移（按传输能力矩阵校验）、快照回收站、自动/自定义**功能介绍**，密钥全程脱敏/加密 |
| **API 切换** | 为无图形界面的 CLI（Claude Code、Codex）一键切换服务商、模型、API Key，配置自动备份、原子写入，密钥 DPAPI 加密存储；其余四端应用自带供应商配置界面，由用户自行配置 |
| **本地模型** | 把任意 `.gguf` 模型一键变成 OpenAI 兼容的本地/局域网 API 服务：引擎按显卡自动获取、模型库扫描、显存风险评估，一键接入配置方案（Claude Code / Codex） |
| **任务下发** | 七端以**无头模式**后台执行任务：下发即返回、下发前自动预检（引擎可用 / 配置就绪 / 真实连通）、队列轮询、产出留档、取消与超时、关窗不杀任务。直接使用你在各应用内配好的配置 |
| **通知提醒** | 两条独立通道让你不必守在电脑前，且**全端一个样式**——话题名称 / 通知类型 / 应用名称三行制，纯文字无 Emoji：**手机推送**（ntfy）在任务与会话终局（成功 / 失败 / 超时）送达，始终带 **Agent 最后一段输出**；**桌面横幅**（来源区 logo + a4agent）覆盖完成 / 失败 / 权限申请 / 提问，点击唤回窗口。会话 hook 覆盖 **Claude Code / Codex / ZCode / Qoder / WorkBuddy / dsh**，可在手机上直接回答提问、审批权限 |

---

## OpenCode 接入（v0.6.0 起）

OpenCode 与其他六端完全同权，没有任何专属界面：

- **技能 / MCP**：作为第七端统一托管（见上文，本机装了即可用——服务密码与默认端口 `49374` 自动发现）
- **任务下发**：多一个 OpenCode 引擎，直接对它常驻的服务建会话执行，而不是每次新起一个 CLI 进程。
  免审批是**会话级权限**，不改你的全局配置；产出、成本与用量按会话结构化取回；模型被服务端判废时
  会自动换一个可用模型重试
- **会话交互**：「通知提醒」页的宿主表格多一行 OpenCode，「注册」即开启服务事件监听——
  你在 OpenCode 网页端里发起的会话，跑完 / 出错 / 被中断都走同一条桌面横幅 + 手机推送
  （横幅无论窗口是否开着都会弹）；
  外出模式下权限请求会推手机批办（批准 / 总是允许 / 拒绝），批复经它的 permission API 回给
  服务、会话继续。OpenCode 没有 hook 协议，注册**不写它的任何文件**

---

## 技能管理

六个目标使用同一套技能格式（`<skill-name>/SKILL.md` 目录 + frontmatter 的 `name` 与 `description`），因此 a4agent 可以把它们当作一种资源统一管理。

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

**ZCode**：官方还识别跨工具兼容目录 `~/.agents/skills` 与 `<项目>/.agents/skills`；a4agent 统一托管到 `.zcode` 前缀，与其它端保持一致。迁移到 ZCode 的 skill 会被 ZCode 客户端真实读取。

**pi**：全局技能在 **agent 子目录**下（`~/.pi/agent/skills`，不是 `~/.pi/skills`）。pi 对 skill 的校验比其它端严格：`name` 只能是小写字母、数字与连字符（≤64 字符，不得以连字符首尾或含连续连字符），`description` 必填（≤1024 字符），不合规的 skill **pi 会直接不加载**。迁移到 pi 时 a4agent 会按 pi 的规则复查一次，不合规则在迁移结果里明确警告，而不是留下「迁成功却找不到」的状态。

**Qoder**：除 `~/.qoder/skills` 外还识别跨工具目录 `~/.agents/skills` 与 `<项目>/.agents/skills`（优先级低于前者），a4agent 统一托管到 `.qoder/skills` 前缀，与其它端一致，项目级因此无需特判。配置目录名随发行版而变（国际版 `.qoder`、国内版 `.qoder-cn`），默认探测实际存在的那个，也可用 `A4AGENT_QODER_HOME` 直接指定。

**OpenCode**：全局根在 `~/.config/opencode/skills`（不是 `~/.opencode/skills`）；项目级是 `.opencode/skills`。两处与常规端不同，都已按其真实行为特判：

- **技能 ID 取目录名**，frontmatter `name` 只是显示名。因此 a4agent 在 OpenCode 端按目录名聚合并判定冲突——
  否则两个「显示名相同、ID 不同」的技能会被错并成一份，或在迁移时被误删（它们在 OpenCode 里是两个各自工作的技能）。
- **它会自动发现 `~/.claude/skills` 与 `~/.agents/skills`**（`.agents` 同时是 ZCode / Qoder 的跨工具目录）。
  这类条目会被标注「OpenCode 已可见」，一键适配时跳过——不必为了 OpenCode 再迁一份。

---

## MCP 管理

七端的 MCP server 都写在各自的配置文件里，a4agent 把它们归一为统一视图（`name` / `transport` / `command` / `args` / `env` / `url` / `headers`）管理。

### 发现与聚合

- 自动发现七端全局与项目级 MCP server，同名 server 跨端聚合标注（「已在 N 端存在」）。
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
| OpenCode | stdio / streamable-http | `~/.config/opencode/opencode.json(c)`、`<项目>/.opencode/opencode.json(c)`（`mcp.servers`） |

**dsh**：MCP server 挂在 `@deepseek-ai/dsh-mcp-client` 插件条目下，重写时保留其它非管理条目；无项目级 MCP。

**ZCode**：配置 schema 严格（未知键会被丢弃），a4agent 只写其规范字段（`type`/`command`/`args`/`cwd`/`env`/`url`/`headers`/`enabled`/`timeoutMs`），迁移到 ZCode 的 server 会被客户端自动连接。

**pi**：`mcp.json` 与 Claude Code 同构（顶层 `mcpServers`），但**明确拒绝 legacy SSE**（`type: "sse"` 会被判废），迁移到 pi 的 sse 条目会整对失败并留日志。pi 另有自有键 `exposure` / `toolExposure` / `enabled` / `timeout` / `auth` / `oauth` 决定工具如何暴露给模型，a4agent 读写时**无损保留**这些键与顶层 `autoEnableCodemode`。pi 的 server 名只接受字母、数字、`_`、`-`，含中文或点号的名字会在迁移/安装时被明确拒绝（写出即废条目）；项目级文件不接受 `auth` 键，写入时自动剔除并记录日志。

**Qoder**：`~/.qoder/mcp.json` 与 Claude Code 同构（顶层 `mcpServers`，`type` 认 stdio / sse / http / streamable-http），自有键是 `disabled`（在 Qoder 界面里关掉某个 server 的开关，注意**不是** `enabled`）、`timeout`、`authType`，a4agent 读写时一并**无损保留**。Qoder 的**项目级 MCP 与 Claude Code 共用同一个 `<项目>/.mcp.json`**（它按 `.mcp.json` → `mcp.json` 的顺序查找），所以 a4agent 不让 Qoder 端再接管项目级文件——同一文件被两端各写一次只会互相踩；迁到 Claude 项目级即 Qoder 项目级同时生效。运行时镜像 `%APPDATA%\Qoder\SharedClientCache\extension\local\mcp.json` 与 `~/.qoder/mcp-router.json` 是进程内状态，a4agent 既不读也不写、不纳入备份。

**Qoder 视图「看不到已连接 MCP」是正常的**——a4agent 管理的是**用户自定义**那一份 `~/.qoder/mcp.json`；而 Qoder 会话里实际连着的多数连接器并不来自这个文件：内置连接器（浏览器控制、node REPL 等）随 Qoder 安装包分发，插件自带的连接器写在 `~/.qoder/plugins/cache/<来源>/<插件>/mcp.json` 里，两者都不属于用户配置文件，因此不会出现在 a4agent 的 Qoder 卡片上。判断依据很简单：`~/.qoder/mcp.json` 为空、而 Qoder 里已连着若干 MCP，二者并不矛盾。想让某个 server 出现在 Qoder 端视图，就用「安装 MCP」或跨端迁移把它写进这个文件。

**OpenCode**：配置在 `mcp.servers` 下，只有两种传输——`local`（stdio）与 `remote`（Streamable HTTP），**没有 legacy SSE 的位置**，所以 sse 来源的条目整对失败并留日志。三处形状差异由 a4agent 抹平：命令是**单个数组**（可执行与参数合一，写回时才拆开）、环境变量键叫 `environment`（不是 `env`）、启停开关叫 `disabled`。`codemode` / `timeout` / `protocol` / `oauth` 等自有键**无损保留**。

> OpenCode 的配置文件是 JSONC（可带注释与尾逗号）。a4agent 写入的是标准 JSON（合法 JSONC），能正确读取带注释的既有文件，但**该文件上的手写注释会在写入时丢失**——这是「整子树替换」的固有取舍。介意的话改用 OpenCode 自己的界面配置即可；配置读取失败时 a4agent 会中止写入，绝不按空配置重建（那会清空你的 model / agents / permissions）。

### 回收站与数据保护

- 被替换/删除的 server 配置片段**快照进回收站**（30 天内可恢复）；快照中的 `env` / `headers` 用 **DPAPI 加密**落盘，恢复时解密写回。
- 每次迁移写入日志；发现、预览、回收站接口对敏感字段全程脱敏。

---

## API 服务商切换

a4agent 为**没有图形配置界面的 CLI 工具**提供一键切换：目前是 **Claude Code** 与 **Codex** 两端。

- **Claude Code**：Anthropic 协议直连，或经内置**本地翻译代理**把请求实时翻译为 OpenAI Chat Completions 转发给 OpenAI 兼容服务商（代理仅监听 `127.0.0.1`、随机 token 鉴权，工具退出后仍存活）。
- **Codex**：OpenAI Responses 协议写入 `~/.codex/config.toml`；上游原生支持 Responses（如 DeepSeek）时直连，否则经本地代理翻译转发。

### 为什么只代管这两端

判定标准只有一条：**目标应用是否自带完整的供应商配置界面**。

| 端 | 应用内能做什么 | a4agent 的做法 |
|---|---|---|
| Claude Code | 无，纯 CLI | 代管（唯一可行路径） |
| Codex CLI | 无，纯 CLI | 代管（唯一可行路径） |
| dsh | Settings → Models，可选三种 API 协议 + 自动拉取模型清单 | 移交用户自配 |
| ZCode | 设置 → 模型供应商，双接口地址 + 按模型声明能力 | 移交用户自配 |
| pi | `/model` 里直接切换 | 移交用户自配 |
| Qoder | 9 个预置供应商 + 自定义端点 + 连通性校验 | 移交用户自配 |

对这些应用，外部代管配置文件既不省事（用户点几下更快），又有额外风险：界面上的能力开关、上下文窗口、启用状态外部写入时无从得知，只能填保守默认值。

需要注意，判定依据是「**用户自己能配**」，不是「我们读不懂它的配置」——Qoder 的模型目录虽经专有加密、外部无法写明文，但用户自己在界面里配得好好的，技术障碍不构成产品理由。

换服务商请直接在对应应用的设置里操作，各端的字段对照与界面入口见 [迁移对照表](docs/迁移对照表-三端API配置.md)。

切换前自动备份目标配置文件（滚动保留最近 5 份）并原子写入；API Key 使用 Windows DPAPI 加密存储，接口永不回显明文。

### 无头任务下发与配置的关系

无头任务下发覆盖六端（Claude Code / Codex / ZCode / Qoder / dsh / pi），**直接使用你在各应用内自己配好的配置**——a4agent 不再为这些应用写入 API 配置，改为在下发前做一次真实连通性验证：

| 步骤 | 检查什么 |
|---|---|
| 引擎可用 | 命令是否存在、能否执行、版本号 |
| 配置就绪 | 读你自己的配置，判断是否已配好服务商与模型（Qoder 因配置加密读不到，如实说明并以实测为准） |
| 真实连通 | 用最小提示词真实跑一次无头调用，拿到产出才算通过 |

这样配置自由完全归你：无论你在界面里怎么配，验证的都是「能不能真跑」，而不是「配置字段是否等于我们写的值」。

---

## 本地模型（llama.cpp 推理控制台）

「本地模型」页签把 llama.cpp 的 `llama-server` 封装为一键启动的 **OpenAI 兼容 API 服务**：装哪个引擎、选哪个模型、给什么参数，全程可视化操作。

**引擎版本**：llama.cpp **b11370**（2026-10-03 发布，钉死在 `backend/app/llama/catalog.py`，为单一事实来源）。该版本的 `llama-server` 包含决策模型端点，因此下面这项能力可用。

### 决策模型（System One）

除常规的 `/v1/chat/completions` 与 `/v1/models` 外，该版本引擎还提供 **`/v1/systemone` 决策模型端点**——接入卡片里会直接给出它的完整地址，附带 curl / OpenAI SDK 调用示例，可用于把「该用哪个模型、怎么拆解任务」这类调度决策交给本地引擎，同时主模型交给云上服务，形成云边协同。

### 首次配置向导

按「获取引擎 → 检测硬件 → 选择模型目录 → 选择默认模型 → 服务端口」五步完成全部配置：

- **硬件检测与预设推荐**：通过 `nvidia-smi` 与注册表枚举显卡，列出厂商、显存、驱动版本；依据显存档位推荐推理预设（上下文长度、KV 缓存量化、MTP 投机解码），并在选择模型时给出显存溢出风险提示
- **引擎自动获取**：按显卡自动下载 llama.cpp **官方预编译引擎**，四套可选：
  | 引擎包 | 适用显卡 | 体积 | 最低驱动 |
  |---|---|---|---|
  | CUDA 13.4 | NVIDIA（性能优先） | 525 MB | 580 |
  | CUDA 12.4 | NVIDIA（兼容旧驱动） | 597 MB | 550 |
  | Vulkan | NVIDIA / AMD / Intel 独显与核显 | 32 MB | 无 |
  | CPU | 无可用显卡时的兜底 | 18 MB | 无 |

  下载的是钉定版本的官方预编译二进制（可复现），也支持**离线安装**（指定含 `llama-server.exe` 的目录）或暂时跳过；CUDA 引擎 = 主包 + cudart 运行库包，下载后自动解压合并
- 已装有旧版 a4agent 时自动**接管其引擎目录**，无需重复下载

### 模型库与服务运行

- 添加多个模型目录，自动扫描 `.gguf` 并解析元数据：**大小、量化级别、原生上下文、是否含 MTP 层**（支持投机解码）；一键「设为默认模型」
- 「启动服务」实时显示运行状态（启动中 / 运行中 / 失败 / 已停止）与引擎日志；健康检查就绪后通知；端口占用、引擎缺失、进程崩溃均有明确日志提示；支持定时内存裁剪
- 推理参数可视化调整：上下文长度、KV 缓存级别（f16/q8_0/q4_0）、Flash Attention、GPU 层数、API Key 鉴权、附加参数逃生舱
- **MTP 投机解码步数**： llama.cpp 的 MTP（Multi-Token Prediction）写法随版本变化——旧版用 `--mtp N`，b105xx 起改为 `--spec-type draft-mtp --spec-draft-n-max N`。a4agent 通过跑一次 `llama-server --help` **自动探测**当前后端支持哪种写法再下发参数，跨版本升级不会因此启动失败

### 局域网开放与一键接入

- 「接入」卡片直接给出 Base URL、Chat Completions 地址、决策端点（System One）、模型名（`model` 字段）与可直接复制的 **curl / OpenAI SDK** 示例；推理设置切换为 `0.0.0.0` 监听后显示实际局域网调用地址
- **「接入配置方案」**一键创建指向本地服务的配置方案，弹窗里勾选要写入的应用（Claude Code / Codex），到「配置方案」页切换即可让对应应用使用本地模型，与云上 API 无缝互切

---

## 任务下发（无头 Agent 任务）

「任务下发」页签把各端的**无头（headless）模式**产品化：在界面里写一句任务描述、选一个引擎，a4agent 就在后台把 CLI 拉起来跑完、把产出留档，你可以继续干别的事（机制见使用文档《让 Agent 后台自主执行任务》）。

### 支持的引擎

| 引擎 | 无头命令 | 产出形态 |
|---|---|---|
| **Claude Code** | `claude -p "<任务>" --output-format json` | JSON 单对象，产出在 `result`，含 token 用量与成本 |
| **Codex** | `codex exec --json --ephemeral` | JSONL 事件流，取 `agent_message` 类型的产出 |
| **ZCode** | `zcode -p "<任务>" --json` | JSON 单对象，产出在 `text`，含 `toolCalls` / `usage` / `stopReason` |
| **Qoder** | `qodercli -p "<任务>" -o json` | JSON 单对象 |
| **dsh** | `dsh --profile headless "<任务>"` | stdout 直出（能解析为 JSON 时抽正文字段） |
| **pi** | `pi -p "<任务>" --mode json --no-session` | JSONL 事件流，按 `stopReason` 定成败、按事件取文本 |
| **OpenCode** | **不走 CLI**，对运行中的服务建会话 | 会话消息流，取 assistant 文本；成本/用量/成败取自会话终态 |

- 引擎自动探测（安装状态 / 版本 / 可执行路径），**未安装的在界面置灰**并给出原因
- 所有命令都先把命令名解析成绝对路径：Windows 上这些 CLI 都是 npm 的 `.cmd` 壳，直接把命令名交给 `subprocess` 会启动失败。Qoder 的命令名官方文档写 `qodercli`、博客写 `qoder`，两个名字都探测
- 无头模式下没有人能点审批，因此每条命令都带权限预授权参数，否则会卡在审批或被直接拒绝：Claude Code 用 `--permission-mode bypassPermissions`、Codex 用 `--sandbox danger-full-access`、ZCode 用 `--mode yolo`、Qoder 与 pi 用 `--yolo`（dsh 不需要，其 headless profile 本身即无头入口）

### OpenCode：为什么它不一样

其它六端都是「每次新起一个 CLI 进程」，OpenCode 则是**对接你已经开着的那台服务**（本机默认 `127.0.0.1:49374`，也可填远程地址）。这样做换来三样命令行走不到的能力：

- **免审批不污染全局配置**：预授权是**会话级权限规则**（实测会话对象原样回显），任务结束即失效。其它端只能用 `--yolo` / `bypassPermissions` 这类全局开关
- **成败不靠猜**：会话终态直接给 `outcome`（成功 / 失败 / 被中断）、成本与用量；失败原因取自结构化的 `error` 字段，不用从退出码反推
- **可中断**：取消是对服务端会话发 interrupt，不是杀进程

因此 OpenCode 也不受「必须先有一个生效的配置方案」的限制——它的模型与凭据在 OpenCode 里，不走 a4agent 的方案体系。另外实测有一类隐蔽故障：服务端模型清单里标记为可用的模型可能已被上游废弃，真跑才报错。所以预检第三步与正式执行都会在遇到「模型被拒」时换一个可用模型重试。
- dsh 额外需要屏蔽 profile 里的外部插件：headless profile 缺少它们依赖的 host 服务，不屏蔽会在启动阶段直接失败。a4agent 生成 `--patch` 覆盖层只禁用那批相对路径插入的外部插件，不改 profile 本身

### 下发前自动预检（不通就不入队）

点「下发任务」会先同步做三步检查，任一步失败就**不启动引擎**，并用人话告诉你为什么：

1. **引擎可用**：目标 CLI 已安装且真能执行（结果缓存 5 分钟）
2. **配置就绪**：读**你自己在应用内配好的配置**，判断是否已配好服务商与模型。读不到时（如 Qoder 的模型目录经专有加密）如实说明「无法校验，请以实测结果为准」，不假装通过
3. **真实连通**：用最小提示词真实跑一次无头调用，拿到产出才算通过

第 2、3 步不再比对「配置字段是否等于我们写的值」——那会把「配置得和我不一样」误判成不可用。真实调用不管怎么配都能给出诚实答案：密钥有效没、模型名拼写对不对、网络通不通，一次调用全都会暴露出来。配置还没配好时不会跑实测（跑必然失败，白白等待）。

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
- 任务到终态时：窗口开着就在页面内提示；窗口已隐藏则弹**原生系统通知**（带提示音，来源区显示 a4agent logo 与应用名，点击唤回窗口）并闪烁任务栏图标

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
3. 界面七个页签：配置方案（API 切换，仅 Claude Code 与 Codex）、供应商管理、**技能管理**、**MCP 管理**、**本地模型**（llama.cpp 推理控制台）、**任务下发**（无头 Agent 任务）、**通知提醒**（手机推送 + 桌面横幅）

### 数据与隐私

- 运行时数据（数据库、配置备份）写入 `%APPDATA%\a4agent\`，日志写入 `~/.a4agent/logs/`
- 无头任务的引擎产出写入 `%APPDATA%\a4agent\task_outputs\`（与配置备份同级、仅本机用户可访问）；产出是引擎原始输出，可能包含代码与路径等敏感内容，「问题反馈」**不会**自动附带任务产出，删除任务会同步删除其产出文件
- API Key 使用 Windows DPAPI 加密存储，与当前 Windows 用户绑定
- 修改前自动备份原配置文件（`~/.claude/settings.json` / `~/.codex/config.toml`，滚动保留最近 5 份）

### 常见问题

- **杀毒软件报毒**：PyInstaller 打包的程序偶被安全软件误报，请添加信任或排除；可将样本提交给对应厂商申诉误报
- **升级**：直接运行新版 `a4agent-setup-*.exe` 覆盖安装即可，数据与配置（`%APPDATA%\a4agent\`）会保留；升级前会自动停止后台翻译代理并清理旧文件
- **从 a4api 升级（v0.3.x → v0.4.0+）**：产品已更名为 a4agent，直接安装新版即可——首次启动会把 `%APPDATA%\a4api\` 数据自动迁入 `%APPDATA%\a4agent\`，安装器会把程序目录从 `Programs\a4api` 迁到 `Programs\a4agent` 并清理旧快捷方式；此前写入 Claude Code / Codex 的 `a4api_p*` 托管条目会在下次切换时自动替换为 `a4a_p*`，无需手工处理。v0.3.x 的「检查更新」也能直接升级到新版
- **卸载**：在「设置 → 应用」中卸载；程序文件会移除，运行数据（数据库、配置备份）保留在 `%APPDATA%\a4agent\`，如需彻底清除请手动删除该目录
- **任务下发**：关闭主窗口**不等于退出**——a4agent 会转入后台把任务跑完，再次启动程序即唤回原窗口；要真正结束请用顶栏「退出」（会先告知还有几个任务在跑）。任务到终态时，窗口已隐藏则闪烁任务栏图标提醒
- **反馈问题**：推荐用应用内入口——页脚「问题反馈」直接提交，支持截图（≤10 张 × ≤1MB）、自动附带环境信息与可选日志，直达开发者邮箱 eogee@qq.com；也可附上 `~/.a4agent/logs/a4agent.log` 日志片段提 Issue

---

## 提交 Issue 要求

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

提交前请先搜索是否已有相同 Issue，避免重复提交。

---

## 自动更新

发布新版本到 GitHub/Gitee 后，应用会在**启动时静默检查**或点顶部「检查更新」时发现更新，经你确认后下载安装包并显示进度，下载校验通过后再次确认即可运行安装器完成升级；也可选择「忽略此版本」。

### 更新源与校验

- **双源竞速下载**：安装包从 GitHub 与 Gitee 两个镜像**同时下载、快者胜出**（同一 SHA256 绑定两个镜像地址），慢源/不可达源自动落败并提前收手；领先超过 8MB 即判胜，进度条始终显示领先者。不再串行等待慢源（国内访问 GitHub 资产常只有 ~0.1 MB/s，竞速后自动落到 Gitee 的 ~2 MB/s）。
- **更新清单签名**：发布侧用 Ed25519 私钥签名 `latest.json`（版本、更新说明、安装包 SHA256 等字段），应用内置对应公钥验签；任何字段异常或签名不符，清单直接作废、**不弹更新提示**。URL 不入签名，因此 GitHub/Gitee 两份清单字节一致、共用同一签名，URL 指向的内容由被签名的 SHA256 绑死。
- **完整性校验**：安装包边下边算 SHA256，与签名过的清单比对通过才落盘（存于 `%APPDATA%\a4agent\updates\<版本>\`）；点击「立即更新」时会对磁盘文件**再次校验**才启动安装器。
- **传输白名单**：仅 HTTPS，且每次重定向逐跳校验主机白名单（`github.com` / `gitee.com` / 两个 `*.githubusercontent.com` 对象存储域 / `*.gitee.com`），拦截跳转到任意域名。
- **防降级**：候选版本需严格高于当前版本；低于清单 `min_version`（过旧需完整安装包）时拒绝；预发布版本仅当当前运行版本也是预发布时才提示。
- **尺寸上限**：清单 512KB、安装包 300MB，超限拒绝；下载只写入用户数据目录，不信任系统临时目录。

### 更新流程（一次点击走完）

1. 检测到新版本 → 弹窗展示版本号与更新说明（说明内容由签名清单携带）。
2. 确认 → 后台下载，前端实时进度；下载中可取消。
3. 校验通过 → 提示「立即更新 / 稍后」。点击立即更新：应用先停掉本地翻译代理、自动退出并释放单实例锁，随后拉起 Inno Setup 安装向导；安装完成后启动的是新版本，配置、数据库与密钥完整保留。

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
a4agent/
├── desktop.py                # pywebview 桌面入口（关窗后台常驻、二次启动唤回原窗口）
├── build.py                  # 打包脚本（PyInstaller onedir + Inno Setup 安装包）
├── installer.iss             # Inno Setup 安装脚本（每用户安装、免 UAC）
├── dev_server.py             # 开发态后端启动脚本（浏览器访问调试）
├── pyproject.toml            # 依赖清单与版本号单一来源
├── backend/app/              # FastAPI 后端
│   ├── main.py               # 应用入口、生命周期与路由挂载
│   ├── api/v1/               # REST 路由层：configs / providers / switch / skills / mcp /
│   │                         #   llama / tasks / update / feedback / desktop / fs / removal
│   ├── models.py             # SQLAlchemy 数据模型
│   ├── schemas.py            # Pydantic 请求/响应模式
│   ├── crud.py               # 数据库增删改查
│   ├── database.py           # 数据库初始化与目录布局
│   ├── config_manager.py     # 配置方案与服务商管理
│   ├── config_io.py          # 公共配置 IO（逐端备份、原子写入）
│   ├── openai_proxy.py       # Anthropic → OpenAI 本地翻译代理
│   ├── responses_translator.py  # OpenAI Responses 协议翻译
│   ├── proxy_standalone.py   # 独立进程形态的翻译代理
│   ├── crypto.py             # Windows DPAPI 密钥加密
│   ├── skill_manager.py      # 六端技能发现、聚合、迁移与回收站
│   ├── mcp_manager.py        # 六端 MCP 发现、安装、迁移与快照
│   ├── removal.py            # 移交三端的托管配置清理（只删自写条目）
│   ├── removal_backup.py     # 清理前的永久快照
│   ├── task_engines.py       # 无头任务引擎适配（六端命令与产出解析）
│   ├── task_precheck.py      # 下发前三步预检（引擎 / 配置 / 真实连通）
│   ├── task_runner.py        # 任务执行器（线程池、进程树、超时）
│   ├── task_notify.py        # 任务终态事件广播（桌面 toast / 手机推送的共同源头）
│   ├── phone/                # 通知提醒：config（话题 / 令牌 / 事件开关）/ ntfy（推送客户端）/
│   │                         #   notifier（统一样式拼装：话题名称/通知类型/应用名称 + AI 最后输出）
│   ├── hooks/                # 会话 hook：dispatch（事件路由）/ handlers（提问 / 审批 / 完成）/
│   │                         #   register（挂载到各工具）/ deskqueue（桌面弹窗延迟代发）
│   │                         #   transcript（最后输出与话题名称抽取）/ dsh（dsh 事件处理）/
│   │                         #   dsh_register（Cordis 插件挂载）
│   ├── win_toast.py          # 桌面通知横幅门面（原生 WinRT toast 优先，legacy 气泡降级）
│   ├── win_toast_native.py   # WinRT 原生 toast（ctypes 直调 COM，零第三方依赖）
│   ├── updater.py            # 应用自更新（清单验签、双源竞速下载）
│   ├── llama/                # 本地模型推理控制台：catalog（引擎目录）/ gguf（模型解析）/
│   │                         #   gpu（硬件检测）/ downloader（引擎下载）/ runtime 与 server
│   │                         #   （服务运行）/ presets（推理预设）/ config / lan（局域网）
│   └── tests/                # pytest 测试套件（24 个模块）
├── frontend/
│   ├── index.html            # 七页签主界面
│   ├── js/app.js             # 配置方案 / 供应商 / 技能管理 / MCP 管理页逻辑
│   ├── js/llama.js           # 本地模型页逻辑
│   ├── js/tasks.js           # 任务下发页逻辑
│   ├── js/phone.js           # 通知提醒页逻辑（手机推送 / 桌面横幅 / hook 挂载）
│   ├── js/markdown.js        # 更新说明 Markdown 渲染
│   ├── css/ · layui/         # 样式与 LayUI 组件库
│   └── changelog.md          # 应用内更新弹窗展示的更新说明
├── docs/                     # 设计文档、迁移对照表、实施计划
├── resources/                # logo 与安装包图标 / dsh-hook（挂到 ~/.dsh 的 Cordis 插件源码）
└── tools/                    # 端到端烟测脚本（e2e_task_smoke.py）
```

运行时数据（数据库、配置备份、llama 配置与引擎）写入 `backend/database/`（开发）或 `%APPDATA%\a4agent\`（打包后）。

---

## 联系方式

- **官网**：<https://eogee.com>
- **使用文档**：<https://eogee.com/article/83>
- **QQ**：3886370035 ｜ **微信**：eogee2022
- **问题反馈**：推荐应用内页脚「问题反馈」直接提交（支持截图），直达开发者邮箱 eogee@qq.com；也可按「提交 Issue 要求」章节提 Issue
