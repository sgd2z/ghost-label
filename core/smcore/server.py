"""Local HTTP API for the Electron UI (127.0.0.1 only, per-launch token). Also serves the UI
files, library media (with Range support for seeking) and a server-sent events stream."""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import errno
import secrets as pysecrets
import sys
import threading
import traceback
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from smcore.llm import interactive_lane

ROUTES = []


def route(method, pattern):
    def wrap(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return wrap


class HTTPFail(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# MARK: routes (fn(app, body, query, *groups) -> JSON-able)

@route("GET", "/api/state")
def _state(app, body, q):
    return app.state()


@route("GET", "/api/settings")
def _settings(app, body, q):
    return app.settings.get()


@route("PUT", "/api/settings")
def _settings_put(app, body, q):
    return app.settings.update(body or {})


@route("PUT", "/api/secret")
def _secret(app, body, q):
    name = body.get("name") or "apiKey"
    if name not in ("apiKey", "imagesApiKey", "hfToken"):
        raise HTTPFail(400, "unknown secret")
    r = app.secrets.set(name, body.get("value") or "")
    return {"ok": True, **r}


@route("PUT", "/api/provider")
def _provider_put(app, body, q):
    """Save the provider form in one call: settings (base URL, models, ...) and, if given, the keys.
    Reports where everything went after reading it back."""
    return app.save_provider(body.get("provider") or {}, body.get("apiKey"), body.get("imagesApiKey"))


@route("POST", "/api/provider/test")
def _provider_test(app, body, q):
    return app.provider.test()


@route("GET", "/api/provider/models")
def _provider_models(app, body, q):
    """Chat models for the Main/Fast pickers, fetched fresh (Settings calls this every time it opens)."""
    return app.provider.chat_models()


@route("POST", "/api/provider/test-images")
def _provider_test_images(app, body, q):
    return app.provider.test_images()


@route("POST", "/api/albums/([\\w-]+)/cover")
def _album_cover(app, body, q, album_id):
    """New cover with the current image settings (the AI writes a fresh prompt from the cover brief).
    A job: the answer comes back at once and the UI waits for the job, so a slow model never holds
    one of the window's few connections to the core."""
    row = app.db.one("SELECT id, title FROM albums WHERE id = ?", (album_id,))
    if not row:
        raise HTTPFail(404, "no such album")
    feedback = str((body or {}).get("feedback") or "").strip() or None
    return app.jobs.start("artwork", f"New cover: {row['title']}"[:60], lambda j: app.builder.cover(album_id, feedback),
                          {"albumId": album_id}, key=f"cover:{album_id}")


@route("POST", "/api/artists/([\\w-]+)/portrait")
def _artist_portrait(app, body, q, artist_id):
    row = app.db.one("SELECT id, name FROM artists WHERE id = ?", (artist_id,))
    if not row:
        raise HTTPFail(404, "no such artist")
    feedback = str((body or {}).get("feedback") or "").strip() or None
    return app.jobs.start("artwork", f"New photo: {row['name']}"[:60], lambda j: app.builder.portrait(artist_id, feedback),
                          {"artistId": artist_id}, key=f"portrait:{artist_id}")


@route("GET", "/api/queue")
def _queue(app, body, q):
    """Every AI queue: chat and image requests per server/model, the music engine's recording
    queue and background tasks, each item with its position in its resource's queue."""
    return app.queue.snapshot()


@route("POST", "/api/queue/cancel")
def _queue_cancel(app, body, q):
    return app.queue.cancel(str((body or {}).get("id") or ""))


@route("GET", "/api/home")
def _home(app, body, q):
    return app.library.home()


@route("GET", "/api/artists")
def _artists(app, body, q):
    return app.library.artists(q.get("sort", "name"))


@route("GET", "/api/artists/([\\w-]+)")
def _artist(app, body, q, artist_id):
    a = app.library.artist(artist_id)
    if not a:
        raise HTTPFail(404, "no such artist")
    return a


@route("DELETE", "/api/artists/([\\w-]+)")
def _artist_delete(app, body, q, artist_id):
    return dict(app.delete("artist", artist_id), ok=True)


@route("DELETE", "/api/albums/([\\w-]+)")
def _album_delete(app, body, q, album_id):
    return dict(app.delete("album", album_id), ok=True)


@route("DELETE", "/api/tracks/([\\w-]+)")
def _track_delete(app, body, q, track_id):
    return dict(app.delete("track", track_id), ok=True)


@route("GET", "/api/albums")
def _albums(app, body, q):
    return app.library.albums()


@route("GET", "/api/albums/([\\w-]+)")
def _album(app, body, q, album_id):
    a = app.library.album(album_id)
    if not a:
        raise HTTPFail(404, "no such album")
    return a


@route("GET", "/api/songs")
def _songs(app, body, q):
    return app.library.songs()


@route("GET", "/api/tracks/([\\w-]+)")
def _track(app, body, q, track_id):
    t = app.library.track(track_id)
    if not t:
        raise HTTPFail(404, "no such track")
    return t


@route("POST", "/api/tracks/([\\w-]+)/rewrite")
def _track_rewrite(app, body, q, track_id):
    job = app.jobs.start("lyrics", "Rewriting lyrics", lambda j: {"track": app.builder.write_song(track_id)["id"]})
    return job


@route("POST", "/api/tracks/([\\w-]+)/rerecord")
def _track_rerecord(app, body, q, track_id):
    row = app.db.one("SELECT lyrics FROM tracks WHERE id = ?", (track_id,))
    if not row:
        raise HTTPFail(404, "no such track")
    # A new take: a new seed, today's caption and vocal tags; the old audio stays until it's saved.
    # With feedback, the AI first revises the song's original values following it (a job).
    feedback = str((body or {}).get("feedback") or "").strip()
    if not feedback:
        app.db.update("tracks", track_id, {"seed": int.from_bytes(os.urandom(4), "big") % 2**31})
        return app.studio.rerecord(track_id)
    title = app.db.one("SELECT title FROM tracks WHERE id = ?", (track_id,))["title"]

    def run(job):
        changed = app.builder.retake(track_id, feedback)
        app.db.update("tracks", track_id, {"seed": int.from_bytes(os.urandom(4), "big") % 2**31})
        app.studio.rerecord(track_id)
        return {"trackId": track_id, "changed": changed.get("changed")}
    return app.jobs.start("retake", f"New take: {title}"[:60], run, {"trackId": track_id}, key=f"retake:{track_id}")


@route("GET", "/api/search")
def _search(app, body, q):
    return app.library.search(q.get("q", ""))


@route("GET", "/api/favorites")
def _favorites(app, body, q):
    return app.library.favorites()


@route("POST", "/api/favorite")
def _favorite(app, body, q):
    out = app.library.set_favorite(body["kind"], body["id"], bool(body.get("on", True)))
    app.after_signal()
    return out


@route("POST", "/api/play-event")
def _play_event(app, body, q):
    out = app.library.play_event(body["trackId"], body["kind"], body.get("position"))
    app.after_signal()
    return out


@route("GET", "/api/kv/([\\w.-]+)")
def _kv_get(app, body, q, key):
    return {"value": app.db.kv_get("ui:" + key)}


@route("PUT", "/api/kv/([\\w.-]+)")
def _kv_put(app, body, q, key):
    app.db.kv_set("ui:" + key, body.get("value"))
    return {"ok": True}


# create

def _need_provider(app):
    if not app.provider.configured():
        raise HTTPFail(400, "Set up an AI provider first (Settings › AI provider).")


@route("POST", "/api/create/build")
def _build(app, body, q):
    _need_provider(app)
    req = {k: body.get(k) for k in ("brief", "count", "seedArtistId", "output", "artistId", "songCount",
                                    "coreShare", "adjacentShare") if body.get(k) is not None}
    if req.get("seedArtistId"):
        name = (app.db.one("SELECT name FROM artists WHERE id = ?", (req["seedArtistId"],)) or {}).get("name", "")
        title = f"More like {name}"
    elif req.get("artistId"):
        title = "New release"
    elif req.get("brief"):
        title = req["brief"][:60]
    else:
        title = "Discover"
    return app.jobs.start("build", title, lambda j: app.builder.build(j, req), req)


@route("POST", "/api/create/custom")
def _custom(app, body, q):
    _need_provider(app)
    def run(j):
        out = app.builder.custom_song(j, body)
        app.studio.request([out["trackId"]])
        return out
    return app.jobs.start("custom", (body.get("title") or "Custom song")[:60], run, body)


@route("GET", "/api/jobs/([\\w-]+)")
def _job(app, body, q, job_id):
    row = app.jobs.decorate(app.jobs.row(job_id))
    if not row:
        raise HTTPFail(404, "no such job")
    return row


@route("GET", "/api/jobs")
def _jobs(app, body, q):
    return app.jobs.list()


@route("DELETE", "/api/jobs/([\\w-]+)")
def _job_dismiss(app, body, q, job_id):
    return {"ok": app.jobs.dismiss(job_id)}


@route("POST", "/api/jobs/([\\w-]+)/cancel")
def _job_cancel(app, body, q, job_id):
    return {"ok": app.jobs.cancel(job_id)}


# onboarding

@route("GET", "/api/catalog/genres")
def _genres(app, body, q):
    return app.catalog.genres()


@route("GET", "/api/catalog/genre/(\\d+)")
def _genre_artists(app, body, q, gid):
    return app.catalog.genre_artists(gid, int(q.get("limit", 30)))


@route("GET", "/api/catalog/search")
def _catalog_search(app, body, q):
    return app.catalog.search_artists(q.get("q", ""))


@route("GET", "/api/catalog/related/(\\d+)")
def _related(app, body, q, aid):
    return app.catalog.related(aid)


@route("GET", "/api/taste")
def _taste(app, body, q):
    return app.taste.state()


@route("POST", "/api/onboarding/next")
def _onboarding_next(app, body, q):
    _need_provider(app)
    tag = str(body.get("tag") or "onboarding")
    return app.taste.next_question(body.get("reactions") or [], body.get("answers") or [], int(body.get("max", 4)),
                                   status=lambda text: app.events.emit("llm", tag=tag, text=text))


@route("GET", "/api/llm/activity")
def _llm_activity(app, body, q):
    return app.llm_activity()


@route("GET", "/api/llm/problem")
def _llm_problem(app, body, q):
    return {"problem": app.provider.problem}


@route("POST", "/api/onboarding/finish")
def _onboarding_finish(app, body, q):
    _need_provider(app)
    return app.taste.finish(body.get("reactions") or [], body.get("answers") or [], body.get("notes") or "",
                            body.get("allowExplicit"))


@route("POST", "/api/taste/refresh")
def _taste_refresh(app, body, q):
    _need_provider(app)
    return app.jobs.start("taste", "Updating your taste", lambda j: app.taste.write_profile(
        listening=app.library.listening_summary()), {}, key="taste-refresh")


# studio + engine

@route("GET", "/api/studio")
def _studio(app, body, q):
    return app.studio.status()


@route("POST", "/api/runtime/ensure")
def _runtime_ensure(app, body, q):
    return app.runtime.ensure()


@route("POST", "/api/studio/need")
def _studio_need(app, body, q):
    return app.studio.need(body.get("trackIds") or [])


@route("POST", "/api/studio/request")
def _studio_request(app, body, q):
    ids = list(body.get("trackIds") or [])
    if body.get("albumId"):
        ids += [r["id"] for r in app.db.all("SELECT id FROM tracks WHERE album_id = ? AND status != 'recorded' ORDER BY number",
                                            (body["albumId"],))]
    return app.studio.request(ids)


@route("POST", "/api/studio/cancel")
def _studio_cancel(app, body, q):
    return app.studio.cancel(body["trackId"])


@route("POST", "/api/studio/pause")
def _studio_pause(app, body, q):
    return app.studio.set_paused(bool(body.get("paused", True)))


@route("GET", "/api/engine/detect")
def _detect(app, body, q):
    return app.detect()


@route("POST", "/api/engine/repair")
def _repair(app, body, q):
    return app.repair_engine()


@route("POST", "/api/engine/install")
def _install(app, body, q):
    return app.install_engine(body.get("profile") or "cpu", body.get("rocm") or "10.0.0", body.get("arch") or "gfx1151",
                              bool(body.get("downloadModels", True)))


@route("POST", "/api/engine/test-lemonade")
def _engine_test_lemonade(app, body, q):
    from smcore import lemonade
    return lemonade.test(app.settings, app.secrets)


@route("POST", "/api/engine/restart")
def _engine_restart(app, body, q):
    app.engine.stop()
    app.builtin_engine.stop()
    return app.engine.status()


@route("POST", "/api/engine/load")
def _engine_load(app, body, q):
    threading.Thread(target=lambda: _safe(app, app.engine.ensure_ready), daemon=True).start()
    return app.engine.status()


def _safe(app, fn):
    try:
        fn()
    except Exception as error:  # noqa: BLE001
        app.log(f"[engine] {error}")


@route("GET", "/api/logs/(engine|core)")
def _logs(app, body, q, name):
    path = os.path.join(app.paths.logs, name + ".log")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    return {"path": path, "text": text}


# links

@route("GET", "/api/links/sets")
def _sets(app, body, q):
    return app.links.sets()


@route("POST", "/api/links/sets")
def _set_create(app, body, q):
    _need_provider(app)
    urls = body.get("urls") or []
    if isinstance(urls, str):
        urls = [u for u in re.split(r"[\s,]+", urls) if u]
    return app.links.create(urls, body.get("note") or "", body.get("mode"), body.get("title"), body.get("kind"),
                            real_lyrics=bool(body.get("realLyrics")))


@route("GET", "/api/links/sets/([\\w-]+)")
def _set_get(app, body, q, set_id):
    s = app.links.get(set_id)
    if not s:
        raise HTTPFail(404, "no such set")
    return s


@route("DELETE", "/api/links/sets/([\\w-]+)")
def _set_delete(app, body, q, set_id):
    app.links.delete_set(set_id)
    return {"ok": True}


@route("POST", "/api/links/sets/([\\w-]+)/refine")
def _set_refine(app, body, q, set_id):
    _need_provider(app)
    return app.links.refine(set_id, body.get("instruction") or "")


@route("POST", "/api/links/tracks/([\\w-]+)/lyrics/fetch")
def _links_lyrics_fetch(app, body, q, track_id):
    """Fetch again: LRCLIB, then the video's captions (a job; the set page updates when it ends)."""
    return app.links.refetch_lyrics(track_id)


@route("PUT", "/api/links/tracks/([\\w-]+)/lyrics")
def _links_lyrics_save(app, body, q, track_id):
    return app.links.save_lyrics(track_id, (body or {}).get("text") or "")


@route("POST", "/api/links/tracks/([\\w-]+)/include")
def _set_include(app, body, q, track_id):
    app.links.set_included(track_id, bool(body.get("included", True)))
    return {"ok": True}


@route("POST", "/api/links/briefs/([\\w-]+)/generate")
def _brief_generate(app, body, q, brief_id):
    _need_provider(app)
    return app.links.generate(brief_id, body.get("output") or "artists", body.get("count"), body.get("songCount"),
                              body.get("artistId"))


# sharing

@route("POST", "/api/share/export")
def _share_export(app, body, q):
    return app.sharing.export(body["artistId"], body.get("albumIds"), bool(body.get("includeAudio", True)),
                              body.get("path"), body.get("sharedBy") or "")


@route("POST", "/api/share/preview")
def _share_preview(app, body, q):
    return app.sharing.preview(body["path"])


@route("POST", "/api/share/import")
def _share_import(app, body, q):
    return app.sharing.import_share(body["token"])


@route("POST", "/api/export/audio")
def _export_audio(app, body, q):
    return app.sharing.export_audio(body["folder"], body.get("albumIds"), body.get("artistId"))


# rules

@route("GET", "/api/rules")
def _rules(app, body, q):
    return [{"name": n, "text": app.rules.get(n), "edited": app.rules.is_edited(n)} for n in app.rules.names()]


@route("PUT", "/api/rules/(\\w+)")
def _rule_put(app, body, q, name):
    app.rules.set(name, body.get("text") or "")
    return {"ok": True}


@route("POST", "/api/rules/(\\w+)/reset")
def _rule_reset(app, body, q, name):
    app.rules.reset(name)
    return {"name": name, "text": app.rules.get(name), "edited": False}


# MARK: handler

# The UI aborting a request (a fetch cancelled, a page left, an audio seek): not an error.
DISCONNECTS = (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)
DISCONNECT_CODES = {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED, 10053, 10054}  # 1005x: WinError


def is_disconnect(error) -> bool:
    if isinstance(error, DISCONNECTS):
        return True
    return isinstance(error, OSError) and (getattr(error, "winerror", None) in DISCONNECT_CODES
                                           or error.errno in DISCONNECT_CODES)


def debug_log(app, text):
    """One line, only with GHOSTLABEL_DEBUG set."""
    if os.environ.get("GHOSTLABEL_DEBUG"):
        app.log(f"[http] {text}")


class CoreHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    app = None

    def handle_error(self, request, client_address):
        """socketserver prints a traceback for anything a request raised, including the client
        hanging up (Windows: ConnectionAbortedError [WinError 10053]). Those get a debug line at
        most; everything else keeps the traceback."""
        error = sys.exc_info()[1]
        if is_disconnect(error):
            if self.app is not None:
                debug_log(self.app, f"client {client_address[0]}:{client_address[1]} disconnected "
                                    f"({type(error).__name__})")
            return
        super().handle_error(request, client_address)


def make_handler(app, token, ui_dir):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "smcore"

        def log_message(self, fmt, *args):  # quiet
            pass

        def write(self, data, flush=False) -> bool:
            """Write to the client; False (and the connection closed) if it went away."""
            try:
                self.wfile.write(data)
                if flush:
                    self.wfile.flush()
                return True
            except OSError as error:
                if not is_disconnect(error):
                    raise
                self.close_connection = True
                debug_log(app, f"client went away during {self.command} {self.path} ({type(error).__name__})")
                return False

        def authorized(self):
            if self.headers.get("X-SM-Token") == token:
                return True
            jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            return "smtoken" in jar and jar["smtoken"].value == token

        def send_json(self, status, value, extra_headers=None):
            data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.write(data)

        def do_GET(self):
            self.dispatch("GET")

        def do_POST(self):
            self.dispatch("POST")

        def do_PUT(self):
            self.dispatch("PUT")

        def do_DELETE(self):
            self.dispatch("DELETE")

        def dispatch(self, method):
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                if path.startswith("/api/") or path.startswith("/media/"):
                    if not self.authorized():
                        return self.send_json(401, {"error": "unauthorized"})
                    if path == "/api/events":
                        return self.events()
                    if path.startswith("/media/"):
                        return self.media(path[len("/media/"):])
                    with interactive_lane():   # someone is waiting on this request
                        return self.api(method, path, query)
                return self.static(path, query)
            except OSError as error:
                if not is_disconnect(error):
                    raise
                self.close_connection = True
                debug_log(app, f"client went away during {method} {path} ({type(error).__name__})")

        def api(self, method, path, query):
            body = None
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    return self.send_json(400, {"error": "body must be JSON"})
            body = body or {}
            for m, pattern, fn in ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if match:
                    try:
                        return self.send_json(200, fn(app, body, query, *match.groups()))
                    except HTTPFail as fail:
                        return self.send_json(fail.status, {"error": str(fail)})
                    except (KeyError, ValueError) as error:
                        app.log(traceback.format_exc())
                        return self.send_json(400, {"error": f"{type(error).__name__}: {error}"})
                    except Exception as error:  # noqa: BLE001
                        app.log(traceback.format_exc())
                        return self.send_json(500, {"error": str(error) or repr(error)})
            return self.send_json(404, {"error": f"no route {method} {path}"})

        def events(self):
            q = app.events.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                if not self.write(b": hello\n\n", flush=True):
                    return
                while True:
                    try:
                        message = q.get(timeout=15)
                        data = b"data: " + message.encode("utf-8") + b"\n\n"
                    except queue.Empty:
                        data = b": ping\n\n"
                    if not self.write(data, flush=True):
                        return
            except OSError:
                pass   # the stream ends either way
            finally:
                app.events.unsubscribe(q)
                self.close_connection = True

        def serve_file(self, full, cache="no-cache"):
            size = os.path.getsize(full)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if full.endswith(".m4a"):
                ctype = "audio/mp4"
            start, end = 0, size - 1
            status = 200
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", cache)
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(full, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    if not self.write(chunk):
                        return
                    left -= len(chunk)

        def media(self, rel):
            full = os.path.realpath(os.path.join(app.paths.home, *rel.split("/")))
            if not full.startswith(os.path.realpath(app.paths.home) + os.sep) or not os.path.isfile(full):
                return self.send_json(404, {"error": "not found"})
            self.serve_file(full, "private, max-age=86400")

        def static(self, path, query):
            extra = {}
            if query.get("token") == token:
                extra["Set-Cookie"] = f"smtoken={token}; Path=/; HttpOnly; SameSite=Strict"
            if not ui_dir:
                return self.send_json(200, {"app": "smcore", "ui": None}, extra)
            rel = path.lstrip("/") or "index.html"
            full = os.path.realpath(os.path.join(ui_dir, rel))
            if not full.startswith(os.path.realpath(ui_dir)) or not os.path.isfile(full):
                full = os.path.join(ui_dir, "index.html")
            if extra:
                self.send_response(200)
                data = open(full, "rb").read()
                self.send_header("Content-Type", mimetypes.guess_type(full)[0] or "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Set-Cookie", extra["Set-Cookie"])
                self.end_headers()
                self.write(data)
                return
            self.serve_file(full)

    return Handler


def serve(app, host="127.0.0.1", port=0, token=None, ui_dir=None):
    token = token or pysecrets.token_urlsafe(24)
    server = CoreHTTPServer((host, port), make_handler(app, token, ui_dir))
    server.app = app
    return server, token
