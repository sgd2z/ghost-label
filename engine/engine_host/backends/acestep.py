"""ACE-Step 1.5 (https://github.com/ace-step/ACE-Step-1.5), through its Python API:
AceStepHandler.initialize_service, LLMHandler.initialize and acestep.inference.generate_music
(docs/en/INFERENCE.md in that repo).

ACE-Step is not pip-installed from PyPI; point the backend at a clone with ACESTEP_PROJECT_ROOT
(or --set root=...). Its own environment variables keep their meaning (ACESTEP_CHECKPOINTS_DIR,
ACESTEP_ROCM_DTYPE, ACESTEP_CONFIG_PATH, ACESTEP_LM_MODEL_PATH, ACESTEP_LM_BACKEND, ...).

Options (--set key=value, config "options", or the environment variable shown):

    root               ACESTEP_PROJECT_ROOT      ACE-Step clone (added to sys.path; models go in <root>/checkpoints)
    config_path        ACESTEP_CONFIG_PATH       DiT model, default acestep-v15-turbo (the app's Model field wins)
    lm_model_path      ACESTEP_LM_MODEL_PATH     5 Hz LM planner, default acestep-5Hz-lm-1.7B
    init_llm           ACESTEP_INIT_LLM          load the LM planner (default true)
    lm_backend         ACESTEP_LM_BACKEND        pt | vllm | mlx (default: mlx on Apple silicon, vllm on NVIDIA, else pt)
    device             MUSIC_ENGINE_DEVICE, ACESTEP_DEVICE   auto | cuda | mps | xpu | cpu (ROCm GPUs are "cuda")
    offload_to_cpu     ACESTEP_OFFLOAD_TO_CPU    default false
    offload_dit_to_cpu ACESTEP_OFFLOAD_DIT_TO_CPU default false
    steps_map          MUSIC_ENGINE_ACESTEP_STEPS_MAP   app quality -> diffusion steps, e.g. "8:8,16:12,32:20"
    shift              MUSIC_ENGINE_ACESTEP_SHIFT       timestep shift (default 3.0 for turbo models, else 1.0)
    guidance_scale     MUSIC_ENGINE_ACESTEP_GUIDANCE    CFG for base/sft models (default 7.0; turbo ignores it)
    duration           MUSIC_ENGINE_ACESTEP_DURATION    auto (model/LM picks, cut at maxSeconds) | max (ask for maxSeconds)
    thinking           MUSIC_ENGINE_ACESTEP_THINKING    auto (on unless the app's planning is "off") | on | off
    cot_caption        MUSIC_ENGINE_ACESTEP_COT_CAPTION let the LM rewrite the style caption (default false)
    cot_metas          MUSIC_ENGINE_ACESTEP_COT_METAS   let the LM fill bpm/key/time signature/duration (default true)
    cot_language       MUSIC_ENGINE_ACESTEP_COT_LANGUAGE let the LM detect the vocal language (default true)
    vocal_language     MUSIC_ENGINE_ACESTEP_LANGUAGE    ISO 639-1 code, default "unknown" (auto)
    max_seconds        MUSIC_ENGINE_ACESTEP_MAX_SECONDS cap (default: ACE-Step's own per-GPU limit, at most 600)
"""

from __future__ import annotations

import os
import sys
import time

from engine_host.backends import Audio, Backend, Cancelled, Capabilities
from engine_host.devices import describe_torch_device, pick_torch_device

DEFAULT_CONFIG = "acestep-v15-turbo"
# ACE-Step docs/en/INFERENCE.md: turbo 1-20 steps (8 recommended); base 1-200 (32-64 recommended).
TURBO_STEPS = {8: 8, 16: 12, 32: 20}
BASE_STEPS = {8: 32, 16: 48, 32: 64}


