# ACE-Step 1.5 on Linux

ACE-Step supports Python 3.11–3.12 on Linux
([pyproject.toml](https://github.com/ace-step/ACE-Step-1.5/blob/main/pyproject.toml)). You also
need `ffmpeg` from your distribution's packages. Clone ACE-Step and this repo first:

```sh
git clone https://github.com/ace-step/ACE-Step-1.5 ~/music-engine/ACE-Step-1.5
git clone https://github.com/sgd2z/ghost-label ~/music-engine/ghost-label
```

## NVIDIA (CUDA) or CPU

ACE-Step's standard install pulls CUDA builds of torch on Linux x86_64
([INSTALL.md › Quick Start](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md#quick-start-all-platforms)):

```sh
cd ~/music-engine/ACE-Step-1.5 && uv sync
cd ~/music-engine/ghost-label/engine
python3 driver.py --python ~/music-engine/ACE-Step-1.5/.venv/bin/python \
  --engine-arg=--set --engine-arg=root=$HOME/music-engine/ACE-Step-1.5
```

On NVIDIA, the backend uses ACE-Step's `vllm` (nano-vllm) LM backend by default. Pass
`--set lm_backend=pt` to use plain PyTorch instead.

**CPU** (`--set device=cpu`, or automatic when there's no GPU) also works, but ACE-Step loads
in float32 on CPU. On a 15 GB test box, the turbo DiT alone ran out of memory in float32. Plan on
well over 16 GB of RAM.

## AMD (ROCm), including Strix Halo (gfx1151)

Don't use `uv sync` or `uv run` here: they install CUDA torch over ROCm torch
([INSTALL.md › AMD / ROCm GPUs](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/INSTALL.md#amd--rocm-gpus),
[#106](https://github.com/ace-step/ACE-Step-1.5/issues/106)). Use a plain venv:

```sh
python3.12 -m venv ~/music-engine/venv && PY=~/music-engine/venv/bin/python
# AMD's multi-arch index has Linux wheels too. Pick the device extra for your GPU from
# https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html
$PY -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ \
  "torch[device-gfx1151]==2.13.0+rocm10.0.0" "torchvision[device-gfx1151]==0.28.0+rocm10.0.0" \
  "torchaudio==2.11.0.2+rocm10.0.0"
$PY -m pip freeze | grep -E '^(torch|torchvision|torchaudio)==' > ~/music-engine/torch-constraints.txt
$PY -m pip install -r ~/music-engine/ACE-Step-1.5/requirements-rocm-linux.txt -c ~/music-engine/torch-constraints.txt
python3 ~/music-engine/ghost-label/engine/driver.py --python $PY \
  --engine-arg=--set --engine-arg=root=$HOME/music-engine/ACE-Step-1.5
```

ACE-Step's own Linux ROCm manual, tested on RDNA4 and Strix Halo under CachyOS, uses Python
3.11 and `pip install torch torchaudio torchvision xformers --index-url https://download.pytorch.org/whl/rocm6.4`
instead. It also says Strix Halo needs at least 16 GB of VRAM set in the BIOS
([ACE-Step1.5-Rocm-Manual-Linux.md](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/ACE-Step1.5-Rocm-Manual-Linux.md)).
On Linux, ROCm only sees the full Strix Halo memory with kernel 6.16.9 or later
([ROCm/ROCm#5444](https://github.com/ROCm/ROCm/issues/5444)). **(unverified here)**

Options, mapping and model downloads: see [README.md](README.md).
