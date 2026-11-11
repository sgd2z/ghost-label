"""Test doubles: an OpenAI-compatible server and a Deezer-like catalog, both local."""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore import builder, links, placeholder, taste  # noqa: E402

TASKS = [  # (start of the user message, schema)
    ("Plan exactly one new release", builder.DISCOGRAPHY),
    ("Plan this artist's releases", builder.DISCOGRAPHY),
    ("Plan ", builder.ROSTER),
    ("Write this artist's dossier", builder.DOSSIER),
    ("Write this release", builder.ALBUM),
    ("Write this song", builder.SONG),
    ("Write the image prompt", builder.ART),
    ("Decide whether to ask", taste.QUESTION),
    ("Write the listener's taste", taste.PROFILE),
    ("Update the listener's taste", taste.PROFILE),
    ("Write the style brief", links.BRIEF),
    ("Refine the previous style brief", links.BRIEF),
    ("Write the mashup plan", links.MASHUP),
    ("Revise the mashup plan", links.MASHUP),
    ("Reply with {", {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}}}),
]


def example(schema, key="value", n=0, data=None):
    data = data or {}
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    if "enum" in schema:
        return schema["enum"][0]
    if kind == "object":
        return {k: example(v, k, n, data) for k, v in schema.get("properties", {}).items()}
    if kind == "array":
        count = {"artists": int(data.get("wanted") or 1), "releases": 1, "tracks": int(data.get("songCount") or 3),
                 "songs": 1}.get(key, 2)
        return [example(schema["items"], key[:-1] if key.endswith("s") else key, i, data) for i in range(count)]
    if kind == "integer":
        return {"year": 2019, "seconds": 30, "bpm": 96, "number": n + 1, "draws_on": n + 1}.get(key, n + 1)
    if kind == "number":
        return 0.5
    if kind == "boolean":
        return {"done": True, "ok": True, "instrumental": False, "explicit": False}.get(key, False)
    if key == "lyrics":
        return "[Verse]\nLights on the water\nI keep the time\n[Chorus]\nHold on, hold on"
    if key == "language":
        return "en"
    if key == "name" or key == "artist":
        return f"The Test Pattern {n + 1}" if not data.get("_counter") else f"Act {data['_counter']}-{n + 1}"
    return f"{key} {n + 1}"