def find_acestep_root(configured=None):
    """The ACE-Step-1.5 folder to put on sys.path: the configured one (option/ACESTEP_PROJECT_ROOT)
    if it holds the package, else the Ghost Label install layout (GHOSTLABEL_ENGINE_ROOT, or the
    folder next to this venv: <Engine>/venv -> <Engine>/ACE-Step-1.5), else the cwd. Returns
    (root or None, [paths checked]). None also when acestep is already importable (pip/.pth)."""
    import importlib.util

    candidates = []
    if configured:
        candidates.append(os.path.abspath(os.path.expanduser(configured)))
    if os.environ.get("GHOSTLABEL_ENGINE_ROOT"):
        candidates.append(os.path.join(os.environ["GHOSTLABEL_ENGINE_ROOT"], "ACE-Step-1.5"))
    prefix = getattr(sys, "prefix", "") or ""
    if prefix:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(prefix)), "ACE-Step-1.5"))
    candidates.append(os.getcwd())
    seen = []
    for path in candidates:
        if path in seen:
            continue
        seen.append(path)
        if os.path.isfile(os.path.join(path, "acestep", "__init__.py")):
            return path, seen
    try:
        if importlib.util.find_spec("acestep") is not None:
            return None, seen
    except (ImportError, ValueError):
        pass
    return None, seen


