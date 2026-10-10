# a4agent

English | [简体中文](README.zh-CN.md)

**A management console for seven AI coding tools + a local LLM inference console**: unified **skill management** and **MCP management** across all seven tools — **Claude Code, Codex, dsh (DeepSeek Harness), ZCode (Zhipu Agentic Dev Environment), pi (local pi coding agent), Qoder (AI IDE), and OpenCode** — plus API provider switching for the two CLI tools without a graphical config UI (Claude Code, Codex), and a built-in llama.cpp local model inference console. Everything is done through a visual interface; no manual config-file editing required.

| Capability | Description |
|---|---|
| **Skill Management** | Auto-discovery of global/project-level skills on all seven tools, aggregation badges, cross-tool migration, recycle-bin restore, one-click sync of a project's skills to every missing tool |
| **MCP Management** | Auto-discovery and **one-click install** of MCP servers on all seven tools, cross-tool migration (validated by a transport capability matrix), snapshot recycle bin, automatic/custom **descriptions**; secrets are masked/encrypted throughout |
| **API Switching** | One-click provider / model / API-key switching for the two CLI tools without a graphical UI (Claude Code, Codex), with automatic backup, atomic writes, and DPAPI-encrypted key storage; the other four tools have their own provider UIs and are configured by the user in-app |
| **Local Models** | Turn any `.gguf` model into an OpenAI-compatible local/LAN API service in one click: engine auto-download by GPU, model library scanning, VRAM risk assessment, one-click profile creation for Claude Code / Codex |
| **Task Dispatch** | Run any of the seven tools in **headless mode** as background tasks: dispatch-and-return, automatic pre-flight checks (engine ready / config ready / real connectivity), queue polling, output archiving, cancel & timeout, and tasks survive closing the window. Uses the config you already set up in each app |
| **Notifications** | Two independent channels so you are not tied to the desk: **phone push** (ntfy) for task completion/failure/timeout with the agent's last output, and **desktop toasts** that wake the window on click. Session hooks for **Claude Code / Codex / ZCode / Qoder / WorkBuddy / dsh** let you answer the agent's questions and approve permissions from your phone |

---

## OpenCode Integration (since v0.6.0)

Beyond managing OpenCode's skills and MCP like any other tool, a4agent **drives the OpenCode instance you already have running**. It works out of the box on this machine — a4agent reads that service's password and default port (`49374`) on its own. Enable it on the Notifications page and you get:

- **OpenCode as a dispatch engine**: tasks run as sessions against the live service instead of spawning a fresh CLI process each time. Approval-free execution uses **session-scoped permission rules**, so your global config is left untouched; cost, usage, and outcome come back structured; cancelling sends an interrupt rather than killing a process.
- **Notifications for sessions you start in OpenCode's web UI**: when those finish, fail, or get interrupted, they reach you through the same desktop toast and phone push.

For a remote OpenCode server, fill in the address and password on the same card (the password is stored with Windows DPAPI encryption and is never echoed back in the UI).

---

## Skill Management

All seven targets use the same skill format (a `<skill-name>/SKILL.md` directory with `name` and `description` frontmatter), so a4agent can manage them as one kind of resource.

### Discovery & Aggregation

- Scans the **global** and **project-level** skill directories of all seven tools, and aggregates by frontmatter `name`: when a skill with the same name exists on several tools, it is badged as "already on N tools".
- The project root list is configurable (default scan root: `C:\ProgramMine`); only projects containing at least one skill are listed.
- Codex's reserved global directories (`.system/` and other dot-prefixed dirs) are skipped automatically and not treated as user skills.

### Cross-Tool Migration & "One-Click Sync to Every Tool"

- Any skill on any tool can be migrated to any target (global or any project). Migration is a **non-destructive copy**: the source is always kept.
- Migration targets support **picking any project folder**: the target project needs no prior skills, and a missing `.{tool}/skills` directory is created automatically; the desktop app uses the native folder picker, the browser version falls back to a path input.
- If the target already has a skill with the same name (matching frontmatter `name` or directory name), the old version goes to the recycle bin instead of being silently overwritten.
- **"One-click sync to every tool"**: fills in every missing tool for all skills in a project at once — first shows a plan list, then runs with a progress bar while the page is temporarily locked to prevent accidental edits.
- Every migration writes a **migration log** (time, skill, source, target, result) for full traceability.

### Recycle Bin

- Deleted skills go to the recycle bin and can be restored in place or permanently deleted within **30 days**; restoring into an occupied location fails with a clear error — never overwrites.
- Expired entries are lazily cleaned the next time you open the recycle bin, and the result reports how many were purged.

### Skill Directories per Tool

