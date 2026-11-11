"""The protocol side: one JSON object per line on stdin/stdout, exactly as described in
standardagents/music docs/custom-engines.md and mirrored from tools/engines/minimax_engine.py."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

_protocol = None
_send_lock = threading.Lock()


def claim_stdout() -> None:
    """stdout belongs to the protocol. Anything a library prints (even from C) goes to stderr."""
    global _protocol
    if _protocol is not None:
        return
    _protocol = io.TextIOWrapper(os.fdopen(os.dup(1), "wb", buffering=0), encoding="utf-8",
                                 newline="\n", line_buffering=True, write_through=True)
    os.dup2(2, 1)
    sys.stdout = sys.stderr


def send(message: dict) -> None:
    with _send_lock:
        _protocol.write(json.dumps(message, ensure_ascii=False) + "\n")
        _protocol.flush()


def log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def progress_message(jid, stage, stage_fraction, overall, elapsed, song_seconds=None, live=None) -> dict:
    payload = {"stage": stage, "stageFraction": float(min(max(stage_fraction, 0.0), 1.0)),
               "overall": float(min(max(overall, 0.0), 1.0)), "elapsed": float(elapsed)}
    if song_seconds:
        payload["estimatedSongSeconds"] = float(song_seconds)
    # Extension fields (ignored by apps that don't know them): detail, tokens, step, steps.
    for key, value in (live or {}).items():
        if value not in (None, ""):
            payload[key] = value
    return {"progress": {"id": jid, "_1": payload}}


def native_stdin_lines():
    """Lines from stdin without ever sitting inside a blocking ReadFile on Windows.

    A thread blocked in ReadFile on a synchronous pipe handle makes every other call on that
    handle (GetFileType, which DLL runtimes such as scipy's OpenBLAS/gfortran make on the std
    handles while loading) wait until a command arrives: the engine hung importing
    scipy.linalg while the app waited for it. So we PeekNamedPipe and only read what is there."""
    import ctypes
    import msvcrt
    from ctypes import wintypes
    fd = sys.stdin.fileno()
    handle = msvcrt.get_osfhandle(fd)
    if ctypes.windll.kernel32.GetFileType(wintypes.HANDLE(handle)) != 3:  # FILE_TYPE_PIPE
        yield from io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
        return
    peek = ctypes.windll.kernel32.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    peek.restype = wintypes.BOOL
    buf = b""
    while True:
        avail = wintypes.DWORD(0)
        if not peek(handle, None, 0, None, ctypes.byref(avail), None):
            break  # not a pipe any more / the app closed it
        if avail.value == 0:
            time.sleep(0.05)
            continue
        chunk = os.read(fd, avail.value)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", "replace")
    if buf:
        yield buf.decode("utf-8", "replace")


def loading_message(stage, elapsed) -> dict:
    """Extension: sent while the model loads (every couple of seconds), before `ready`."""
    return {"loading": {"stage": stage, "elapsed": float(elapsed)}}


# Where backend progress lands on the overall bar; the host owns the last few percent.
GENERATION_END = 0.95
STAGE_SPANS = {"loading": (0.0, 0.05), "planning": (0.05, 0.35), "composing": (0.35, 0.6),
               "rendering": (0.6, GENERATION_END)}


class Host:
    def __init__(self, backend, program_dir: str, ffmpeg: Optional[str] = None,
                 bitrate: Optional[int] = None):
        from engine_host import audio  # numpy is imported here, after stdout is claimed
        self.audio = audio
        self.backend = backend
        self.program_dir = os.path.realpath(program_dir)
        self.ffmpeg = ffmpeg
        self.bitrate = bitrate or audio.BITRATE
        self.cancelled = set()
        self.loaded = False
        self.model_path: Optional[str] = None
        self.speed = None  # seconds of work per second of song, learned as songs finish

    # MARK: load

    def check_encoder(self) -> None:
        """Find ffmpeg before loading anything, so a missing encoder fails at start, not after a
        song has been recorded."""
        looked = []
        found = self.audio.find_ffmpeg(self.ffmpeg, looked)
        if found:
            if found != self.ffmpeg:
                log(f"[audio] ffmpeg: {found}")
            self.ffmpeg = found
            return
        if sys.platform == "darwin" and os.path.exists(self.audio.AFCONVERT):
            return
        shown = [x for x in looked if x != "PATH"][:6]
        raise FileNotFoundError(self.audio.FFMPEG_MISSING + " Looked on PATH" +
                                (" and in: " + "; ".join(shown) if shown else "") + ".")

    def normalize_model_path(self, path: Optional[str]) -> Optional[str]:
        # The app sends the engine's own folder when the Model setting is empty.
        if not path or not path.strip():
            return None
        path = os.path.expanduser(path.strip())
        if os.path.isdir(path) and os.path.realpath(path) == self.program_dir:
            return None
        return path

    def load(self, path: Optional[str]) -> None:
        path = self.normalize_model_path(path)
        if self.loaded and self.model_path == path:
            send({"ready": {"loadSeconds": 0, "capabilities": self.capability_payload()}})
            return
        self.check_encoder()
        start = time.time()
        self.loaded = False
        self.model_path = path
        current = {"stage": "Starting the engine"}
        done = threading.Event()

        from engine_host.native import Watchdog
        watch = Watchdog(120, what="loading the model")

        def status(text):
            watch.touch()
            current["stage"] = text
            log(f"[load] {text} ({time.time() - start:.0f} s)")
            send(loading_message(text, time.time() - start))

        def heartbeat():
            while not done.wait(2.0):
                send(loading_message(current["stage"], time.time() - start))

        self.backend.status = status
        threading.Thread(target=heartbeat, daemon=True).start()
        try:
            with watch:  # stack dump to Logs/engine-stall.log only after 120 s without a load step
                self.backend.load(path)
        finally:
            done.set()
            self.backend.status = lambda text: None
        self.loaded = True
        send({"ready": {"loadSeconds": time.time() - start, "capabilities": self.capability_payload()}})

    def capability_payload(self) -> dict:
        caps = self.backend.capabilities()
        return {"maxSeconds": caps.max_seconds, "lyrics": caps.supports_lyrics,
                "analyze": bool(getattr(caps, "analyze", False)),
                "referenceAudio": bool(getattr(caps, "reference_audio", False)),
                "languages": list(getattr(caps, "languages", None) or [])}

    # MARK: generate

    def make_request(self, job: dict):
        from engine_host.backends import SongRequest
        caps = self.backend.capabilities()
        wanted = job.get("maxSeconds")
        seconds = min(float(wanted) if wanted else caps.max_seconds, caps.max_seconds)
        quality = int(job.get("steps") or 16)
        instrumental = bool(job.get("isInstrumental"))
        lyrics = job.get("lyrics") or ""
        if not caps.supports_lyrics:
            lyrics = ""
        return SongRequest(
            id=job["id"], style=job.get("style") or "", lyrics=lyrics,
            seed=int(job.get("seed") or 0) % 2**31, quality=quality,
            steps=self.backend.map_steps(quality), max_seconds=seconds,
            instrumental=instrumental, planning=job.get("planning") or "full",
            title=job.get("title"), artist=job.get("artist"), voice=job.get("voice"),
            vocal_language=job.get("vocalLanguage") or None,
            reference_audio=(job.get("referenceAudio") or None) if getattr(caps, "reference_audio", False) else None,
            bpm=int(job["bpm"]) if str(job.get("bpm") or "").isdigit() else None,
            keyscale=job.get("keyscale") or None, raw=job)

    # MARK: analyze (extension)

    def analyze(self, job: dict) -> None:
        jid = job.get("id")
        if not self.loaded:
            self.load(self.model_path)
        if not getattr(self.backend.capabilities(), "analyze", False):
            send({"failed": {"id": jid, "message": "This engine can't analyze audio."}})
            return
        start = time.time()
        result = self.backend.analyze(job["audioPath"])
        send({"analyzed": {"_0": {"id": jid, "seconds": time.time() - start, "result": result}}})

    def encode_kept(self, job: dict, kept: str) -> None:
        """A song recorded earlier whose encoding failed: encode the kept WAV, don't record again."""
        jid, start = job["id"], time.time()
        self.check_encoder()
        log(f"[audio] encoding the kept recording {kept}")
        send(progress_message(jid, "encoding", 0, 0.97, 0.0, None))
        audio, sr = self.audio.read_wav(kept)
        audio = self.audio.as_frames(audio)
        self.audio.write_m4a(audio, sr, job["outputPath"], self.ffmpeg, self.bitrate)
        os.remove(kept)
        duration = len(audio) / sr
        send({"finished": {"_0": {"id": jid, "duration": duration, "loudness": self.audio.loudness(audio, sr),
                                  "seconds": time.time() - start,
                                  "truncated": bool(duration >= float(job.get("maxSeconds") or 1e9) - 0.5)}}})

    def generate(self, job: dict) -> None:
        jid = job["id"]
        if jid in self.cancelled:
            self.cancelled.discard(jid)
            send({"cancelled": {"id": jid}})
            return
        kept = self.audio.kept_wav_path(job["outputPath"])
        if os.path.exists(kept):
            return self.encode_kept(job, kept)
        if not self.loaded:
            self.load(self.model_path)
        start = time.time()
        caps = self.backend.capabilities()
        request = self.make_request(job)
        seconds = request.max_seconds
        speed = self.speed or caps.seconds_per_song_second
        expected = max(seconds * speed, 1.0)
        state = {"stage": "composing", "fraction": 0.0, "overall": 0.0, "reported": False}
        state_lock = threading.Lock()

        from engine_host.native import Watchdog
        watch = Watchdog(120, what=f"recording {jid}")
        if getattr(self.backend, "remote", False):
            watch = contextlib.nullcontext()   # the work runs in another process: no stack to dump
        last_live = [None]

        def live():
            try:
                now = self.backend.live() or {}
            except Exception:  # noqa: BLE001 — counters are a nicety
                now = {}
            key = {k: v for k, v in now.items() if k != "stageElapsed"}  # a timer isn't progress
            if key != last_live[0]:  # a new token / step / decoded chunk counts as progress
                last_live[0] = key
                touch()
            return now

        def touch():
            if hasattr(watch, "touch"):
                watch.touch()

        def report(stage, stage_fraction, overall=None):
            touch()
            if stage not in STAGE_SPANS:
                stage = "composing"
            lo, hi = STAGE_SPANS[stage]
            value = lo + (hi - lo) * float(stage_fraction) if overall is None else float(overall) * GENERATION_END
            with state_lock:
                state.update(stage=stage, fraction=float(stage_fraction), reported=True,
                             overall=max(state["overall"], min(value, GENERATION_END)))
            send(progress_message(jid, stage, stage_fraction, state["overall"], time.time() - start, seconds, live()))

        # Heartbeat: estimated progress when the backend gives none, and proof of life either way
        # (the app restarts an engine that stays silent for ten minutes mid-song).
        done = threading.Event()

        def heartbeat():
            while not done.wait(1.0):
                elapsed = time.time() - start
                with state_lock:
                    if not state["reported"]:
                        fraction = min(0.97, elapsed / expected)
                        state.update(fraction=fraction, overall=fraction * GENERATION_END)
                    stage, fraction, overall = state["stage"], state["fraction"], state["overall"]
                    guessed = not state["reported"]
                # `estimated`: the fraction is a time-based guess, not the backend's own progress.
                send(progress_message(jid, stage, fraction, overall, elapsed, seconds,
                                      dict(live(), estimated=True) if guessed else dict(live(), estimated=False)))

        threading.Thread(target=heartbeat, daemon=True).start()
        from engine_host.backends import Cancelled
        try:
            with watch:  # stack dump to Logs/engine-stall.log only after 120 s without progress
                result = self.backend.generate(request, report, lambda: jid in self.cancelled)
        except Cancelled:
            result = None
        except Exception:
            if jid in self.cancelled:
                result = None  # the backend stopped because we asked it to
            else:
                raise
        finally:
            done.set()
        if jid in self.cancelled or result is None:
            self.cancelled.discard(jid)
            send({"cancelled": {"id": jid}})
            return

        send(progress_message(jid, "mastering", 0, GENERATION_END, time.time() - start, seconds))
        audio = self.audio.as_frames(result.samples)
        if audio.size == 0:
            raise RuntimeError("The model returned no audio.")
        audio = self.audio.trim_leading_silence(audio)
        audio, cut = self.audio.cut_and_fade(audio, result.sample_rate, seconds)
        duration = len(audio) / result.sample_rate
        send(progress_message(jid, "encoding", 0, 0.97, time.time() - start, seconds))
        try:
            self.audio.write_m4a(audio, result.sample_rate, job["outputPath"], self.ffmpeg, self.bitrate)
        except Exception as error:
            # Never throw a recording away: keep it as WAV; the next try for this song encodes it.
            kept = self.audio.kept_wav_path(job["outputPath"])
            try:
                os.makedirs(os.path.dirname(os.path.abspath(kept)), exist_ok=True)
                self.audio.write_wav(audio, result.sample_rate, kept)
                log(f"[audio] encoding failed; kept the recording at {kept}")
            except Exception as keep_error:  # noqa: BLE001
                log(f"[audio] couldn't keep the WAV either: {keep_error!r}")
                raise error
            raise RuntimeError(f"{error} The recording was kept ({os.path.basename(kept)}); press Retry "
                               "after fixing it and it will be encoded without recording again.") from error
        elapsed = time.time() - start
        measured = elapsed / max(duration, 1.0)
        self.speed = measured if self.speed is None else 0.7 * self.speed + 0.3 * measured
        send({"finished": {"_0": {
            "id": jid,
            "duration": duration,
            "loudness": self.audio.loudness(audio, result.sample_rate),
            "seconds": elapsed,
            "truncated": bool(cut or duration >= seconds - 0.5),
        }}})

    # MARK: main loop

    def run(self, stdin=None) -> None:
        if stdin is None:
            # Native libraries first, while no thread is blocked reading stdin (see native.py).
            if hasattr(self.backend, "preload"):
                self.backend.preload()
            stdin = native_stdin_lines() if sys.platform == "win32" else \
                io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8")
        work = []
        ready = threading.Condition()

        # Read commands on their own thread so `cancel` arrives while a song is rendering.
        def reader():
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    command = json.loads(line)
                except json.JSONDecodeError:
                    log("unreadable command: " + line[:200])
                    continue
                if "cancel" in command:
                    self.cancelled.add((command["cancel"] or {}).get("id"))
                    continue
                with ready:
                    work.append(command)
                    ready.notify()
            with ready:  # stdin closed: the app is gone
                work.append({"shutdown": {}})
                ready.notify()

        threading.Thread(target=reader, daemon=True).start()
        while True:
            with ready:
                while not work:
                    ready.wait()
                command = work.pop(0)
            if "shutdown" in command:
                sys.stderr.flush()
                os._exit(0)
            if "load" in command:
                try:
                    self.load((command["load"] or {}).get("modelPath"))
                except Exception as error:  # noqa: BLE001 — report everything to the app
                    log(f"load failed: {error!r}")
                    send({"failed": {"message": f"Couldn't load the model: {error}"}})
            elif "generate" in command:
                job = (command["generate"] or {}).get("_0") or {}
                try:
                    self.generate(job)
                except Exception as error:  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    send({"failed": {"id": job.get("id"), "message": str(error) or repr(error)}})
            elif "analyze" in command:
                job = (command["analyze"] or {}).get("_0") or {}
                try:
                    self.analyze(job)
                except Exception as error:  # noqa: BLE001
                    send({"failed": {"id": job.get("id"), "message": str(error) or repr(error)}})
            else:
                log("unknown command: " + json.dumps(command)[:200])


