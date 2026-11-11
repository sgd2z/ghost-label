"""ACE-Step 1.5 served by AMD Lemonade Server (model `ACE-Step-Music`: acestep.cpp's ace-server).

Lemonade's `POST /v1/audio/generations` takes {model, prompt, lyrics, vocal_language, duration,
steps, seed, response_format:"wav"} and answers once, with the whole song as a WAV (24-bit) or a
JSON {"error": {...}}. It reports no progress and can't be cancelled: a stopped song is abandoned
here, and Lemonade finishes it in the background. It forwards no reference audio, bpm or key.

Options (`--set key=value`, or the environment):
  url       Lemonade's API base, e.g. http://localhost:13305/v1   ($LEMONADE_URL)
  model     the music model                                      ($LEMONADE_MUSIC_MODEL; ACE-Step-Music)
  api_key   only when Lemonade runs with LEMONADE_API_KEY         ($LEMONADE_API_KEY)
  timeout   seconds to wait for one song (default 3600)
"""

from __future__ import annotations

import json
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from engine_host.backends import Audio, Backend, Cancelled, Capabilities

DEFAULT_URL = "http://localhost:13305/v1"
DEFAULT_MODEL = "ACE-Step-Music"
PULL = "lemonade pull ACE-Step-Music"

# ACE-Step 1.5's vocal languages (acestep/constants.py VALID_LANGUAGES, minus "unknown");
# Lemonade passes `vocal_language` straight to ace-server.
LANGUAGES = ["ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "he", "hi",
             "hr", "ht", "hu", "id", "is", "it", "ja", "ko", "la", "lt", "ms", "ne", "nl", "no", "pa", "pl",
             "pt", "ro", "ru", "sa", "sk", "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi",
             "yue", "zh"]


class LemonadeError(RuntimeError):
    pass


def api_base(url):
    """Lemonade serves /v1, /api/v1, /v0 and /api/v0; a bare host gets /v1."""
    url = (url or DEFAULT_URL).strip().rstrip("/")
    path = urllib.parse.urlparse(url).path
    return url if path not in ("", "/") else url + "/v1"


