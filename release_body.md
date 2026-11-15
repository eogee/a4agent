# a4agent v0.7.2（设备运行时 node 0.5.1 + 九端支持 + 配对 toast 与多实例 / Runtime node 0.5.1, nine-tool support, pairing toast & multi-instance）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.7.2.exe——九端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）

Windows: a4agent-setup-0.7.2.exe — the nine-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway").

**v0.7.2 更新内容：**

内嵌设备运行时升级 node 0.5.1（CrossDevice v0.5.1 随版）——安装器 `node_runtime\` 随构建取 CrossDevice 同版本源码：① put 白名单可发现性（B42）：默认暂存目录（node 数据目录下 `tmp`）恒可写，`SANDBOX_DENIED` 报错 message 携带当前允许根列表、error 对象新增 `allowed_roots`/`staging_root`，并新增 `GET /api/v1/devices/{name}/workspaces` 白名单查询端点；② exec 命令引号逐字透传（B43）：cmd 形态逐字直达 `COMSPEC /c`（带引号参数不再失效）、powershell 改 `-EncodedCommand`、none 形态按 Windows argv 语义切分；③ 内置 crossdevice 技能副本同步更新（新增「大 payload / 长脚本投递」节与「宏（xd macro rec/run）」能力节；宏为 CrossDevice 控制端 CLI 本地能力，不经 a4agent HTTP 接口）

配对系统 toast（B39）——backend 常驻 pair_watcher 监听本机 node 配对会话，有设备发起配对时直发系统 toast「有设备请求配对，请在设备页确认」，approve 成功再发回执 toast（不含配对码/IP/令牌）；toast 不可用或异常时静默降级，绝不阻断配对流程

本机被控端多实例（B40）——运行时设置可维护实例名清单，守护进程按实例逐个拉起独立 node 子进程（`--instance`，独立配置/审计/任务库，共享 TLS 证书）；`GET /api/v1/devices/runtime` 返回 `instances` 数组并新增实例 start/stop 端点；设备页按实例渲染多卡片（启停/移除/添加入口）；默认无实例时单实例行为零变化（spawn 参数逐字节回归断言），配对协议零变更

九端支持（TRAE）——技能管理全量接入 TRAE（全局根 `~/.trae-cn/skills`、项目级 `<repo>/.trae/skills`；内置技能目录随 TRAE 更新整体重写，不写入不托管）+ MCP 只读发现（明文缓存读取，写入/删除/迁移闸门全部拒绝并如实标注只读）；设备页「安装控制技能」全局档覆盖九端

发现池回环帧过滤（B45）——本机回环来源的发现广播帧直接丢弃，修复设备页「发现的设备」幽灵条目；自动化测试/门槛运行不再要求退出本机 a4agent

卸载器修复（B38 卸载黑盒实测发现，v0.7.1 同源缺陷）——修复卸载链路两处缺陷：① 卸载阶段误用 WizardSilent 触发 Inno fatal（Internal error: Cannot call "WizardSilent" function during Uninstall），静默与交互式卸载均必然中止；② KillNodeProcesses 守护清理的 PowerShell 过滤器误用裸 `and`/`or`（应为 `-and`/`-or`）导致 node 子进程从不被终止，安装目录因文件锁残留。修复后实测：卸载完整执行、安装目录全清（含内嵌 node_runtime）、数据目录按设计保留、node 子进程零残留

稳定性与 i18n——deskqueue 毫秒级文件名碰撞治理（测试侧注入受控时钟，产品代码零改动、断言零削弱）；设备页标签 EN 模式漏译修复；i18n-check 纳入 HTML 文本节点收集

质量门槛——后端全量 793 通过（连跑 3 次 0 failed）；i18n 413 条未命中 0；CrossDevice 侧六道发布门槛全绿（8/8、10/10 发布档 --full 2048MB 实测、10/10、14/14、5/5、5/5）；随版双机实测完成：升级路径（push-node 0.5.1 → 分块投递安装包 → 静默覆盖安装 0.7.2，注册表/可执行文件/UI 页脚三处版本留证，期间远端断链约 30 秒自动恢复）、配对 toast 链路（watcher 边沿触发与发送实证；横幅视觉在测试机因系统全局通知关闭被压制，详见验收记录）、多实例真机（第二实例与默认实例共存、配对连通、移除清理）、卸载黑盒（核查清单逐项通过：安装目录全清/数据目录保留/任务·服务·自启·防火墙零残留）；CrossDevice tag v0.5.1 已随独立包发布

---

**What's new in v0.7.2:**

Embedded device runtime upgraded to node 0.5.1 (shipped with CrossDevice v0.5.1) — the installer's `node_runtime\` is staged from the same-version CrossDevice sources at build time: (1) put-sandbox discoverability (B42): the default staging directory (`tmp` under the node data directory) is always writable, `SANDBOX_DENIED` messages now carry the current allowed-roots list, the error object gains `allowed_roots`/`staging_root`, and a new `GET /api/v1/devices/{name}/workspaces` query endpoint is available; (2) verbatim exec quoting (B43): the cmd form passes the command line straight to `COMSPEC /c` (quoted arguments no longer break), powershell switches to `-EncodedCommand`, and the none form splits arguments by Windows argv semantics; (3) the bundled crossdevice skill copy is updated (new "large payload / long script delivery" section and an "xd macro rec/run" capability section — macros are a CrossDevice control-side CLI capability, not an a4agent HTTP API)

Pairing system toast (B39) — a resident pair_watcher thread watches the local node's pairing sessions and fires a system toast ("a device is requesting pairing, please confirm on the Devices page") on the pending edge, with a second acknowledgment toast after approval (no pairing code / IP / tokens included); silent fallback when toasts are unavailable — pairing is never blocked

Multi-instance local node (B40) — runtime settings can maintain an instance-name list; the guardian spawns one independent node subprocess per instance (`--instance`, separate config/audit/task storage, shared TLS certificate); `GET /api/v1/devices/runtime` returns an `instances` array plus instance start/stop endpoints; the Devices page renders one card per instance (start/stop/remove/add); with no instances configured, single-instance behavior is byte-for-byte unchanged, and the pairing protocol is untouched

Nine-tool support (TRAE) — full skill-management integration for TRAE (global root `~/.trae-cn/skills`, project level `<repo>/.trae/skills`; TRAE's built-in skill directory is rewritten wholesale on TRAE updates and is never written or managed) plus read-only MCP discovery (plaintext caches are read; write/delete/move gates all refuse and are labeled read-only); the Devices page "Install control skill" global scope now covers all nine tools

Discovery-pool loopback filtering (B45) — discovery broadcast frames sourced from loopback are dropped, fixing ghost entries in the Devices page "Discovered Devices" list; automated tests/gates no longer require quitting the local a4agent

Uninstaller fixes (found by the B38 uninstall black-box test; the same defects shipped in v0.7.1) — two uninstall-path defects repaired: (1) the uninstall phase called WizardSilent, which raises an Inno fatal ("Cannot call WizardSilent function during Uninstall") so every uninstall, silent or interactive, aborted immediately; (2) the KillNodeProcesses PowerShell filter used bare `and`/`or` (should be `-and`/`-or`) so the node subprocess was never terminated and locked files left the install directory behind. Verified after the fix: the uninstall completes, the install directory (including the embedded node_runtime) is fully removed, the data directory is kept as documented, and no node subprocess survives

Stability & i18n — deskqueue millisecond filename-collision hardening (controlled-clock injection in tests; zero product-code changes, zero assertion weakening); fixed a Devices-page label missing its EN translation; i18n-check now collects HTML text nodes

Quality gates — 793 backend tests passing (3 consecutive runs, 0 failures); i18n 413 strings, 0 misses; all six CrossDevice release gates green (8/8, 10/10 with the release-mode --full 2048 MB run, 10/10, 14/14, 5/5, 5/5); dual-machine release testing completed: the upgrade path (push-node to 0.5.1, chunked delivery of the installer, silent in-place upgrade to 0.7.2 with version evidence in the registry, executable and UI footer, ~30 s channel gap that auto-recovered), the pairing-toast chain (watcher edge-trigger and delivery verified in logs; the banner visual was suppressed by the test machine's system-wide notifications setting — see the acceptance record), multi-instance on real hardware (a second instance coexisting with the default, paired and reachable, then removed and cleaned), and the uninstall black-box (checklist fully passed: install directory fully removed / data directory kept / tasks, services, autoruns and firewall rules with zero residue); CrossDevice tag v0.5.1 shipped as a standalone package

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94

## 校验 / Checksum

```
7D640A74D650737C883C841A453995BC70BF125C956F17A928BA138617535A5D
```
