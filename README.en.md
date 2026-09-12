# a4agent

[中文说明](README.md)

**Local LLM inference console for Windows**: turn any `.gguf` model into an **OpenAI-compatible LAN API** with a few clicks. a4agent is a graphical front-end for [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server`, covering what the engine itself does not — **which engine to install, which model to load, and with which parameters**.

| Capability | Description |
|---|---|
| **First-run wizard** | Detects GPUs / VRAM / driver versions, recommends an inference preset (context size, KV-cache quantization, MTP speculative decoding), warns about VRAM risks |
| **Automatic engine setup** | Lite installer is only ~64 MB; on first run it downloads the matching **official prebuilt llama.cpp engine** (Vulkan / CUDA 12.4 / CUDA 13.3 / CPU), or points to an existing engine directory for offline setups |
| **GUI management** | Model library scanning with GGUF metadata parsing (quant / native context / MTP), visual engine tuning, live logs, tray-resident operation |
| **LAN access** | One switch between `127.0.0.1` / `0.0.0.0`, with ready-to-copy LAN address plus curl and openai-SDK examples |

---

## First-run wizard

The wizard completes everything in five steps: acquire engine → detect hardware → add model directories → pick default model → service port.

### Hardware detection & preset

- Lists GPUs with vendor, VRAM and driver version (`nvidia-smi` + registry enumeration);
- Recommends a preset (context size, KV level, MTP) from VRAM and vendor, and assesses VRAM-overflow risk for the selected model;
- Everything remains adjustable later in the *Settings* tab.

### Engine acquisition (Lite installer)

- The recommended pack is pre-selected by hardware detection; click next to download with progress and cancel support;
- Offline users can point to an existing engine directory, or skip and configure later;
- Full installers (engine bundled) never show this step.

---

## Automatic engine setup

### Engine packs

Packs are pinned to a fixed llama.cpp release — no auto-tracking, so binaries are reproducible and upgrades are verified by a human before shipping. The pinned tag has a **single source of truth**: `RepoTag` in `src/Core/Engine/EnginePack.cs` (bumping = editing that one file plus asset names/sizes), with the matching page at `https://github.com/ggml-org/llama.cpp/releases/tag/<RepoTag>`:

| Pack | Size | Works on | Requirement |
|---|---|---|---|
| **Vulkan (default)** | ~30 MB | NVIDIA / AMD / Intel, desktop or iGPU | any recent driver |
| CPU | ~18 MB | fallback without a usable GPU | — |
| CUDA 12.4 | ~616 MB | NVIDIA | driver ≥ 550 |
| CUDA 13.3 | ~516 MB | NVIDIA | driver ≥ 580 |

- NVIDIA cards pick CUDA 12.4/13.3 by **driver major version**, falling back to Vulkan when too old; CPU pack when no GPU is present;
- CUDA = main pack + cudart runtime pack, downloaded and **merged** into `engine\` automatically.

### Download & install safety

- Engine binaries come **only from the official llama.cpp GitHub Release over HTTPS**;
- Downloads go to a temp directory and are **atomically swapped** into `engine\` after `llama-server.exe` is verified — a failed or cancelled download never leaves a broken engine behind;
- Restricted networks: download the official zips manually and install offline (extract, then point the wizard / settings to that directory):
  - **Release list**: https://github.com/ggml-org/llama.cpp/releases ; the pinned version's page is `https://github.com/ggml-org/llama.cpp/releases/tag/<RepoTag>` (`<RepoTag>` per `src/Core/Engine/EnginePack.cs`)
  - Asset (zip) names per engine pack, where `<tag>` = `RepoTag`:
    - Vulkan: `llama-<tag>-bin-win-vulkan-x64.zip`
    - CPU: `llama-<tag>-bin-win-cpu-x64.zip`
    - CUDA 12.4: `llama-<tag>-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`
    - CUDA 13.3: `llama-<tag>-bin-win-cuda-13.3-x64.zip` + `cudart-llama-bin-win-cuda-13.3-x64.zip`
  - For CUDA, merge the main pack and the cudart pack into the **same** engine directory; the cudart file name carries no tag and follows the CUDA major version

---

## Model library

- Add multiple model directories; `.gguf` files are scanned with metadata parsing: **size, quantization, native context, MTP tensors**; unparsable files are greyed out with the reason;
- One click sets the default model loaded at service start;
- Directories and models persist across runs; portable mode supported (`config.json` next to the exe).

---

## Service & LAN access

- The *Status* tab shows live state (starting / running / failed / stopped) and engine logs, with health polling;
- Port conflicts, missing engine and crashes produce explicit log hints; scheduled RAM trimming supported;
- **LAN toggle**: check *Allow LAN access* in Settings to listen on `0.0.0.0`, then restart the service;
- The *Connect* tab generates the base URL, chat-completions URL, model id, LAN address (real LAN IP shown once enabled), plus copy-ready **curl** and **openai SDK (Python)** examples.

---

## Download, install & usage

### Download

Grab `a4agent-Lite-setup-*.exe` from the **Releases** page:

- **Download**: https://gitee.com/eogee/a4agent/releases
- **Requirements**: Windows 10/11 64-bit
- The installer is ~64 MB (v0.2.0); the SHA256 checksum is listed in each release note — verify it before running

Per-user installation (no UAC), with Start-menu and desktop shortcuts created automatically.

### Running

1. Launch a4agent — the first-run wizard opens automatically;
2. If **Windows SmartScreen** appears, click *More info → Run anyway* (the app is not code-signed; this is expected);
3. After the engine download, add model directories → pick a default model → done; the service starts automatically and the *Connect* tab has your URLs;
4. Closing the window minimizes to the tray and keeps the service running; right-click the tray icon to exit (stops the service first).

### Data & privacy

- Config lives in `config.json` next to the exe (portable mode) or `%APPDATA%\a4agent\config.json`;
- The engine installs under `engine\` in the install directory; a4agent collects no telemetry — all inference stays on your machine.

### FAQ

- **Fresh Lite install complains about a missing engine**: on startup the app probes previous install directories (`a4agent*\engine`) and adopts an existing engine automatically — no re-download needed. If no engine exists anywhere, a prompt offers to re-run the setup wizard for an online download (also available under *Settings → Wizard*)
- **LAN devices can't connect**: enable *Allow LAN access* in Settings and restart; then allow `llama-server.exe` through Windows Firewall (the first `0.0.0.0` start triggers a prompt)
- **Is LAN access safe**: the service has no built-in auth; on untrusted networks add `--api-key <secret>` via *Settings → Extra args*
- **Engine download fails / slow**: choose *Offline install* in the wizard and point it to a manually downloaded official zip; on poor networks start with the 30 MB Vulkan pack
- **Upgrade**: run the new installer over the old one; config and the downloaded engine are preserved
### Reporting issues: attach the log text from the *Status* tab

---

## Auto-update & security

The app ships with self-update: it silently checks on startup (or via *Settings → Software update → Check*). After you confirm, it downloads the new installer, verifies it, and the setup wizard completes the upgrade — config and the downloaded engine are preserved. You can also *skip this version*.

### Sources & verification (anti-MITM / anti-forgery)

- **Dual sources**: the signed manifest `latest.json` is published to both GitHub and Gitee; the client fetches both in parallel and the first signature-valid one wins. Installer download falls back from GitHub to Gitee automatically.
- **Signed manifest**: the release side signs `latest.json` with an Ed25519 private key (version, notes, installer SHA256); the app verifies it with the embedded public key — any tampering invalidates the manifest silently. URLs are not part of the signature, so both copies are byte-identical; the content behind a URL is pinned by the signed SHA256.
- **Integrity**: the installer SHA256 is computed while streaming and compared against the manifest before it lands on disk; the file is re-verified again before launching the installer.
- **Transport whitelist**: HTTPS only, and every redirect hop is checked against a host whitelist (`github.com` / `gitee.com` / `*.githubusercontent.com` / `*.gitee.com`).
- **Anti-downgrade**: candidates must be strictly newer; pre-releases are only offered to pre-release installations; individual versions can be skipped.
- **Size caps**: manifest 512KB, installer 300MB; downloads land in the app data directory (`%APPDATA%\a4agent\updates\`), never the system temp folder.

### Releasing (maintainers)

```powershell
powershell -File installer/build.ps1 -Version 0.2.1 -Lite      # build installer (version stamped into the assembly)
git tag v0.2.1; git push origin v0.2.1; git push github v0.2.1
node tools/update-manifest.js --installer installer/out/a4agent-Lite-setup-v0.2.1.exe --version 0.2.1
```

The script signs `latest.json`, ensures the release on both platforms, uploads the installer + manifest, and updates the notes. The signing key lives in `.claude/keys/update-signing.pem` (gitignored; rotate immediately if leaked). Protocol consistency between the Node publisher and the C# client is enforced by `Smoke --updatetest` (cross-language signature interop + tamper-rejection cases).

---

## Security design

- **Engine supply chain**: binaries only from the official llama.cpp release over HTTPS, pinned to a fixed version (bumping requires an explicit code change); zip entries are checked against path traversal; installs are atomic (temp dir + verified swap), so a half-installed engine can never be started
- **Closed by default**: the service listens on `127.0.0.1` unless explicitly switched; docs and UI both point to `--api-key` for authentication
- **Process management**: the engine runs as a managed child process (killed as a whole tree on stop), and quitting the app always stops the service
- **Single instance**: a named Windows mutex ensures only one console runs at a time

---

## Development

Requirements: Windows 10+, [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)

```bash
dotnet build src/App/App.csproj -c Release
src/App/bin/Release/net8.0-windows/a4agent.exe --uitest   # headless smoke test
```

## Packaging

Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php).

```bash
powershell -File installer/build.ps1 -Version 0.2.0 -Lite   # lite installer (~64 MB, engine downloaded on first run)
powershell -File installer/build.ps1 -Version 0.2.0         # 3 offline installers (cu12.4 / cu13.3 / vulkan, payloads required)
```

## Project layout

```
src/
  App/     WinForms UI: main window (status/models/settings/connect), first-run wizard, tray
  Core/    Engine process management (start-stop/health polling/RAM trim), command-line builder,
           engine pack catalog & downloader, GGUF parsing, GPU detection, preset recommendation
  Smoke/   Smoke tests
installer/ Inno Setup template & packaging scripts
```
