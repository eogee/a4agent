"""llama 控制台运行时管理器：组合配置、进程、下载器与模型库。

模块级单例 runtime()；日志走环形缓冲，前端轮询读取。
"""
import logging
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from . import catalog, downloader, gguf, gpu, lan, presets, server
from .config import (LlamaConfig, ModelEntry, default_engine_dir,
                     load_config, save_config)

logger = logging.getLogger(__name__)

LOG_CAPACITY = 800


def _programs_root() -> Path:
    """Windows 用户级程序目录（安装器默认落点）。单列出来便于测试替换。"""
    return Path.home() / "AppData" / "Local" / "Programs"


class _LogRing:
    """线程安全日志环形缓冲，单调 id 支持增量拉取。"""

    def __init__(self, capacity: int = LOG_CAPACITY):
        self._lock = threading.Lock()
        self._lines: deque = deque(maxlen=capacity)
        self._next_id = 1

    def append(self, text: str) -> None:
        with self._lock:
            self._lines.append((self._next_id, text))
            self._next_id += 1

    def after(self, last_id: int = 0) -> list:
        with self._lock:
            return [{"id": i, "text": t} for i, t in self._lines if i > last_id]

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class LlamaRuntime:
    def __init__(self):
        self.cfg: LlamaConfig = load_config()
        self.logs = _LogRing()
        self.engine = server.ServerEngine(
            on_log=self.logs.append,
            on_state=self._on_state,
        )
        self.downloader = downloader.EngineDownloader()
        self._scan_cache: dict = {}   # path -> GgufModelInfo
        self._scan_cache_ts = 0.0
        self._state_listeners: list = []

    # ───────────────────────── 状态联动 ─────────────────────────

    def _on_state(self, state: str) -> None:
        for cb in list(self._state_listeners):
            try:
                cb(state)
            except Exception:
                pass

    # ───────────────────────── 引擎目录 ─────────────────────────

    def engine_dir(self) -> Path:
        return self.cfg.effective_engine_dir()

    def engine_present(self) -> bool:
        d = self.engine_dir()
        if downloader.is_engine_present(d):
            return True
        return self.locate_engine() is not None

    def locate_engine(self) -> str | None:
        """引擎目录自动接管：配置（或默认 engine\\）缺失时，探测本机 a4agent /
        旧版 a4api 安装目录下已有的引擎，免重复下载；找不到返回 None。

        有多个候选时按「后端是否匹配显卡」优先，再按 llama-server.exe 修改时间
        从新到旧——NVIDIA 卡上同时存在 CUDA 与 Vulkan 引擎时选 CUDA，而不是
        谁的文件更新选谁。
        """
        own = self.engine_dir()
        if downloader.is_engine_present(own):
            return str(own)
        entries = self._candidate_engines()
        if not entries:
            return None
        pref = catalog.backend_preference(gpu.best())

        def rank(entry) -> tuple:
            path, mtime = entry
            backend = catalog.detect_backend(path)
            # 认不出的后端排在所有已知后端之后，但仍可作为兜底被选中
            try:
                prio = pref.index(backend)
            except ValueError:
                prio = len(pref)
            return (prio, -mtime)

        return str(sorted(entries, key=rank)[0][0])

    def _candidate_engines(self) -> list:
        """候选引擎：(engine 目录, llama-server.exe 修改时间)。

        只看 %LOCALAPPDATA%\\Programs 下 a4agent* / a4api* 的 engine 子目录；
        改名过的旧引擎目录（engine-spark、engine-*-backup 之类）不参与接管。
        """
        root = _programs_root()
        try:
            dirs = [d for d in root.iterdir()
                    if d.is_dir() and d.name.lower().startswith(("a4agent", "a4api"))]
        except OSError:
            return []
        found = []
        for d in dirs:
            exe = d / "engine" / "llama-server.exe"
            try:
                if exe.is_file():
                    found.append((exe.parent, exe.stat().st_mtime))
            except OSError:
                continue
        return found

    def engine_backend(self) -> str:
        """当前实际会被加载的引擎后端：cuda / vulkan / cpu，未安装为空串。"""
        found = self.locate_engine()
        return catalog.detect_backend(found) if found else ""

    def installed_backends(self) -> list:
        """本机已安装的引擎后端（含旧版命名目录），按 cuda/vulkan/cpu 固定排序。

        供设置页渲染后端选择：已安装的可直接切换，未安装的置灰并给下载入口。
        """
        found = set()
        for d, _mt in self._candidate_engines():
            b = catalog.detect_backend(d)
            if b:
                found.add(b)
        return [b for b in ("cuda", "vulkan", "cpu") if b in found]

    def engine_dir_for(self, pack) -> Path:
        """某个引擎包的安装目录：Programs\\a4agent-<后端>\\engine。

        按后端分目录而不是全部塞进一个 engine\\：目录名即内容语义，换后端不会
        覆盖掉正在用的那套；同时 a4agent-* + engine 的形状正好被上面的自动接管
        逻辑认出来，多套引擎并存时按显卡后端偏好挑一套。
        """
        return _programs_root() / f"a4agent-{catalog.pack_backend(pack)}" / "engine"

    def download_engine(self, pack) -> dict:
        """下载引擎包到其专属目录，成功后把配置指向它（失败/取消则保持原引擎）。"""
        target = self.engine_dir_for(pack)
        return self.downloader.start(
            pack, target,
            on_done=lambda error: self._after_engine_download(target, error))

    def _after_engine_download(self, target: Path, error: str) -> None:
        """下载结束回调（下载线程内）：成功才切换，避免把配置指到半个引擎上。"""
        if error or not downloader.is_engine_present(target):
            return
        self.cfg.engine_dir = str(target)
        save_config(self.cfg)
        backend = catalog.detect_backend(target)
        self.logs.append(
            f"[引擎] 已切换到 {catalog.backend_label(backend) or '未知'} 后端: {target}")

    def switch_backend(self, backend: str) -> dict:
        """设置页切换引擎后端：auto 恢复自动接管，具体后端要求本机已装。

        只改写 engine_dir 指针、不触碰引擎文件：服务运行中允许切换，下次启动
        生效；下载进行中禁止——下载完成回调也会改写配置，两者不能互相覆盖。
        标准目录（a4agent-<后端>\\engine）缺失时从旧命名候选目录兜底，如
        a4agent-cu12.4。
        """
        if self.downloader.progress()["running"]:
            return {"ok": False, "error": "引擎正在下载中，请等待完成"}
        if backend == "auto":
            self.cfg.engine_dir = ""
        elif backend in catalog.BACKEND_LABELS:
            target = _programs_root() / f"a4agent-{backend}" / "engine"
            if not downloader.is_engine_present(target):
                for d, _mt in self._candidate_engines():
                    if catalog.detect_backend(d) == backend:
                        target = d
                        break
                else:
                    return {"ok": False, "error":
                            f"{catalog.backend_label(backend)} 引擎尚未安装，请先下载引擎包"}
            self.cfg.engine_dir = str(target)
        else:
            return {"ok": False, "error": f"未知后端: {backend}"}
        save_config(self.cfg)
        return {"ok": True}


    def adopt_engine(self) -> bool:
        """把探测到的已有引擎目录写进配置（仅当默认位置没有引擎时）。"""
        if downloader.is_engine_present(self.engine_dir()):
            return True
        found = self.locate_engine()
        if found:
            self.cfg.engine_dir = found
            save_config(self.cfg)
            self.logs.append(f"[引擎] 自动接管已有引擎目录: {found}")
            return True
        return False

    # ───────────────────────── 服务控制 ─────────────────────────

    def start(self) -> dict:
        if self.downloader.progress()["running"]:
            return {"ok": False, "error": "引擎正在下载中，请等待完成"}
        self.adopt_engine()
        self._log_backend_hint()
        cfg = self.cfg
        model = cfg.default_model_path
        if not model or not Path(model).is_file():
            self.engine.mark_failed("未设置有效的默认模型，请先在模型库选择")
            return {"ok": False, "error": "未设置有效的默认模型"}

        caps = server.detect_capabilities(self.engine_dir())
        include_mtp = caps.supports_mtp
        if not caps.supports_mtp and cfg.infer.mtp_steps > 0:
            self.logs.append("[提示] 当前引擎不支持 MTP（投机解码），MTP 步数将被忽略")
        elif include_mtp and cfg.infer.mtp_steps > 0 and not self._model_has_mtp_head(model):
            self.logs.append("[提示] 当前模型不包含 MTP 层（nextn 张量），MTP 步数将被忽略")
            include_mtp = False

        if cfg.mmproj_path.strip() and not Path(cfg.mmproj_path.strip()).is_file():
            self.logs.append(
                f"[提示] 投影模型（mmproj）文件不存在，本次启动不加载: {cfg.mmproj_path}")
            mmproj = ""
        else:
            mmproj = cfg.mmproj_path.strip()

        args = self._build_args(model, include_mtp, caps, mmproj)
        self.engine.start(
            self.engine_dir(), args, cfg.host, cfg.port,
            trim_enabled=cfg.run.auto_trim_ram,
            trim_delay_seconds=cfg.run.trim_delay_seconds,
            restart_on_crash=cfg.run.auto_restart_on_crash,
        )
        return {"ok": True}

    def _build_args(self, model: str, include_mtp: bool, caps, mmproj: str) -> list:
        """build_args 组装；mmproj 由 runtime 先行校验后注入。"""
        cfg = self.cfg
        origin = cfg.mmproj_path
        try:
            cfg.mmproj_path = mmproj
            return server.build_args(cfg.infer, model, cfg.host, cfg.port,
                                     include_mtp, caps.mtp)
        finally:
            cfg.mmproj_path = origin

    def _log_backend_hint(self) -> None:
        """NVIDIA 卡却跑在 Vulkan / CPU 后端时提示一次。

        只在启动时打日志：status() 每 1.5s 轮询一次，提示放那里会刷屏。
        """
        backend = self.engine_backend()
        best = gpu.best()
        if backend in ("", "cuda") or best is None or not best.is_nvidia:
            return
        self.logs.append(
            f"[提示] 当前引擎为 {catalog.backend_label(backend)} 后端，"
            f"NVIDIA 显卡建议改用 CUDA 引擎包（推理设置 → 引擎后端）")

    def stop(self) -> dict:
        self.engine.stop()
        return {"ok": True}

    @staticmethod
    def _model_has_mtp_head(model_path: str) -> bool:
        """MTP（投机解码）需要模型自带 nextn 头；解析失败时保守放行。"""
        try:
            info = gguf.parse(model_path)
            return not info.ok or info.has_nextn_tensors
        except Exception:
            return True

    # ───────────────────────── 模型库 ─────────────────────────

    def scan_models(self, force: bool = False) -> list:
        """扫描全部模型目录下的 .gguf（仅顶层），带缓存（10 秒）。"""
        now = time.monotonic()
        if not force and self._scan_cache and now - self._scan_cache_ts < 10:
            return list(self._scan_cache.values())
        self._scan_cache.clear()
        for d in self.cfg.model_dirs:
            p = Path(d)
            if not p.is_dir():
                continue
            for f in sorted(p.glob("*.gguf")):
                self._scan_cache[str(f)] = gguf.parse(str(f))
        self._scan_cache_ts = now
        return list(self._scan_cache.values())

    def models_payload(self) -> dict:
        models = []
        for info in self.scan_models():
            d = info.to_dict()
            entry = self._model_entry(info.file_path)
            d["alias"] = entry.alias if entry else ""
            d["is_default"] = info.file_path == self.cfg.default_model_path
            d["size_gb"] = round(info.file_size / (1024 ** 3), 2)
            models.append(d)
        models.sort(key=lambda m: (not m["ok"], m["file_path"]))
        return {"dirs": list(self.cfg.model_dirs), "models": models,
                "default_model_path": self.cfg.default_model_path}

    def _model_entry(self, path: str) -> ModelEntry | None:
        for e in self.cfg.models:
            if e.get("path") == path:
                return ModelEntry(**e)
        return None

    def add_model_dir(self, path: str) -> dict:
        path = str(Path(path).resolve())
        if path not in self.cfg.model_dirs:
            self.cfg.model_dirs.append(path)
            save_config(self.cfg)
        self.scan_models(force=True)
        return self.models_payload()

    def remove_model_dir(self, path: str) -> dict:
        path = str(Path(path).resolve())
        self.cfg.model_dirs = [d for d in self.cfg.model_dirs
                               if str(Path(d).resolve()) != path]
        save_config(self.cfg)
        self.scan_models(force=True)
        return self.models_payload()

    def set_default_model(self, path: str) -> dict:
        path = str(Path(path).resolve())
        info = self._scan_cache.get(path) or gguf.parse(path)
        if not info.ok:
            return {"ok": False, "error": f"模型解析失败：{info.error}"}
        self.cfg.default_model_path = path
        if not any(e.get("path") == path for e in self.cfg.models):
            self.cfg.models.append(ModelEntry(path=path).__dict__)
        save_config(self.cfg)
        return {"ok": True}

    def model_risk(self, path: str, context_tokens: int | None = None,
                   kv_level: str | None = None) -> dict:
        """模型详情 + 按当前推理参数的显存风险评估。"""
        info = gguf.parse(path)
        ctx = context_tokens or self.cfg.infer.context_tokens
        kv = kv_level or self.cfg.infer.kv_level
        best_gpu = gpu.best()
        vram = best_gpu.vram_gb if best_gpu else 0
        has_gpu = bool(best_gpu and best_gpu.is_nvidia)
        risk = presets.assess_risk(info, ctx, kv, vram, has_gpu)
        return {
            "model": info.to_dict(),
            "context_tokens": ctx,
            "kv_level": kv,
            "vram_gb": vram,
            "estimated_vram_gb": round(info.estimate_vram_gb(ctx, kv), 2) if info.ok else None,
            "risk": risk,
        }

    # ───────────────────────── 状态 / 接入 ─────────────────────────

    def status(self) -> dict:
        cfg = self.cfg
        dl = self.downloader.progress()
        engine_found = self.locate_engine()
        caps = None
        if engine_found:
            caps = server.detect_capabilities(engine_found).to_dict()
        backend = catalog.detect_backend(engine_found) if engine_found else ""
        best_gpu = gpu.best()
        model_name = ""
        if cfg.default_model_path:
            model_name = Path(cfg.default_model_path).stem
        return {
            "state": self.engine.state,
            "wizard_done": cfg.wizard_done,
            "engine_present": engine_found is not None,
            "engine_dir": str(engine_found or self.engine_dir()),
            "engine_backend": backend,
            "engine_backend_label": catalog.backend_label(backend),
            "installed_backends": self.installed_backends(),
            "port": cfg.port,
            "host": cfg.host,
            "default_model_path": cfg.default_model_path,
            "default_model_name": model_name,
            "mmproj_path": cfg.mmproj_path,
            "capabilities": caps,
            "last_exit_code": self.engine.last_exit_code,
            "downloading": dl["running"],
            "download_progress": dl,
            "gpu": best_gpu.to_dict() if best_gpu else None,
        }

    def served_context_for(self, api_base: str, model: str) -> int | None:
        """方案确实指向本机当前 llama-server 时，返回服务实际的上下文窗口。

        供 pi 写模型条目的 contextWindow 用：pi 缺省按 128000 计，用户在推理设置
        里把 `-c` 调到 256k 后，若不同步给 pi，pi 会提前截断上下文。
        """
        cfg = self.cfg
        if not cfg.default_model_path:
            return None
        if model != Path(cfg.default_model_path).stem:
            return None
        endpoints = {(h, int(cfg.port)) for h in ("127.0.0.1", "localhost")}
        if cfg.host in ("0.0.0.0", "::"):
            lan_ip = lan.get_best_ipv4()
            if lan_ip:
                endpoints.add((lan_ip, int(cfg.port)))
        parsed = urlparse(str(api_base or ""))
        if parsed.scheme != "http" or (parsed.hostname, parsed.port) not in endpoints:
            return None
        return int(cfg.infer.context_tokens) or None

    def connect(self) -> dict:
        """接入页：Base URL、Chat 地址、System One 决策端点、模型名、局域网地址与调用示例。"""
        cfg = self.cfg
        probe_host = "127.0.0.1"
        base = f"http://{probe_host}:{cfg.port}"
        model = Path(cfg.default_model_path).stem if cfg.default_model_path else ""
        lan_ip = lan.get_best_ipv4()
        lan_base = f"http://{lan_ip}:{cfg.port}" if (cfg.host in ("0.0.0.0", "::") and lan_ip) else None
        curl = (
            f"curl {base}/v1/chat/completions \\\n"
            f"  -H \"Content-Type: application/json\" \\\n"
            f"  -d '{{\"model\": \"{model}\", \"messages\": "
            f"[{{\"role\": \"user\", \"content\": \"你好\"}}]}}'"
        )
        api_key_line = f'    api_key="{cfg.infer.api_key}",\n' if cfg.infer.api_key.strip() else ""
        sdk = (
            "from openai import OpenAI\n\n"
            f"client = OpenAI(\n    base_url=\"{base}/v1\",\n"
            f"{api_key_line})\n\n"
            "resp = client.chat.completions.create(\n"
            f"    model=\"{model}\",\n"
            "    messages=[{\"role\": \"user\", \"content\": \"你好\"}],\n)\n"
            "print(resp.choices[0].message.content)"
        )
        return {
            "base_url": base,
            "chat_url": f"{base}/v1/chat/completions",
            "systemone_url": f"{base}/v1/systemone",
            "models_url": f"{base}/v1/models",
            "model": model,
            "api_key_set": bool(cfg.infer.api_key.strip()),
            "lan_open": cfg.host in ("0.0.0.0", "::"),
            "lan_base_url": lan_base,
            "state": self.engine.state,
            "curl_example": curl,
            "sdk_example": sdk,
        }

    # ───────────────────────── 向导 ─────────────────────────

    def wizard_packs(self) -> dict:
        """向导引擎步骤数据：目录 + 推荐标记 + 硬件检测结果。"""
        self.adopt_engine()
        gpus = gpu.detect()
        best_gpu = gpu.best(gpus)
        preset = presets.recommend(best_gpu.vram_gb if best_gpu else 0,
                                   bool(best_gpu and best_gpu.is_nvidia))
        rec = catalog.recommend(best_gpu)
        return {
            "gpus": [g.to_dict() for g in gpus],
            "best_gpu": best_gpu.to_dict() if best_gpu else None,
            "preset": preset.to_dict(),
            "engine_present": downloader.is_engine_present(self.engine_dir()),
            "packs": [{**p.to_dict(), "target_dir": str(self.engine_dir_for(p))}
                      for p in catalog.PACKS],
            "recommended_pack_id": rec.pack_id,
            "repo_tag": catalog.REPO_TAG,
        }

    def wizard_engine_offline(self, dir_path: str) -> dict:
        if not downloader.is_engine_present(dir_path):
            return {"ok": False, "error": "所选目录中没有 llama-server.exe，请重新选择。"}
        self.cfg.engine_dir = str(Path(dir_path).resolve())
        save_config(self.cfg)
        return {"ok": True}

    def wizard_finish(self, port: int, default_model_path: str,
                      context_tokens: int | None = None,
                      kv_level: str | None = None,
                      preset: dict | None = None) -> dict:
        cfg = self.cfg
        if not (1024 <= port <= 65535):
            return {"ok": False, "error": "端口需在 1024–65535 之间"}
        cfg.port = port
        if default_model_path:
            cfg.default_model_path = str(Path(default_model_path).resolve())
        p = None
        if isinstance(preset, dict) and preset.get("context_tokens"):
            p = presets.Preset(str(preset.get("label", "")),
                               int(preset["context_tokens"]),
                               str(preset.get("kv_level", cfg.infer.kv_level)),
                               str(preset.get("note", "")))
        elif context_tokens or kv_level:
            p = presets.Preset("", int(context_tokens or cfg.infer.context_tokens),
                               str(kv_level or cfg.infer.kv_level), "")
        if p is not None:
            cfg.infer.context_tokens = p.context_tokens
            cfg.infer.kv_level = p.kv_level
        # 目录扫描里发现的可用模型落库
        ok_models = [m for m in self.scan_models() if m.ok]
        cfg.models = [ModelEntry(path=m.file_path).__dict__ for m in ok_models]
        cfg.wizard_done = True
        save_config(self.cfg)
        return {"ok": True}

    # ───────────────────────── 设置 ─────────────────────────

    def settings_payload(self) -> dict:
        cfg = self.cfg
        # engine_mode：设置页「引擎后端」勾选值（auto / cuda / vulkan / cpu）。
        # 指针指向的目录里真有引擎才算钉定；目录缺失时实际行为已是自动接管，
        # 如实标 auto，保存时顺便把死指针清掉。
        pinned = bool(cfg.engine_dir) and downloader.is_engine_present(cfg.engine_dir)
        found = self.locate_engine()
        return {
            "port": cfg.port,
            "host": cfg.host,
            "engine_dir": cfg.engine_dir,
            "engine_mode": catalog.detect_backend(cfg.engine_dir) if pinned else "auto",
            "engine_backend": catalog.detect_backend(found) if found else "",
            "default_model_path": cfg.default_model_path,
            "mmproj_path": cfg.mmproj_path,
            "infer": {
                "context_tokens": cfg.infer.context_tokens,
                "kv_level": cfg.infer.kv_level,
                "flash_attention": cfg.infer.flash_attention,
                "ngl": cfg.infer.ngl,
                "threads": cfg.infer.threads,
                "ubatch": cfg.infer.ubatch,
                "parallel": cfg.infer.parallel,
                "mtp_steps": cfg.infer.mtp_steps,
                "api_key": cfg.infer.api_key,
                "extra_args": cfg.infer.extra_args,
            },
            "run": {
                "auto_trim_ram": cfg.run.auto_trim_ram,
                "trim_delay_seconds": cfg.run.trim_delay_seconds,
                "auto_restart_on_crash": cfg.run.auto_restart_on_crash,
            },
        }

    def save_settings(self, data: dict) -> dict:
        cfg = self.cfg
        busy = self.engine.is_busy
        if busy and (data.get("port") not in (None, cfg.port)
                     or data.get("host") not in (None, cfg.host)):
            return {"ok": False, "error": "服务运行中不能修改监听地址/端口，请先停止服务"}

        # 引擎后端切换（auto / cuda / vulkan / cpu）：只改指针，重启服务生效
        backend = data.get("engine_backend")
        if backend:
            result = self.switch_backend(str(backend))
            if not result.get("ok"):
                return result

        if "port" in data and data["port"] is not None:
            if not (1024 <= int(data["port"]) <= 65535):
                return {"ok": False, "error": "端口需在 1024–65535 之间"}
            cfg.port = int(data["port"])
        if "host" in data and data["host"] is not None:
            if data["host"] not in ("127.0.0.1", "0.0.0.0"):
                return {"ok": False, "error": "host 仅支持 127.0.0.1 / 0.0.0.0"}
            cfg.host = data["host"]
        if "engine_dir" in data and data["engine_dir"] is not None:
            cfg.engine_dir = str(data["engine_dir"]).strip()
        if "default_model_path" in data and data["default_model_path"]:
            cfg.default_model_path = str(Path(data["default_model_path"]).resolve())
        if "mmproj_path" in data and data["mmproj_path"] is not None:
            cfg.mmproj_path = str(data["mmproj_path"]).strip()
        infer = data.get("infer") or {}
        _apply_fields(cfg.infer, infer, {
            "context_tokens": int, "kv_level": str, "flash_attention": bool,
            "ngl": int, "threads": int, "ubatch": int, "parallel": int,
            "mtp_steps": int, "api_key": str, "extra_args": str,
        })
        run = data.get("run") or {}
        _apply_fields(cfg.run, run, {
            "auto_trim_ram": bool, "trim_delay_seconds": int,
            "auto_restart_on_crash": bool,
        })
        save_config(self.cfg)
        return {"ok": True}

    def logs_after(self, last_id: int) -> list:
        return self.logs.after(last_id)

    def shutdown(self) -> None:
        """应用退出时调用：确保 llama-server 子进程被回收。"""
        try:
            self.engine.stop()
        except Exception as e:
            logger.warning("llama 引擎停止失败：%s", e)


def _apply_fields(obj, data: dict, types: dict) -> None:
    for k, t in types.items():
        if k in data and data[k] is not None:
            try:
                setattr(obj, k, t(data[k]))
            except (TypeError, ValueError):
                logger.warning("设置字段 %s 类型转换失败：%r", k, data[k])


_instance: LlamaRuntime | None = None
_instance_lock = threading.Lock()


def runtime() -> LlamaRuntime:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LlamaRuntime()
        return _instance


def shutdown() -> None:
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.shutdown()