| Tool | Global (user-level) | Project-level |
|---|---|---|
| Claude Code | `~/.claude/skills/` | `<project>/.claude/skills/` |
| Codex | `~/.codex/skills/` | `<project>/.codex/skills/` |
| dsh | `~/.dsh/skills/` (overridable via `$DSH_HOME`) | `<project>/.dsh/skills/` |
| ZCode | `~/.zcode/skills/` | `<project>/.zcode/skills/` |
| pi | `~/.pi/agent/skills/` (overridable via `$PI_CODING_AGENT_DIR`) | `<project>/.pi/skills/` |
| Qoder | `~/.qoder/skills/` (overridable via `$A4AGENT_QODER_HOME`) | `<project>/.qoder/skills/` |
| OpenCode | `~/.config/opencode/skills/` | `<project>/.opencode/skills/` |

**ZCode**: the official client also recognizes the cross-tool directories `~/.agents/skills` and `<project>/.agents/skills`; a4agent standardizes on the `.zcode` prefix for consistency with the other tools. Skills migrated to ZCode are really loaded by the ZCode client.

**pi**: global skills live under the **agent subdirectory** (`~/.pi/agent/skills`, not `~/.pi/skills`). pi validates skills more strictly than the other tools: `name` must be lowercase letters, digits, and hyphens (≤64 chars, no leading/trailing hyphen or consecutive hyphens), and `description` is required (≤1024 chars). Non-conforming skills are **simply not loaded by pi**. When migrating to pi, a4agent re-checks against pi's rules and warns clearly in the migration result instead of leaving a "migrated successfully but nowhere to be found" state.

**Qoder**: besides `~/.qoder/skills`, it also recognizes the cross-tool directories `~/.agents/skills` and `<project>/.agents/skills` (lower priority); a4agent standardizes on the `.qoder/skills` prefix, so no project-level special-casing is needed. The config directory name varies by distribution (international `.qoder`, China `.qoder-cn`); a4agent detects whichever actually exists, or you can set `A4AGENT_QODER_HOME` directly.

**OpenCode**: the global root is `~/.config/opencode/skills` (not `~/.opencode/skills`); project-level skills live in `.opencode/skills`. Two behaviors are handled specially, because getting them wrong would be visible:

- **Skill IDs come from the directory name**; the frontmatter `name` is only a display label. a4agent therefore aggregates and resolves conflicts by directory name on the OpenCode side — otherwise two skills with the same display name but different IDs would be merged into one card, or one of them would be deleted during migration (in OpenCode they are two independently working skills).
- **It also auto-discovers `~/.claude/skills` and `~/.agents/skills`** (`.agents` is additionally ZCode's / Qoder's cross-tool directory). Skills found there are badged "already visible to OpenCode" and the one-click sync skips them, so you never end up with a pointless duplicate copy.

---

## MCP Management

Each of the seven tools stores its MCP servers in its own config file; a4agent normalizes them into one view (`name` / `transport` / `command` / `args` / `env` / `url` / `headers`).

### Discovery & Aggregation

- Auto-discovers global and project-level MCP servers on all seven tools and aggregates same-name servers across tools ("already on N tools").
- `env` / `headers` are always **masked** in detail views (key names only); the API never returns plaintext secrets.
- Every server card shows a **description**: common servers are recognized automatically (a built-in digest library plus live npm registry lookups for npx packages, both cached with failure fallbacks), and you can also click "Describe" to write your own (stored locally, shared across tools, cleared when left empty).
- Project-level files are recognized automatically: Claude Code `.mcp.json`, Codex `.codex/config.toml`, ZCode `.zcode/config.json`, pi `.pi/mcp.json` (Qoder's project level *is* Claude Code's `.mcp.json`, managed under the Claude tool and not discovered twice).

### Installing MCP Servers

- Click "Install MCP" to create a server from scratch on any app: pick a target app (Claude Code / Codex / dsh / ZCode / pi / Qoder / OpenCode — written to that app's global config) and a transport (stdio / http / sse, filtered by the target's capabilities — e.g. Codex is stdio-only, pi and OpenCode have no sse), then fill in command/args/env or URL/headers.
- You can also **paste JSON to import in bulk**: paste an existing MCP config snippet (three formats supported: a top-level "mcpServers" object, a name-keyed server dictionary, or a single server object) and install many at once — each entry is independent, so one failure (name conflict, unsupported transport, etc.) doesn't abort the rest, and each reports success/failure.
- Installation uses each tool's native rendering and atomic writes (a same-name server on the target is explicitly rejected — use migration or delete first); existing servers and unrelated config keys are preserved. Installed servers appear in the card list immediately with descriptions auto-matched.

### Cross-Tool Migration & the Transport Capability Matrix

- Any server on any tool can be migrated to any target; if the target has a same-name server, it is **snapshotted to the recycle bin first**, and the target config file is backed up before writing.
- Migration is strictly validated against the **transport capability matrix**; incompatible pairs fail as a whole and are logged — **no silent degradation**:

| Target | Transports supported | Config file |
|---|---|---|
| Claude Code | stdio / sse / http | `~/.claude.json` (global), `<project>/.mcp.json` (project) |
| Codex | stdio | `~/.codex/config.toml` (`[mcp_servers.*]`) |
| dsh | stdio / streamable-http | `~/.dsh/profiles/<profile>/cordis.patch.yml` |
| ZCode | stdio / sse / http | `~/.zcode/cli/config.json`, `<project>/.zcode/config.json` (`mcp.servers`) |
| pi | stdio / streamable-http | `~/.pi/agent/mcp.json` (global), `<project>/.pi/mcp.json` (project; the project must be trusted by pi) |
| Qoder | stdio / sse / http | `~/.qoder/mcp.json` (global; project level reuses Claude Code's `<project>/.mcp.json`) |
| OpenCode | stdio / streamable-http | `~/.config/opencode/opencode.json(c)`, `<project>/.opencode/opencode.json(c)` (`mcp.servers`) |

**dsh**: MCP servers hang off the `@deepseek-ai/dsh-mcp-client` plugin entry; rewrites preserve other non-managed entries. No project-level MCP.

**ZCode**: the config schema is strict (unknown keys are dropped), so a4agent writes only its canonical fields (`type`/`command`/`args`/`cwd`/`env`/`url`/`headers`/`enabled`/`timeoutMs`); servers migrated to ZCode are connected automatically by the client.

**pi**: `mcp.json` is structurally identical to Claude Code's (top-level `mcpServers`), but it **explicitly rejects legacy SSE** (`type: "sse"` is invalid); migrating an sse entry to pi fails as a whole and is logged. pi also has its own keys `exposure` / `toolExposure` / `enabled` / `timeout` / `auth` / `oauth` that control how tools are exposed to the model — a4agent **preserves them losslessly**, along with the top-level `autoEnableCodemode`. pi server names accept only letters, digits, `_`, and `-`; names containing Chinese characters or dots are rejected explicitly at migration/install time (they would be dead entries once written). The project-level file does not accept the `auth` key — it is stripped automatically on write and logged.

**Qoder**: `~/.qoder/mcp.json` is structurally identical to Claude Code's (top-level `mcpServers`; `type` accepts stdio / sse / http / streamable-http). Its own keys are `disabled` (the switch for turning a server off inside Qoder's UI — note it is **not** `enabled`), `timeout`, and `authType`, all **preserved losslessly**. Qoder's **project-level MCP shares the same `<project>/.mcp.json` as Claude Code** (it looks up `.mcp.json` first, then `mcp.json`), so a4agent does not let the Qoder tool take over project-level files — the same file written by two tools would just stomp each other; migrating to Claude project level makes it effective for Qoder project level too. The runtime mirrors `%APPDATA%\Qoder\SharedClientCache\extension\local\mcp.json` and `~/.qoder/mcp-router.json` are in-process state; a4agent neither reads nor writes them and excludes them from backups.

**It is normal for the Qoder view to "show no connected MCP"** — a4agent manages the **user-defined** `~/.qoder/mcp.json`; most connectors actually connected in a Qoder session do not come from that file: built-in connectors (browser control, node REPL, etc.) ship with the Qoder installer, and plugin-bundled connectors live in `~/.qoder/plugins/cache/<source>/<plugin>/mcp.json`. Neither belongs to the user config file, so neither shows up on a4agent's Qoder cards. The test is simple: an empty `~/.qoder/mcp.json` while several MCPs are connected inside Qoder is not a contradiction. To make a server show up in the Qoder view, write it into this file via "Install MCP" or cross-tool migration.

**OpenCode**: servers live under `mcp.servers` with exactly two transports — `local` (stdio) and `remote` (Streamable HTTP). There is **no place for legacy SSE**, so sse sources fail as a whole and are logged. Three shape differences are smoothed over by a4agent: the command is a **single array** (executable and arguments merged; split only when writing back), the environment key is `environment` (not `env`), and the on/off switch is `disabled`. Its own keys `codemode` / `timeout` / `protocol` / `oauth` are **preserved losslessly**.

> OpenCode's config file is JSONC (comments and trailing commas allowed). a4agent writes standard JSON (which is valid JSONC) and reads existing files with comments correctly, but **hand-written comments in that file are lost on write** — an inherent trade-off of whole-subtree replacement. If comments matter to you, configure it in OpenCode's own UI instead. If reading the config fails, a4agent aborts the write rather than rebuilding from an empty config (which would wipe your model / agents / permissions).

### Recycle Bin & Data Protection