class AceStepBackend(Backend):
    name = "acestep"

    def __init__(self, options=None, log=print):
        super().__init__(options, log)
        self.dit = None
        self.llm = None
        self.config_path = None
        self.loaded_key = None
        self.device = None
        self.max_seconds = None

    # MARK: options

    def o(self, key, default=None, *env):
        return self.option(key, default, list(env))

    def ob(self, key, default, *env):
        return self.option_bool(key, default, list(env))

    @property
    def is_turbo(self) -> bool:
        return "turbo" in (self.config_path or DEFAULT_CONFIG).lower()

    @property
    def steps_map(self):
        return TURBO_STEPS if self.is_turbo else BASE_STEPS

    def map_steps(self, quality: int) -> int:
        if "steps_map" not in self.options and os.environ.get("MUSIC_ENGINE_ACESTEP_STEPS_MAP"):
            self.options["steps_map"] = os.environ["MUSIC_ENGINE_ACESTEP_STEPS_MAP"]
        return super().map_steps(quality)

    def capabilities(self) -> Capabilities:
        return Capabilities(max_seconds=self.max_seconds or 600.0, supports_lyrics=True,
                            cooperative_cancel=True, reports_progress=True,
                            seconds_per_song_second=1.0 / 1.8,
                            analyze=getattr(self, "llm", None) is not None, reference_audio=True,
                            languages=self.languages())

    def languages(self):
        try:
            from acestep.constants import VALID_LANGUAGES
            return [code for code in VALID_LANGUAGES if code != "unknown"]
        except Exception:  # noqa: BLE001 — not installed yet
            return []

    # MARK: load

    def resolve_model(self, model_path):
        """The app's Model field: a DiT folder (has config.json), a checkpoints folder, or a DiT
        name such as acestep-v15-turbo / acestep-v15-xl-turbo / acestep-v15-base."""
        config = self.o("config_path", DEFAULT_CONFIG, "ACESTEP_CONFIG_PATH")
        checkpoints = None
        if model_path:
            if os.path.isdir(model_path) and os.path.isfile(os.path.join(model_path, "config.json")):
                checkpoints = os.path.dirname(os.path.abspath(model_path))
                config = os.path.basename(os.path.abspath(model_path))
            elif os.path.isdir(model_path):
                if looks_like_checkpoints(model_path):
                    checkpoints = os.path.abspath(model_path)
                else:  # some other folder: don't download 10 GB into it
                    self.log(f"[acestep] {model_path!r} is not a DiT or checkpoints folder; using {config!r}")
            else:
                config = model_path
        return config, checkpoints

    def is_loaded_for(self, model_path):
        return self.dit is not None and self.loaded_key == self.resolve_model(model_path)

    def preload(self):
        """Before the host reads stdin: scipy's native modules (see engine_host/native.py)."""
        from engine_host import native
        native.preload(lambda text: self.log("[acestep] " + text))

    def load(self, model_path):
        config, checkpoints = self.resolve_model(model_path)
        if self.dit is not None and self.loaded_key == (config, checkpoints):
            return
        root, checked = find_acestep_root(self.o("root", None, "ACESTEP_PROJECT_ROOT"))
        if root:
            os.environ["ACESTEP_PROJECT_ROOT"] = root
            if root not in sys.path:
                sys.path.insert(0, root)
        if checkpoints:
            os.environ["ACESTEP_CHECKPOINTS_DIR"] = checkpoints
        # From ACE-Step's start_gradio_ui_rocm.bat: fast MIOpen kernel search (otherwise the first
        # VAE decode can hang for minutes) and no tokenizer thread pool. Harmless elsewhere.
        os.environ.setdefault("MIOPEN_FIND_MODE", "FAST")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

        from engine_host import native
        say = lambda text: self.log("[acestep] " + text)  # noqa: E731
        self.status("Loading native libraries (scipy, before PyTorch)")
        native.preload(say)
        self.status("Importing ACE-Step and PyTorch")
        try:
            done = native.step(say, "import torch")
            import torch  # noqa: F401
            done(f"(torch {getattr(torch, '__version__', '?')}, hip {getattr(torch.version, 'hip', None)})")
            done = native.step(say, "import acestep.handler")
            from acestep.handler import AceStepHandler
            done()
            done = native.step(say, "import acestep.llm_inference")
            from acestep.llm_inference import LLMHandler
            done()
            done = native.step(say, "import acestep.model_downloader")
            from acestep.model_downloader import DEFAULT_LM_MODEL, get_checkpoints_dir
            done()
        except ImportError as error:
            looked = "; ".join(checked) or "nowhere"
            raise RuntimeError(
                f"ACE-Step is not importable ({error}) with Python {sys.executable}. Looked for an "
                f"ACE-Step-1.5 folder in: {looked}. In Ghost Label, open Settings › Music engine and "
                "press Repair engine; otherwise set ACESTEP_PROJECT_ROOT to your ACE-Step-1.5 clone "
                "and run the engine with that environment's python.") from error
        import torch

        self.status("Starting the GPU")
        done = native.step(say, "pick and initialise the torch device")
        device = pick_torch_device(self.o("device", None, "MUSIC_ENGINE_DEVICE", "ACESTEP_DEVICE"))
        self.log("[acestep] " + describe_torch_device(device))
        done(device)
        project_root = os.environ.get("ACESTEP_PROJECT_ROOT") or os.getcwd()
        offload = self.ob("offload_to_cpu", False, "ACESTEP_OFFLOAD_TO_CPU")
        offload_dit = self.ob("offload_dit_to_cpu", False, "ACESTEP_OFFLOAD_DIT_TO_CPU")

        init_llm = self.ob("init_llm", True, "ACESTEP_INIT_LLM")
        lm_model = self.o("lm_model_path", DEFAULT_LM_MODEL, "ACESTEP_LM_MODEL_PATH") if init_llm else None
        if self.ob("vram_check", True, "MUSIC_ENGINE_VRAM_CHECK") and not offload:
            self.status("Checking free GPU memory")
            check_vram(torch, device, get_checkpoints_dir(), config, lm_model, self.log)

        start = time.time()
        done = native.step(say, "create AceStepHandler")
        dit = AceStepHandler()
        done()
        use_flash = False
        try:
            use_flash = bool(dit.is_flash_attention_available(device))
        except Exception:  # noqa: BLE001
            pass
        self.log(f"[acestep] loading DiT {config!r} (checkpoints: {get_checkpoints_dir()}) ...")
        self.status("Loading the music model into GPU memory (the first time takes a while)")
        done = native.step(say, "initialize_service (DiT, VAE, text encoder)")
        status, ok = dit.initialize_service(
            project_root=project_root, config_path=config, device=device,
            use_flash_attention=use_flash, compile_model=False,
            offload_to_cpu=offload, offload_dit_to_cpu=offload_dit)
        done(str(ok))
        if not ok:
            raise RuntimeError(f"ACE-Step DiT failed to load: {status}")
        self.log(f"[acestep] DiT ready in {time.time() - start:.1f}s")

        llm = None
        if init_llm:
            lm_backend = self.o("lm_backend", None, "ACESTEP_LM_BACKEND") or default_lm_backend(device, torch)
            done = native.step(say, f"check/download LM {lm_model}")
            ensure_lm(lm_model, self.log)
            done()
            self.log(f"[acestep] loading LM {lm_model!r} (backend {lm_backend}) ...")
            self.status(f"Loading the song planner ({lm_model})")
            llm = LLMHandler()
            done = native.step(say, "LLMHandler.initialize")
            status, ok = llm.initialize(
                checkpoint_dir=str(get_checkpoints_dir()), lm_model_path=lm_model,
                backend=lm_backend, device=device, offload_to_cpu=offload, dtype=None)
            done(str(ok))
            if not ok:
                raise RuntimeError(f"ACE-Step LM planner failed to load: {status}")
        self.dit, self.llm, self.device = dit, llm, device
        self.install_counters(dit, llm)
        self.config_path = config
        self.loaded_key = (config, checkpoints)
        self.max_seconds = self.resolve_max_seconds(llm is not None)

    # MARK: real progress counters

    def install_counters(self, dit, llm):
        """Count what actually runs: one DiT decoder pass per diffusion step (the Heun corrector
        pass, use_cache=False, isn't a step) and one LM forward per generated token (pt backend)."""
        self.counters = {"dit": 0, "lm": 0}
        self.steps_total = 0
        self.phase_desc = ""
        decoder = getattr(getattr(dit, "model", None), "decoder", None)
        if decoder is not None and hasattr(decoder, "register_forward_pre_hook"):
            def on_decoder(_module, _args, kwargs):
                if kwargs.get("use_cache") is not False:
                    self.counters["dit"] += 1
            try:
                decoder.register_forward_pre_hook(on_decoder, with_kwargs=True)
            except TypeError:  # torch < 2.0
                decoder.register_forward_pre_hook(lambda m, a: self.counters.__setitem__("dit", self.counters["dit"] + 1))
        lm = getattr(llm, "llm", None) if llm is not None else None
        if lm is not None and hasattr(lm, "register_forward_pre_hook"):
            lm.register_forward_pre_hook(lambda m, a: self.counters.__setitem__("lm", self.counters["lm"] + 1))
        self.install_decode_hooks(dit)

    def install_decode_hooks(self, dit):
        """The VAE decode after diffusion: ACE-Step reports one fixed value for it (and its
        diffusion bar is a time estimate that tops out near 97%), so we watch the calls
        themselves: decode start, each tiled chunk (and how many there are), and the device."""
        self.decode = None
        vae = getattr(dit, "vae", None)
        if vae is None or not callable(getattr(vae, "decode", None)):
            return
        original = vae.decode
        backend = self

        def decode(*args, **kwargs):
            backend.on_decode_chunk(vae)
            started = time.time()
            try:
                return original(*args, **kwargs)
            finally:
                d = backend.decode
                if d is not None:
                    d["done"] += 1
                    d["last"] = time.time() - started
                    backend.log(f"[acestep] decoded audio chunk {d['done']}"
                                f"{'/' + str(d['total']) if d.get('total') else ''} in {d['last']:.1f} s on {d['device']}")
                    backend.report_decode()
        vae.decode = decode

        for name in ("_tiled_decode_gpu", "_tiled_decode_offload_cpu"):
            fn = getattr(dit, name, None)
            if not callable(fn):
                continue

            def tiled(*args, _fn=fn, **kwargs):
                steps = kwargs.get("num_steps", args[-1] if args else None)
                if isinstance(steps, int):
                    backend.on_decode_chunk(vae, total=steps)
                return _fn(*args, **kwargs)
            setattr(dit, name, tiled)

    def on_decode_chunk(self, vae, total=None):
        if self.decode is not None and total and not self.decode.get("total"):
            self.decode["total"] = total
        if self.decode is None:
            try:
                device = next(vae.parameters()).device.type
            except Exception:  # noqa: BLE001
                device = "?"
            self.decode = {"started": time.time(), "done": 0, "total": total, "device": device}
            where = "on the CPU (GPU memory was nearly full)" if device == "cpu" else ""
            self.log(f"[acestep] diffusion finished; decoding the audio {where or 'on ' + device}")
            self.report_decode()

    def report_decode(self):
        progress = getattr(self, "_progress", None)
        d = self.decode
        if progress and d is not None:
            total = d.get("total") or 0
            progress("rendering", (d["done"] / total) if total else 0.0)

    def live(self):
        counters = getattr(self, "counters", None)
        if not counters:
            return {}
        d = getattr(self, "decode", None)
        if d is not None:
            out = {"detail": "Decoding the audio" + (" on the CPU" if d["device"] == "cpu" else ""),
                   "stageElapsed": round(time.time() - d["started"], 1)}
            if d.get("total"):
                out.update(chunk=min(d["done"] + 1, d["total"]), chunks=d["total"])
            return out
        out = {}
        if self.phase_desc:
            out["detail"] = self.phase_desc
        if counters["dit"]:
            total = self.steps_total or counters["dit"]
            out.update(step=min(counters["dit"], total), steps=total)
        elif counters["lm"]:
            out["tokens"] = counters["lm"]
        return out

    def resolve_max_seconds(self, with_lm: bool) -> float:
        explicit = self.o("max_seconds", None, "MUSIC_ENGINE_ACESTEP_MAX_SECONDS")
        if explicit:
            return float(explicit)
        limit = 600.0
        try:
            from acestep.constants import DURATION_MAX
            from acestep.gpu_config import get_gpu_config
            limit = float(DURATION_MAX)
            gpu = get_gpu_config()
            limit = float(gpu.max_duration_with_lm if with_lm else gpu.max_duration_without_lm)
        except Exception as error:  # noqa: BLE001
            self.log(f"[acestep] couldn't read ACE-Step's duration limit ({error!r}); using {limit:.0f}s")
        return min(limit, 600.0)

    # MARK: generate

    def build(self, request):
        from acestep.inference import GenerationConfig, GenerationParams

        thinking_mode = (self.o("thinking", "auto", "MUSIC_ENGINE_ACESTEP_THINKING") or "auto").lower()
        thinking = {"on": True, "true": True, "1": True, "off": False, "false": False, "0": False}.get(
            thinking_mode, request.planning != "off")
        has_lm = self.llm is not None
        duration_mode = (self.o("duration", "auto", "MUSIC_ENGINE_ACESTEP_DURATION") or "auto").lower()
        duration = -1.0 if duration_mode == "auto" else max(10.0, min(request.max_seconds, 600.0))
        shift_default = "3.0" if self.is_turbo else "1.0"
        caption = caption_with_voice(request.style, None if request.instrumental else request.voice)
        cot_caption = has_lm and self.ob("cot_caption", False, "MUSIC_ENGINE_ACESTEP_COT_CAPTION")
        self.log(f"[acestep] {request.id}: caption sent to ACE-Step: {caption}")
        if cot_caption:
            self.log("[acestep] cot_caption is on: ACE-Step's LM may rewrite this caption (the singer can change)")
        params = GenerationParams(
            task_type="text2music",
            caption=caption,
            lyrics="[Instrumental]" if request.instrumental else request.lyrics,
            instrumental=request.instrumental,
            vocal_language=request.vocal_language or self.o("vocal_language", "unknown", "MUSIC_ENGINE_ACESTEP_LANGUAGE"),
            reference_audio=request.reference_audio,
            bpm=request.bpm,
            keyscale=request.keyscale or "",
            duration=duration,
            inference_steps=request.steps,
            seed=request.seed,
            guidance_scale=float(self.o("guidance_scale", "7.0", "MUSIC_ENGINE_ACESTEP_GUIDANCE")),
            shift=float(self.o("shift", shift_default, "MUSIC_ENGINE_ACESTEP_SHIFT")),
            thinking=thinking and has_lm,
            use_cot_caption=cot_caption,
            use_cot_metas=has_lm and self.ob("cot_metas", True, "MUSIC_ENGINE_ACESTEP_COT_METAS"),
            use_cot_language=has_lm and self.ob("cot_language", True, "MUSIC_ENGINE_ACESTEP_COT_LANGUAGE"),
        )
        config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[request.seed])
        return params, config

    def generate(self, request, progress, cancelled):
        from acestep.inference import generate_music

        params, config = self.build(request)
        self.log(f"[acestep] {request.id}: steps={params.inference_steps} seed={params.seed} "
                 f"duration={params.duration} thinking={params.thinking} shift={params.shift}")

        if getattr(self, "counters", None) is not None:
            self.counters.update(dit=0, lm=0)
        self.steps_total = int(params.inference_steps or 0)
        self.phase_desc = ""
        self.decode = None
        self._progress = progress

        def on_progress(*args, **kwargs):
            # ACE-Step's own scale: LM phase 1 ~0.1, phase 2 ~0.5, DiT 0.51-0.8, VAE decode 0.8-0.99.
            if cancelled():
                raise Cancelled()
            value = args[0] if args else kwargs.get("value", kwargs.get("progress"))
            desc = kwargs.get("desc") or (args[1] if len(args) > 1 and isinstance(args[1], str) else None)
            if desc:
                self.phase_desc = str(desc).strip()[:120]
            if not isinstance(value, (int, float)):
                return
            value = float(min(max(value, 0.0), 1.0))
            if self.decode is not None:
                return  # decode progress comes from the chunk hooks, not ACE-Step's fixed value
            if value < 0.5:
                progress("planning", value / 0.5, value)
            elif value < 0.8:
                # ACE-Step's diffusion value is a time estimate; the real steps come from the hook.
                done = (getattr(self, "counters", None) or {}).get("dit", 0)
                if done and self.steps_total:
                    progress("composing", min(done / self.steps_total, 1.0))
                else:
                    progress("composing", (value - 0.5) / 0.3, value)
            else:
                progress("rendering", 0.0)

        result = generate_music(self.dit, self.llm, params, config, save_dir=None, progress=on_progress)
        if cancelled():
            raise Cancelled()
        if not result.success or not result.audios:
            raise RuntimeError(result.error or result.status_message or "ACE-Step returned no audio")
        first = result.audios[0]
        if first.get("tensor") is None:
            raise RuntimeError("ACE-Step returned no audio tensor")
        return Audio(first["tensor"], int(first.get("sample_rate") or 48000))

    def analyze(self, audio_path):
        """ACE-Step's own audio understanding: audio -> semantic codes (DiT handler) -> 5 Hz LM
        `understand_music` (caption, lyrics, BPM, key, language, time signature)."""
        if self.llm is None:
            raise RuntimeError("Audio analysis needs ACE-Step's LM planner (init_llm=true).")
        from acestep.inference import understand_music
        codes = self.dit.convert_src_audio_to_codes(audio_path)
        if not codes or str(codes).startswith("\u274c"):
            raise RuntimeError(f"ACE-Step couldn't read the audio: {codes}")
        result = understand_music(llm_handler=self.llm, audio_codes=codes)
        if not getattr(result, "success", True):
            raise RuntimeError(getattr(result, "error", None) or "ACE-Step couldn't analyze the audio")
        keys = ["caption", "lyrics", "bpm", "duration", "keyscale", "language", "timesignature"]
        return {k: getattr(result, k, None) for k in keys}


