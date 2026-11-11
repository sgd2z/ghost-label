import dataclasses
import math
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

from acestep import record


@dataclass
class GenerationParams:
    task_type: str = "text2music"
    caption: str = ""
    lyrics: str = ""
    instrumental: bool = False
    vocal_language: str = "unknown"
    duration: float = -1.0
    inference_steps: int = 8
    seed: int = -1
    guidance_scale: float = 7.0
    shift: float = 1.0
    thinking: bool = True
    use_cot_metas: bool = True
    use_cot_caption: bool = True
    use_cot_language: bool = True
    reference_audio: Optional[str] = None
    bpm: Optional[int] = None
    keyscale: str = ""


@dataclass
class GenerationConfig:
    batch_size: int = 2
    use_random_seed: bool = True
    seeds: Optional[List[int]] = None
    audio_format: str = "flac"


@dataclass
class GenerationResult:
    audios: list = field(default_factory=list)
    status_message: str = ""
    extra_outputs: dict = field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None


def generate_music(dit_handler, llm_handler, params, config, save_dir=None, progress=None):
    record("generate_music", params=dataclasses.asdict(params), config=dataclasses.asdict(config),
           save_dir=save_dir, has_llm=llm_handler is not None)
    if params.caption == "fail please":
        return GenerationResult(success=False, error="stub failure", status_message="stub failure")
    delay = float(os.environ.get("FAKE_ACESTEP_DELAY", "0.05"))
    try:
        for value, desc in [(0.1, "Phase 1: Generating CoT metadata"), (0.5, "Phase 2: Generating audio codes"),
                            (0.52, "Generating music"), (0.7, "Generating music"), (0.8, "Decoding audio..."),
                            (0.99, "Preparing audio data...")]:
            time.sleep(delay)
            progress(value, desc=desc)
            if value == 0.8 and hasattr(dit_handler, "_tiled_decode_gpu"):
                dit_handler._tiled_decode_gpu(None, 1, 0, int(os.environ.get("FAKE_ACESTEP_CHUNKS", "3")))
    except Exception as error:  # ACE-Step's generate_music catches everything and reports it
        return GenerationResult(success=False, error=str(error) or repr(error))
    seconds = params.duration if params.duration > 0 else float(os.environ.get("FAKE_ACESTEP_SECONDS", "5"))
    sr = 48000
    n = int(seconds * sr)
    import torch
    t = torch.arange(n, dtype=torch.float32) / sr
    left = 0.3 * torch.sin(2 * math.pi * 440.0 * t)
    tensor = torch.stack([left, left * 0.8])  # [channels, samples], like ACE-Step
    return GenerationResult(audios=[{"path": "", "tensor": tensor, "key": "k", "sample_rate": sr,
                                     "params": {"seed": params.seed}}])


@dataclass
class UnderstandResult:
    caption: str = ""
    lyrics: str = ""
    bpm: Optional[int] = None
    duration: Optional[float] = None
    keyscale: str = ""
    language: str = ""
    timesignature: str = ""
    status_message: str = ""
    success: bool = True
    error: Optional[str] = None


def understand_music(llm_handler, audio_codes, **kwargs):
    record("understand_music", audio_codes=audio_codes)
    return UnderstandResult(caption="slow ballad, strings, male and female duet", lyrics="[Verse]\n...",
                            bpm=72, duration=241.0, keyscale="E minor", language="hi", timesignature="4")
