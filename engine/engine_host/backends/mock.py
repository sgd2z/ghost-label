"""A stand-in model for tests and for checking a setup without downloading anything.

It renders a short, seed-determined chord progression with a little leading silence, reports
progress through every stage, honors cancel, and can be told to fail. Options (--set key=value or
MUSIC_ENGINE_MOCK_<KEY>):

    seconds      length of the song it makes (default 6; still capped by maxSeconds)
    delay        seconds of pretend work per stage (default 0.2)
    load_delay   seconds the load takes (default 0)
    fail_load    1 to fail every load
    fail         1 to fail every song
    sample_rate  default 48000
"""

from __future__ import annotations

import time

import numpy as np

from engine_host.backends import Audio, Backend, Cancelled, Capabilities


class MockBackend(Backend):
    name = "mock"
    steps_map = {8: 2, 16: 4, 32: 8}

    def opt(self, key, default):
        return self.option(key, default, [f"MUSIC_ENGINE_MOCK_{key.upper()}"])

    def capabilities(self) -> Capabilities:
        return Capabilities(max_seconds=float(self.opt("max_seconds", "600")), supports_lyrics=True,
                            cooperative_cancel=True, reports_progress=True, seconds_per_song_second=0.1,
                            analyze=True, reference_audio=True, languages=["en", "hi", "ur", "es", "ja"])

    def load(self, model_path):
        self.status("Loading test tones")
        time.sleep(float(self.opt("load_delay", "0")))
        if self.opt("fail_load", "0") == "1":
            raise RuntimeError(f"mock: refusing to load {model_path!r}")
        self.model_path = model_path
        self.log(f"[mock] loaded model_path={model_path!r}")

    def generate(self, request, progress, cancelled):
        delay = float(self.opt("delay", "0.2"))
        sr = int(self.opt("sample_rate", "48000"))
        if self.opt("fail", "0") == "1":
            raise RuntimeError("mock: generation failed on purpose")
        stages = ["planning", "composing", "rendering"]
        ticks = max(1, request.steps)
        self.counters = {}
        for s, stage in enumerate(stages):
            for t in range(ticks):
                if cancelled():
                    raise Cancelled()
                time.sleep(delay / ticks)
                # Real counts of this backend's own loop, like the ACE-Step backend's.
                self.counters = {"tokens": (t + 1) * 10} if stage == "planning" else (
                    {"step": t + 1, "steps": ticks} if stage == "composing" else {})
                progress(stage, (t + 1) / ticks, (s + (t + 1) / ticks) / len(stages))
        seconds = min(float(self.opt("seconds", "6")), request.max_seconds + 5)  # may overshoot: tests the cut
        rng = np.random.default_rng(request.seed)
        roots = 220.0 * 2 ** (rng.integers(0, 12, size=4) / 12)
        t = np.arange(int(seconds * sr)) / sr
        segment = len(t) // 4 or 1
        wave = np.zeros_like(t)
        for i, root in enumerate(roots):
            part = slice(i * segment, (i + 1) * segment if i < 3 else len(t))
            for ratio in (1.0, 1.25, 1.5):
                wave[part] += np.sin(2 * np.pi * root * ratio * t[part])
        wave *= 0.2
        silence = np.zeros(int(0.5 * sr))
        mono = np.concatenate([silence, wave]).astype(np.float32)
        stereo = np.stack([mono, mono * 0.9], axis=1)
        self.last_request = request
        return Audio(stereo, sr)

    def live(self):
        return dict(getattr(self, "counters", {}) or {})

    def analyze(self, audio_path):
        """Measured, not guessed: duration and RMS level from the file, plus a fixed stub caption."""
        try:
            import soundfile as sf
            data, sr = sf.read(audio_path, always_2d=True)
        except ImportError:  # WAV only without soundfile
            import wave
            with wave.open(audio_path) as w:
                sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
                data = np.frombuffer(w.readframes(n), dtype=np.int16).reshape(-1, ch) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(data)))) if data.size else 0.0
        return {"caption": "mock analysis", "duration": len(data) / sr, "rms": rms,
                "bpm": None, "keyscale": None, "language": "unknown", "lyrics": ""}