def caption_with_voice(style, voice):
    """ACE-Step has no separate voice input: the vocal description goes in the caption. Appended
    unless the style already contains it verbatim."""
    style = (style or "").strip().rstrip(",")
    voice = (voice or "").strip().strip(",")
    if not voice or voice.lower() == "instrumental" or voice.lower() in style.lower():
        return style
    return f"{style}, {voice}" if style else voice


def _weights_bytes(folder):
    """(bytes on disk, bytes per parameter) of the safetensors in a folder, from their headers."""
    import json as _json
    import struct
    size = 0
    widths = {}
    for dirpath, _dirs, files in os.walk(folder):
        for f in files:
            if not f.endswith(".safetensors"):
                continue
            path = os.path.join(dirpath, f)
            size += os.path.getsize(path)
            try:
                with open(path, "rb") as fh:
                    n = struct.unpack("<Q", fh.read(8))[0]
                    header = _json.loads(fh.read(min(n, 50_000_000)))
                for k, v in header.items():
                    if k != "__metadata__" and isinstance(v, dict):
                        dt = v.get("dtype", "")
                        widths[dt] = widths.get(dt, 0) + 1
            except Exception:  # noqa: BLE001
                pass
    common = max(widths, key=widths.get) if widths else "BF16"
    return size, {"F32": 4, "F16": 2, "BF16": 2, "F8_E4M3": 1}.get(common, 2)


