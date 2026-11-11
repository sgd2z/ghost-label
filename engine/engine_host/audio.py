"""Post-processing and encoding shared by every backend: trim, fade, loudness, AAC/.m4a."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

import numpy as np

BITRATE = 256000          # docs/custom-engines.md: "44.1 or 48 kHz stereo at 256 kbps is ideal"
SILENCE_DBFS = -60.0      # leading samples quieter than this are trimmed
FADE_SECONDS = 3.0        # fade applied when a song is cut at maxSeconds
AFCONVERT = "/usr/bin/afconvert"


def as_frames(samples) -> np.ndarray:
    """Any array-like (or torch tensor) -> float32 [frames, channels]. Accepts [channels, frames]."""
    if hasattr(samples, "detach"):  # torch tensor, without importing torch here
        samples = samples.detach().float().cpu().numpy()
    x = np.asarray(samples, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    elif x.ndim == 2 and x.shape[0] <= 8 and x.shape[1] > x.shape[0]:
        x = x.T  # channels-first
    elif x.ndim != 2:
        raise ValueError(f"audio must be 1-D or 2-D, got shape {x.shape}")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)  # stereo output, as the app expects
    return np.ascontiguousarray(x)


def trim_leading_silence(x: np.ndarray, threshold_dbfs: float = SILENCE_DBFS) -> np.ndarray:
    level = 10 ** (threshold_dbfs / 20)
    loud = np.nonzero(np.max(np.abs(x), axis=1) > level)[0]
    return x[loud[0]:] if loud.size else x


def cut_and_fade(x: np.ndarray, sample_rate: int, max_seconds: float,
                 fade_seconds: float = FADE_SECONDS) -> Tuple[np.ndarray, bool]:
    """Cut to max_seconds with a fade-out. Returns (audio, was_cut)."""
    limit = int(max_seconds * sample_rate)
    if len(x) <= limit:
        return x, False
    x = x[:limit].copy()
    n = min(len(x), int(fade_seconds * sample_rate))
    if n > 0:
        x[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)[:, None]
    return x, True


def loudness(audio: np.ndarray, sample_rate: int = 48000) -> float:
    """Gated loudness in dB (BS.1770 blocks and gates, no K-weighting), the same measure as the
    reference engine (tools/engines/minimax_engine.py), so Sound Check treats songs alike."""
    x = audio if audio.ndim > 1 else audio[:, None]
    block, hop = int(0.4 * sample_rate), int(0.1 * sample_rate)  # 400 ms blocks, 75% overlap
    energies = np.array([float(np.sum(np.mean(x[i:i + block] ** 2, axis=0)))
                         for i in range(0, max(len(x) - block, 0) + 1, hop)])
    lufs = lambda e: -0.691 + 10 * np.log10(np.maximum(e, 1e-12))  # noqa: E731
    if energies.size == 0:
        return -70.0
    gated = energies[lufs(energies) > -70]
    if gated.size == 0:
        return -70.0
    relative = gated[lufs(gated) > lufs(gated.mean()) - 10]
    return float(lufs((relative if relative.size else gated).mean()))


FFMPEG_MISSING = ("ffmpeg not found. In Ghost Label press Settings › Music engine › Repair engine (it downloads "
                  "a portable ffmpeg), install it with `winget install Gyan.FFmpeg`, or set MUSIC_ENGINE_FFMPEG.")


def ffmpeg_candidates() -> list:
    """Where ffmpeg may be besides PATH: Ghost Label's own copy (Engine/tools/ffmpeg), winget's
    links and packages, and the usual hand-installed folders."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    out = []
    root = os.environ.get("GHOSTLABEL_ENGINE_ROOT")
    if root:
        out += [os.path.join(root, "tools", "ffmpeg", "bin", exe), os.path.join(root, "ffmpeg", "bin", exe)]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        out.append(os.path.join(local, "Microsoft", "WinGet", "Links", exe))
        packages = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages):
            for pkg in sorted(os.listdir(packages)):
                if "ffmpeg" not in pkg.lower():
                    continue
                for dirpath, _dirs, files in os.walk(os.path.join(packages, pkg)):
                    if exe in files:
                        out.append(os.path.join(dirpath, exe))
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\"):
            out.append(os.path.join(base, "ffmpeg", "bin", exe))
        scoop = os.path.join(os.path.expanduser("~"), "scoop", "shims", exe)
        out.append(scoop)
        choco = os.path.join(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"), "bin", exe)
        out.append(choco)
    else:
        out += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
    return out


def find_ffmpeg(explicit: Optional[str] = None, looked: Optional[list] = None) -> Optional[str]:
    """--ffmpeg, then $MUSIC_ENGINE_FFMPEG, then PATH, then ffmpeg_candidates(). A configured path
    that no longer exists is skipped (and listed in `looked`) rather than ending the search."""
    looked = looked if looked is not None else []
    for candidate in (explicit, os.environ.get("MUSIC_ENGINE_FFMPEG")):
        if candidate:
            candidate = os.path.expanduser(candidate)
            found = shutil.which(candidate) or (candidate if os.path.isfile(candidate) else None)
            if found:
                return found
            looked.append(candidate)
    found = shutil.which("ffmpeg")
    if found:
        return found
    looked.append("PATH")
    for candidate in ffmpeg_candidates():
        if os.path.isfile(candidate):
            return candidate
        looked.append(candidate)
    return None


def write_wav(x: np.ndarray, sample_rate: int, path: str) -> None:
    try:
        import soundfile as sf
        sf.write(path, x, sample_rate, subtype="FLOAT")
    except ImportError:  # stdlib fallback: 16-bit PCM
        import wave
        pcm = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(path, "wb") as w:
            w.setnchannels(x.shape[1])
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm.tobytes())


def write_m4a(x: np.ndarray, sample_rate: int, output_path: str, ffmpeg: Optional[str] = None,
              bitrate: int = BITRATE) -> None:
    """AAC in .m4a, written next to output_path and renamed into place (never half-written)."""
    folder = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(folder, exist_ok=True)
    encoder = find_ffmpeg(ffmpeg)
    use_afconvert = encoder is None and sys.platform == "darwin" and os.path.exists(AFCONVERT)
    if encoder is None and not use_afconvert:
        raise FileNotFoundError(FFMPEG_MISSING)
    partial = os.path.join(folder, "." + os.path.basename(output_path) + ".partial.m4a")
    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "song.wav")
        write_wav(x, sample_rate, wav)
        if use_afconvert:
            cmd = [AFCONVERT, "-f", "m4af", "-d", "aac", "-b", str(bitrate), wav, partial]
        else:
            cmd = [encoder, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", wav,
                   "-c:a", "aac", "-b:a", str(bitrate), "-ac", "2",
                   "-movflags", "+faststart", "-f", "ipod", partial]
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)
        if result.returncode != 0:
            if os.path.exists(partial):
                os.remove(partial)
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError("Encoding failed: " + (detail[-1] if detail else f"exit {result.returncode}"))
    os.replace(partial, output_path)


def kept_wav_path(output_path: str) -> str:
    """Where a song's audio is kept when it couldn't be encoded; the next generate for the same
    output path encodes this instead of recording again."""
    return os.path.splitext(output_path)[0] + ".unencoded.wav"


def read_wav(path: str):
    try:
        import soundfile as sf
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        return data, sr
    except ImportError:
        import wave
        with wave.open(path) as w:
            sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
            data = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, ch).astype(np.float32) / 32767.0
        return data, sr
