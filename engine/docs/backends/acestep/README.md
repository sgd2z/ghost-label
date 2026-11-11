# ACE-Step 1.5 backend

[ACE-Step 1.5](https://github.com/ace-step/ACE-Step-1.5) is an MIT-licensed text-to-music model.
It has a DiT (diffusion transformer) and an optional 5 Hz language-model "planner" that fills in
BPM, key and duration and writes audio codes. The backend calls ACE-Step's Python API as described
in its [`docs/en/INFERENCE.md`](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INFERENCE.md):
`AceStepHandler.initialize_service(...)`, `LLMHandler.initialize(...)` and
`acestep.inference.generate_music(dit, llm, GenerationParams, GenerationConfig, save_dir=None, progress=cb)`.

ACE-Step isn't on PyPI. Clone it, and point the backend at the clone with `ACESTEP_PROJECT_ROOT`
or `--set root=...`. Don't `pip install -e` it on AMD: its `pyproject.toml` pins CUDA builds of
torch on Windows and Linux.

Setup guides: **[Windows + AMD ROCm (gfx1151)](windows-rocm.md)** · [Linux](linux.md) · [macOS](macos.md)

## How a song maps onto ACE-Step

| Standard Music | ACE-Step | Default |
|---|---|---|
| `style` | `GenerationParams.caption` | Passed as-is. ACE-Step documents a 512-character limit. |
| `lyrics` (section tags such as `[Verse]` and `[Chorus]`) | `lyrics` | Passed as-is. |
| `isInstrumental` | `instrumental=True`, `lyrics="[Instrumental]"` | |
| `seed` | `seed`, plus `GenerationConfig(seeds=[seed], use_random_seed=False, batch_size=1)` | `seed % 2**31` |
| `steps` 8 / 16 / 32 | `inference_steps` | Turbo models: 8 / 12 / 20 (ACE-Step: turbo 1–20, 8 recommended). Base/SFT: 32 / 48 / 64 (32–64 recommended). `steps_map` overrides. |
| — | `shift` | 3.0 for turbo models, as ACE-Step recommends; otherwise 1.0. |
| — | `guidance_scale` | 7.0. Turbo pipelines replace it with 1.0. |
| `maxSeconds` | `duration` | `duration=auto` (the default) sends `-1`, so the LM or the lyrics length sets it, and the host cuts and fades at `maxSeconds`. `duration=max` asks for `maxSeconds`, clamped to 10–600 s. |
| capability `max_seconds` | — | ACE-Step's own per-GPU limit from `gpu_config.get_gpu_config()` (`max_duration_with_lm` / `max_duration_without_lm`), at most 600 s (`DURATION_MAX`). |
| `planning` | `thinking` (LM planner on or off) | `thinking=auto` turns the planner on unless the app sends `planning: "off"`. |
| — | `use_cot_caption` | **false**: the app's style stays the caption. Set `cot_caption=true` to let the LM rewrite it. |
| — | `use_cot_metas`, `use_cot_language` | true: the LM fills BPM, key, time signature and duration, and detects the vocal language. |
| `title`, `artist`, `voice`, `maxSemanticTokens` | — | Not used. The voice is already part of `style`. |

Progress: ACE-Step's callback runs from 0 to 1. LM phase 1 is about 0.1, phase 2 about 0.5,
DiT steps run 0.51–0.8 and VAE decoding 0.8–0.99. These map onto the protocol stages `planning`
(below 0.5), `composing` (0.5–0.8) and `rendering` (0.8 and up). The host adds `mastering` and
`encoding`.

Cancel: ACE-Step has no cancel API. The progress callback raises when a cancel arrives, and
`generate_music` stops at the next progress call that lets the error through. Some of the DiT
loop's progress comes from a timer thread that swallows errors
(`acestep/core/generation/handler/progress.py`), so a cancel can take until the next stage
boundary, at the latest the VAE-decode report.

## Options

Set options with `--set key=value`, in the config file's `options`, or through the environment
variable shown. ACE-Step's own variables keep their meaning.

| Option | Environment | Meaning |
|---|---|---|
| `root` | `ACESTEP_PROJECT_ROOT` | ACE-Step clone. It is added to `sys.path`, and models default to `<root>/checkpoints`. |
| — | `ACESTEP_CHECKPOINTS_DIR` | ACE-Step's own setting for a shared model folder. |
| `config_path` | `ACESTEP_CONFIG_PATH` | DiT model (default `acestep-v15-turbo`). The app's **Model** field wins. |
| `lm_model_path` | `ACESTEP_LM_MODEL_PATH` | LM planner (default `acestep-5Hz-lm-1.7B`, which ships with the main model). `acestep-5Hz-lm-0.6B` and `-4B` download on first use. |
| `init_llm` | `ACESTEP_INIT_LLM` | Set `false` to skip the LM (DiT only: faster, less memory). |
| `lm_backend` | `ACESTEP_LM_BACKEND` | `pt`, `vllm` or `mlx`. The default follows ACE-Step's launchers: `mlx` on Apple silicon, `pt` on ROCm and CPU, `vllm` on NVIDIA. |
| `device` | `MUSIC_ENGINE_DEVICE`, `ACESTEP_DEVICE` | `auto`, `cuda`, `mps`, `xpu` or `cpu`. AMD GPUs under ROCm are `cuda`. |
| — | `ACESTEP_ROCM_DTYPE` | ACE-Step's ROCm dtype. The default is `float32`, because its code notes `bfloat16` segfaults on Strix Halo iGPUs. Try `bfloat16` or `float16` at your own risk. |
| `offload_to_cpu` / `offload_dit_to_cpu` | `ACESTEP_OFFLOAD_TO_CPU` / `ACESTEP_OFFLOAD_DIT_TO_CPU` | Default false. Turn on for GPUs with little VRAM. |
| `steps_map` | `MUSIC_ENGINE_ACESTEP_STEPS_MAP` | For example `8:8,16:12,32:20`. |
| `shift`, `guidance_scale` | `MUSIC_ENGINE_ACESTEP_SHIFT`, `MUSIC_ENGINE_ACESTEP_GUIDANCE` | See the mapping table above. |
| `duration` | `MUSIC_ENGINE_ACESTEP_DURATION` | `auto` or `max`. |
| `thinking` | `MUSIC_ENGINE_ACESTEP_THINKING` | `auto`, `on` or `off`. |
| `cot_caption`, `cot_metas`, `cot_language` | `MUSIC_ENGINE_ACESTEP_COT_CAPTION`, `_COT_METAS`, `_COT_LANGUAGE` | See the mapping table above. |
| `vocal_language` | `MUSIC_ENGINE_ACESTEP_LANGUAGE` | ISO 639-1 code (default `unknown`, which auto-detects). |
| `max_seconds` | `MUSIC_ENGINE_ACESTEP_MAX_SECONDS` | Overrides the length cap. |

The backend also sets `MIOPEN_FIND_MODE=FAST` and `TOKENIZERS_PARALLELISM=false` unless you set
them yourself. Both come from ACE-Step's `start_gradio_ui_rocm.bat`. Without fast MIOpen search,
the first VAE decode can hang for minutes.

## Models and downloads

On first load, `initialize_service` downloads the main model `ACE-Step/Ace-Step1.5` (about 10 GB:
`acestep-v15-turbo` 4.8 GB, `acestep-5Hz-lm-1.7B` 3.8 GB, `Qwen3-Embedding-0.6B` 1.2 GB,
`vae` 0.3 GB) and any other DiT you name. To download in advance, run this from the clone:

```sh
python -m acestep.model_downloader --model main               # or --list, --model acestep-v15-xl-turbo, ...
```

Pass `--dir` or set `ACESTEP_CHECKPOINTS_DIR` to choose where the models go.