def estimate_vram(checkpoints, config, lm_model, dit_dtype_bytes):
    """Bytes ACE-Step needs on the GPU: DiT, VAE and text encoder at the handler's dtype, the LM
    in bf16 (ACE-Step's default for CUDA/ROCm), plus working memory for one song."""
    need = 0
    for part in (config, "vae", "Qwen3-Embedding-0.6B"):
        folder = os.path.join(checkpoints, part) if part and not os.path.isabs(part) else part
        if folder and os.path.isdir(folder):
            size, width = _weights_bytes(folder)
            need += size * dit_dtype_bytes / width
    if lm_model:
        folder = lm_model if os.path.isabs(lm_model) else os.path.join(checkpoints, lm_model)
        if os.path.isdir(folder):
            size, width = _weights_bytes(folder)
            need += size * 2 / width
    return need + 3 * 2**30


def check_vram(torch, device, checkpoints, config, lm_model, log):
    """Fail fast with the shortfall instead of hanging or spilling into shared memory when another
    program (often a local LLM server) holds the GPU."""
    if not str(device).startswith("cuda") or not hasattr(torch.cuda, "mem_get_info"):
        return
    try:
        free, total = torch.cuda.mem_get_info()
    except Exception as error:  # noqa: BLE001
        log(f"[acestep] couldn't read free GPU memory ({error!r}); skipping the check")
        return
    rocm = bool(getattr(torch.version, "hip", None))
    dit_bytes = {"float32": 4, "float16": 2, "bfloat16": 2}.get(
        (os.environ.get("ACESTEP_ROCM_DTYPE") or "float32").lower(), 4) if rocm else 2
    need = estimate_vram(str(checkpoints), config or "acestep-v15-turbo", lm_model, dit_bytes)
    gib = 2**30
    log(f"[acestep] GPU memory: {free / gib:.1f} GiB free of {total / gib:.1f} GiB; ACE-Step needs about {need / gib:.1f} GiB")
    if free < need:
        raise RuntimeError(
            f"Not enough free GPU memory for ACE-Step: it needs about {need / gib:.0f} GB, but only "
            f"{free / gib:.1f} GB of {total / gib:.0f} GB is free. Another program is holding GPU memory "
            "(for example LM Studio or Ollama with a model loaded). Unload that model, then press play "
            "again. (To try anyway, add vram_check=false to the engine's backend options.)")