class FakeProvider:
    """Records requests. Options: reject_json_schema (400 on json_schema), broken_first (first JSON
    reply malformed, to exercise repair), images ('b64' | 'url' | 'fail')."""

    def __init__(self, reject_json_schema=False, broken_first=False, images="b64", key=None, content=None, image_size=64,
                 chat_delay=0.0):
        self.requests = []
        self.chat_delay = chat_delay   # seconds each chat reply takes (a slow local model)
        self.in_flight = 0
        self.max_in_flight = 0
        self.custom = content is not None
        self.content = content or (lambda schema, data, user: example(schema, data=data))
        self.image_size = image_size
        self.reject_json_schema = reject_json_schema
        self.broken_first = broken_first
        self.images = images
        self.key = key
        self.counter = 0
        self.gets = []   # (path, Authorization) of GET requests
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, status, value, ctype="application/json"):
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                fake.gets.append((self.path, self.headers.get("Authorization")))
                if self.path.endswith("/models"):
                    if fake.key and self.headers.get("Authorization") != f"Bearer {fake.key}":
                        return self.reply(401, {"error": {"message": "bad key"}})
                    return self.reply(200, {"data": [{"id": "fake-text"}, {"id": "fake-fast"}, {"id": "fake-image", "labels": ["image"]}]})
                if self.path.startswith("/img.png"):
                    return self.reply(200, placeholder.artwork("url image", 64), "image/png")
                if self.path.startswith("/page"):
                    n = self.path.split("/")[-1]
                    body = (f"<html><head><title>Song {n} - Some Channel</title>"
                            f"<meta property='og:description' content='A slow song number {n} with strings'>"
                            "</head></html>").encode()
                    return self.reply(200, body, "text/html")
                return self.reply(404, {"error": "nope"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                if fake.key and self.headers.get("Authorization") != f"Bearer {fake.key}":
                    return self.reply(401, {"error": {"message": "bad key"}})
                if self.path.endswith("/unload"):
                    return self.reply(200, {"status": "success", "model_name": body.get("model_name")})
                if self.path.endswith("/images/generations"):
                    if fake.images == "fail":
                        return self.reply(404, {"error": "no images here"})
                    if fake.images == "url":
                        host = self.headers["Host"]
                        return self.reply(200, {"data": [{"url": f"http://{host}/img.png"}]})
                    prompt = str(body.get("prompt") or "img")
                    return self.reply(200, {"data": [{"b64_json": base64.b64encode(placeholder.artwork(prompt, fake.image_size)).decode()}]})
                if fake.chat_delay and self.path.endswith("/chat/completions"):
                    import time as _t
                    fake.in_flight += 1
                    fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
                    try:
                        _t.sleep(fake.chat_delay)
                    finally:
                        fake.in_flight -= 1
                fmt = body.get("response_format") or {}
                if fmt.get("type") == "json_schema" and fake.reject_json_schema:
                    return self.reply(400, {"error": {"message": "response_format json_schema is not supported"}})
                user = body["messages"][1]["content"] if len(body["messages"]) > 1 else ""
                last = body["messages"][-1]["content"]
                if last.startswith("That reply doesn't match"):
                    user = body["messages"][1]["content"]
                schema = next((s for prefix, s in TASKS if user.startswith(prefix)), None)
                if schema is None:
                    return self.reply(200, {"choices": [{"message": {"content": "{}"}}]})
                data = {}
                m = re.search(r"Data:\n(.*)$", user, re.S)
                if m:
                    try:
                        data = json.loads(m.group(1))
                    except ValueError:
                        data = {}
                fake.counter += 1
                data["_counter"] = fake.counter
                value = fake.content(schema, data, user)
                if fake.custom:
                    pass
                elif user.startswith("Decide whether") and not data.get("answersSoFar"):
                    value = {"question": "Do you like your music calm or lively?",
                             "options": [{"label": "Calm", "detail": "slow, gentle"}, "Lively"], "either": "A bit of both"}
                content = json.dumps(value)
                if fake.broken_first and fake.counter == 1:
                    content = "Sure! Here it is: {\"artists\": [ {\"concept\": \"missing name\"} ] }"
                else:
                    content = "<think>planning</think>\n```json\n" + content + "\n```"
                return self.reply(200, {"choices": [{"message": {"role": "assistant", "content": content}}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 20}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class FakeCatalog:
    def __init__(self, preview_path=None):
        fake = self
        self.preview_path = preview_path

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, value, ctype="application/json"):
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                host = self.headers["Host"]
                p = self.path
                artist = lambda i: {"id": i, "name": f"Real Artist {i}", "picture_medium": f"http://{host}/pic/{i}.jpg", "nb_fan": 1000 * i}
                if p.startswith("/genre/") and p.endswith("/artists"):
                    return self.reply({"data": [artist(i) for i in range(1, 9)]})
                if p.startswith("/genre"):
                    return self.reply({"data": [{"id": 0, "name": "All"}, {"id": 132, "name": "Pop"}, {"id": 106, "name": "Indian Music"}]})
                if p.startswith("/search/artist"):
                    return self.reply({"data": [artist(42)]})
                if p.startswith("/search/track"):
                    return self.reply({"data": [{"id": 7, "title": "A Real Song", "duration": 200, "preview": f"http://{host}/preview.wav",
                                                 "artist": {"id": 42, "name": "Real Artist 42"}, "album": {"id": 9, "title": "Real LP"}}]})
                if p.startswith("/album/"):
                    return self.reply({"genres": {"data": [{"name": "Films/Games"}]}, "release_date": "2014-01-01", "title": "Real LP"})
                if p.startswith("/preview.wav") and fake.preview_path:
                    return self.reply(open(fake.preview_path, "rb").read(), "audio/wav")
                if p.startswith("/pic/"):
                    return self.reply(placeholder.artwork(p, 32), "image/png")
                return self.reply({"data": []})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
