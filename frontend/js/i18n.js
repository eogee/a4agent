/* a4agent 国际化（中/英）
 *
 * 设计取舍：本前端是 layui + 原生 JS，界面文案大量存在于「拼好的 HTML 字符串」里，
 * 若改成键值型 t('key') 需要动上千处模板拼接，风险远大于收益。这里采用
 * 「文案字典 + DOM 应用器」：
 *   1) DICT       —— 精确匹配的短文案（按钮、表头、提示、独立句子）；
 *   2) PATTERNS   —— 带 {0} 通配的句式，用于「文案 + 变量」拼出来的整句，
 *                    保证英文语序正确，而不是逐词替换成中式英文；
 *   3) 应用器遍历文本节点与 title/placeholder/aria-label/alt 属性，命中才替换，
 *      不命中保持原文（用户数据、后端文案不会被误伤）。
 *
 * 幂等：中文模式下只处理「不含中文」的节点，英文模式下只处理「含中文」的节点，
 * 因此重复执行不会来回翻转，也不需要记录原文。
 */
(function (global) {
  'use strict';

  var LANG_KEY = 'a4agent.lang';
  var SUPPORTED = ['zh', 'en'];
  var CJK = /[\u3400-\u9fff\uf900-\ufaff]/;
  var LATIN = /[A-Za-z]/;
  var ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
  var SKIP_TAGS = { SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, TEXTAREA: 1, NOSCRIPT: 1, SVG: 1 };

  /* ---------------- 文案字典：中文 => English ---------------- */
  var DICT = {
    /* ---------- 静态框架（index.html） ---------- */
    "当前生效：": "Active:",
    "加载中…": "Loading…",
    "代理服务": "Proxy",
    "检查更新": "Check for updates",
    "退出": "Quit",
    "新增配置": "New configuration",
    "配置方案": "Configurations",
    "供应商管理": "Providers",
    "技能管理": "Skills",
    "MCP 管理": "MCP",
    "本地模型": "Local models",
    "任务下发": "Tasks",
    "通知提醒": "Notifications",
    "设备": "Devices",
    "供应商模板": "Provider templates",
    "新增供应商": "New provider",
    "全局视图": "Global view",
    "项目视图": "Project view",
    "项目来源": "Project sources",
    "迁移日志": "Migration log",
    "回收站": "Trash",
    "刷新": "Refresh",
    "安装 MCP": "Install MCP",
    "官网 eogee.com": "Website eogee.com",
    "使用文档": "Documentation",
    "问题反馈": "Feedback",
    "QQ 3886370035": "QQ 3886370035",
    "微信 eogee2022": "WeChat eogee2022",
    "退出 a4agent（关窗只是转入后台）": "Quit a4agent (closing the window only sends it to the background)",
    "搜索名称、描述或项目文件夹": "Search name, description or folder",
    "按技能名称、描述、项目文件夹名或根路径搜索": "Search by skill name, description, project folder name or root path",
    "按 MCP 名称、描述、项目文件夹名或根路径搜索": "Search by MCP name, description, project folder name or root path",
    "清空": "Clear",
    "清空搜索": "Clear search",
    "扫描范围与自动发现的项目来源": "Scan roots and auto-discovered project sources",
    "查看版本更新详情与检查更新": "Version details and update check",
    "官方网站": "Official website",
    "GitHub 官方主页": "GitHub repository",
    "在应用内提交需求与 Bug 反馈": "Report bugs and request features in-app",
    "点击复制 QQ 号": "Click to copy the QQ number",
    "点击复制微信号": "Click to copy the WeChat ID",

    /* ---------- 通用状态与动作 ---------- */
    "保存": "Save",
    "取消": "Cancel",
    "删除": "Delete",
    "编辑": "Edit",
    "切换": "Switch",
    "关闭": "Close",
    "确定": "OK",
    "刷新状态": "Refresh",
    "操作": "Actions",
    "名称": "Name",
    "协议": "Protocol",
    "来源": "Source",
    "成功": "Success",
    "失败": "Failed",
    "未知": "Unknown",
    "详情": "Details",
    "介绍": "Notes",
    "预览": "Preview",
    "打开": "Open",
    "复制": "Copy",
    "加载失败": "Load failed",
    "未选择": "Not selected",
    "已复制": "Copied",
    "已复制：": "Copied:",
    "已删除": "Deleted",
    "已保存": "Saved",
    "保存成功": "Saved",
    "已取消": "Cancelled",
    "已忽略": "Ignored",
    "请求失败": "Request failed",
    "未设置（使用默认配置）": "Not set (using the default configuration)",
    "状态获取失败": "Failed to load status",
    "代理服务（运行中）": "Proxy (running)",
    "代理服务已关闭": "Proxy turned off",
    "关闭代理服务": "Turn off proxy",
    "关闭后当前 OpenAI 服务商将无法使用，需要重新切换配置才能恢复。确定关闭代理服务吗？": "With the proxy off, the current OpenAI provider stops working and you must switch the configuration again to recover. Turn off the proxy?",
    "· 已移交": "· Handed over",
    "使用中": "In use",
    "自定义": "Custom",
    "预置": "Built-in",
    "删除确认": "Confirm deletion",

    /* ---------- 供应商 ---------- */
    "OpenAI 兼容（原生 Responses）": "OpenAI-compatible (native Responses)",
    "OpenAI 兼容（本地代理）": "OpenAI-compatible (local proxy)",
    "还没有供应商，点击右上角「新增供应商」创建": "No providers yet. Click \"New provider\" in the top right to create one.",
    "加载失败：": "Failed to load: ",
    "如：硅基流动": "e.g. SiliconFlow",
    "协议类型": "Protocol",
    "Anthropic（Claude Code 原生）": "Anthropic (native to Claude Code)",
    "OpenAI 兼容（走本地翻译代理）": "OpenAI-compatible (via the local translating proxy)",
    "Codex 需使用 OpenAI 兼容（Responses）接口；上游原生支持 Responses 时可勾选下方开关直连，否则经本地代理。": "Codex needs an OpenAI-compatible (Responses) endpoint. If the upstream supports Responses natively, turn on the switch below to connect directly; otherwise traffic goes through the local proxy.",
    "原生 Responses": "Native Responses",
    "上游原生支持 OpenAI Responses（如 DeepSeek）时开启：Codex 将直连上游，无需本地代理。": "Turn on when the upstream natively supports OpenAI Responses (e.g. DeepSeek): Codex then connects directly, without the local proxy.",
    "编辑供应商": "Edit provider",
    "请填写名称和 Base URL": "Name and Base URL are required",
    "该供应商下有配置方案时无法删除": "It can't be deleted while configurations still use it",
    "该端已改为在应用内自配，本工具不再代管": "This target is now configured inside its own app; a4agent no longer manages it",

    /* ---------- 配置方案 ---------- */
    "还没有配置方案，点击页头「新增配置」创建": "No configurations yet. Click \"New configuration\" in the page header to create one.",
    "切换后重启 Claude Code（若正在运行）": "Restart Claude Code after switching (if it is running)",
    "Codex 配置写入后需重启 Codex 才生效": "Restart Codex after switching for the new configuration to take effect",
    "确认切换": "Confirm switch",
    "切换成功": "Switched",
    "（原生Responses）": " (native Responses)",
    "方案名称": "Config name",
    "如：日常开发 / 写作": "e.g. Daily coding / Writing",
    "服务商": "Provider",
    "选择或搜索服务商": "Select or search a provider",
    "没有你的供应商？点此添加": "Provider not listed? Add it here",
    "留空则不修改": "Leave empty to keep the current key",
    "粘贴 API Key": "Paste the API Key",
    "模型": "Model",
    "应用目标": "Targets",
    "DSH / ZCode / Pi 已在各自应用内支持自定义供应商，请直接在那里配置": "DSH / ZCode / Pi support custom providers inside their own apps — configure them there",
    "编辑配置方案": "Edit configuration",
    "新增配置方案": "New configuration",
    "请填写方案名称、服务商和模型": "Configuration name, provider and model are required",
    "请填写 API Key": "API Key is required",
    "请至少选择一个应用目标（Claude Code / Codex）": "Select at least one target (Claude Code / Codex)",
    "Codex 需使用 OpenAI 兼容接口，请更换服务商或去掉 Codex 目标": "Codex requires an OpenAI-compatible endpoint — choose another provider or drop the Codex target",
    "删除任务": "Delete task",
    "至少需要一个扫描范围；不需要扫描范围可以点「恢复默认」": "At least one scan root is required; click \"Restore defaults\" if you don't need any",
    "该目录已在扫描范围中": "That folder is already in the scan roots",
    "已重新扫描": "Rescanned",
    "重扫失败": "Rescan failed",
    "已保存。": "Saved.",
    "扫描范围": "Scan roots",
    "根下的一级子目录视为项目": "First-level subfolders under a root count as projects",
    "恢复默认": "Restore defaults",
    "+ 添加文件夹": "+ Add folder",
    "目录选择组件未加载，请刷新页面": "The folder picker did not load — refresh the page",
    "添加扫描范围": "Add a scan root",
    "选中一个包含多个项目的父文件夹，它下面的一级子目录会被当作项目": "Pick a parent folder that contains several projects; each first-level subfolder becomes a project",
    "暂无迁移记录": "No migrations yet",
    "时间": "Time",
    "源": "Source",
    "目标": "Target",
    "结果": "Result",
    "配置自带": "From config",
    "自动识别": "Auto-detected",
    "npm 自动识别": "npm auto-detected",
    "（来源：": " (source: ",
    "无功能介绍（可点「介绍」补充）": "No description (click \"Notes\" to add one)",
    "各端还未配置任何 MCP server": "No MCP server is configured on any target",
    "未发现任何含 MCP server 的项目": "No project with an MCP server was found",
    "该项目下没有 MCP server": "No MCP server in this project",
    "端不支持": "Target does not support",
    "不支持": "Not supported",
    "端": "target",
    "传输": "Transport",
    "命令": "Command",
    "参数": "Arguments",
    "环境变量": "Environment variables",
    "（已脱敏）：": " (masked): ",
    "配置文件": "Config file",
    "功能介绍 ·": "Notes ·",
    "为这个 MCP server 备注功能说明，展示在卡片上（配置文件自带的 description 也会显示）。留空保存即清除自定义介绍。": "Add a note about what this MCP server does; it shows on the card (a description from the config file is shown too). Save an empty note to clear it.",
    "例如：GitHub 仓库与 Issue 管理；连接本地 Postgres 查询数据…": "e.g. managing GitHub repos and issues; querying a local Postgres database…",
    "功能介绍已保存": "Notes saved",
    "粘贴 JSON": "Paste JSON",
    "表单填写": "Fill in the form",
    "安装到": "Install to",
    "全局配置": "Global config",
    "如：postgres / github": "e.g. postgres / github",
    "启动命令": "Start command",
    "如：npx / python / 程序路径": "e.g. npx / python / path to the program",
    "空格分隔，如：-y @modelcontextprotocol/server-github": "Space separated, e.g. -y @modelcontextprotocol/server-github",
    "每行 KEY=VALUE": "One KEY=VALUE per line",
    "服务地址": "Endpoint URL",
    "请求头": "Headers",
    "每行 KEY=VALUE，如 Authorization=Bearer xxx": "One KEY=VALUE per line, e.g. Authorization=Bearer xxx",
    "可选：这个 MCP 是干什么的": "Optional: what this MCP is for",
    "配置片段": "Config snippet",
    "兼容「mcpServers」顶层 / 名称作键的 server 字典 / 单对象，多条可一次批量导入": "Accepts a top-level \"mcpServers\" object, a name-keyed server map, or a single object; several can be imported at once",
    "安装 MCP 服务": "Install MCP servers",
    "安装": "Install",
    "请粘贴 MCP 配置 JSON": "Paste the MCP configuration JSON",
    "成功安装": "Installed successfully",
    "个：": ": ",
    "请填写 MCP server 名称": "MCP server name is required",
    "已安装到": "Installed to",
    "确定删除这个 MCP server？": "Delete this MCP server?",
    "将从配置中移除并移入回收站，30 天内可恢复": "It will be removed from the configuration and moved to the trash; restorable for 30 days",
    "MCP 回收站（30 天内可恢复）": "MCP trash (restorable for 30 days)",
    "MCP 迁移日志": "MCP migration log",
    "浏览器形态请直接关闭页面": "In the browser, just close the page",
    "退出": "Quit",
    "确定退出 a4agent？": "Quit a4agent?",
    "移除": "Remove",

    /* ---------- 技能 / 迁移 / 回收站 ---------- */
    "全局": "Global",
    "无描述": "No description",
    "兼容目录": "compatible directory",
    "迁移…": "Migrate…",
    "各端的全局目录还没有任何 skill": "No skill in the global directories of any target",
    "未发现任何含 skill 的项目，可点击右上角「项目来源」调整扫描范围": "No project with skills was found. Click \"Project sources\" in the top right to adjust the scan roots.",
    "一键适配全部端": "Adapt to all targets",
    "该项目下没有 skill": "No skill in this project",
    "技能": "skills",
    "准备中…": "Preparing…",
    "迁移进行中，请勿进行其他操作": "Migration in progress — hold on",
    "选择项目文件夹": "Choose a project folder",
    "进到目标项目文件夹后点「选择此文件夹」；该文件夹下不存在 skill 也没关系，迁移会自动创建目录": "Navigate into the target project folder and click \"Choose this folder\"; the folder does not need to contain a skill — directories are created automatically",
    "数据已过期，请刷新后重试": "The data is stale — refresh and try again",
    "迁移哪一份？": "Which copy should be migrated?",
    "复制到哪些位置？（目标端同名旧版将移入回收站）": "Where should it be copied to? (An older same-named copy on a target moves to the trash)",
    "自选项目文件夹": "Custom project folder",
    "目录缺失自动创建": "Directories are created if missing",
    "选择文件夹": "Choose folder",
    "所选文件夹": "Selected folder",
    "开始迁移": "Start migration",
    "请先选择项目文件夹": "Choose a project folder first",
    "请至少勾选一个迁移目标": "Select at least one migration target",
    "该项目没有可迁移的 skill": "No skill to migrate in this project",
    "将按以下计划复制补齐（源端保留）：": "The following copies will be added (the source is kept):",
    "执行迁移": "Run migration",
    "（源端保留）": " (source kept)",
    "描述": "Description",
    "路径": "Path",
    "SKILL.md 没有正文内容": "SKILL.md has no body content",
    "预览 ·": "Preview ·",
    "已在资源管理器打开": "Opened in File Explorer",
    "确定删除这个 skill？": "Delete this skill?",
    "将移入回收站，30 天内可恢复": "It moves to the trash and can be restored within 30 days",
    "已移入回收站": "Moved to the trash",
    "回收站（30 天内可恢复）": "Trash (restorable for 30 days)",
    "已自动清理": "Auto-cleaned",
    "回收站是空的": "The trash is empty",
    "原位置": "Original location",
    "剩余天数": "Days left",
    "恢复": "Restore",
    "彻底删除": "Delete permanently",
    "已恢复到原位置": "Restored to its original location",
    "已彻底删除": "Permanently deleted",
    "自动发现": "Auto-discovery",
    "检测中…": "Checking…",
    "可访问": "Accessible",
    "不存在或无权限": "Missing or no permission",
    "还没有任何扫描范围。点「添加文件夹」选一个，或点「恢复默认」使用本机所有盘符。": "No scan roots yet. Click \"Add folder\" to pick one, or \"Restore defaults\" to use every drive on this machine.",
    "没有可自动发现的项目。填过历史记录的 AI 工具（Claude Code / Codex / VS Code 系编辑器）会在下次启动后出现在这里。": "No auto-discoverable projects. AI tools with saved history (Claude Code / Codex / VS Code-family editors) appear here after their next start.",
    "扫描": "Scan",
    "忽略": "Ignore",
    "尚未读到任何历史记录": "No history records read yet",
    "重新扫描": "Rescan",
    "加载失败": "Load failed",
    "迁移日志": "Migration log",
    "时间": "Time",
    "结果": "Result",
    "详情": "Details",

    /* ---------- 版本与更新 ---------- */
    "当前版本过旧，请下载完整安装包更新": "This version is too old — download the full installer to update",
    "网络或服务器不可达": "Network or server unreachable",
    "DSH / ZCode / Pi 不再由 a4agent 代管 API 配置": "DSH / ZCode / Pi API configuration is no longer managed by a4agent",
    "这三端应用自带完整的供应商配置界面，由你在应用内自行配置更可靠。": "These three apps ship with a full provider setup screen, and configuring them inside the app is more reliable.",
    "你原先用 a4agent 配的条目已自动清理（清理前已做永久快照），你自己配的 provider 与 Key 一概未动。": "Entries a4agent created earlier were cleaned up automatically (a permanent snapshot was taken first); providers and keys you configured yourself are untouched.",
    "如需继续使用，请到各应用设置里重新配置一次。": "To keep using them, set them up once more in each app's settings.",
    "暂无更新说明": "No release notes",
    "查看完整发布说明": "View the full release notes",
    "立即下载更新": "Download and update",
    "忽略此版本": "Skip this version",
    "暂不": "Not now",
    "取消下载": "Cancel download",
    "已取消下载": "Download cancelled",
    "未知错误": "Unknown error",
    "更新包下载完成": "Update downloaded",
    "安装包已下载并通过 SHA256 校验。": "The installer was downloaded and passed SHA256 verification.",
    "是否现在运行安装程序完成更新？更新过程中 a4agent 将自动关闭。": "Run the installer now to finish updating? a4agent closes automatically during the update.",
    "立即更新": "Update now",
    "稍后": "Later",
    "正在启动更新安装程序，a4agent 即将关闭…": "Starting the installer — a4agent is closing…",
    "版本与更新": "Version & updates",
    "当前版本": "Current version",
    "完整发布说明（GitHub Releases）": "Full release notes (GitHub Releases)",
    "暂无更新说明，可点击上方链接查看 GitHub Releases。": "No release notes here — use the link above to open GitHub Releases.",
    "退出 a4agent": "Quit a4agent",

    /* ---------- 反馈 ---------- */
    "需求与反馈": "Feedback",
    "Bug 报告": "Bug report",
    "功能需求": "Feature request",
    "Bug 请写清「做了什么 → 实际结果 → 预期结果」；需求请写清使用场景与期望方案": "For bugs: what you did → what happened → what you expected. For features: the use case and what you'd like to see.",
    "点击选择或拖入问题截图": "Click to choose screenshots, or drop them here",
    "选填 · 最多 10 张 · 单张不超过 1MB": "Optional · up to 10 images · 1MB each",
    "联系方式：邮箱 / QQ / 微信（选填，方便回访）": "Contact: email / QQ / WeChat (optional, so we can follow up)",
    "提交后反馈与截图将直接发送给开发者；自动附带系统环境信息。": "Your report and screenshots go straight to the developer, along with system environment details.",
    "请勿包含 API Key 等敏感信息": "Please don't include sensitive data such as API keys",
    "，日志建议先自查。": "; skim the log first.",
    "也可直接联系：QQ 3886370035 ｜ 微信 eogee2022。": "You can also reach us directly: QQ 3886370035 | WeChat eogee2022.",
    "提交反馈": "Submit feedback",
    "请先填写问题描述（至少 5 个字）": "Please describe the problem (at least 5 characters)",
    "正在提交…": "Submitting…",
    "提交失败，请稍后重试": "Submission failed — please try again later",
    "反馈已提交，感谢您的支持！": "Feedback submitted — thank you!",
    "反馈已保存，但邮件发送失败（网络问题不影响留档）": "Feedback saved, but the email could not be sent (network issues do not affect the record)",
    "网络异常，提交失败": "Network error — submission failed",

    /* ---------- 本地模型（llama.cpp） ---------- */
    "运行中": "Running",
    "启动中": "Starting",
    "已停止": "Stopped",
    "请输入目录路径": "Enter a folder path",
    "本地大模型推理控制台": "Local LLM console",
    "把任意 .gguf 模型一键变成 OpenAI 兼容的本地 API 服务": "Turn any .gguf model into an OpenAI-compatible local API",
    "（基于 llama.cpp llama-server）。首次使用请运行配置向导：": " (powered by llama.cpp llama-server). On first use, run the setup wizard:",
    "自动获取推理引擎、检测显卡、选择模型。": "It fetches the inference engine, detects your GPU and lets you pick a model.",
    "✔ 检测到本机已有推理引擎，向导中将自动复用": "✔ An inference engine is already installed and will be reused by the wizard",
    "运行首次配置向导": "Run the setup wizard",
    "未设置默认模型": "No default model",
    "端口": "Port",
    "局域网开放": "LAN access",
    "仅本机": "This machine only",
    "引擎": "Engine",
    "就绪": "Ready",
    "缺失": "Missing",
    "配置向导": "Setup wizard",
    "启动服务": "Start service",
    "停止": "Stop",
    "接入信息": "Connection info",
    "接入配置方案": "Create a configuration",
    "模型库": "Model library",
    "输入包含 .gguf 模型的目录路径": "Folder path containing .gguf models",
    "添加目录": "Add folder",
    "运行日志": "Runtime log",
    "暂无日志": "No log yet",
    "推理设置": "Inference settings",
    "保存设置": "Save settings",
    "点击「添加目录」选择模型文件夹": "Click \"Add folder\" to pick a model folder",
    "正在启动…": "Starting…",
    "尚未添加模型目录": "No model folder added yet",
    "默认": "Default",
    "设为默认": "Set as default",
    "文件": "File",
    "大小": "Size",
    "量化": "Quantization",
    "原生上下文": "Native context",
    "⚠ 所选目录下没有找到可用的 .gguf 模型文件": "⚠ No usable .gguf model found in the selected folder",
    "已设为默认模型": "Set as the default model",
    "局域网地址": "LAN address",
    "局域网": "LAN",
    "未开放（在推理设置里切换监听为 0.0.0.0）": "Not exposed (switch the listener to 0.0.0.0 in Inference settings)",
    "决策模型端点": "Decision model endpoint",
    "模型名 (model)": "Model name (model)",
    "请先在模型库中设置默认模型": "Set a default model in the model library first",
    "将创建（或更新）一个指向本地 llama-server 的配置方案，": "A configuration pointing at the local llama-server will be created (or updated),",
    "模型为当前默认模型，并写入勾选的应用：": "using the current default model, and written into the checked apps:",
    "本地服务讲 OpenAI Chat Completions：Claude Code / Codex 经本地翻译代理接入。": "The local service speaks OpenAI Chat Completions: Claude Code / Codex connect through the local translating proxy.",
    "接入": "Connect",
    "请至少选择一个应用": "Select at least one app",
    "已创建并切换": "Created and switched",
    "已创建配置方案，请到「配置方案」页切换生效": "Configuration created — switch to it on the Configurations page",
    "· 推荐": "· Recommended",
    "（未安装）": " (not installed)",
    "下载": "Download",
    "引擎后端": "Engine backend",
    /* 引擎后端对比表（自动 / CUDA / Vulkan / CPU 横排） */
    "使用": "Use",
    "自动": "Auto",
    "已安装": "Installed",
    "推荐": "Recommended",
    "适配": "Fit",
    "跟随推荐": "Follows recommendation",
    "自动选用": "Auto-selected",
    "跟随显卡推荐": "GPU-based",
    "NVIDIA 显卡": "NVIDIA GPU",
    "通用后端": "Universal backend",
    "各品牌显卡": "Any GPU brand",
    "兜底方案": "Fallback",
    "无显卡时使用": "No GPU available",
    "✓ 当前": "✓ Current",
    "暂不可用": "Unavailable",
    "点「使用」选中后按「保存设置」生效；未安装的点「下载」，装好即自动切换": "Click \"Use\" then \"Save settings\" to apply; for missing engines click \"Download\" — it switches over as soon as it is installed",
    "监听": "Listen on",
    "仅本机 127.0.0.1": "This machine only, 127.0.0.1",
    "允许局域网 0.0.0.0": "Allow LAN, 0.0.0.0",
    "默认模型": "Default model",
    "投影模型 mmproj": "Projection model (mmproj)",
    "多模态投影模型路径，纯文本留空": "Multimodal projection model path; leave empty for text-only",
    "浏览": "Browse",
    "上下文长度": "Context length",
    "KV 缓存": "KV cache",
    "开启": "On",
    "GPU 层数 -ngl": "GPU layers (-ngl)",
    "99 = 全部 offload": "99 = offload everything",
    "线程数": "Threads",
    "0 = 自动": "0 = auto",
    "MTP 投机解码": "MTP speculative decoding",
    "0 = 关闭；需引擎与模型支持": "0 = off; needs engine and model support",
    "留空 = 不鉴权": "Empty = no authentication",
    "生成": "Generation",
    "内存裁剪": "Memory trimming",
    "运行就绪后定时释放文件缓存": "Periodically drop the file cache once the server is ready",
    "附加参数": "Extra arguments",
    "原样追加的 llama-server 参数": "Extra llama-server arguments, passed through as-is",
    "开始下载，完成后将自动切换到该引擎": "The download starts and the engine switches over automatically when it finishes",
    "浏览器模式下请手动粘贴文件路径": "In browser mode, paste the file path manually",
    "已生成，点击「保存设置」生效；服务运行中需重启服务加载新 Key": "Generated. Click \"Save settings\" to apply; restart a running service to load a new key",
    "设置已保存，引擎切换将在重启服务后生效": "Settings saved; the engine switch takes effect after restarting the service",
    "设置已保存": "Settings saved",
    "< 上一步": "< Back",
    "下一步 >": "Next >",
    "a4agent 本地模型 · 首次配置向导": "a4agent local models · first-run setup",
    "步骤": "Step",
    "获取推理引擎": "Get the inference engine",
    "检测硬件": "Detect hardware",
    "选择模型目录": "Choose a model folder",
    "选择默认模型": "Choose a default model",
    "服务端口": "Service port",
    "完成": "Done",
    "— 约": "— approx. ",
    "轻量安装不内置推理引擎（llama-server）。下面按你的显卡列出 llama.cpp 官方发布的引擎包": "The light installer does not bundle the inference engine (llama-server). Below are the official llama.cpp engine packages for your GPU",
    "离线安装：指定本机已有的引擎目录（含 llama-server.exe）": "Offline install: point at an existing engine folder (containing llama-server.exe)",
    "包含 llama-server.exe 的目录": "Folder containing llama-server.exe",
    "暂时跳过（稍后在设置里指定引擎目录）": "Skip for now (set the engine folder later in settings)",
    "请选择一个引擎包。": "Select an engine package.",
    "（可改用离线安装，或检查网络后重试）": "(try an offline install, or check the network and retry)",
    "✔ 引擎安装完成。": "✔ Engine installed.",
    "— 显存": "— VRAM",
    "· 驱动": "· Driver",
    "未检测到独立显卡（将以 CPU 模式运行，速度受限）": "No discrete GPU detected (running in CPU mode, expect lower speed)",
    "检测到的显卡": "Detected GPU",
    "为你推荐的推理预设": "Recommended inference preset",
    "预设档位：": "Preset:",
    "上下文": "Context",
    "预设只是起点：稍后可以随时在「推理设置」里调整上下文长度、KV 缓存级别、MTP 等所有参数。": "The preset is only a starting point: context length, KV cache level, MTP and the rest can be adjusted later in Inference settings.",
    "输入包含 .gguf 模型文件的目录路径": "Folder path containing .gguf model files",
    "请先添加模型所在目录。": "Add the folder that holds the model first.",
    "⚠ 所选目录下没有找到可用的 .gguf 模型文件。": "⚠ No usable .gguf model file in the selected folder.",
    "默认启动的模型：": "Model to start by default:",
    "文件：": "File:",
    "架构": "Architecture",
    "· 层数": "· Layers",
    "· 量化": "· Quantization",
    "· 大小": "· Size",
    "· 含 MTP 层（可在设置里开启投机解码）": "· Has MTP layers (speculative decoding can be enabled in settings)",
    "✔ 以当前预设运行没有显存风险。": "✔ This preset fits in your VRAM.",
    "服务端口：": "Service port:",
    "配置摘要": "Summary",
    "默认模型：": "Default model:",
    "推理预设：": "Inference preset:",
    "完成后立即启动服务": "Start the service right after finishing",
    "配置完成": "Setup complete",
    "所选目录中未找到有效的 .gguf 模型文件，请检查目录或添加其他目录。": "No valid .gguf model file in the selected folder — check it or add another folder.",
    "请选择一个默认模型。": "Select a default model.",

    /* ---------- 任务下发 ---------- */
    "排队中": "Queued",
    "执行中": "Running",
    "已完成": "Completed",
    "超时": "Timed out",
    "超时时间": "Timeout",
    "预检未通过": "Precheck failed",
    "下发任务": "Dispatch task",
    "执行计划": "Execution plan",
    "立即执行": "Run now",
    "按计划循环": "On a schedule",
    "计划名称": "Plan name",
    "周期": "Schedule",
    "下发后立即进队列，运行记录出现在下方任务列表": "Queued immediately after dispatch; runs appear in the task list below",
    "保存为计划，到点自动执行；应用开着才会触发": "Saved as a plan and runs on time; the app must be open to trigger",
    "保存计划": "Save plan",
    "保存修改": "Save changes",
    "保存失败": "Save failed",
    "计划已保存": "Plan saved",
    "循环任务已创建，到点自动执行": "Loop task created; it will run on schedule",
    "保存后到点自动执行；引擎以无头模式获得完全权限，请只把计划交给可信的工作目录": "Runs on schedule once saved; the engine runs headless with full permissions — only point plans at working directories you trust",
    "正在编辑计划": "Editing plan",
    "取消编辑": "Cancel editing",
    "默认取任务描述开头": "Defaults to the start of the task description",
    "2 小时": "2 hours",
    "4 小时": "4 hours",
    "当前为测试版本，功能仍在调整": "Test build — features are still being adjusted",
    "要交给 Agent 的任务描述，例如：审查当前项目的错误处理，列出最值得修的三处": "Describe the task for the agent, e.g. review error handling in this project and list the three fixes worth doing first",
    "使用模型": "Model",
    "以实测为准": "As measured",
    "工作目录": "Working directory",
    "选择文件夹…": "Choose folder…",
    "5 分钟": "5 minutes",
    "10 分钟": "10 minutes",
    "30 分钟": "30 minutes",
    "60 分钟": "60 minutes",
    "下发前自动检测引擎、服务商连通与端配置一致性": "Checks the engine, provider connectivity and target consistency before dispatching",
    "任务队列": "Task queue",
    "未选择 · 引擎在用户主目录执行": "Not selected · the engine runs in your home directory",
    "清除": "Clear",
    "选择工作目录": "Choose the working directory",
    "进到目标文件夹后点「选择此文件夹」，该文件夹下的内容就是引擎的工作目录": "Navigate into the target folder and click \"Choose this folder\" — its contents become the engine's working directory",
    "当前方案：": "Active configuration:",
    "尚未切换生效方案": "No configuration is active yet",
    "未安装": "Not installed",
    "服务未运行": "Service not running",
    "重新检测": "Re-check",
    "请先填写任务描述": "Describe the task first",
    "请选择引擎": "Select an engine",
    "已入队": "Queued",
    "还没有任务。填写任务描述并点「下发任务」，": "No tasks yet. Describe a task and click \"Dispatch task\";",
    "引擎会在后台跑完并留下产出": "the engine runs it in the background and leaves its output here",
    "耗时": "Duration",
    "创建": "Created",
    "目录": "Folder",
    "查看产出": "View output",
    "取消任务": "Cancel task",
    "产出": "Output",
    "退出码": "Exit code",
    "（无产出）": "(no output)",
    "\\n\\n…（产出较长，此处仅显示末尾部分，完整内容见产出文件）": "\\n\\n… (the output is long — only the tail is shown; open the output file for everything)",

    /* ---------- 任务下发 · 技能选择（tasks.js / skillpick.js） ---------- */
    "选择技能": "Choose skills",
    "选择技能…": "Choose skills…",
    "已选": "Selected",
    "个 · 任务结束自动移除": "· auto-removed when the task ends",
    "未选工作目录时技能不会注入到项目，仅作为提示词建议": "Without a working directory, skills are not injected into the project and serve only as prompt suggestions",
    "搜索名称或描述": "Search by name or description",
    "全局技能": "Global skills",
    "项目技能": "Project skills",
    "工作目录技能": "Working-directory skills",
    "工作目录": "Working dir",
    "项目": "Project",
    "该端不会加载此技能": "The selected target won't load this skill",
    "一次最多选择 20 个技能": "Pick at most 20 skills",
    "没有可用的技能": "No skills available",
    "没有匹配的技能": "No matching skills",
    "技能选择组件未加载，请刷新页面": "The skill picker is not loaded; refresh the page",
    "表单组件未加载，请刷新页面": "The task form is not loaded; refresh the page",
    "同名技能已存在，使用已有版本：": "Skills with the same name already exist; the existing copies are used: ",
    "未能注入：": "Failed to mount: ",
    "提示：": "Notes: ",

    /* ---------- 循环任务（并入任务下发页） ---------- */
    "循环任务": "Loops",
    "单次任务": "Tasks",
    "新建循环任务": "New loop",
    "编辑循环任务": "Edit loop",
    "调度中": "Scheduling",
    "调度已停止": "Scheduling stopped",
    "如：每日巡检": "e.g. Daily check",
    "每轮要交给 Agent 的任务，例如：检查昨天的报错日志并汇总到报告": "The task for each run, e.g. check yesterday's error logs and summarize them into a report",
    "触发计划": "Schedule",
    "按间隔重复": "Repeat at an interval",
    "每天固定时刻": "Every day at a set time",
    "每周选定几天": "On selected weekdays",
    "仅一次": "Once",
    "分钟一轮（上一轮结束或跳过后才计下一轮）": "minutes per run (the next run counts after the previous one finishes or is skipped)",
    "每天到这个点执行": "Runs every day at this time",
    "选中星期到这个点执行": "Runs at this time on the selected weekdays",
    "应用关着时错过的触发不会补跑": "Runs missed while the app is closed are not caught up",
    "周一": "Mon",
    "周二": "Tue",
    "周三": "Wed",
    "周四": "Thu",
    "周五": "Fri",
    "周六": "Sat",
    "周日": "Sun",
    /* 每周计划的首日带前缀：中英文语序不同（每周一 / Weekly on Mon），
       前缀跟着首日走才能两种语言都自然 */
    "每周一": "Weekly on Mon",
    "每周二": "Weekly on Tue",
    "每周三": "Weekly on Wed",
    "每周四": "Weekly on Thu",
    "每周五": "Weekly on Fri",
    "每周六": "Weekly on Sat",
    "每周日": "Weekly on Sun",
    "高级": "Advanced",
    "启动时补跑错过的触发": "Catch up on missed runs at startup",
    "保存后立即启用": "Enable right after saving",
    "循环任务与「任务下发」相同，以无头模式执行：引擎会自动获得完全权限（跳过交互确认），请只把循环任务交给可信的工作目录。": "Loops run headless, just like \"Dispatch task\": the engine gets full permissions automatically (no interactive confirmation). Only point loops at working directories you trust.",
    "超时 5 分钟": "Timeout 5 minutes",
    "超时 10 分钟": "Timeout 10 minutes",
    "超时 30 分钟": "Timeout 30 minutes",
    "超时 60 分钟": "Timeout 60 minutes",
    "超时 2 小时": "Timeout 2 hours",
    "超时 4 小时": "Timeout 4 hours",
    "按计划自动执行，应用开着才会触发": "run on a schedule — triggers only while the app is open",
    "还没有循环任务。点「新建循环任务」让 Agent": "No loops yet. Click \"New loop\" to let an agent ",
    "立即执行": "Run now",
    "运行历史": "Run history",
    "尚未运行": "Not run yet",
    "启动时补跑": "Catch up",
    "按当前显卡自动挑选引擎，换显卡后无需改设置": "Picks the engine for your current GPU — no setting to change when you swap GPUs",
    "已停用": "Paused",
    "即将执行": "due now",
    "已跳过": "Skipped",
    "已错过": "Missed",
    "已结束": "Finished",
    "删除循环任务": "Delete loop",
    "日志摘要": "Log excerpt",
    "查看日志": "View log",
    "完整日志": "Full log",
    "手动": "Manual",
    "取消运行": "Cancel run",
    "确定取消这次运行？会终止引擎进程。": "Cancel this run? This terminates the engine process.",
    "这次运行已经结束": "This run has already finished",
    "还没有运行记录": "No runs yet",
    "无法立即执行": "Cannot run now",
    "请填写名称": "Enter a name",
    "请填写任务内容": "Describe the task",
    "请选择执行时间": "Pick a time",
    "请选择时刻": "Pick a time of day",
    "间隔至少 1 分钟": "The interval must be at least 1 minute",
    "每周计划至少要选一天": "Pick at least one weekday",
    "循环任务已创建": "Loop created",
    "\\n\\n…（日志超过 4MB，仅显示开头部分，完整内容见产出文件）": "\\n\\n… (the log is larger than 4 MB — only the beginning is shown; open the output file for everything)",

    /* ---------- 通知提醒 ---------- */
    "通知通道": "Notification channels",
    "手机推送": "Phone push",
    "加载中": "Loading",
    "桌面横幅": "Desktop toast",
    "桌面横幅由 a4agent 常驻进程代发（hook 事件经请求文件队列，每 2 秒扫描一次），": "Desktop toasts are sent by the resident a4agent process (hook events go through a request-file queue scanned every 2 seconds),",
    "请保持 a4agent 运行；关闭 a4agent 后手机推送仍可用，桌面横幅会静默丢弃。": "so keep a4agent running. Phone push still works after quitting a4agent; desktop toasts are silently dropped.",
    "经 ntfy 推送到手机 App，扫码订阅后生效": "Pushed to the phone app through ntfy; takes effect once you subscribe with the QR code",
    "启用通知": "Enable notifications",
    "开启后，任务完成/失败/超时等终态实时推送手机": "When on, finished, failed and timed-out tasks are pushed to your phone in real time",
    "同一事件同时弹出 Windows 系统通知；与手机推送互不影响，可单独关闭": "Also raises a Windows notification for the same event; independent of phone push and can be turned off separately",
    "ntfy 服务器": "ntfy server",
    "话题": "Topic",
    "换一个": "Rotate",
    "访问令牌": "Access token",
    "可选；自建 ntfy 服务开启鉴权时填写": "Optional; only for self-hosted ntfy with auth",
    "推送时机": "When to push",
    "发送测试通知": "Send a test notification",
    "测试桌面横幅": "Test the desktop toast",
    "手机订阅": "Phone subscription",
    "手机安装 ntfy App 后扫码或输入话题名订阅": "Install the ntfy app, then scan the code or enter the topic name",
    "1. 手机安装 ntfy App（App Store / Google Play / F-Droid）": "1. Install the ntfy app (App Store / Google Play / F-Droid)",
    "2. 用 App 扫左侧二维码订阅，或手动添加订阅输入下方话题名": "2. Scan the QR code on the left, or add a subscription manually with the topic name below",
    "3. 在该订阅的设置里开启「即时交付」，否则消息需手动刷新": "3. Turn on \"Instant delivery\" in that subscription's settings, otherwise messages need a manual refresh",
    "订阅地址：": "Subscription URL:",
    "话题名即推送凭据：请勿外传；手机丢失时点「换一个」重置，旧话题立即作废。": "The topic name is the push credential — keep it private. If the phone is lost, click \"Rotate\" to reset it; the old topic is invalidated immediately.",
    "ntfy.sh 为国外免费服务，推送不稳时可自建服务器后改「ntfy 服务器」地址。": "ntfy.sh is a free service hosted abroad; if pushes are unreliable, self-host a server and change the \"ntfy server\" address.",
    "会话交互": "Interactive sessions",
    "终端里 AI 的提问 / 权限请求推手机作答（Claude Code 等）": "Push the AI's questions and permission prompts from the terminal to your phone (Claude Code etc.)",
    "交互模式": "Interaction mode",
    "终端优先": "Terminal first",
    "外出 · 手机优先": "Away · phone first",
    "作答等待": "Response wait",
    "30 秒": "30 seconds",
    "60 秒": "60 seconds",
    "2 分钟": "2 minutes",
    "5 分钟": "5 minutes",
    "外出· 手机优先模式下，手机超过这个时间没回复，就自动切回终端继续等。": "In Away · phone-first mode, if the phone does not reply within this time, the prompt falls back to the terminal.",
    "WorkBuddy 最多只能等 45 秒，选更大的值对它无效。": "WorkBuddy waits at most 45 seconds; larger values have no effect on it.",
    "宿主": "Host",
    "配置文件": "Config file",
    "状态": "Status",
    "宿主注册只决定 AI 的提问 / 权限请求是否转推手机；": "Registering a host only decides whether the AI's questions and permission prompts are forwarded to the phone;",
    "桌面横幅由左侧「桌面横幅」开关统一控制。DSH 注册会把内置 Cordis 插件挂进": "desktop toasts are controlled by the \"Desktop toast\" switch on the left. Registering DSH mounts the built-in Cordis plugin into",
    "~/.dsh/profiles 各 profile，重启 dsh 后生效。OpenCode 注册即开启服务事件监听": "each profile under ~/.dsh/profiles and takes effect after restarting dsh. Registering OpenCode just starts listening to service events",
    "（会话终局提醒 + 外出模式权限批办），不写入它的任何文件。": "(session-end reminders and away-mode permission batching) without writing to any of its files.",
    "已设置；留空保持不变": "Set; leave empty to keep it",
    "已开启": "On",
    "待生成话题": "Topic pending",
    "已关闭": "Off",
    "已注册": "Registered",
    "未注册": "Not registered",
    "注册": "Register",
    "外出模式：提问/权限请求推手机作答": "Away mode: questions and permission prompts go to the phone",
    "终端优先：手机不参与交互": "Terminal first: the phone stays out of the loop",
    "测试通知已发出，请查看手机": "Test notification sent — check your phone",
    "未知原因": "Unknown reason",
    "测试通知": "Test notification",
    "桌面横幅已触发，请看右下角系统通知": "Desktop toast fired — look for the notification in the bottom right",
    "（非 Windows 系统、或系统通知被禁用）": "(not on Windows, or system notifications are disabled)",
    "换一个新话题？当前话题立即作废，手机需要重新扫码订阅。": "Rotate to a new topic? The current one is invalidated immediately and the phone must subscribe again.",
    "重置话题": "Reset topic",
    "已生成新话题，请重新扫码订阅": "New topic generated — scan the QR code again",

    /* ---------- 目录选择 ---------- */
    "我的电脑": "This PC",
    "子目录过多，仅列出前面部分": "Too many subfolders — only the first ones are listed",
    "没有子文件夹": "No subfolders",
    "选择此文件夹": "Choose this folder",
    "上级": "Up",
    "主目录": "Home",
    "点文件夹进入下一级；进到目标文件夹后点「选择此文件夹」": "Click a folder to go deeper; when you reach the target, click \"Choose this folder\"",
    "手动输入路径": "Enter a path manually",
    "输入文件夹完整路径（支持 \\\\服务器\\共享 这类 UNC 路径）": "Full folder path (UNC paths such as \\\\server\\share are supported)",
    "请先进入一个文件夹再确认": "Open a folder before confirming",

    /* ---------- 多段中文拼成的整句（源码里是累加字符串，运行时只有一个文本节点） ---------- */
    "把任意 .gguf 模型一键变成 OpenAI 兼容的本地 API 服务（基于 llama.cpp llama-server）。首次使用请运行配置向导：自动获取推理引擎、检测显卡、选择模型。": "Turn any .gguf model into an OpenAI-compatible local API (powered by llama.cpp llama-server). On first use, run the setup wizard: it fetches the inference engine, detects your GPU and lets you pick a model.",
    "还没有任务。填写任务描述并点「下发任务」，引擎会在后台跑完并留下产出": "No tasks yet. Describe a task and click \"Dispatch task\" — the engine runs it in the background and leaves its output here.",
    "桌面横幅由 a4agent 常驻进程代发（hook 事件经请求文件队列，每 2 秒扫描一次），请保持 a4agent 运行；关闭 a4agent 后手机推送仍可用，桌面横幅会静默丢弃。": "Desktop toasts are sent by the resident a4agent process (hook events go through a request-file queue scanned every 2 seconds), so keep a4agent running. Phone push still works after quitting a4agent; desktop toasts are silently dropped.",
    "话题名即推送凭据：请勿外传；手机丢失时点「换一个」重置，旧话题立即作废。ntfy.sh 为国外免费服务，推送不稳时可自建服务器后改「ntfy 服务器」地址。": "The topic name is the push credential — keep it private. If the phone is lost, click \"Rotate\" to reset it and the old topic is invalidated immediately. ntfy.sh is a free service hosted abroad; if pushes are unreliable, self-host a server and change the \"ntfy server\" address.",
    "外出· 手机优先模式下，手机超过这个时间没回复，就自动切回终端继续等。WorkBuddy 最多只能等 45 秒，选更大的值对它无效。": "In Away · phone-first mode, if the phone does not reply within this time the prompt falls back to the terminal. WorkBuddy waits at most 45 seconds; larger values have no effect on it.",
    "宿主注册只决定 AI 的提问 / 权限请求是否转推手机；桌面横幅由左侧「桌面横幅」开关统一控制。DSH 注册会把内置 Cordis 插件挂进 ~/.dsh/profiles 各 profile，重启 dsh 后生效。OpenCode 注册即开启服务事件监听（会话终局提醒 + 外出模式权限批办），不写入它的任何文件。": "Registering a host only decides whether the AI's questions and permission prompts are forwarded to the phone; desktop toasts are controlled by the \"Desktop toast\" switch on the left. Registering DSH mounts the built-in Cordis plugin into every profile under ~/.dsh/profiles, taking effect after restarting dsh. Registering OpenCode just starts listening to service events (session-end reminders and away-mode permission batching) without writing to any of its files.",
    "这三端应用自带完整的供应商配置界面，由你在应用内自行配置更可靠。你原先用 a4agent 配的条目已自动清理（清理前已做永久快照），你自己配的 provider 与 Key 一概未动。如需继续使用，请到各应用设置里重新配置一次。": "These three apps ship with a full provider setup screen, and configuring them inside the app is more reliable. Entries a4agent created earlier were cleaned up automatically (a permanent snapshot was taken first); providers and keys you configured yourself are untouched. To keep using them, set them up once more in each app's settings.",
    "默认范围是本机所有盘符；「自动发现」来自各 AI 工具的历史记录，可覆盖任意深度的项目。保存后只影响扫描范围，忽略名单改动即刻生效。": "The default covers every drive on this machine; \"Auto-discovery\" comes from the history of your AI tools and can cover projects at any depth. Saving only changes the scan roots — ignore-list edits apply immediately.",

    /* ---------- 零散补充 ---------- */
    "按名称、描述或所在项目文件夹搜索，空格分隔表示同时满足": "Search by name, description or project folder; separate terms with a space",
    "· Codex 待配置": "· Codex not configured",
    "如：https://api.siliconflow.cn/v1": "e.g. https://api.siliconflow.cn/v1",
    "如：glm-4.7-flash": "e.g. glm-4.7-flash",
    "当前": "Current",
    "→ 最新": "→ latest",
    "http(s)://host:port/mcp 或 …/sse": "http(s)://host:port/mcp or …/sse",
    "直连": "Direct",
    "代理": "Proxy",
    "，日志建议先自查。也可直接联系：QQ 3886370035 ｜ 微信 eogee2022。": " — skim the log first. You can also reach us directly: QQ 3886370035 | WeChat eogee2022.",

    /* ---------- 设备页（CrossDevice 集成） ---------- */
    "本机被控端（node）": "Local node (controlled side)",
    "启动": "Start",
    "运行时设置": "Runtime settings",
    "已配对设备": "Paired devices",
    "发现的设备（同一局域网，尚未配对）": "Discovered devices (same LAN, not paired)",
    "手动添加 IP": "Add IP manually",
    "配对说明：在对方设备的「设备」页确认请求后，这里输入它展示的 6 位配对码。": "Pairing: after the request is confirmed on the other device's \"Devices\" page, enter the 6-digit pairing code it shows here.",
    "发现列表只提供连接线索，未经确认不会建立连接。": "The discovery list only offers connection hints; nothing connects without confirmation.",
    "端口未就绪": "Port not ready",
    "证书就绪": "Certificate ready",
    "证书缺失": "Certificate missing",
    "node.py：": "node.py:",
    "未找到，请在运行时设置中指定": "Not found — set the path in runtime settings",
    "发现广播监听：": "Discovery broadcast listener:",
    "在线": "Online",
    "离线": "Offline",
    "主机名": "Hostname",
    "地址": "Address",
    "版本": "Version",
    "设备 ID": "Device ID",
    "最近出现": "Last seen",
    "发起配对": "Pair…",
    "移除": "Remove",
    "暂未发现设备。确认对方已安装并启动，": "No devices found yet. Make sure the other device is installed and running,",
    "或点「手动添加 IP」": "or click \"Add IP manually\"",
    "还没有已配对设备。在下方发现列表点「发起配对」": "No paired devices yet. Click \"Pair…\" in the discovery list below",
    "，对方确认后输入配对码即可建立连接": "and enter the pairing code after the other side confirms to connect",
    "命令": "Run command",
    "截屏": "Screenshot",
    "上传": "Upload",
    "任务历史": "Task history",
    "探活": "Ping",
    "忘记": "Forget",
    "设备在线": "Device is online",
    "已忘记": "Forgotten",
    "停止本机被控端？其他设备将无法连接本机。": "Stop the local node? Other devices will no longer be able to connect to this machine.",
    "停止 node": "Stop node",
    "启动指令已下发": "Start command sent",
    "已停止": "Stopped",
    "node.py 路径": "node.py path",
    "留空自动探测（安装目录 node_runtime/node.py）": "Leave empty to auto-detect (node_runtime/node.py in the install folder)",
    "Python 解释器": "Python interpreter",
    "留空自动探测": "Leave empty to auto-detect",
    "gen_cert.ps1 路径": "gen_cert.ps1 path",
    "留空随 node.py 自动探测": "Leave empty to auto-detect alongside node.py",
    "附加参数": "Extra arguments",
    "空格分隔，如：--port 51888": "Space separated, e.g. --port 51888",
    "应用启动时自动拉起本机被控端": "Start the local node automatically when the app launches",
    "IP 地址": "IP address",
    "端口": "Port",
    "添加": "Add",
    "广播不可达（跨网段）时手动填地址": "For manual entry when broadcast cannot reach (across subnets)",
    "如 192.168.3.20": "e.g. 192.168.3.20",
    "请在对方设备的「设备」页确认本次配对，": "Confirm this pairing on the other device's \"Devices\" page,",
    "然后输入它展示的 6 位配对码。": "then enter the 6-digit pairing code it shows.",
    "请求有效期至 ": "Request valid until ",
    "配对码": "Pairing code",
    "6 位数字": "6 digits",
    "确认配对": "Confirm pairing",
    "请输入 6 位数字配对码": "Enter the 6-digit pairing code",
    "发起配对失败": "Failed to start pairing",
    "配对请求": "Pairing request",
    "来自 ": "From ",
    " 的设备请求与本机配对。": " a device requests to pair with this machine.",
    "确认前请核实这是你自己的设备。确认后将展示一次性配对码，有效期 5 分钟。": "Verify it is really your own device before confirming. After confirmation a one-time pairing code (valid for 5 minutes) is shown.",
    "拒绝": "Reject",
    "已拒绝，本次配对未建立": "Rejected — this pairing was not established",
    "请在控制端输入此码。有效期剩余 ": "Enter this code on the controlling side. Valid for another ",
    " 秒": " sec",
    "倒计时结束或对方完成配对后，此码自动失效。": "The code expires automatically when the countdown ends or the pairing completes.",
    "配对码已过期，如需配对请重新发起": "Pairing code expired — start pairing again if needed",
    /* ---------- 设备页：安装控制技能（B35） ---------- */
    "安装控制技能": "Install control skill",
    "把 CrossDevice 控制技能安装到 Agent 技能目录，": "Install the CrossDevice control skill into the agent skill folders so that",
    "工作空间内的 Agent 即可发现并使用已配对设备的能力。": "agents in your workspaces can discover and use paired-device capabilities.",
    "安装范围": "Install scope",
    "全局技能库（八端通用）": "Global skill folders (all eight tools)",
    "项目目录": "Project folder",
    "自定义目录": "Custom folder",
    "选择项目": "Pick a project",
    "目录路径": "Folder path",
    "确认安装": "Install",
    "正在安装…": "Installing…",
    "尚无已注册项目，请改选全局或自定义目录": "No registered projects yet — choose Global or Custom instead",
    "请填写目录路径": "Enter the folder path",
    "安装失败": "Install failed",
    "（尚无已注册项目，先在技能管理页注册）": "(no registered projects — register one on the Skills page first)",
    "（项目列表加载失败）": "(failed to load project list)",
    "none（直接执行）": "none (run directly)",
    "超时默认 60 秒": "Timeout defaults to 60 seconds",
    "执行": "Run",
    "（等待执行）": "(waiting to run)",
    "执行中…": "Running…",
    "未返回任务 ID": "No job ID returned",
    "查询任务失败": "Failed to fetch the job",
    "显示器": "Monitor",
    "全部（虚拟桌面）": "All (virtual desktop)",
    "显示器 1": "Monitor 1",
    "显示器 2": "Monitor 2",
    "保存到本机": "Save locally",
    "正在保存…": "Saving…",
    "正在截取…": "Capturing…",
    "截屏失败：设备不可达或没有桌面会话": "Screenshot failed: device unreachable or no desktop session",
    "选择截屏保存目录": "Choose a folder for the screenshot",
    "截屏将保存为 PNG 文件到所选文件夹": "The screenshot will be saved as a PNG file into the chosen folder",
    "本地文件": "Local file",
    "远端路径": "Remote path",
    "目标完整路径，如 C:\\Users\\pub\\desk\\log.txt": "Full destination path, e.g. C:\\Users\\pub\\desk\\log.txt",
    "上传中…": "Uploading…",
    "请先选择文件": "Choose a file first",
    "请填写远端目标路径": "Enter the remote destination path",
    "保存目录": "Save folder",
    "选择文件夹…": "Choose folder…",
    "下载中…": "Downloading…",
    "请填写远端文件路径": "Enter the remote file path",
    "请先选择保存目录": "Choose a save folder first",
    "选择下载保存目录": "Choose a folder for the download",
    "文件将保存到所选文件夹（同名文件覆盖）": "The file will be saved into the chosen folder (same-name files are overwritten)",
    "该设备还没有任务记录": "No task records on this device yet",
    "开始时间": "Started at",
    "目录选择组件未加载": "Folder picker component not loaded",
    "指纹已重置，请重试操作": "Fingerprint reset — retry the operation",
    "证书指纹变更": "Certificate fingerprint changed",
    "忘记设备": "Forget device",
    "配对失败": "Pairing failed",
    "请输入命令": "Enter a command",
    "执行失败": "Execution failed",
    "（无输出）": "(no output)",
    "（无内容）": "(nothing)",

    /* ---------- 设备页：配对系统 toast（B39；backend 直发的中文文案） ---------- */
    "a4agent 设备配对": "a4agent device pairing",
    "有设备请求配对，请在设备页确认": "A device is requesting pairing — confirm it on the Devices page",
    "配对请求已确认，请在设备页查看配对码": "Pairing request confirmed — see the pairing code on the Devices page",

    /* ---------- 设备页：具名实例（B40 多实例） ---------- */
    "添加实例": "Add instance",
    "实例名": "Instance name",
    "字母/数字/连字符/下划线，最长 32 位": "Letters, digits, hyphen or underscore, up to 32 characters",
    "每个实例是独立的被控端设备，端口自动顺延": "Each instance is an independent controlled device; ports increment automatically",
    "请填写实例名": "Enter an instance name",
    "实例名限字母、数字、连字符或下划线（最长 32 位）": "Instance name allows letters, digits, hyphen or underscore (up to 32 characters)",
    "实例已存在": "Instance already exists",
    "移除实例": "Remove instance",
    "停止实例": "Stop instance",
    "已移除": "Removed",
  };

  /* ---------------- 句式规则：中文模板 => English 模板（{n} 为通配） ---------------- */
  var PATTERNS = [
    /* 通用错误 / 提示 */
    ["请求失败（HTTP {0}）", "Request failed (HTTP {0})"],
    ["响应不是有效 JSON（HTTP {0}）", "Response was not valid JSON (HTTP {0})"],
    ["加载失败：{0}", "Failed to load: @{0}"],
    ["保存失败：{0}", "Save failed: @{0}"],
    ["删除失败：{0}", "Delete failed: @{0}"],
    ["读取失败：{0}", "Read failed: @{0}"],
    ["检查更新失败：{0}", "Update check failed: @{0}"],
    ["开始下载失败：{0}", "Could not start the download: @{0}"],
    ["下载失败：{0}", "Download failed: @{0}"],
    ["启动更新失败：{0}", "Could not start the update: @{0}"],
    ["引擎探测失败：{0}", "Engine probe failed: @{0}"],
    ["硬件检测失败：{0}", "Hardware detection failed: @{0}"],
    ["模型库加载失败：{0}", "Failed to load the model library: @{0}"],
    ["推送失败：{0}", "Push failed: @{0}"],
    ["桌面横幅未发出：{0}", "The desktop toast was not raised: @{0}"],
    ["重扫失败：{0}", "Rescan failed: @{0}"],

    /* 列表搜索 / 范围 */
    ["项目视图中有 {0} 个匹配，分布在 {1}，", "{0} matches in Project view, across {1},"],
    ["没有匹配「{0}」的{1}", "No @{1} matching \"{0}\""],
    ["项目「{0}」", "Project \"{0}\""],
    ["{0} 端存在", "In {0} targets"],
    ["OpenCode 已可见（经 {0}），无需重复迁移", "Already visible to OpenCode (via @{0}) — no migration needed"],

    /* 供应商 / 配置 */
    ["确定删除供应商「{0}」？{1}", "Delete provider \"{0}\"? @{1}"],
    ["确定删除「{0}」？", "Delete \"{0}\"?"],
    ["确定切换到「{0}」？", "Switch to \"{0}\"?"],
    ["{0} 的配置不再由本工具写入，请在该应用内自行配置", "{0} is no longer written by a4agent — configure it inside that app"],
    ["切换成功，{0}", "Switched — @{0}"],
    ["⚠ 配置文件内为 {0}", "⚠ The config file holds {0}"],

    /* 更新流程 */
    ["已是最新版本 v{0}", "You're on the latest version, v{0}"],
    ["已忽略该版本（v{0}）", "Version v{0} skipped"],
    ["破坏性变更：{0}", "Breaking change: {0}"],
    ["发现新版本 v{0}", "New version v{0}"],
    ["正在下载更新 v{0}", "Downloading update v{0}"],
    ["已下载 {0} / {1}", "Downloaded {0} / {1}"],
    ["已下载 {0}", "Downloaded {0}"],
    ["v{0} 安装包已下载并通过 SHA256 校验。", "Installer v{0} downloaded and verified with SHA256."],

    /* 技能 / MCP 迁移 */
    ["迁移「{0}」（复制，保留原件）", "Migrate \"{0}\" (copy, the original stays)"],
    ["迁移「{0}」（源端保留）", "Migrate \"{0}\" (source kept)"],
    ["一键适配全部端 · {0}（源端保留）", "Adapt to all targets · {0} (source kept)"],
    ["一键适配全部端 · {0}", "Adapt to all targets · {0}"],
    ["该项目的 skill 已在 {0} 齐全（其中 {1} 个 OpenCode 已可见，无需重复适配）", "This project's skills already cover {0} ({1} of them visible to OpenCode, no re-adapting needed)"],
    ["该项目的 skill 已在 {0} 齐全", "This project's skills already cover {0}"],
    ["（其中 {0} 个 OpenCode 已可见，无需重复适配）", " ({0} of them already visible to OpenCode)"],
    ["适配完成：已迁移 {0} 处", "Adapted: {0} items migrated"],
    ["已自动清理 {0} 条超过 30 天的过期条目", "Auto-cleaned {0} expired items older than 30 days"],
    ["{0} 天", "{0} days"],
    ["准备中…（0/{0}）", "Preparing… (0/{0})"],
    ["预览 · {0}", "Preview · {0}"],
    ["功能介绍 · {0}", "Notes · {0}"],
    ["读了 {0} 条历史路径，其中 {1} 个含 skill · 已忽略 {2} 个", "Read {0} history paths, {1} with skills · {2} ignored"],
    ["读了 {0} 条历史路径，其中 {1} 个含 skill", "Read {0} history paths, {1} with skills"],
    ["· 已忽略 {0} 个", "· {0} ignored"],
    ["失败：{0}", "Failed: @{0}"],
    ["成功安装 {0} 个", "Installed {0}"],
    ["失败 {0} 个：{1}", "{0} failed: @{1}"],
    ["已安装到 {0} 全局配置", "Installed into the global configuration of {0}"],
    ["迁移 MCP「{0}」（复制，保留原件）", "Migrate MCP \"{0}\" (copy, the original stays)"],
    ["迁移 MCP「{0}」（源端保留）", "Migrate MCP \"{0}\" (source kept)"],
    ["迁移 MCP「{0}」", "Migrate MCP \"{0}\""],

    /* 任务下发 */
    ["任务 #{0} 已入队", "Task #{0} queued"],
    ["任务 #{0}：{1}", "Task #{0}: @{1}"],
    ["任务 #{0} 产出", "Output of task #{0}"],
    ["任务 #{0}", "Task #{0}"],
    ["确定取消任务 #{0}？会终止引擎进程。", "Cancel task #{0}? This terminates the engine process."],
    ["删除任务 #{0} 及其产出文件？", "Delete task #{0} and its output files?"],
    ["当前方案：{0} · {1}", "Active configuration: {0} · {1}"],
    ["耗时 {0}", "Took {0}"],
    ["创建 {0}", "Created {0}"],
    ["目录 {0}", "Folder {0}"],
    ["模型 {0}", "Model {0}"],
    ["退出码 {0}", "Exit code {0}"],
    /* 时间单位用 sec/min 而非 s/m：反向（EN→中文）时 "…s" 这种一字符模板
       会把任何以 s 结尾的英文误判成时长，锚点必须够具体 */
    ["{0} 分 {1} 秒", "{0} min {1} sec"],
    ["{0} 秒", "{0} sec"],
    ["技能 {0} 个：{1}", "Skills ({0}): {1}"],
    ["技能列表加载失败：{0}", "Failed to load the skill list: @{0}"],
    ["正在编辑计划 #{0} · {1}", "Editing plan #{0} · {1}"],

    /* 循环管理 */
    ["{0} 执行一次", "Once at {0}"],
    ["每 {0} 分钟", "Every {0} minutes"],
    ["每 {0} 小时", "Every {0} hours"],
    ["每 {0} 天", "Every {0} days"],
    ["每天 {0}", "Every day at {0}"],
    ["调度中 · 每 {0} 秒扫描", "Scheduling · scanning every {0} sec"],
    ["超时 {0} 分钟", "Timeout {0} min"],
    ["最近 {0} · {1}", "Last @{0} · {1}"],
    ["· 下次运行", "· Next run"],
    ["{0} 小时 {1} 分后", "{0} hr {1} min from now"],
    ["{0} 小时后", "{0} hr from now"],
    ["{0} 分后", "{0} min from now"],
    ["{0} 秒后", "{0} sec from now"],
    ["循环 #{0} 已启用", "Loop #{0} enabled"],
    ["循环 #{0} 已停用", "Loop #{0} paused"],
    ["循环 #{0} 已立即执行（任务 #{1}）", "Loop #{0} started now (task #{1})"],
    ["循环 #{0}（{1}）：{2}", "Loop #{0} ({1}): @{2}"],
    ["删除循环「{0}」及其全部运行记录？", "Delete loop \"{0}\" and all of its run records?"],
    ["已删除，含 {0} 条运行记录", "Deleted, including {0} run records"],
    ["运行历史 · {0}", "Run history · {0}"],
    ["共 {0} 条", "{0} in total"],
    ["（显示最近 {0} 条）", " (showing the latest {0})"],
    ["开始 {0}", "Started {0}"],
    ["结束 {0}", "Finished {0}"],
    ["{0} · 运行 #{1}", "{0} · run #{1}"],

    /* 反馈 */
    ["仍有 {0} 个任务在跑，退出会终止它们。确定退出 a4agent？", "{0} tasks are still running and quitting will stop them. Quit a4agent?"],
    ["{0}：最多 {1} 张", "{0}: at most {1} images"],
    ["{0}：不是图片", "{0}: not an image"],
    ["{0}：超过 1MB", "{0}: larger than 1MB"],
    ["移除 {0}", "Remove {0}"],
    ["附带最近 120 行日志（{0}）", "Includes the last 120 log lines ({0})"],
    ["已复制：{0}", "Copied: {0}"],

    /* 本地模型 */
    ["端口 {0} · {1}", "Port {0} · @{1}"],
    ["引擎 {0}", "Engine {0}"],
    ["驱动 ≥ {0}", "driver ≥ {0}"],
    ["自动（跟随显卡推荐，当前 {0}）", "Auto (follows the GPU recommendation, currently {0})"],
    ["自动（跟随显卡推荐）", "Auto (follows the GPU recommendation)"],
    ["下载 {0} 引擎包（约 {1}）", "Download the {0} engine package (approx. {1})"],
    ["（钉定版本 {0}，取自 github.com/ggml-org/llama.cpp 官方 Release），选择后自动下载安装；也可以离线安装或暂时跳过。", " (pinned to {0}, taken from official github.com/ggml-org/llama.cpp releases). Pick one and it downloads and installs automatically; you can also install offline or skip for now."],
    ["下载失败：{0}（可改用离线安装，或检查网络后重试）", "Download failed: @{0} (try an offline install, or check the network and retry)"],
    ["（可改用离线安装，或检查网络后重试）", "(try an offline install, or check the network and retry)"],
    ["步骤 {0}/{1} · {2}", "Step {0}/{1} · @{2}"],
    ["— 约 {0}", "— approx. {0}"],
    ["装到 {0}", "into {0}"],
    ["— 显存 {0} GB · 驱动 {1}", "— {0} GB VRAM · driver {1}"],
    ["— 显存 {0} GB", "— {0} GB VRAM"],
    ["已找到 {0} 个可用模型（灰色行解析失败，将被忽略）。", "Found {0} usable models (rows in grey failed to parse and are ignored)."],
    ["服务端口：{0}", "Service port: {0}"],
    ["默认模型：{0}", "Default model: {0}"],
    ["推理预设：{0}", "Inference preset: {0}"],
    ["—— 上下文 {0}", "— context {0}"],
    ["tokens · KV 缓存 {0}", "tokens · KV cache {0}"],
    ["文件：{0}", "File: {0}"],
    ["引擎包（约 {0}）", "engine package (approx. {0})"],
    ["已下载 {0}%（{1} / {2}）", "{0}% downloaded ({1} / {2})"],
    ["正在下载… {0}", "Downloading… {0}"],
    ["设置已保存，引擎切换将在重启服务后生效", "Settings saved; the engine switch takes effect after restarting the service"],
    ["{0}（原生Responses）", "{0} (native Responses)"],
    ["当前版本 {0}", "Current version {0}"],
    ["扫描 {0} · {1}", "Scanning {0} · {1}"],
    ["{0} — 显存 {1} GB · 驱动 {2}", "{0} — {1} GB VRAM · driver {2}"],
    ["{0} — 显存 {1} GB", "{0} — {1} GB VRAM"],
    ["预设档位：{0}", "Preset: {0}"],
    ["上下文 {0} tokens · KV 缓存 {1}", "Context {0} tokens · KV cache {1}"],
    ["{0}（OpenAI）", "{0} (OpenAI)"],

    /* 设备页（CrossDevice 集成） */
    ["已开启（UDP {0}）", "On (UDP {0})"],
    ["未开启：{0}", "Off: @{0}"],
    ["配对于 {0}", "Paired @{0}"],
    ["已安装到 {0} 个技能目录：", "Installed into {0} skill folder(s):"],
    ["已跳过 {0} 个（同名已存在，未覆盖）：", "Skipped {0} (a same-name skill already exists, not overwritten):"],
    ["没有可安装的目标目录。", "There were no target skill folders to install into."],
    ["设备在线（node v{0}）", "Device is online (node v{0})"],
    ["忘记设备「{0}」？将删除本机保存的配对凭据，再次连接需重新配对。", "Forget device \"{0}\"? The pairing credentials stored on this machine are removed; pairing again is required to reconnect."],
    ["配对 {0}", "Pairing {0}"],
    ["请求有效期至 {0}", "Request valid until {0}"],
    ["配对成功：{0}", "Paired: @{0}"],
    ["命令 - {0}", "Command - {0}"],
    ["已分离启动（PID {0}），不回收输出", "Started detached (PID {0}); no output is collected"],
    ["截屏 - {0}", "Screenshot - {0}"],
    ["已保存：{0}", "Saved: {0}"],
    ["上传文件 - {0}", "Upload file - {0}"],
    ["已上传：{0}（{1} 字节）", "Uploaded: {0} ({1} bytes)"],
    ["下载文件 - {0}", "Download file - {0}"],
    ["已下载：{0}（{1} 字节）", "Downloaded: {0} ({1} bytes)"],
    ["任务历史 - {0}", "Task history - {0}"],
    ["任务 {0}", "Task {0}"],
    ["显示最近 {0} 条（共 {1} 条）", "Showing the latest {0} of {1} records"],

    /* 设备页：具名实例（B40） */
    ["移除实例「{0}」？进程将被停止并从清单移除。", "Remove instance \"{0}\"? Its process will be stopped and it is removed from the list."],
    ["停止实例「{0}」？依赖它的设备将无法连接本机。", "Stop instance \"{0}\"? Devices relying on it will not be able to connect to this machine."],
  ];

  /* 迁移结果提示是「基数 + 若干可选后缀」拼出来的整句，
     这里按后缀组合批量生成模板，避免手写 8 种组合。 */
  (function () {
    var tails = [
      ['，跳过 {N}', ', {N} skipped'],
      ['，旧版入回收站 {N}', ', {N} older copies trashed'],
      ['，失败 {N}', ', {N} failed']
    ];
    for (var mask = 0; mask < 8; mask++) {
      var zh = '已迁移 {0} 处';
      var en = 'Migrated {0} items';
      var idx = 1;
      for (var i = 0; i < tails.length; i++) {
        if (mask & (1 << i)) {
          zh += tails[i][0].replace('{N}', '{' + idx + '}');
          en += tails[i][1].replace('{N}', '{' + idx + '}');
          idx++;
        }
      }
      PATTERNS.push([zh, en]);
    }
  })();

  /* ---------------- 编译 ---------------- */
  var zh2en = Object.create(null);
  var en2zh = Object.create(null);
  Object.keys(DICT).forEach(function (zh) {
    var en = DICT[zh];
    if (!en || en === zh) return;
    if (!zh2en[zh]) zh2en[zh] = en;
    if (!en2zh[en]) en2zh[en] = zh;
  });

  var escapeRe = /[.*+?^${}()|[\]\\]/g;

  function compile(tpl) {
    var parts = String(tpl).split(/\{(\d+)\}/);
    var body = parts.map(function (seg, i) {
      return (i % 2) ? '(.+?)' : seg.replace(escapeRe, '\\$&');
    }).join('');
    return new RegExp('^' + body + '$');
  }

  var rules = PATTERNS.map(function (pair) {
    return { zh: pair[0], en: pair[1], re: compile(pair[0]), reBack: compile(pair[1]) };
  });

  function fill(tpl, m) {
    // @{n} 表示「把捕获到的中文再翻一次」，用于 标签/枚举值 嵌在整句里的情况。
    // 必须先于 {n} 处理，否则 @{0} 里的占位符会先被吃掉，只留下一个孤零零的 @。
    return tpl.replace(/@\{(\d+)\}/g, function (whole, n) {
      return translate(String(m[+n + 1] || '').trim());
    }).replace(/\{(\d+)\}/g, function (whole, n) { return m[+n + 1] || ''; });
  }

  /* 英文成句后的收尾：纯拉丁的「」书名号换成双引号，顿号换成逗号 */
  function polishEn(s) {
    if (s.indexOf('「') === -1 && s.indexOf('、') === -1) return s;
    var out = s.replace(/「([^「」]*?)」/g, function (whole, inner) {
      return CJK.test(inner) ? whole : '"' + inner + '"';
    }).replace(/([^、])、([^、])/g, '$1, $2');
    if (out.indexOf('、') !== -1) out = out.replace(/、/g, ', ');
    return out;
  }

  function translate(text) {
    var m = text.match(/^(\s*)([\s\S]*?)(\s*)$/);
    var lead = m[1], core = m[2], tail = m[3];
    if (!core) return text;

    var out = null;
    if (lang === 'en') {
      if (!CJK.test(core)) return text;
      if (zh2en[core]) out = zh2en[core];
      if (out == null) {
        for (var i = 0; i < rules.length; i++) {
          var hit = core.match(rules[i].re);
          if (hit) { out = fill(rules[i].en, hit); break; }
        }
      }
    } else {
      if (CJK.test(core) || !LATIN.test(core)) return text;
      if (en2zh[core]) out = en2zh[core];
      if (out == null) {
        for (var j = 0; j < rules.length; j++) {
          var back = core.match(rules[j].reBack);
          if (back) { out = fill(rules[j].zh, back); break; }
        }
      }
    }
    return out == null ? text : lead + (lang === 'en' ? polishEn(out) : out) + tail;
  }

  /* ---------------- DOM 应用 ---------------- */
  function translateTextNode(node) {
    var v = node.nodeValue;
    if (!v) return;
    var next = translate(v);
    if (next !== v) node.nodeValue = next;
  }

  /* ---------------- 英文态图标按钮 ----------------
   * 卡片操作行（迁移 / 介绍 / 预览）在中文里是 2 个字，英文却要
   * Migrate… / Notes / Preview 三个词，248px 宽的卡片放不下会溢出。
   * 英文下改成图标 + 悬停 tooltip；文字仍留在 DOM 里（仅视觉隐藏），
   * 因此切回中文时只需去掉类名即可复原，不需要重新渲染。
   */
  var ICON_TARGETS = [
    { sel: '[data-sk="migrate"], [data-mc="migrate"]', icon: 'migrate' },
    { sel: '[data-mc="intro"]', icon: 'notes' },
    { sel: '[data-sk="preview"], [data-mc="preview"]', icon: 'preview' }
  ];

  function applyIconMode() {
    var on = lang === 'en';
    ICON_TARGETS.forEach(function (t) {
      var els = document.querySelectorAll(t.sel);
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var cls = 'i18n-icon-' + t.icon;
        if (!on) {
          el.classList.remove('i18n-icon-only', cls);
          el.removeAttribute('title');
          el.removeAttribute('aria-label');
          continue;
        }
        el.classList.add('i18n-icon-only', cls);
        // 用已翻译的文案做 tooltip（去掉「迁移…」这类省略号）
        var label = (el.textContent || '').replace(/[\s.…]+$/, '').trim();
        if (label) {
          el.setAttribute('title', label);
          el.setAttribute('aria-label', label);
        }
      }
    });
  }

  function translateAttrs(el) {
    if (el.closest && el.closest('[data-i18n-skip]')) return;
    // layui 的下拉框把当前选中的文案镜像到只读 input 的 value 上，这里一并翻译
    if (el.tagName === 'INPUT' && el.classList.contains('layui-unselect')) {
      var v = el.value;
      if (v) {
        var nv = translate(v);
        if (nv !== v) el.value = nv;
      }
    }
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      var v = el.getAttribute(a);
      if (!v) continue;
      var next = translate(v);
      if (next !== v) el.setAttribute(a, next);
    }
  }

  function apply(root) {
    root = root || document.body;
    if (!root) return;
    if (root.nodeType === 3) { translateTextNode(root); return; }

    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (n) {
        if (n.nodeType === 1) {
          if (SKIP_TAGS[n.tagName]) return NodeFilter.FILTER_REJECT;
          if (n.hasAttribute && n.hasAttribute('data-i18n-skip')) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_SKIP;
        }
        var p = n.parentNode;
        if (p && p.tagName === 'svg') return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = walker.nextNode())) translateTextNode(n);

    var els = root.querySelectorAll ? root.querySelectorAll('[title],[placeholder],[aria-label],[alt],input.layui-unselect') : [];
    for (var i = 0; i < els.length; i++) translateAttrs(els[i]);
    if (root.nodeType === 1) translateAttrs(root);
    applyIconMode();
  }

  /* ---------------- 语言状态 ---------------- */
  var lang = (function detect() {
    var saved = null;
    try { saved = global.localStorage && localStorage.getItem(LANG_KEY); } catch (e) {}
    if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;
    var nav = String((navigator.language || navigator.userLanguage || 'zh')).toLowerCase();
    return nav.indexOf('zh') === 0 ? 'zh' : 'en';
  })();

  function syncChrome() {
    document.documentElement.setAttribute('lang', lang === 'en' ? 'en' : 'zh-CN');
    var seg = document.getElementById('lang-seg');
    if (seg) {
      var btns = seg.querySelectorAll('[data-lang]');
      for (var i = 0; i < btns.length; i++) {
        btns[i].classList.toggle('seg-active', btns[i].getAttribute('data-lang') === lang);
      }
    }
  }

  function applyAll() {
    apply(document.body);
    syncChrome();
    document.documentElement.classList.remove('i18n-pending');
  }

  var queued = false;
  function schedule() {
    if (queued) return;
    queued = true;
    (global.requestAnimationFrame || setTimeout)(function () {
      queued = false;
      apply(document.body);
    });
  }

  function setLang(next) {
    if (SUPPORTED.indexOf(next) === -1 || next === lang) return;
    lang = next;
    try { localStorage.setItem(LANG_KEY, next); } catch (e) {}
    applyAll();
    if (typeof onChange === 'function') onChange(lang);
  }

  var onChange = null;

  /* ---------------- 启动 ---------------- */
  if (lang === 'en') document.documentElement.classList.add('i18n-pending');

  function boot() {
    applyAll();
    document.addEventListener('click', function (e) {
      var btn = e.target && e.target.closest ? e.target.closest('#lang-seg [data-lang]') : null;
      if (!btn) return;
      e.preventDefault();
      setLang(btn.getAttribute('data-lang'));
    });
    if (global.MutationObserver) {
      new MutationObserver(schedule).observe(document.body, {
        childList: true, subtree: true, characterData: true
      });
    }
    // 兜底：JSON 渲染异常时也不要让页面一直隐藏
    setTimeout(function () { document.documentElement.classList.remove('i18n-pending'); }, 1500);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();

  /* ---------------- 对外接口 ---------------- */
  global.i18n = {
    get lang() { return lang; },
    t: function (zh) { return lang === 'en' ? translate(zh) : zh; },
    setLang: setLang,
    apply: applyAll,
    onChange: function (fn) { onChange = fn; },
    /* 自查：返回当前仍含中文的文本节点，用于人工校对漏译 */
    audit: function () {
      var out = [];
      var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
      var n;
      while ((n = walker.nextNode())) {
        var t = (n.nodeValue || '').trim();
        if (!t || !CJK.test(t)) continue;
        var p = n.parentNode;
        if (SKIP_TAGS[p.tagName]) continue;
        if (p.closest && p.closest('[data-i18n-skip]')) continue;
        out.push(t);
      }
      return out;
    }
  };
})(window);