def looks_like_checkpoints(folder: str) -> bool:
    """An ACE-Step checkpoints folder holds vae/ or model folders with a config.json."""
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return "vae" in names or any(
        os.path.isfile(os.path.join(folder, n, "config.json")) for n in names if n.startswith("acestep-"))


def ensure_lm(name: str, log) -> None:
    """Download a registered LM (e.g. acestep-5Hz-lm-4B) the way ACE-Step's cli.py does; the
    default 1.7B LM ships with the main model."""
    from acestep.model_downloader import (SUBMODEL_REGISTRY, check_model_exists, ensure_lm_model,
                                          get_checkpoints_dir)
    if os.path.isabs(name) and os.path.exists(name):
        return
    checkpoints = get_checkpoints_dir()
    if check_model_exists(name, checkpoints) or name not in SUBMODEL_REGISTRY:
        return
    log(f"[acestep] downloading LM {name!r} into {checkpoints} ...")
    ok, message = ensure_lm_model(name, checkpoints_dir=checkpoints)
    if not ok:
        raise RuntimeError(f"Couldn't download LM {name!r}: {message}")


def default_lm_backend(device: str, torch) -> str:
    """ACE-Step's own launchers: mlx on Apple silicon (start_*_macos.sh), pt on ROCm
    (start_*_rocm.bat/.sh), vllm (nano-vllm) on NVIDIA CUDA; pt anywhere else."""
    if device == "mps":
        return "mlx"
    if device.startswith("cuda") and not getattr(torch.version, "hip", None):
        return "vllm"
    return "pt"
