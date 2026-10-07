# a4agent v0.5.1

## ⚠️ 破坏性变更：dsh / ZCode / pi 不再由 a4agent 代管 API 配置

**如果你没有用 a4agent 配过这三端的 API，可以直接忽略本节。**

### 为什么要改

dsh、ZCode、pi 这三个应用**自身都自带完整的供应商配置界面**，用户自己点几下就能配好：

| 应用 | 自带能力 |
|---|---|
| **dsh** | Settings → Models → Add a custom provider，可选 `openai-completions` / `openai-responses` / `anthropic-messages` 三种协议，支持 **Fetch available models** 自动拉取模型清单，会话右下角直接切模型不用重启 |
| **ZCode** | 设置 → 模型供应商 → 添加供应商，可同时填 Anthropic 与 OpenAI 两个接口地址，按模型声明上下文窗口 / 视觉 / 思考模式，保存前自动校验连通性 |
| **pi** | `/model` 里直接选，`models.json` 每次打开都会热重载，另有社区 GUI 扩展可管理 |

a4agent 代管这三端既**不省事**（用户自己点几下更快），也**有额外风险**：界面上的能力开关、上下文窗口、1M 后缀、启用状态等，外部写配置文件时无从得知，只能填保守默认值。

**判定标准是「用户自己能配」，不是「我们读不懂」。** 例如 Qoder 的模型目录虽经专有加密、外部无法写入明文，但用户自己在界面里配得好好的。

a4agent 现在的定位更清晰了：**为没有图形界面的 CLI 工具（Claude Code、Codex）提供图形化配置面板**。技能管理与 MCP 管理仍是六端全覆盖，不受影响。

### 你的配置会怎样

- **旧配置会被自动清理**：a4agent 写入的托管条目（`a4a_p*`）与指向本工具本地代理的 dsh 连接配置会被移除，清理前先做**永久快照**（存 `<数据目录>/backups/pre-switch-removal/`）
- **你自己配的一概不动**：dsh 界面配的供应商、ZCode 与 pi 的自定义 provider、以及 `.credentials.yaml` 里的所有 Key 都原样保留
- **卡片上会看到灰色「已移交」标记**：告诉你曾经配过、现在归应用内管
- **不需要你做任何操作**，配置方案会自动收敛为 Claude Code / Codex

### 想继续用 dsh / ZCode / pi 怎么办

在这三个应用**自己的设置界面**里重新配一次即可，字段对照与各端坑位见仓库文档：
[`docs/迁移对照表-三端API配置.md`](https://github.com/eogee/a4agent/blob/main/docs/迁移对照表-三端API配置.md)

各端入口：

- **dsh**：Settings → Models → Add a custom provider，填 Provider ID（如 `deepseek`）、Base URL、API protocol、API Key，点 Fetch available models
- **ZCode**：模型选择器 → 管理模型 → 添加供应商，填名称、**Anthropic 接口地址（不带 /v1）**、**OpenAI 接口地址（必须带 /v1）**、API Key；添加模型后**务必开启「启用」开关**
- **pi**：直接敲 `/model` 选，或编辑 `~/.pi/agent/models.json` 的 `providers`

## 新增：无头任务下发扩展到六端，且不再依赖托管配置

原先任务下发只支持 pi 与 dsh，且**依赖 a4agent 写入的托管配置才能跑**——移除托管会导致该功能报废。现改为**接管你自己配好的配置**，六端全部支持：

| 引擎 | 无头命令 |
|---|---|
| Claude Code | `claude -p "<任务>" --output-format json` |
| Codex | `codex exec --json --ephemeral` |
| ZCode | `zcode -p "<任务>" --json` |
| Qoder | `qodercli -p "<任务>" -o json` |
| dsh | `dsh --profile headless "<任务>"` |
| pi | `pi -p "<任务>" --mode json --no-session` |

下发前的预检从「比对配置字段是否等于我们写的值」改为**真实跑一次最小无头调用**——密钥有效没、模型名拼写对不对、网络通不通，一次调用全都会暴露出来。这样无论你在应用界面里怎么配都能被支持。

每条命令都带权限预授权参数（无头模式无人审批，不预授权会卡在审批或被拒绝）。

## 改进：修复引擎失败时看不到原因

- 引擎错误通常打在 stderr，此前只解析 stdout，用户只能看到「引擎退出码 1」；现在 stderr 一并参与解析，能显示「引擎退出码 1：上游 401 unauthorized」这类真实原因
- 新增裸错误行识别（HTTP 4xx/5xx、认证失败、模型不存在等），只认强信号不做泛化关键词匹配，避免「0 errors」被误判

## 改进：代码结构收敛

- 抽出公共 IO 层，此前6 个逐行重复的备份函数与 4 个重复的原子写入函数合为一套
- 切换接入的目标校验、前端「应用目标」勾选项同步收窄

## 兼容性说明

- Windows 10/11 64 位
- 配置与数据仍存于 `%APPDATA%\a4agent`，全程未受影响
- 技能管理与 MCP 管理六端功能不变
- 本地模型推理控制台功能不变

## 升级说明

- 直接运行新版 `a4agent-setup-0.5.1.exe` 覆盖安装即可，数据与配置（`%APPDATA%\a4agent\`）全程保留
- 升级后 dsh / ZCode / pi 的 API 配置请在各自应用内完成（见上方破坏性变更说明）；技能与 MCP 管理不受影响

## 校验

- 安装包：`a4agent-setup-0.5.1.exe`（21,143,637 字节）
- SHA256：`D96327FD810350E1AC639E3762F95F237B9D7098ADCEC03B2A197CCAF5D1C05F`