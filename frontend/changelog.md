# a4agent v0.6.5（提醒不再后知后觉 / Timely reminders）

**下载对应平台的安装包 / Download for your platform：**

Windows：a4agent-setup-0.6.5.exe——八端 AI 编程工具管理台 + 本地大模型推理控制台，双击安装（per-user 免管理员；SmartScreen 提示时选「更多信息 → 仍要运行」）

Windows: a4agent-setup-0.6.5.exe — the eight-tool AI coding console with a local LLM inference panel. Double-click to install (per-user, no admin; if SmartScreen prompts, choose "More info → Run anyway").

**v0.6.5 更新内容：**

WorkBuddy 提问提醒不再后置——WorkBuddy 把提问做成宿主原生面板，面板期间既不触发 hook 也不写载荷，提醒只能等作答完才到（实测迟 3 分钟）。现在改由会话雷达轮询会话记录，「有调用无结果」即判定面板正在等待作答，面板出现 354 毫秒内就弹横幅；权限审批不受影响，实测其 hook 在弹窗期间正常执行

通知标题不再是一串标签——附件、技能引用、长文本引用被折叠成的 `@image#1:`、`@skill:`、`@long-text:` 占位符，以及后台任务事件的 `<task-notification>` 块，都不再进标题；技能引用只留技能名、长文本只留预览，标题同时压成单行并限长 80 字

桌面提醒更快——提醒队列的消费轮询从 2 秒缩到 0.4 秒，各端共用，从任务结束到横幅弹出压到 0.5 秒内

---

**What's new in v0.6.5:**

Question prompts from WorkBuddy are no longer late — WorkBuddy renders questions in a native panel that triggers no hook and writes no payload while it is open, so the reminder could only arrive after you had already answered (measured: about three minutes late). A session radar now polls the transcript and treats a call without a result as "the panel is waiting", firing the banner within 354 ms of the panel appearing. Permission requests are unaffected — their hook does run while the dialog is open

Notification titles are no longer a wall of tags — the `@image#1:`, `@skill:` and `@long-text:` placeholders that the host folds attachments, skills and quoted text into, plus the `<task-notification>` block of background-task events, no longer reach the title; a skill reference keeps only its name and a quoted text only its preview, while the title is collapsed to one line and capped at 80 characters

Faster desktop reminders — the reminder queue is drained every 0.4 s instead of 2 s, shared by every host, so a finished task reaches the banner in under half a second

**源码与说明 / Source and docs：** https://github.com/eogee/a4agent ｜ https://eogee.com/article/94
