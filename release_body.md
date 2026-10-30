# a4agent v0.6.6（任务可带技能，循环融入下发 / Skills for tasks, loops inline）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.6.6.exe——八端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）

Windows: a4agent-setup-0.6.6.exe — the eight-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway").

**v0.6.6 更新内容：**

任务下发支持配置技能——下发与循环任务都能勾选技能（上限 20 个），运行前自动注入工作目录的 `.<端>/skills`：优先建 Windows 目录联接（秒挂零拷贝）、源不可用自动回退复制、同名已有技能一律跳过绝不覆盖、多个任务共享同一技能按引用计数、任务结束只删自建目录并自动清空空壳；选择器区分全局/项目/工作目录三档来源，未注册为项目的目录（含 a4agent 自身）也能选到其项目级技能

循环任务并入任务下发——不再有独立板块，同一张表单里把「执行计划」切到「按计划循环」即创建循环（按间隔 / 每天固定时刻 / 每周选定几天 / 仅一次，超时上限放宽到 4 小时），计划以横条常驻表单上方，启停、立即执行、运行历史、日志都在原位；点「编辑」把计划灌回主表单修改，周期与时刻选择全部换成 layui 风格面板（含圆角），不再出现浏览器原生的蓝色控件

工作目录首次真正生效——修复引擎进程没有拿到所选目录的缺陷：命令行目录参数与进程工作目录强制同值（Claude `--add-dir`、Codex `-C`、ZCode `--cwd`、Qoder `-w`），预检冒烟仍跑在主目录不受影响；此前选了目录却只有部分端真的在那个目录里执行

OpenCode 探测三处修复——服务地址从 `service.json` 自动发现（OpenCode 每次启动可能换端口，此前硬编码导致永远连不上）；回环请求绕过环境代理（带 `http_proxy` 的终端里探测到的是代理 502，真因被掩盖）；「明明装了却显示未安装」区分「未安装」与「已装但服务未运行」，引擎行新增「重新检测」按钮

界面归位与统一——「当前生效」状态与「新增配置」按钮从全局顶栏移入配置方案页头，顶栏只留全局项；任务队列的完成提示覆盖所有页面；新增/调整测试 26 条，全量 686 通过

---

**What's new in v0.6.6:**

Dispatched tasks can carry skills — both one-shot and loop tasks accept up to 20 skills, injected automatically into the working directory's `.<tool>/skills` before the run: Windows junctions first (instant, zero-copy), automatic fallback to copying when the source is unusable, existing same-name skills are always skipped and never overwritten, multiple tasks sharing one skill are reference-counted, and after the run only directories we created are removed. The picker distinguishes global / project / working-directory sources, and directories not registered as projects (a4agent itself included) can now expose their project-level skills too

Loops merge into dispatch — the separate loops tab is gone. In the same form, switch the execution plan to "on a schedule" to create a loop (interval / daily time / selected weekdays / once; timeout raised to 4 hours). Plans live in a strip above the form with enable, run-now, history and logs in place; "edit" pours the plan back into the main form. Schedule and time pickers are now layui-styled panels with rounded corners — no more native blue browser widgets

Working directories finally work — fixed a defect where the engine process never received the chosen directory: the command-line directory argument and the process cwd are now forced to the same value (Claude `--add-dir`, Codex `-C`, ZCode `--cwd`, Qoder `-w`), while the precheck smoke run stays in the home directory. Previously only some targets actually ran inside the directory you picked

Three OpenCode detection fixes — the service address is discovered from `service.json` (OpenCode may change ports per launch; the old hard-coded one never connected); loopback requests bypass the environment proxy (with `http_proxy` set you saw the proxy's 502 instead of the real cause); and "installed but service not running" is now distinguished from "not installed", with a re-detect button on the engine row

UI realignment — the active-configuration status and the "new configuration" button moved from the global header into the configurations page header, leaving only global items up top; task-finish toasts now reach you on any tab; 26 tests added or adjusted, 686 passing

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94

## 校验 / Checksum

```
75CBDE01554FBE831703DCDE1BC8BCF79BB201D78849FD33368AF9873453CA1C  a4agent-setup-0.6.6.exe
```