def decode_wav(data: bytes):
    """PCM 16/24/32-bit or float32 WAV bytes -> (float32 frames x channels, sample rate)."""
    import numpy as np
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise LemonadeError("Lemonade's reply wasn't a WAV file.")
    pos, fmt, samples = 12, None, None
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            tag, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if tag == 0xFFFE and len(body) >= 26:   # WAVE_FORMAT_EXTENSIBLE: the real tag is in the GUID
                tag = struct.unpack("<H", body[24:26])[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            samples = body
        pos += 8 + size + (size & 1)
    if not fmt or samples is None:
        raise LemonadeError("Lemonade's WAV had no audio.")
    tag, channels, rate, bits = fmt
    width = bits // 8
    samples = samples[:len(samples) - len(samples) % (width * channels)]
    if tag == 3 and bits == 32:
        x = np.frombuffer(samples, dtype="<f4").astype(np.float32)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        b = np.frombuffer(samples, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = (np.where(v >= 1 << 23, v - (1 << 24), v)).astype(np.float32) / float(1 << 23)
    elif tag == 1 and bits == 32:
        x = np.frombuffer(samples, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise LemonadeError(f"Lemonade sent a WAV format this engine can't read (format {tag}, {bits}-bit).")
    return x.reshape(-1, channels), rate


def explain(status, payload, model, url):
    """Lemonade's JSON error -> a message saying what to do. Uses only the status and error type."""
    err = (payload or {}).get("error") if isinstance(payload, dict) else None
    err = err if isinstance(err, dict) else {"message": str(err or payload or "")}
    message = (err.get("message") or "").strip()
    kind = err.get("type") or err.get("code") or ""
    if status == 404 or kind == "model_not_found":
        return f"Lemonade doesn't have {model}. In a terminal run: lemonade pull {model}"
    if kind == "model_load_error":
        return f"Lemonade couldn't load the music model {model}: {message or 'no reason given'}. " \
               "Free GPU memory (unload other models) and try again."
    if status == 502:
        return f"Lemonade's music backend stopped without a song (it crashed or ran out of GPU memory): " \
               f"{message or 'no output'}."
    if status == 401:
        return "Lemonade asked for an API key (LEMONADE_API_KEY is set on the server); add it in Settings › AI provider."
    return f"Lemonade failed ({status}): {message or 'no message'}"


class LemonadeBackend(Backend):
    name = "lemonade"
    remote = True   # the work happens in another process: no Python stack to watch for stalls
    # quality -> inference steps for ACE-Step-Music (the XL SFT model, not a turbo one): the same
    # table as the built-in engine's non-turbo models. `steps_map=8:0,...` sends none (model default).
    steps_map = {8: 32, 16: 48, 32: 64}

    def __init__(self, options=None, log=print):
        super().__init__(options, log)
        self.url = api_base(self.option("url", None, ["LEMONADE_URL"]))
        self.model = self.option("model", DEFAULT_MODEL, ["LEMONADE_MUSIC_MODEL"]) or DEFAULT_MODEL
        self.key = self.option("api_key", None, ["LEMONADE_API_KEY"]) or ""
        self.timeout = float(self.option("timeout", "3600") or 3600)
        self.started = None

    def capabilities(self):
        return Capabilities(max_seconds=600.0, supports_lyrics=True, cooperative_cancel=True,
                            reports_progress=False, seconds_per_song_second=1.0, analyze=False,
                            reference_audio=False, languages=list(LANGUAGES))

    def live(self):
        if self.started is None:
            return {}
        return {"detail": "Lemonade is making the song (it reports no progress)"}

    # MARK: HTTP

    def request(self, method, path, body=None, timeout=15.0):
        headers = {"Accept": "application/json, audio/wav"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers.get("Content-Type") or "", r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type") or "", e.read()
        except (urllib.error.URLError, OSError) as e:
            raise LemonadeError(f"Lemonade isn't answering at {self.url} ({getattr(e, 'reason', e)}). "
                                "Start Lemonade Server, or fix the URL in Settings › Music engine.") from None

    @staticmethod
    def parse(content):
        try:
            return json.loads(content.decode("utf-8", "replace"))
        except ValueError:
            return {"error": {"message": content[:300].decode("utf-8", "replace")}}

    # MARK: Backend

    def load(self, model_path):
        """Checks the server and that the music model is downloaded. It doesn't load the model:
        Lemonade loads it on the first song, so nothing sits in memory before it is needed."""
        self.status(f"Checking Lemonade at {self.url}")
        status, _ctype, content = self.request("GET", "/models/" + urllib.parse.quote(self.model))
        info = self.parse(content) if status == 200 else {}
        if (status != 200 or info.get("downloaded") is False) and self.loaded_now():
            self.log(f"[lemonade] {self.model} is loaded (its model entry: HTTP {status}, "
                     f"downloaded={info.get('downloaded')})")
            return   # loaded per /health: usable whatever the list says
        if status != 200:
            raise LemonadeError(explain(status, self.parse(content), self.model, self.url))
        if info.get("downloaded") is False:
            raise LemonadeError(f"Lemonade knows {self.model} but hasn't downloaded it. In a terminal run: "
                                f"lemonade pull {self.model}")
        self.log(f"[lemonade] {self.model} at {self.url} ({info.get('recipe') or 'recipe?'}, "
                 f"{info.get('size') or '?'} GB)")

    def loaded_now(self):
        """Lemonade's /health lists the models it has loaded."""
        try:
            status, _ctype, content = self.request("GET", "/health")
        except LemonadeError:
            return False
        health = self.parse(content) if status == 200 else {}
        names = {m.get("model_name") for m in health.get("all_models_loaded") or [] if isinstance(m, dict)}
        return self.model in names or health.get("model_loaded") == self.model

    def is_loaded_for(self, model_path):
        return True

    def body(self, request):
        lyrics = "" if request.instrumental else (request.lyrics or "")
        from engine_host.backends.acestep import caption_with_voice
        body = {"model": self.model, "prompt": caption_with_voice(request.style, None if request.instrumental else request.voice),
                "lyrics": lyrics.strip() or "[Instrumental]", "duration": int(round(request.max_seconds)),
                "seed": int(request.seed), "response_format": "wav"}
        if request.vocal_language:
            body["vocal_language"] = request.vocal_language
        if request.steps:
            body["steps"] = int(request.steps)
        return body

    def generate(self, request, progress, cancelled):
        body = self.body(request)
        self.log(f"[lemonade] POST {self.url}/audio/generations "
                 + json.dumps({k: v for k, v in body.items() if k != "lyrics"}, ensure_ascii=False))
        box = {}

        def call():
            try:
                box["reply"] = self.request("POST", "/audio/generations", body, timeout=self.timeout)
            except Exception as error:  # noqa: BLE001 — handed to the caller below
                box["error"] = error

        self.started = time.time()
        worker = threading.Thread(target=call, daemon=True, name="lemonade-request")
        worker.start()
        try:
            while worker.is_alive():
                worker.join(0.25)
                if cancelled():
                    # Lemonade can't stop a song; it finishes in the background and we drop it.
                    self.log("[lemonade] stopped waiting; Lemonade finishes this song in the background")
                    raise Cancelled()
        finally:
            self.started = None
        if "error" in box:
            raise box["error"]
        status, ctype, content = box["reply"]
        if status == 200 and content[:4] == b"RIFF":
            samples, rate = decode_wav(content)
            return Audio(samples, rate)
        raise LemonadeError(explain(status if status != 200 else 502, self.parse(content), self.model, self.url))