# MARK: command line

def reexec_under(python: str) -> None:
    """Re-run this program under another interpreter (e.g. a venv), so the #! line stays generic."""
    python = os.path.expanduser(python)
    if os.path.realpath(sys.executable) == os.path.realpath(python):
        return
    argv = list(sys.argv)
    i = argv.index("--python")
    rest = argv[:i] + argv[i + 2:]
    if os.name == "nt":
        # Windows has no real exec: os.execv would end this process while the child runs. Stay
        # alive with the same stdin/stdout/stderr and pass the exit code through.
        sys.exit(subprocess.call([python] + rest))
    os.execv(python, [python] + rest)


def load_config(path: Optional[str], program_dir: str) -> Dict:
    candidates = [path, os.environ.get("MUSIC_ENGINE_CONFIG"), os.path.join(program_dir, "engine.json")]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.expanduser(candidate)):
            with open(os.path.expanduser(candidate), encoding="utf-8-sig") as f:  # PowerShell 5 writes a BOM
                config = json.load(f)
            config["_path"] = os.path.expanduser(candidate)
            return config
        if candidate == path and path:
            raise FileNotFoundError(f"config file not found: {path}")
    return {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Ghost Label engine host (standardagents/music custom-engine protocol). Speaks docs/custom-engines.md on stdin/stdout.")
    parser.add_argument("--python", help="re-run under this interpreter (e.g. a venv's python)")
    parser.add_argument("--backend", help="backend name (default: $MUSIC_ENGINE_BACKEND, config, or 'acestep')")
    parser.add_argument("--config", help="JSON config file (default: $MUSIC_ENGINE_CONFIG or engine.json beside engine.py)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="backend option, repeatable (e.g. --set config_path=acestep-v15-turbo)")
    parser.add_argument("--ffmpeg", help="path to ffmpeg (default: $MUSIC_ENGINE_FFMPEG or PATH)")
    parser.add_argument("--bitrate", type=int, help="AAC bitrate in bit/s (default 256000)")
    parser.add_argument("--list-backends", action="store_true", help="print known backends and exit")
    return parser.parse_args(argv)


def main(program_path: str, argv=None) -> None:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--python" in raw:
        reexec_under(raw[raw.index("--python") + 1])
    claim_stdout()
    args = parse_args(raw)
    program_dir = os.path.dirname(os.path.abspath(program_path))
    from engine_host import backends

    if args.list_backends:
        log("\n".join(sorted(backends.BACKENDS)))
        return
    config = load_config(args.config, program_dir)
    for key, value in (config.get("env") or {}).items():
        os.environ.setdefault(key, str(value))  # the real environment wins over the file
    options = {k: str(v) for k, v in (config.get("options") or {}).items()}
    for item in args.set:
        key, _, value = item.partition("=")
        options[key.strip()] = value
    name = args.backend or os.environ.get("MUSIC_ENGINE_BACKEND") or config.get("backend") or "acestep"
    log(f"[engine-host] backend={name} python={sys.version.split()[0]} platform={sys.platform}"
        + (f" config={config['_path']}" if config.get("_path") else ""))
    try:
        backend = backends.create(name, options, log)
    except Exception as error:  # noqa: BLE001
        # Still speak the protocol so the app shows something readable.
        log(f"backend {name!r} unavailable: {error!r}")
        host_error = f"Backend {name!r} unavailable: {error}"

        class Broken:
            def capabilities(self):
                return backends.Capabilities()

            def map_steps(self, quality):
                return quality

            def load(self, path):
                raise RuntimeError(host_error)

        backend = Broken()
    Host(backend, program_dir, ffmpeg=args.ffmpeg or config.get("ffmpeg"),
         bitrate=args.bitrate or config.get("bitrate")).run()


