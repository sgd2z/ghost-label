"""The backend interface and registry.

A backend is a small class that loads one music model and renders one song at a time. It never
touches stdin/stdout or files: it gets a `SongRequest` and returns an `Audio`.

To add a model, write `engine_host/backends/<name>.py` with a `Backend` subclass, then add it to
`BACKENDS` below. See README.md, "Adding a backend".
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# name -> "module:Class". Modules are imported lazily, so a missing dependency of one backend
# never breaks another.
BACKENDS: Dict[str, str] = {
    "acestep": "engine_host.backends.acestep:AceStepBackend",
    "mock": "engine_host.backends.mock:MockBackend",
    "lemonade": "engine_host.backends.lemonade:LemonadeBackend",
}


class Cancelled(Exception):
    """Raise from inside a backend (or a progress callback) to abandon the current song."""


@dataclass
class Capabilities:
    """What the host needs to know about a backend."""

    max_seconds: float = 240.0          # longest song the model can make; maxSeconds is capped to this
    supports_lyrics: bool = True        # False: lyrics are not passed (instrumental-only models)
    cooperative_cancel: bool = False    # True: generate() polls `cancelled()` and stops early
    reports_progress: bool = False      # True: generate() calls `progress()`; else the host estimates
    seconds_per_song_second: float = 1.0  # first guess at render speed, for estimated progress
    # Optional extensions (not in docs/custom-engines.md; advertised in `ready.capabilities`):
    analyze: bool = False               # `analyze` command: audio file -> descriptive metadata
    reference_audio: bool = False       # `referenceAudio` on a generate job: style/timbre guidance
    languages: Optional[list] = None    # vocal language codes accepted via `vocalLanguage`


@dataclass
class SongRequest:
    """One `generate` job from the app, normalized. `raw` keeps the original JSON."""

    id: str
    style: str
    lyrics: str
    seed: int
    quality: int                 # the app's quality setting: 8 (Demo), 16 (Balanced), 32 (Studio)
    steps: int                   # quality mapped through Backend.map_steps()
    max_seconds: float           # min(maxSeconds from the app, Capabilities.max_seconds)
    instrumental: bool
    planning: str = "full"       # YuE2-specific; backends may map it onto their own planner
    title: Optional[str] = None
    artist: Optional[str] = None
    voice: Optional[str] = None
    vocal_language: Optional[str] = None
    reference_audio: Optional[str] = None
    bpm: Optional[int] = None
    keyscale: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Audio:
    """Rendered audio: float samples shaped [frames] or [frames, channels], and a sample rate."""

    samples: Any
    sample_rate: int


# progress(stage, stage_fraction, overall=None). Stages are the protocol's:
# loading, planning, composing, rendering, mastering, encoding.
ProgressFn = Callable[..., None]


class Backend:
    """Base class. Override `load`, `generate` and, if useful, `capabilities` and `map_steps`."""

    name = "base"
    # Step counts for the app's three quality settings. Override per backend.
    steps_map: Dict[int, int] = {8: 8, 16: 16, 32: 32}

    def __init__(self, options: Optional[Dict[str, str]] = None, log: Callable[[str], None] = print):
        self.options: Dict[str, str] = dict(options or {})
        self.log = log
        # Set by the host: status(text) while loading, shown in the app ("Loading the DiT…").
        self.status: Callable[[str], None] = lambda text: None

    def live(self) -> Dict[str, Any]:
        """Real counters for the current song (e.g. {"tokens": 312} or {"step": 5, "steps": 8,
        "detail": "..."}), sent with every progress heartbeat. Only report what you measure."""
        return {}

    # Options come from `--set key=value` / the config file first, then the environment.
    def option(self, key: str, default: Optional[str] = None, env: Optional[list] = None) -> Optional[str]:
        if key in self.options and self.options[key] != "":
            return self.options[key]
        for name in env or []:
            value = os.environ.get(name)
            if value not in (None, ""):
                return value
        return default

    def option_bool(self, key: str, default: bool, env: Optional[list] = None) -> bool:
        value = self.option(key, None, env)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def map_steps(self, quality: int) -> int:
        override = self.option("steps_map")
        table = parse_steps_map(override) if override else self.steps_map
        if quality in table:
            return table[quality]
        # Not one of the app's three settings: pick the nearest one.
        nearest = min(table, key=lambda q: abs(q - quality))
        return table[nearest]

    def load(self, model_path: Optional[str]) -> None:
        """Load the model. `model_path` is the app's Model field, or None when it was left empty."""
        raise NotImplementedError

    def is_loaded_for(self, model_path: Optional[str]) -> bool:
        return False

    def generate(self, request: SongRequest, progress: ProgressFn, cancelled: Callable[[], bool]) -> Audio:
        raise NotImplementedError

    def analyze(self, audio_path: str) -> Dict[str, Any]:
        """Describe an audio file (only when `capabilities().analyze`). Return plain JSON data."""
        raise NotImplementedError("this backend can't analyze audio")


def parse_steps_map(text: str) -> Dict[int, int]:
    """"8:8,16:12,32:20" -> {8: 8, 16: 12, 32: 20}."""
    table = {}
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        quality, steps = part.split(":")
        table[int(quality)] = int(steps)
    if not table:
        raise ValueError(f"empty steps map: {text!r}")
    return table


def create(name: str, options: Optional[Dict[str, str]] = None, log: Callable[[str], None] = print) -> Backend:
    target = BACKENDS.get(name)
    if target is None and ":" in name:
        target = name  # "package.module:Class" for out-of-tree backends
    if target is None:
        raise ValueError(f"Unknown backend {name!r}. Known: {', '.join(sorted(BACKENDS))}")
    module_name, class_name = target.split(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls(options=options, log=log)
