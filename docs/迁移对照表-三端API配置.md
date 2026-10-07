# 迁移对照表：dsh / ZCode / pi 的API 配置改回应用内自配

> 适用版本：v0.5.1 起
>
> a4agent 不再代管dsh、ZCode、pi 三端的 API 配置。这三端应用**自身都自带完整的供应商配置界面**，由用户自己配置即可，不再需要通过本工具写入配置文件。

## 为什么要变

| 端 | 应用内自带的能力 | 旧版a4agent 做的事 |
|---|---|---|
| **dsh** | Settings → Models → Add a custom provider，可选 `openai-completions` / `openai-responses` / `anthropic-messages` 三种协议，支持 Fetch available models 自动拉模型清单，会话右下角直接切模型不用重启 | 写 `~/.dsh/settings.yaml` 的 `llm-deepseek` 段（只认 DeepSeek 官方一家），并把baseURL 指向本工具的本地代理 |
| **ZCode** | 设置 → 模型供应商 → 添加供应商，可填 Anthropic 接口地址 + OpenAI 接口地址 + API Key，添加模型后需开启「启用」开关 | 写 `~/.zcode/cli/config.json` 与 `v2/config.json` 两份文件，模型元数据靠猜 |
| **pi** | `/model` 里直接选，`models.json` 每次打开 `/model` 都热重载，另有社区 GUI 扩展可管理 | 写 `~/.pi/agent/models.json` 与 `settings.json`，上下文窗口靠探针探测 |

代管不仅没有省事，还引入了额外风险：应用界面上的能力开关、上下文窗口、1M 后缀等状态，外部写入时无从得知，只能填保守默认值。

**移除后配置自由归你**，a4agent 不再碰这三端的任何配置文件。

## 怎么迁移

以原来在 a4agent 里配过的一个方案（假设服务商是 DeepSeek、模型是 `deepseek-v4-flash`）为例。

### dsh

1. 打开 dsh → **Settings** → **Models** → **Add a custom provider**
2. 按下表填写：

   | 字段 | 填写内容 |
   |---|---|
   | Provider ID | `deepseek`（小写，建后不可改，建议按端点而非模型命名） |
   | Base URL | `https://api.deepseek.com/v1` |
   | API protocol | `openai-completions` |
   | API Key | 你的 DeepSeek API Key |
   | Models | 点 **Fetch available models** 自动拉取，或手动填写 `deepseek-v4-flash` |

3. 保存后在会话右下角模型选择器里选中即可

**注意**：旧版 a4agent 写的 `maxTokens: 131072` 是为了规避 dsh 适配器默认 256000 超出多数上游上限的问题。在界面里配置时，dsh 会按你填的能力声明处理，无需手动补这一项。

### ZCode

1. 打开 ZCode → 左下角模型选择器 → **管理模型** → **模型设置** → **添加供应商**
2. 填写：

   | 字段 | 填写内容 |
   |---|---|
   | 名称 | DeepSeek |
   | Anthropic 接口地址 | `https://api.deepseek.com/anthropic`（不带 `/v1`） |
   | OpenAI 接口地址 | `https://api.deepseek.com/v1`（必须带 `/v1`） |
   | API Key | 你的 Key |

   两个地址的 `/v1` 写法相反，填反会出现 401 / 404。

3. **添加模型** —— 自定义供应商不会自动拉取模型清单，必须手动添加，模型 ID 需精确匹配
4. **开启「启用」开关** —— 不开模型不会出现在选择器里
5. 重启 ZCode 后生效

### pi

1. 方案一：直接编辑 `~/.pi/agent/models.json`

   ```json
   {
     "providers": {
       "deepseek": {
         "baseUrl": "https://api.deepseek.com/v1",
         "api": "openai-completions",
         "apiKey": "sk-你的Key",
         "models": [{ "id": "deepseek-v4-flash" }]
       }
     }
   }
   ```

2. 在 `~/.pi/agent/settings.json` 里把 `defaultProvider` 设为 `deepseek`、`defaultModel` 设为模型 ID
3. 方案二：直接敲 `/model` 在交互界面里选（models.json 每次打开 `/model` 都会热重载，无需重启）

## 原配置去哪了

移除时会自动做三件事：

1. **ZCode / pi**：删除 `a4a_p*` 开头的托管条目。这些条目是 a4agent 生成的，删掉不影响你自己添加的 provider。若原`model`字段正指向被删条目，会一并清空，让 ZCode / pi 回到「未选模型」状态
2. **dsh**：仅当 `llm-deepseek.baseURL` 确实指向本工具的本地代理端口（17890–17899）时，删除 `llm-deepseek` 段。你在 dsh 界面里配的 `llm-pi-ai` 段**完全不受影响**
3. **`.credentials.yaml`**：移除写入的代理 token。`.credentials.yaml` 里你在 dsh 界面配的其它凭证键原样保留

所有被修改的文件在操作前都会备份到 `<数据目录>/backups/pre-switch-removal/`，**永不自动清理**。任何一步失败都会回滚。

## 常见问题

**Q：dsh 打开报连不上localhost:17890？**
说明旧配置的 `llm-deepseek` 段还在。升级到新版本会自动清理；若未自动清理，请手动删除 `~/.dsh/settings.yaml` 里的 `llm-deepseek` 段。

**Q：dsh 里我配的 8 个服务商 Key 还在吗？**
在。清理只针对 `llm-deepseek` 段和 a4agent 写入的 `DEEPSEEK_API_KEY`，你在 dsh 界面配置的其它凭证键（如 `OPENROUTER_API_KEY`、`KIMI_API_KEY` 等）原样保留。

**Q：能不能不迁移，继续用a4agent 配？**
不能。a4agent 只为**没有图形界面**的CLI 工具（Claude Code、Codex）代管配置。这三端有界面，代管属于重复且易错。

**Q：无头任务下发还能用吗？**
能，而且更自由。任务下发已改为**接管你在应用内自己配好的配置**——直接跑一次真实无头调用来验证连通性，不再依赖 a4agent 托管的配置条目。六个端（Claude Code、Codex、ZCode、Qoder、dsh、pi）都支持。