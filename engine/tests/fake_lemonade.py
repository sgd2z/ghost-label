"""A stand-in for Lemonade Server's music API, for tests: /models/{id}, /health, /unload and a
synchronous /audio/generations that answers with a 24-bit WAV (as acestep.cpp does) or an error."""

import json
import os
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np


def wav24(seconds=2.0, rate=48000, channels=2, freq=330.0):
    t = np.arange(int(seconds * rate)) / rate
    x = 0.5 * np.sin(2 * np.pi * freq * t)
    v = np.round(np.repeat(x[:, None], channels, axis=1) * ((1 << 23) - 1)).astype(np.int32).reshape(-1)
    v = np.clip(v, -(1 << 23), (1 << 23) - 1) & 0xFFFFFF
    raw = np.stack([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF], axis=1).astype(np.uint8).tobytes()
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * channels * 3, channels * 3, 24)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(raw)) + raw
    return b"RIFF" + struct.pack("<I", len(body)) + body


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lemonade_models.json")


def realistic():
    """(catalog, loaded) from lemonade_models.json: a custom extra_models_dir GGUF, SDXL-Turbo
    loaded but marked not downloaded, ACE-Step-Music pulled, and catalog models not pulled."""
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    return data["models"], data["loaded"]


class FakeLemonade:
    def __init__(self, model="ACE-Step-Music", downloaded=True, key="", catalog=None, loaded=None):
        self.model, self.downloaded, self.key = model, downloaded, key
        # catalog: full model entries (GET /models?show_all=true); None = just the music model.
        self.catalog, self.loaded = catalog, list(loaded or [])
        self.requests = []       # (method, path, json body)
        self.reply = None        # (status, json) to fail /audio/generations; None = a song
        self.delay = 0.0
        self.seconds = 2.0
        self.load_delay = 0.0
        self.chat_models = []    # models that answered /chat/completions
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, status, payload, ctype="application/json"):
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the engine stopped waiting (a cancelled song)

            def handle_any(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}") if length else None
                path = self.path.split("?")[0]
                for prefix in ("/api/v1", "/v1"):
                    if path.startswith(prefix + "/"):
                        path = path[len(prefix):]
                        break
                fake.requests.append((method, path, body, self.headers.get("Authorization")))
                if fake.key and self.headers.get("Authorization") != f"Bearer {fake.key}":
                    return self.send(401, {"error": {"message": "Invalid API key", "type": "auth_error"}})
                query = self.path.split("?", 1)[1] if "?" in self.path else ""
                if path == "/health":
                    return self.send(200, {"status": "ok", "version": "test", "all_models_loaded": fake.loaded,
                                           "model_loaded": fake.loaded[0]["model_name"] if fake.loaded else None})
                if fake.catalog is not None and path.startswith("/models"):
                    if path == "/models":
                        rows = fake.catalog if "show_all=true" in query else [m for m in fake.catalog if m["downloaded"]]
                        return self.send(200, {"object": "list", "data": rows})
                    row = next((m for m in fake.catalog if m["id"] == path[len("/models/"):]), None)
                    if row is None:
                        return self.send(404, {"error": {"message": "model not found", "type": "model_not_found"}})
                    return self.send(200, row)
                if path.startswith("/models/"):
                    name = path[len("/models/"):]
                    if name != fake.model:
                        return self.send(404, {"error": {"message": f"model {name} not found", "type": "model_not_found"}})
                    return self.send(200, {"id": fake.model, "recipe": "acestep", "downloaded": fake.downloaded,
                                           "labels": ["audio-generation"], "size": 10.5})
                if path == "/models":
                    return self.send(200, {"data": [{"id": fake.model, "recipe": "acestep", "downloaded": fake.downloaded,
                                                     "labels": ["audio-generation"]}]})
                if path == "/images/generations":
                    time.sleep(fake.delay)
                    import base64
                    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
                    return self.send(200, {"created": 0, "data": [{"b64_json": base64.b64encode(png).decode()}]})
                if path == "/chat/completions":
                    names = {m["id"] for m in fake.catalog or [] if m.get("downloaded")} | \
                        {m["model_name"] for m in fake.loaded}
                    if body.get("model") not in names:
                        return self.send(404, {"error": {"message": f"Model '{body.get('model')}' not found",
                                                         "type": "not_found"}})
                    fake.chat_models.append(body.get("model"))
                    return self.send(200, {"choices": [{"message": {"role": "assistant",
                                                                    "content": '{"ok": true, "word": "hi"}'}}]})
                if path == "/load":
                    name = (body or {}).get("model_name")
                    time.sleep(fake.load_delay)
                    if name and name not in [m["model_name"] for m in fake.loaded]:
                        fake.loaded.append({"model_name": name, "type": "image", "loaded": True})
                    return self.send(200, {"status": "success", "model_name": name})
                if path == "/unload":
                    return self.send(200, {"status": "success"})
                if path == "/audio/generations":
                    time.sleep(fake.delay)
                    if fake.reply:
                        return self.send(*fake.reply)
                    if body.get("model") != fake.model:
                        return self.send(404, {"error": {"message": "not found", "type": "model_not_found"}})
                    return self.send(200, wav24(fake.seconds), "audio/wav")
                return self.send(404, {"error": {"message": "no route", "type": "not_found"}})

            def do_GET(self):
                self.handle_any("GET")

            def do_POST(self):
                self.handle_any("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, path):
        return [r[2] for r in self.requests if r[0] == "POST" and r[1] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
