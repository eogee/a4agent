# a4agent

[中文说明](README.md)

A local LLM inference console for Windows: turn any `.gguf` model into an **OpenAI-compatible LAN API** with a few clicks.

a4agent is a graphical front-end for [llama.cpp](https://github.com/ggml-org/llama.cpp) `llama-server`,
covering everything the server itself does not: **which engine to install, which model to load, and with which parameters**.

- 🧭 **First-run wizard**: detects GPUs / VRAM / driver versions, recommends an inference preset
  (context size, KV-cache quantization, MTP speculative decoding), and warns about VRAM risks
- 📦 **Lite installer**: the setup only ships the ~64 MB launcher; on first run it downloads the matching
  official llama.cpp engine (Vulkan / CUDA 12.4 / CUDA 13.3 / CPU) based on your hardware,
  or you can point it to an existing engine directory for offline setups
- 🖥️ **GUI management**: model library scanning with GGUF metadata parsing, visual engine tuning,
  live logs, tray-resident operation
- 🌐 **LAN access toggle**: one switch between `127.0.0.1` and `0.0.0.0`, with ready-to-copy
  curl and openai-sdk examples
- 🩹 **Polish**: crash / port-conflict detection, health polling, scheduled RAM trimming

## Quick start

1. Grab `a4agent-Lite-setup` from [Releases](https://gitee.com/eogee/a4agent/releases);
2. The wizard opens on first launch: pick an engine pack → wait for the download → add model directories → choose a default model;
3. The service starts automatically. The *Connect* tab shows the base URL and examples:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "your-model", "messages": [{"role": "user", "content": "hi"}]}'
```

## Build from source

Requires the .NET 8 SDK (and Inno Setup for packaging):

```powershell
dotnet build src/App/App.csproj -c Release
powershell -File installer/build.ps1 -Version 0.1.0 -Lite   # lite installer
powershell -File installer/build.ps1 -Version 0.1.0         # 3 offline installers
```

## License

[MIT](LICENSE) — engine binaries belong to the [llama.cpp](https://github.com/ggml-org/llama.cpp) project and its own license.
