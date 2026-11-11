"""Device selection for PyTorch backends. ROCm builds of torch report AMD GPUs as "cuda"."""

from __future__ import annotations

import os
from typing import Optional

DEVICE_ENV = "MUSIC_ENGINE_DEVICE"


def pick_torch_device(override: Optional[str] = None) -> str:
    """`override` or $MUSIC_ENGINE_DEVICE ("cuda", "cuda:1", "mps", "xpu", "cpu") wins; else
    the first available of cuda (NVIDIA or AMD ROCm), mps (Apple), xpu (Intel), cpu."""
    choice = (override or os.environ.get(DEVICE_ENV) or "auto").strip().lower()
    if choice != "auto":
        return choice
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        return "xpu"
    return "cpu"


def describe_torch_device(device: str) -> str:
    """One line for the log: which torch build and GPU are in use."""
    try:
        import torch
    except ImportError:
        return device
    build = "ROCm " + torch.version.hip if getattr(torch.version, "hip", None) else (
        "CUDA " + torch.version.cuda if getattr(torch.version, "cuda", None) else "CPU/MPS build")
    name = ""
    if device.startswith("cuda") and torch.cuda.is_available():
        index = int(device.split(":")[1]) if ":" in device else 0
        props = torch.cuda.get_device_properties(index)
        arch = getattr(props, "gcnArchName", "")
        name = f", {props.name}{' (' + arch + ')' if arch else ''}, {props.total_memory / 2**30:.1f} GiB"
    return f"device={device}, torch {torch.__version__} ({build}){name}"
