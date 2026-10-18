# a4agent v0.6.3

## 新增：WorkBuddy 成为第八端（技能与 MCP 管理）

「技能」与「MCP」两个页签现在都覆盖 WorkBuddy，和前七端同权：在界面上迁移、编辑、开关一个 skill 或一个 MCP server，落到 WorkBuddy 的就是它会去读的那份文件。

| | 用户级 | 项目级 |
|---|---|---|
| 技能 | `~/.workbuddy/skills` | `<工作区>/.workbuddy/skills` |
| MCP | `~/.workbuddy/mcp.json`（顶层 `mcpServers`） | 无 |

- MCP 侧只托管全局：WorkBuddy 没有工作区级 MCP 配置文件，能力矩阵里标为仅 `global`，界面不会给出「写进一个它永远不读的文件」这种选项；技能侧项目级照常可用
- 传输方式支持 stdio / sse / http，写回时按 WorkBuddy 的驼峰拼写 `streamableHttp`
- WorkBuddy 的 MCP 条目自带 `timeout` 等自有键，读写往返无损保留
- 「一键适配全部端」与跨端迁移已把 WorkBuddy 计入目标，适配完成后不再重复迁移一份

本版来自社区 PR（作者 forcert），合并时收窄了范围。无头任务下发仍是七端——WorkBuddy 的 CLI 无头执行是独立特性，未接入引擎列表。

## 修复：通知话题名不再是标签汤

WorkBuddy 与 Codex 会把 `system-reminder`、`AGENTS.md` 等上下文块注入到会话首条用户消息，桌面横幅和手机推送的话题名称此前直接抓成了这些标签本身，一眼看不出这个会话在做什么。

现在 `extract_first_prompt` 按白名单剥离注入块：配对注入块整段移除，WorkBuddy 的 `<user_query>` 外壳只剥标签、保留内容；剥完为空则跳到下一条真实消息，取到的就是用户真正打的那句话。用户自己贴进对话的 XML 不受影响；ZCode / OpenCode / DSH 与任务下发链路不走这个函数，行为不变。

## 修复：全库审查，二十余处高危问题

凭证与崩溃：

- MCP 回收站恢复解密失败时不再静默清空凭证，改为立即中止并报错
- 第二条桌面横幅不会再崩掉进程（此前 legacy toast 回调会残留）
- 更新安装包路径加约束，离线且缺校验值时拒绝 apply
- `/v1/proxy/refresh` 补上 token 校验
- 退出时补齐任务子进程与 llama-server 回收，不再留孤儿进程占着 8080

竞态：

- 任务取消后复查补杀，RUNNING 落库后再复查，防止「取消后复活」
- OpenCode 的 SSE stop 主动关连接并 join，堵住线程与连接泄漏
- 预检加 300 秒总预算，不再无上限等待

健壮性：

- 损坏的 yaml 不再穿透成 500（`read_doc` 兜底放宽）
- 日志滚动清理恢复生效（前缀改从模板取）
- MCP 配置备份加秒级碰撞防护；下载器校验 `Content-Length`；旧引擎改名时保底换入
- 技能迁移改 staging 换入并跳过 junction / symlink；移除操作一旦部分快照失败即中止，并按端回滚
- 代理退出给孤儿 120 秒宽限；`--api-key` 在日志中脱敏

llama：

- GPU 检测加 60 秒缓存，1.5 秒一次的轮询不再反复 spawn `nvidia-smi`
- 截断、崩溃重启三个运行设置真正接线
- 接入目标收窄为 Claude Code 与 Codex，与 v0.5.1 起的三端移交策略对齐

## 兼容性说明

- Windows 10/11 64 位
- 技能与 MCP 的第八端支持默认生效、无开关；没装 WorkBuddy 时对应目录不存在，界面不显示其条目
- 新增的校验与回滚只让异常路径更早停下，正常流程行为不变
- 配置与数据仍存于 `%APPDATA%\a4agent`，升级全程保留

## 升级说明

直接运行 `a4agent-setup-0.6.3.exe` 覆盖安装即可。

如果你还停在 0.6.2，并且遇到过桌面横幅标题是一堆 `<system-reminder ...>` 的情况：那一版安装包早于修复（10-10 打包，修复在 10-11 提交），本版安装包已包含。

## 校验

```
5CEA550476CD2A56E2CB45C569D1005972BFF4C61EDF18FF0238717F033A989A  a4agent-setup-0.6.3.exe
```
