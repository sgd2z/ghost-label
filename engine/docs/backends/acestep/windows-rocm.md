# ACE-Step 1.5 on Windows with AMD ROCm (Ryzen AI Max+ 395 / Radeon 8060S, gfx1151)

This guide sets up the ACE-Step backend on a Strix Halo PC: Ryzen AI Max+ 395 with the
Radeon 8060S iGPU, which is **gfx1151**. It uses AMD's ROCm PyTorch wheels for Windows. You can
run `scripts/setup-acestep-windows-rocm.ps1` to do all of it, or follow the steps by hand. Every
command and version below links to its source. Notes marked **(unverified)** were not tested on
real gfx1151 hardware for this repo. See [What has and hasn't been tested](#what-has-and-hasnt-been-tested).

Everything goes under one folder. The guide uses `$Root = "$env:USERPROFILE\music-engine"`.

```
music-engine\
  venv\                  Python 3.12 virtual environment (ROCm torch + ACE-Step dependencies)
  ACE-Step-1.5\          ACE-Step clone (models download to ACE-Step-1.5\checkpoints)
  ghost-label\     this repo
```

## 0. Before you start

- **Update the AMD driver.** Install the current *AMD Software: Adrenalin Edition*. AMD's
  ACE-Step 1.5 blog post starts with this step
  ([AMD blog, Feb 2026](https://www.amd.com/en/blogs/2026/commercial-grade-ai-music-generation-on-amd-ryzen-ai-and-radeon-ace-step-1-5.html)).
  A working gfx1151 + `torch 2.13.0+rocm10.0.0` setup was reported on Adrenalin 26.8.1
  ([ROCm/TheRock#7839](https://github.com/ROCm/TheRock/issues/7839)).
- **Keep Variable Graphics Memory (VGM) small.** One user reported that on Windows, a large
  VGM/UMA carve-out (96 GB) capped HIP allocations at roughly the RAM still visible to Windows
  (about 28–32 GiB). With 0.5–1 GB of VGM, the same machine allocated about 100 GiB from shared
  memory ([ROCm/TheRock#8922](https://github.com/ROCm/TheRock/issues/8922), and
  [legacy-rocm-build#6696](https://github.com/ROCm/legacy-rocm-build/issues/6696)). The reporter
  changed VGM in AMD Software, not in the BIOS. AMD **could not reproduce** the cap on its own
  96 GB machine and closed the issue on Nov 2, 2026. Treat a small VGM as a reported workaround,
  not a confirmed bug. If you hit out-of-memory errors with a large VGM, lower it and reboot.
  (ACE-Step's *Linux* guide gives the opposite advice: at least 16 GB of VRAM set in the BIOS,
  [ACE-Step1.5-Rocm-Manual-Linux.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/ACE-Step1.5-Rocm-Manual-Linux.md).
  That is about Linux.)
- **Smart App Control.** On Windows 11 installs with Smart App Control enforcing, some ROCm
  builds' unsigned DLLs are refused with `OSError: [WinError 4551]`. One report says the
  `10.0.0` build used here loads fine, while `rocm-sdk-libraries 7.14.1`, which torch 2.12 pins,
  does not ([ROCm/TheRock#7839](https://github.com/ROCm/TheRock/issues/7839), item 4).

## 1. Python 3.12, Git and ffmpeg

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id Gyan.FFmpeg
```

Open a **new** PowerShell window afterwards so `py`, `git` and `ffmpeg` are on PATH.

Why 3.12: ACE-Step 1.5 supports Python `>=3.11,<3.13`
([pyproject.toml](https://github.com/ace-step/ACE-Step-1.5/blob/main/pyproject.toml)), and its
Windows ROCm instructions require 3.12
([INSTALL.md › AMD / ROCm GPUs](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md#amd--rocm-gpus),
[requirements-rocm.txt](https://github.com/ace-step/ACE-Step-1.5/blob/main/requirements-rocm.txt)).
A 3.11 environment fails against the ROCm wheels
([ACE-Step-1.5#551](https://github.com/ace-step/ACE-Step-1.5/issues/551)). The winget IDs are
from [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs/tree/master/manifests).

## 2. A virtual environment

```powershell
$Root = "$env:USERPROFILE\music-engine"
New-Item -ItemType Directory -Force $Root | Out-Null
py -3.12 -m venv "$Root\venv"
$py = "$Root\venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
```

The rest of the guide calls `& $py` directly instead of activating the venv, so PowerShell's
script execution policy doesn't get in the way.

## 3. PyTorch for ROCm on gfx1151

This command is from AMD's ROCm documentation, *Install PyTorch for ROCm* (Ryzen, gfx1151,
Windows, pip)
([rocm.docs.amd.com](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html?fam=ryzen&gfx=gfx1151&gpu=max-395&i=pip&os=windows&pytorch-ver=2.13.0)).
An AMD engineer repeated it in
[ROCm/TheRock#7839](https://github.com/ROCm/TheRock/issues/7839):

```powershell
& $py -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ `
  "torch[device-gfx1151]==2.13.0+rocm10.0.0" `
  "torchvision[device-gfx1151]==0.28.0+rocm10.0.0" `
  "torchaudio==2.11.0.2+rocm10.0.0"
```

`https://stable.repo.amd.com/rocm/whl-next/` is AMD's stable multi-arch index
([TheRock RELEASES.md](https://github.com/ROCm/TheRock/blob/main/RELEASES.md)). On Nov 10, 2026,
it listed `cp312 win_amd64` wheels for all three versions. The torch wheel's metadata maps the
`device-gfx1151` extra to `amd-torch-device-gfx1151==2.13.0+rocm10.0.0` and
`amd-torch-device-gfx115x`, and torchvision's maps to `amd-torchvision-device-gfx1151`.
**Keep the `[device-gfx1151]` extras.** Without the exact-arch packages, torch imports and
reports a GPU, but every kernel fails with `hipErrorInvalidImage` ("device kernel image is
invalid") ([TheRock#7839](https://github.com/ROCm/TheRock/issues/7839)). You don't need to install
ROCm separately: the torch packages depend on the ROCm Python packages (RELEASES.md).

**Why ROCm 10.0.0.** As of Nov 10, 2026, AMD's page also lists newer ROCm 10.1.0 builds for
gfx1151: `torch 2.13.0+rocm10.1.0` with `torchvision 0.28.0` and `torchaudio 2.11.0.2`, and
`torch 2.14.0+rocm10.1.0` with a pre-release `torchvision 0.29.0a0`. This guide pins 10.0.0
because that is the build with a public report of working on gfx1151 under Windows (TheRock#7839).
If 10.0.0 gives you trouble, the 10.1.0 line from the same page is the next thing to try:
`"torch[device-gfx1151]==2.13.0+rocm10.1.0" "torchvision[device-gfx1151]==0.28.0+rocm10.1.0" "torchaudio==2.11.0.2+rocm10.1.0"`.
The setup script's `-Rocm 10.1.0` switch installs it. **(unverified)**

Check that a kernel actually runs. `is_available()` alone can be `True` even when kernels can't
launch:

```powershell
& $py -c "import torch; print(torch.__version__, 'HIP', torch.version.hip); print(torch.cuda.is_available(), torch.cuda.get_device_name(0), torch.cuda.get_device_properties(0).gcnArchName); print((torch.zeros(8, device='cuda') + 1).sum().item())"
```

You want `2.13.0+rocm10.0.0`, `True`, `AMD Radeon(TM) 8060S Graphics` (or similar), `gfx1151`,
and `8.0`. ROCm builds of torch report AMD GPUs as `cuda`. That's expected, and the engine's
device choice relies on it.

## 4. ACE-Step 1.5, without letting it replace torch

```powershell
git clone https://github.com/ace-step/ACE-Step-1.5 "$Root\ACE-Step-1.5"
& $py -m pip freeze | Select-String '^(torch|torchvision|torchaudio)==' | Set-Content -Encoding ascii "$Root\torch-constraints.txt"
& $py -m pip install -r "$Root\ACE-Step-1.5\requirements-rocm.txt" -c "$Root\torch-constraints.txt"
& $py -c "import torch; print(torch.__version__)"     # still 2.13.0+rocm10.0.0
```

- `requirements-rocm.txt` is ACE-Step's own Windows ROCm dependency list. It contains no torch.
  Skip its commented "STEP 1/STEP 2" lines: they install the older ROCm 7.2 / torch 2.9.1 wheels.
  The constraints file makes pip fail instead of swapping torch if any dependency asks for a
  different build. (`torchao` is left out on purpose, because ROCm Windows torch lacks
  `torch.distributed`, so `--quantization` isn't available.)
- **Don't run `uv sync`, `uv run acestep` or `pip install -e .`** in the clone. ACE-Step's
  `pyproject.toml` pins `torch==2.7.1+cu128` on Windows, and those commands replace a ROCm torch
  with the CUDA build ([INSTALL.md warning](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md#amd--rocm-gpus),
  [ACE-Step-1.5#106](https://github.com/ace-step/ACE-Step-1.5/issues/106)). The engine imports
  ACE-Step straight from the clone through `ACESTEP_PROJECT_ROOT`, the same way ACE-Step's own
  `python -m acestep...` launchers run it.

## 5. This repo

```powershell
git clone https://github.com/sgd2z/ghost-label "$Root\ghost-label"   # private: sign in to GitHub, or unzip a copy there
& $py -m pip install -r "$Root\ghost-label\engine\requirements.txt"               # numpy + soundfile, already present
```

Tell the engine where ACE-Step is. A config file next to `engine.py` is simplest:

```powershell
@{ backend = "acestep"; env = @{ ACESTEP_PROJECT_ROOT = "$Root\ACE-Step-1.5"; ACESTEP_ROCM_DTYPE = "float32" } } |
  ConvertTo-Json | Set-Content -Encoding utf8 "$Root\ghost-label\engine\engine.json"
```

Instead of the file, you can set environment variables
(`$env:ACESTEP_PROJECT_ROOT = "$Root\ACE-Step-1.5"`) or pass
`--set root=...` to the engine. `ACESTEP_ROCM_DTYPE=float32` is already ACE-Step's ROCm default.
Its source notes that `bfloat16` can segfault on Strix Halo iGPUs
(`acestep/core/generation/handler/init_service_orchestrator.py`). It's written out here so it's
easy to experiment with.

## 6. Download the models (about 10 GB)

```powershell
Push-Location "$Root\ACE-Step-1.5"
& $py -m acestep.model_downloader --model main
Pop-Location
```

This downloads `ACE-Step/Ace-Step1.5` from Hugging Face into `ACE-Step-1.5\checkpoints`:
the turbo DiT, the 1.7B LM planner, the Qwen3 text encoder and the VAE. Use
`--model acestep-v15-xl-turbo` (or `--list`) for others. You can skip this step, because the
first load downloads whatever is missing. A shared folder works too: set `ACESTEP_CHECKPOINTS_DIR`
for both the downloader and the engine.

## 7. Record a song with the driver

```powershell
& $py "$Root\ghost-label\engine\driver.py" --steps 8 --max-seconds 60
```

The driver launches `engine.py` with the same Python, sends `load` and then a sample song (indie
rock with lyrics), and shows a progress bar. It writes `out\<id>.m4a` and opens the file in your
default player. Useful variations:

```powershell
& $py "$Root\ghost-label\engine\driver.py" --steps 16 --max-seconds 180 --style "English, synthwave, female vocal" --lyrics-file my-song.txt
& $py "$Root\ghost-label\engine\driver.py" --instrumental --steps 32
& $py "$Root\ghost-label\engine\driver.py" --engine-arg=--set --engine-arg=init_llm=false     # DiT only, no LM planner
& $py "$Root\ghost-label\engine\driver.py" --engine-arg=--backend --engine-arg=mock           # plumbing only
```

**Speed to expect.** AMD measured ACE-Step 1.5 through **ComfyUI**, not this Python pipeline, at
about 66 s for a two-minute track on Ryzen AI Max+, roughly 1.8x faster than real time
([AMD blog](https://www.amd.com/en/blogs/2026/commercial-grade-ai-music-generation-on-amd-ryzen-ai-and-radeon-ace-step-1-5.html);
the 66 s figure is quoted by [TweakTown](https://www.tweaktown.com/news/110042/ai-music-generation-comes-to-amd-ryzen-ai-processors-and-radeon-gpus/index.html)).
The driver prints its own "x realtime" figure for comparison. Expect the first song to be slower
while MIOpen compiles kernels.

## 8. Standard Music itself

Standard Music is a macOS-only app, so on this PC the driver stands in for it. To use the engine
from the app, install it on a Mac instead (see [macos.md](macos.md)) and register it as the
[README](../../../README.md#using-it-from-standard-music-macos) describes.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `device kernel image is invalid` / `hipErrorInvalidImage` on the first GPU operation | The exact-arch wheel `amd-torch-device-gfx1151` is missing. Reinstall with the `[device-gfx1151]` extras ([TheRock#7839](https://github.com/ROCm/TheRock/issues/7839)). |
| `invalid device function` from torchvision ops | `amd-torchvision-device-gfx1151` is missing. Note there is no gfx115x family wheel for torchvision (same issue). |
| `miopenStatusUnknownError` / hiprtc `'type_traits' file not found` from `batch_norm` | MIOpen's runtime-compiled batch norm is reported broken on the Windows 10.0.0 wheels (same issue, item 3). It's unknown whether ACE-Step hits it **(unverified)**. If it does, report it with the full traceback. |
| `OSError: [WinError 4551]` loading a ROCm DLL | Smart App Control rejected an unsigned DLL (same issue, item 4). |
| `torch.__version__` shows `+cu128` or `+cpu`, or the GPU disappeared | Something reinstalled torch: `uv run`, `uv sync` or `pip install -e .` in the ACE-Step clone ([#106](https://github.com/ace-step/ACE-Step-1.5/issues/106)). Redo step 3, then step 4 with the constraints file. |
| Install errors about Python versions | Use the Python 3.12 venv ([#551](https://github.com/ace-step/ACE-Step-1.5/issues/551)). |
| Out of memory or allocation failures with plenty of RAM free | Lower VGM in AMD Software and reboot (see step 0). For older HIP runtimes, AMD suggested `$env:OCL_SET_SVM_SIZE = "262144"` for allocations of 64 GiB and up ([rocm-systems#1786](https://github.com/ROCm/rocm-systems/issues/1786)). You can also try `--set offload_to_cpu=true` or a smaller LM (`--set lm_model_path=acestep-5Hz-lm-0.6B`). |
| Segfault while loading or generating | Keep `ACESTEP_ROCM_DTYPE=float32` (the default). |
| The first song sits at "rendering" for minutes | MIOpen's kernel search. The engine sets `MIOPEN_FIND_MODE=FAST` unless you have set it yourself, as ACE-Step's `start_gradio_ui_rocm.bat` does. |
| `HSA_OVERRIDE_GFX_VERSION` | Don't set it. ACE-Step's `start_*_rocm.bat` sets `11.0.0` for RDNA3 (RX 7900-class) cards, and the gfx1151 wheels target this chip directly. |
| `ffmpeg not found` | `winget install -e --id Gyan.FFmpeg` and open a new terminal, or set `$env:MUSIC_ENGINE_FFMPEG` to the full path of `ffmpeg.exe`. |
| The engine says ACE-Step is not importable | `ACESTEP_PROJECT_ROOT` (or `engine.json`) doesn't point at the clone. |

**Fallbacks.** ACE-Step lists CPU as a supported device ([INSTALL.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md)).
`--engine-arg=--set --engine-arg=device=cpu` works, but on CPU ACE-Step loads in float32 and needs
far more RAM and time. This guide doesn't cover DirectML: ACE-Step doesn't document it. ComfyUI
Desktop's built-in ROCm support is AMD's own path for ACE-Step on this chip (AMD blog), but it is a
separate app, not something this engine uses.

## What has and hasn't been tested

- **Tested on Linux (no AMD hardware):** the protocol, driver, cancel, failure handling and ffmpeg
  `.m4a` output, with the mock backend and with a stub of ACE-Step's API. The real ACE-Step 1.5
  pipeline also ran on CPU through this engine and driver (turbo DiT, with and without the 0.6B
  LM), producing valid AAC `.m4a` files.
- **Checked against the live index:** the wheel versions in step 3 and their `device-gfx1151`
  extras.
- **Not tested on gfx1151 / Windows:** every step above on real hardware, the speed, the VGM
  advice, and whether any ACE-Step op hits a ROCm-on-Windows kernel gap.