- Replaced/deleted server config snippets are **snapshotted to the recycle bin** (restorable within 30 days); `env` / `headers` inside snapshots are **DPAPI-encrypted** at rest and decrypted on restore.
- Every migration writes a log; discovery, preview, and recycle-bin APIs mask sensitive fields throughout.

---

## API Provider Switching

a4agent provides one-click switching for **CLI tools without a graphical config UI**: currently **Claude Code** and **Codex**.

- **Claude Code**: direct Anthropic protocol, or through the built-in **local translation proxy** that translates requests in real time to OpenAI Chat Completions for OpenAI-compatible providers (the proxy listens on `127.0.0.1` only, authenticates with a random token, and survives after the app exits).
- **Codex**: OpenAI Responses protocol written to `~/.codex/config.toml`; connects directly when the upstream natively supports Responses (e.g. DeepSeek), otherwise forwards through the local translation proxy.

### Why Only These Two Are Managed

The criterion is a single one: **does the target app ship a complete provider configuration UI?**

| Tool | What the app itself can do | a4agent's approach |
|---|---|---|
| Claude Code | None, pure CLI | Managed (the only viable path) |
| Codex CLI | None, pure CLI | Managed (the only viable path) |
| dsh | Settings → Models: three API protocols + fetch model list | Configure in-app |
| ZCode | Settings → Model providers: dual endpoint URLs + per-model capabilities | Configure in-app |
| pi | Switch directly via `/model` | Configure in-app |
| Qoder | 9 preset providers + custom endpoints + connectivity check | Configure in-app |

For those apps, managing their config files externally is neither easier (a few clicks in-app is faster) nor safer: capability toggles, context windows, and enablement states visible in the UI are unknowable from outside, so an external writer can only fill conservative defaults.

Note that the criterion is "**the user can configure it themselves**", not "we can't read its config" — Qoder's model catalog may be encrypted so we can't write plaintext, but users configure it just fine in its own UI. A technical obstacle is not a product rationale.

To switch providers, do it directly in each app's settings; see the [migration reference table](docs/迁移对照表-三端API配置.md) (in Chinese) for field mappings and UI entry points per tool.

Before switching, the target config file is backed up automatically (rolling, last 5 kept) and written atomically; API keys are stored encrypted with Windows DPAPI and never echoed back by the API.

### How Task Dispatch Relates to Config

Task dispatch covers all seven tools (Claude Code / Codex / ZCode / Qoder / dsh / pi / OpenCode) and **directly uses the config you already set up in each app** — a4agent no longer writes API configs for these apps; instead it runs a real connectivity check before dispatching:

| Step | What is checked |
|---|---|
| Engine available | The command exists, is executable, and its version |
| Config ready | Reads **your own** config to see whether a provider and model are set (Qoder's config is encrypted and unreadable — stated honestly, judged by the real run) |
| Real connectivity | Actually runs one minimal headless call; only real output counts as pass |

Config freedom stays entirely with you: whatever you configured in the app's UI, the check verifies "can it really run", not "does the config equal the values we wrote".

---

## Local Models (llama.cpp Inference Console)

The "Local Models" tab wraps llama.cpp's `llama-server` into a one-click **OpenAI-compatible API service**: which engine to install, which model to serve, and with what parameters — all through the UI.

**Engine version**: llama.cpp **b11370** (released 2026-10-03, pinned in `backend/app/llama/catalog.py` as the single source of truth). This version's `llama-server` includes the decision-model endpoint, which enables the capability below.

### Decision Model (System One)

Besides the usual `/v1/chat/completions` and `/v1/models`, this engine build also exposes a **`/v1/systemone` decision-model endpoint** — the "Connect" card shows its full URL with ready-to-copy curl / OpenAI SDK examples. You can hand scheduling decisions like "which model to use, how to decompose a task" to the local engine while the main model stays in the cloud, forming a cloud-edge split.

### First-Run Setup Wizard

Five steps: "Get engine → Detect hardware → Pick model directories → Pick default model → Server port":

- **Hardware detection & preset recommendation**: enumerates GPUs via `nvidia-smi` and the registry (vendor, VRAM, driver version); recommends inference presets by VRAM tier (context length, KV cache quantization, MTP speculative decoding) and warns about VRAM overflow when picking a model
- **Engine auto-download**: downloads official prebuilt llama.cpp **engines** matched to your GPU — four options:
  | Engine | For | Size | Min driver |
  |---|---|---|---|
  | CUDA 13.4 | NVIDIA (performance-first) | 525 MB | 580 |
  | CUDA 12.4 | NVIDIA (older drivers) | 597 MB | 550 |
  | Vulkan | NVIDIA / AMD / Intel discrete & integrated | 32 MB | none |
  | CPU | Fallback when no usable GPU | 18 MB | none |

  Downloads are pinned-version official prebuilt binaries (reproducible); **offline install** (point to a directory containing `llama-server.exe`) or skip-for-now are also supported; a CUDA engine = main package + cudart runtime package, auto-merged after download
- If an older a4agent installation exists, its engine directory is **adopted** automatically — no re-download

### Model Library & Running the Service

- Add multiple model directories; `.gguf` files are scanned and their metadata parsed: **size, quantization, native context, whether MTP layers exist** (speculative decoding capable); one click to "set as default model"
- "Start service" shows live status (starting / running / failed / stopped) and engine logs; you're notified when the health check turns ready; port conflicts, missing engines, and process crashes all produce clear log messages; scheduled memory trimming is supported
- Inference parameters are adjustable in the UI: context length, KV cache level (f16/q8_0/q4_0), Flash Attention, GPU layers, API-key auth, and an extra-args escape hatch
- **MTP speculative decoding steps**: llama.cpp's MTP (Multi-Token Prediction) syntax changed across versions — older builds used `--mtp N`, from b105xx it is `--spec-type draft-mtp --spec-draft-n-max N`. a4agent runs `llama-server --help` once to **auto-detect** which syntax the current backend supports before passing parameters, so engine upgrades never break startup

### LAN Access & One-Click Connect

- The "Connect" card shows the Base URL, Chat Completions URL, decision endpoint (System One), model name (the `model` field), and copy-ready **curl / OpenAI SDK** examples; once listening is switched to `0.0.0.0` in inference settings, the actual LAN address is shown
- **"Create connect profile"** builds a configuration profile pointing at the local service in one click; tick which apps to write (Claude Code / Codex) in the dialog, switch on the "Profiles" page, and the app runs on your local model — seamlessly interchangeable with cloud APIs

---

## Task Dispatch (Headless Agent Tasks)

The "Task Dispatch" tab productizes each tool's **headless mode**: write a one-line task description, pick an engine, and a4agent launches the CLI in the background, waits for it to finish, and archives the output while you do other things (mechanics documented in the usage article "Let Agents Run Tasks Autonomously in the Background").

### Supported Engines

| Engine | Headless command | Output format |
|---|---|---|
| **Claude Code** | `claude -p "<task>" --output-format json` | Single JSON object; output in `result`, includes token usage and cost |
| **Codex** | `codex exec --json --ephemeral` | JSONL event stream; output taken from `agent_message` events |
| **ZCode** | `zcode -p "<task>" --json` | Single JSON object; output in `text`, plus `toolCalls` / `usage` / `stopReason` |
| **Qoder** | `qodercli -p "<task>" -o json` | Single JSON object |
| **dsh** | `dsh --profile headless "<task>"` | Raw stdout (body field extracted when parseable as JSON) |
| **pi** | `pi -p "<task>" --mode json --no-session` | JSONL event stream; success decided by `stopReason`, text taken from events |
| **OpenCode** | **No CLI** — a session against the running service | Session message stream; cost/usage/outcome come from the session's terminal state |

- Engines are auto-detected (installed? version? executable path); **uninstalled engines are greyed out** in the UI with the reason
- Every command name is resolved to an absolute path first: on Windows these CLIs are npm `.cmd` shims, and handing the bare name to `subprocess` fails to start. Qoder's command is `qodercli` in official docs but `qoder` in blog posts — both are probed
- Nobody can click "approve" in headless mode, so every command carries permission pre-authorization flags, otherwise the run sticks at an approval prompt or is rejected outright: Claude Code `--permission-mode bypassPermissions`, Codex `--sandbox danger-full-access`, ZCode `--mode yolo`, Qoder and pi `--yolo` (dsh needs none — its headless profile is the headless entry point itself)
- dsh additionally needs the profile's external plugins disabled: the headless profile lacks the host services they depend on, and startup fails without it. a4agent generates a `--patch` overlay that disables only that set of externally-plugged plugins, leaving the profile itself untouched

### Why OpenCode Is Different

The other six spawn a fresh CLI process per task. OpenCode is **driven through the service you already have running** (default `127.0.0.1:49374`; a remote address works too). That buys three things a CLI cannot offer:

- **Approval-free execution without touching global config**: pre-authorization is a **session-scoped permission rule** (verified: the session object echoes it back), discarded when the task ends. Other tools can only rely on global switches like `--yolo` / `bypassPermissions`.
- **Success is reported, not guessed**: the session reports `outcome` (succeeded / failed / interrupted) together with cost and usage, and failures carry a structured `error` instead of requiring you to reverse-engineer an exit code.
- **Interrupt instead of kill**: cancelling sends an interrupt to the server-side session.

OpenCode therefore also skips the "you must have an active profile first" requirement — its models and credentials live in OpenCode, not in a4agent's profile system. Note one subtle failure mode we found by testing: a model the service still reports as `enabled=true` may already be deprecated upstream and only fail on a real run. Both the pre-flight check and the real dispatch retry with another available model when they hit that.

### Automatic Pre-Flight Checks (no dispatch unless everything passes)

Clicking "Dispatch task" runs three synchronous checks; any failure means the engine is **not started**, with a plain-language reason:

1. **Engine available**: the target CLI is installed and really executable (result cached 5 minutes)
2. **Config ready**: reads **your own in-app config** to see whether a provider and model are set. When unreadable (e.g. Qoder's encrypted model catalog), it says honestly "cannot verify — judge by the real run" instead of pretending to pass
3. **Real connectivity**: actually runs one minimal headless call; only real output counts as pass

Steps 2 and 3 no longer compare "does the config equal the values we wrote" — that would misjudge "configured differently from us" as unusable. A real call gives an honest answer no matter how you configured it: bad key, misspelled model name, network down — one call exposes it all. If the config isn't ready, the live test is skipped (it would fail for sure, wasting your time).

A blocked task still leaves a `precheck_failed` record, visible in the queue with which step failed and how long it took.

### Queue & Output

- **Dispatch-and-return**: the API only creates the task row and hands it to a background thread pool (concurrency 5 by default, tunable via `A4AGENT_TASK_CONCURRENCY`); it does not wait for results
- State machine: `queued → running → completed / failed / timeout / canceled / precheck failed`; the UI polls every 2 seconds and the poller stops itself when everything is idle
- The "view output" dialog shows the engine's full output (long outputs return only the tail — the file always holds everything) and per-step pre-check timings
- Supports **cancel** (kills the whole process tree — node shims spawn children), task history, and delete (output file removed with it)
- Timeout defaults to 10 minutes (5–60 selectable); on expiry the process tree is killed and the task marked timeout
- Output lands in `%APPDATA%\a4agent\task_outputs\<task id>.jsonl|md`, alongside other runtime data

### Closing the Window Doesn't Kill Tasks

- **Closing the main window = moving to the background**: tasks keep running; starting a4agent again reactivates the original instance's window instead of spawning a new process
- The real exit is the "**Exit**" button in the top bar, which tells you how many tasks are running and asks twice; on exit, running tasks are honestly marked "app exited" and their process trees terminated
- When a task reaches a final state: an in-page notice if the window is open; a flashing taskbar icon if the window is hidden (zero new dependencies; system notifications and a tray icon are left for later versions)

---

## Download, Installation & Usage

### Download & Install

Download the installer `a4agent-setup-*.exe` from the **Releases** page:

- **Download**: https://github.com/eogee/a4agent/releases (choose the latest version)
- **Requirements**: Windows 10/11 64-bit
- Verify the SHA256 checksum shown on the release page to make sure the file is intact

The installer is **per-user (no UAC)**: double-click and follow the wizard into your user directory — no administrator rights needed at any point. Start-menu and desktop shortcuts are created automatically, and the app can be uninstalled from "Settings → Apps".

### Running

1. After installation, start a4agent from the **Start menu** or the **desktop shortcut**
2. If **Windows SmartScreen** appears on first run, click "More info → Run anyway" (the app has no commercial code signing — normal, doesn't affect functionality)
3. The UI has seven tabs: Profiles (API switching, Claude Code & Codex only), Providers, **Skills**, **MCP**, **Local Models** (llama.cpp inference console), **Task Dispatch** (headless agent tasks), and **Notifications** (phone push + desktop toasts)

### Data & Privacy

- Runtime data (database, config backups) goes to `%APPDATA%\a4agent\`; logs go to `~/.a4agent/logs/`
- Headless task output goes to `%APPDATA%\a4agent\task_outputs\` (same level as config backups, accessible only to the local user); output is raw engine output and may contain code, paths, and other sensitive content — "Feedback" does **not** attach task output automatically, and deleting a task deletes its output file too
- API keys are stored encrypted with Windows DPAPI, bound to the current Windows user
- Original config files are backed up before any modification (`~/.claude/settings.json` / `~/.codex/config.toml`, rolling, last 5 kept)

### FAQ

- **Antivirus flags the app**: PyInstaller-packed programs are occasionally false-flagged; add a trust/exclusion, or submit the sample to the vendor to appeal
- **Upgrading**: just run the new `a4agent-setup-*.exe` over the old install — data and config (`%APPDATA%\a4agent\`) are preserved; the background translation proxy is stopped and old files cleaned before upgrading
- **Upgrading from a4api (v0.3.x → v0.4.0+)**: the product was renamed to a4agent — install the new version directly. First launch migrates `%APPDATA%\a4api\` data into `%APPDATA%\a4agent\` automatically, the installer moves the program directory from `Programs\a4api` to `Programs\a4agent` and cleans old shortcuts, and the `a4api_p*` managed entries previously written to Claude Code / Codex are replaced with `a4a_p*` on the next switch — nothing to do by hand. v0.3.x's "check for updates" also upgrades straight to the new version
- **Uninstall**: uninstall from "Settings → Apps"; program files are removed while runtime data (database, config backups) stays in `%APPDATA%\a4agent\` — delete that directory manually for a thorough cleanup
- **Task dispatch**: closing the main window is **not exiting** — a4agent moves to the background and finishes the tasks; starting the app again brings back the original window. To truly exit use the top-bar "Exit" (it tells you how many tasks are running first). When a task finishes while the window is hidden, the taskbar icon flashes
- **Feedback**: the in-app entry is recommended — the "Feedback" button in the footer submits directly with screenshots (≤10 × ≤1MB), auto-attached environment info, and optional logs, straight to the developer's mailbox eogee@qq.com; you can also file an issue with a relevant excerpt of `~/.a4agent/logs/a4agent.log`

---

## Issue Guidelines

Issues have structured templates (**Bug report / Feature request**) — just fill in the form. The in-app "Feedback" button delivers straight to email and auto-fills items 2 and 4, so it doesn't go through Issues. Before filing manually, confirm the following; missing items may make the problem impossible to locate:

1. **Clear type**: bug report / feature suggestion / usage question — pick the matching label for triage
2. **Environment info (required)**:
   - a4agent version (the version noted on the Releases page)
   - Target app: Claude Code / Codex / dsh / ZCode / other
   - Provider and model: e.g. DeepSeek, Zhipu GLM
3. **Reproduction steps**: the full path from opening the app to the problem, as specific as possible; ideally "what I did → actual result → expected result"
4. **Logs**: attach the **relevant excerpt** of `~/.a4agent/logs/a4agent.log` (not the whole file — the lines around the error)
5. **Error messages & screenshots**: UI error text, terminal output, exception screenshots
6. **Privacy red line**: **never paste API keys, model secrets, or other sensitive info into an issue**; redact logs first if they might contain sensitive content
7. **Troubleshoot first**: before filing, self-check — restart the app, confirm the API key is valid, confirm the upstream is reachable from this machine, confirm no proxy/antivirus interference

Search for an existing duplicate issue before submitting.

---

## Auto-Update

After a new version is released to GitHub/Gitee, the app checks silently **at startup** or when you click "Check for updates" in the top bar. After you confirm, it downloads the installer with a progress bar; once the download passes verification, one more confirmation runs the installer to finish the upgrade. You can also "skip this version".

### Update Sources & Verification

- **Dual-source racing download**: the installer downloads from the GitHub and Gitee mirrors **at the same time, fastest wins** (both mirrors are bound to the same SHA256); the slow/unreachable source loses automatically and gives up early — a lead of more than 8MB declares victory, and the progress bar always shows the leader. No more serial waiting on the slow source (GitHub assets are often ~0.1 MB/s from China; racing lands you on Gitee's ~2 MB/s automatically).
- **Signed update manifest**: the release side signs `latest.json` (version, release notes, installer SHA256, etc.) with an Ed25519 private key, and the app verifies it with the built-in public key; any field anomaly or signature mismatch and the manifest is discarded — **no update prompt**. URLs are not part of the signature, so the GitHub/Gitee manifests are byte-identical and share one signature; what a URL serves is pinned by the signed SHA256.
- **Integrity check**: the installer's SHA256 is computed while downloading and must match the signed manifest before it lands on disk (in `%APPDATA%\a4agent\updates\<version>\`); clicking "Update now" **re-verifies** the on-disk file before launching the installer.
- **Transport whitelist**: HTTPS only, and every redirect hop is checked against a host whitelist (`github.com` / `gitee.com` / the two `*.githubusercontent.com` object-storage domains / `*.gitee.com`); redirects to any other domain are blocked.
- **No downgrades**: a candidate version must be strictly higher than the running one; versions below the manifest's `min_version` (too old, needs a full installer) are refused; pre-release versions are only offered when the running version is also a pre-release.
- **Size caps**: manifest 512KB, installer 300MB — anything over is refused; downloads write only to the user data directory, never the system temp directory.

### Update Flow (one click all the way)

1. A new version is detected → a dialog shows the version number and release notes (carried by the signed manifest).
2. Confirm → background download with live progress; cancellable while downloading.
3. Verification passes → "Update now / Later". Update now: the app first stops the local translation proxy, exits automatically releasing the single-instance lock, then launches the Inno Setup wizard; after installation the new version starts, with config, database, and keys fully preserved.

---

## Development

Requirements: Windows 10+, Python 3.10, [uv](https://docs.astral.sh/uv/)

```bash
uv sync                     # install dependencies
uv run python desktop.py    # start the desktop app
# or debug the backend alone
uv run uvicorn backend.app.main:app --port 8000
```

## Packaging

Prerequisite: building the installer needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install --id JRSoftware.InnoSetup -e --accept-source-agreements`).

```bash
uv run python build.py                 # produce dist/a4agent/ (onedir)
uv run python build.py --installer     # onedir + compile dist/a4agent-setup-<version>.exe
uv run python build.py --onefile       # optional: single exe (for ad-hoc distribution)
```

The version number comes from `pyproject.toml` (`[project].version`) automatically, overridable with `--version`; ISCC.exe is auto-detected (or specified with `--iscc`).

## Project Structure

```
a4agent/
├── desktop.py                # pywebview desktop entry (close-to-background persistence, reactivation on relaunch)
├── build.py                  # packaging script (PyInstaller onedir + Inno Setup installer)
├── installer.iss             # Inno Setup script (per-user install, no UAC)
├── dev_server.py             # dev-mode backend launcher (browser debugging)
├── pyproject.toml            # dependencies; single source of the version number
├── backend/app/              # FastAPI backend
│   ├── main.py               # app entry, lifespan, router mounting
│   ├── api/v1/               # REST route layer: configs / providers / switch / skills / mcp /
│   │                         #   llama / tasks / update / feedback / desktop / fs / removal
│   ├── models.py             # SQLAlchemy data models
│   ├── schemas.py            # Pydantic request/response schemas
│   ├── crud.py               # database CRUD
│   ├── database.py           # database init and directory layout
│   ├── config_manager.py     # profile & provider management
│   ├── config_io.py          # shared config IO (per-tool backup, atomic writes)
│   ├── openai_proxy.py       # Anthropic → OpenAI local translation proxy
│   ├── responses_translator.py  # OpenAI Responses protocol translation
│   ├── proxy_standalone.py   # standalone-process form of the translation proxy
│   ├── crypto.py             # Windows DPAPI key encryption
│   ├── skill_manager.py      # seven-tool skill discovery, aggregation, migration, recycle bin
│   ├── mcp_manager.py        # seven-tool MCP discovery, installation, migration, snapshots
│   ├── removal.py            # cleanup of managed entries handed to the three tools (only self-written ones)
│   ├── removal_backup.py     # permanent snapshots taken before cleanup
│   ├── task_engines.py       # headless task engine adapters (commands & output parsing)
│   ├── task_precheck.py      # three-step pre-flight checks (engine / config / real connectivity)
│   ├── task_runner.py        # task runner (thread pool, process trees, timeout)
│   ├── task_notify.py        # task completion notice (in-page hint / taskbar flash)
│   ├── phone/                # notifications: config (topic / token / event switches) / ntfy (push client) /
│   │                         #   notifier (dispatch of task terminal states to the two channels)
│   ├── hooks/                # session hooks: dispatch (event routing) / handlers (ask / permission /
│   │                         #   completion) / register (mounting to each tool) / deskqueue (deferred toasts)
│   │                         #   dsh (dsh event handling) / dsh_register (Cordis plugin mounting)
│   ├── win_toast.py          # Win32 desktop notification banner
│   ├── updater.py            # self-update (manifest verification, dual-source racing download)
│   ├── llama/                # local model inference console: catalog (engine catalog) / gguf (model parsing) /
│   │                         #   gpu (hardware detection) / downloader (engine download) / runtime & server
│   │                         #   (service lifecycle) / presets (inference presets) / config / lan (LAN)
│   └── tests/                # pytest suite (24 modules)
├── frontend/
│   ├── index.html            # seven-tab main UI
│   ├── js/app.js             # profiles / providers / skills / MCP page logic
│   ├── js/llama.js           # local models page logic
│   ├── js/tasks.js           # task dispatch page logic
│   ├── js/phone.js           # notifications page logic (phone push / desktop toasts / hook binding)
│   ├── js/markdown.js        # markdown rendering for release notes
│   ├── css/ · layui/         # styles and the LayUI component library
│   └── changelog.md          # release notes shown in the in-app update dialog
├── docs/                     # design docs, migration reference tables, implementation plans
├── resources/                # logo and installer icons / dsh-hook (Cordis plugin deployed to ~/.dsh)
└── tools/                    # end-to-end smoke test (e2e_task_smoke.py)
```

Runtime data (database, config backups, llama config and engines) goes to `backend/database/` (development) or `%APPDATA%\a4agent\` (packaged).

---

## Contact

- **Website**: <https://eogee.com>
- **Usage docs**: <https://eogee.com/article/83> (in Chinese)
- **QQ**: 3886370035 | **WeChat**: eogee2022
- **Feedback**: the in-app "Feedback" button in the footer is recommended (screenshots supported) — it goes straight to the developer's mailbox eogee@qq.com; you can also file an issue following the Issue Guidelines above
