"""Links -> similar music. A list of links (videos, songs, playlists) plus an optional note
becomes a numbered set of source tracks; each is analysed on its own (page metadata, possible
catalog matches, optionally the local music model's audio analysis); the AI writes a versioned
style brief from all of it; the listener refines it ("more like track 3", "less sad"); the
builder makes new artists, albums or songs from it.

The code here only fetches and measures. Interpreting, merging and blending is the AI's job,
guided by rules/links.md."""

from __future__ import annotations

import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

from smcore import net
from smcore.db import new_id
from smcore.runtime import ffprobe_for, run_tool

S = {"type": "string"}
I = {"type": "integer"}
INTS = {"type": "array", "items": I}
B = {"type": "boolean"}

BRIEF = {"type": "object", "required": ["title", "shared", "per_track", "plan"], "properties": {
    "title": {"type": "string", "description": "a short name for this style brief"},
    "summary": {"type": "string", "description": "one or two sentences for the listener"},
    "shared": S,
    "per_track": {"type": "array", "items": {"type": "object", "required": ["number", "contributes"],
                                             "properties": {"number": I, "contributes": S}}},
    "interpretation": S,
    "language_guidance": S,
    "plan": {"type": "array", "minItems": 1, "items": {"type": "object", "required": ["kind", "description"], "properties": {
        "kind": {"type": "string", "enum": ["artist", "release", "song"]},
        "idea": S, "description": S, "draws_on": INTS, "blend": S}}}}}

N = {"type": "number"}
MASHUP_MAX_SONGS = 4      # a hard cap on how many songs one mashup request makes

MASHUP = {"type": "object", "required": ["title", "summary", "per_track", "artist", "songs"], "properties": {
    "title": {"type": "string", "description": "a short name for this mashup"},
    "summary": {"type": "string", "description": "one or two sentences for the listener"},
    "heard": {"type": "string", "description": "which sources you could actually hear and which you only read about"},
    "per_track": {"type": "array", "items": {"type": "object", "required": ["number", "contributes"],
                                             "properties": {"number": I, "contributes": S}}},
    "interpretation": S,
    "artist": {"type": "object", "required": ["name", "voice", "vocal_tags"], "properties": {
        "name": S, "concept": S, "voice": S, "visual": S, "genres": {"type": "array", "items": S},
        "vocal_tags": {"type": "string", "description": "lead-vocal caption tags starting with who sings, or 'instrumental'"}}},
    "cover_brief": S,
    "songs": {"type": "array", "minItems": 1, "items": {"type": "object",
              "required": ["title", "caption", "lyrics", "sources"], "properties": {
        "title": S,
        "caption": {"type": "string", "description": "the blended style caption for the music model, under 400 characters"},
        "bpm": {"type": ["integer", "null"]}, "key": {"type": ["string", "null"]},
        "language": S, "instrumental": B, "seconds": I,
        "structure": {"type": "string", "description": "the section plan, e.g. intro, verse, chorus, bridge, outro"},
        "lyrics": S,
        "lyrics_credits": {"type": "array", "description": "lyricsMode real: which source each section's lines come from",
                           "items": {"type": "object", "required": ["section", "sources"], "properties": {
                               "section": S, "sources": INTS, "note": S}}},
        "vocal_tags": {"type": "string", "description": "lead-vocal caption tags for this song, or 'instrumental'"},
        "sources": {"type": "array", "items": {"type": "object", "required": ["number", "share"], "properties": {
            "number": I, "share": {"type": "number", "description": "0 to 1: how much of this source's sound goes into the reference audio"},
            "takes": S}}}}}}}}

KEEP_META = ("title", "fulltitle", "uploader", "channel", "creator", "artist", "track", "album", "release_year",
             "release_date", "upload_date", "duration", "categories", "tags", "language", "description",
             "webpage_url", "view_count", "like_count", "chapters", "playlist_title", "alt_title", "genre")


def trim_meta(info: dict) -> dict:
    out = {}
    for key in KEEP_META:
        value = info.get(key)
        if value in (None, "", []):
            continue
        if key == "description":
            value = value[:1500]
        elif key == "tags":
            value = value[:30]
        elif key == "chapters":
            value = [c.get("title") for c in value[:30] if isinstance(c, dict)]
        out[key] = value
    return out


def youtube_id(url: str):
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/") or None
    if "youtube" in host:
        q = urllib.parse.parse_qs(parsed.query)
        if q.get("v"):
            return q["v"][0]
        m = re.match(r"/(shorts|embed|live)/([\w-]+)", parsed.path)
        if m:
            return m.group(2)
    return None


class Fetcher:
    """yt-dlp (Python module or binary) when available; YouTube oEmbed and page metadata otherwise."""

    def __init__(self, settings, paths, log=print, ffmpeg=None):
        self.settings, self.paths, self.log = settings, paths, log
        self.ffmpeg = ffmpeg or (lambda: shutil.which("ffmpeg"))   # the app passes Runtime.ffmpeg_path

    def ytdlp_binary(self):
        configured = self.settings.section("links").get("ytdlp")
        if configured and os.path.exists(configured):
            return configured
        local = os.path.join(self.paths.envs, "tools", "yt-dlp.exe" if os.name == "nt" else "yt-dlp")
        if os.path.exists(local):
            return local
        return shutil.which("yt-dlp")

    def module(self):
        try:
            import yt_dlp  # type: ignore
            return yt_dlp
        except Exception:  # noqa: BLE001
            return None

    def available(self):
        return {"module": self.module() is not None, "binary": self.ytdlp_binary()}

    def _run_binary(self, args, timeout=180):
        binary = self.ytdlp_binary()
        if not binary:
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        out = subprocess.run([binary] + args, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                             encoding="utf-8", errors="replace")
        if out.returncode != 0:
            raise RuntimeError("yt-dlp: " + (out.stderr.strip().splitlines() or ["failed"])[-1])
        return out.stdout

    def info(self, url: str, flat: bool):
        mod = self.module()
        if mod is not None:
            opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": False}
            if flat:
                opts["extract_flat"] = "in_playlist"
            with mod.YoutubeDL(opts) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        args = ["-J", "--skip-download", "--no-warnings"] + (["--flat-playlist"] if flat else []) + [url]
        text = self._run_binary(args)
        return json.loads(text) if text else None

    def expand(self, url: str, limit: int):
        """A link -> [(url, partial metadata)]. Playlists expand to their entries (capped)."""
        try:
            info = self.info(url, flat=True)
        except Exception as error:  # noqa: BLE001
            self.log(f"[links] yt-dlp couldn't read {url}: {error}")
            info = None
        if info and info.get("_type") == "playlist":
            out = []
            for entry in (info.get("entries") or [])[:limit]:
                if not entry:
                    continue
                link = entry.get("url") or entry.get("webpage_url")
                if link and not link.startswith("http") and entry.get("id"):
                    link = f"https://www.youtube.com/watch?v={entry['id']}"
                if link:
                    out.append((link, dict(trim_meta(entry), playlist_title=info.get("title"))))
            return out, "yt-dlp"
        if info:
            return [(info.get("webpage_url") or url, trim_meta(info))], "yt-dlp"
        return [(url, self.fallback(url))], "page"

    def metadata(self, url: str, partial: dict):
        try:
            info = self.info(url, flat=False)
            if info:
                return dict(partial, **trim_meta(info)), "yt-dlp"
        except Exception as error:  # noqa: BLE001
            self.log(f"[links] full metadata failed for {url}: {error}")
        return dict(self.fallback(url), **partial), "page"

    def fallback(self, url: str) -> dict:
        if youtube_id(url):
            try:
                o = net.get_json("https://www.youtube.com/oembed?" + urllib.parse.urlencode({"url": url, "format": "json"}))
                return {"title": o.get("title"), "channel": o.get("author_name"), "thumbnail": o.get("thumbnail_url"),
                        "webpage_url": url, "source": "oembed"}
            except Exception as error:  # noqa: BLE001
                self.log(f"[links] oEmbed failed for {url}: {error}")
        try:
            page = net.download(url, timeout=20)[:400000].decode("utf-8", "replace")
        except Exception as error:  # noqa: BLE001
            return {"webpage_url": url, "error": str(error)[:200]}
        meta = {"webpage_url": url, "source": "page"}
        m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        if m:
            meta["title"] = html.unescape(m.group(1).strip())[:300]
        for prop, key in (("og:title", "title"), ("og:description", "description"), ("og:image", "thumbnail"),
                          ("music:musician", "artist"), ("og:site_name", "site")):
            m = re.search(r'<meta[^>]+(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]+content=["\']([^"\']*)', page, re.I)
            if m:
                meta[key] = html.unescape(m.group(1))[:1500]
        return meta

    def audio_clip(self, url: str, name: str, seconds: float):
        """Fetch the audio (yt-dlp, told where our ffmpeg is) and keep a `seconds`-long WAV clip."""
        target_dir = os.path.join(self.paths.clips, "dl")
        os.makedirs(target_dir, exist_ok=True)
        template = os.path.join(target_dir, name + ".%(ext)s")
        ffmpeg = self.ffmpeg()
        mod = self.module()
        if mod is not None:
            opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "outtmpl": template, "noplaylist": True}
            if ffmpeg:
                opts["ffmpeg_location"] = ffmpeg
            with mod.YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
        elif self.ytdlp_binary():
            self._run_binary(["-f", "bestaudio/best", "--no-playlist", "-o", template]
                             + (["--ffmpeg-location", ffmpeg] if ffmpeg else []) + [url], timeout=900)
        else:
            raise RuntimeError("Audio analysis needs yt-dlp (install it in Settings › Links).")
        files = [f for f in os.listdir(target_dir) if f.startswith(name + ".")]
        if not files:
            raise RuntimeError("yt-dlp produced no audio file")
        return self.cut(os.path.join(target_dir, files[0]), name, seconds)

    def captions(self, url: str, language: str, automatic: bool) -> str:
        """One caption track as WebVTT text (yt-dlp, no media downloaded)."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="gl-subs-") as tmp:
            template = os.path.join(tmp, "subs.%(ext)s")
            mod = self.module()
            if mod is not None:
                opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                        "outtmpl": template, "subtitleslangs": [language], "subtitlesformat": "vtt",
                        "writesubtitles": not automatic, "writeautomaticsub": automatic}
                with mod.YoutubeDL(opts) as ydl:
                    ydl.extract_info(url, download=True)
            elif self.ytdlp_binary():
                self._run_binary(["--skip-download", "--no-playlist", "--sub-langs", language, "--sub-format", "vtt",
                                  "--write-auto-subs" if automatic else "--write-subs", "-o", template, url], timeout=180)
            else:
                raise RuntimeError("Captions need yt-dlp (install it in Settings › Engine).")
            files = sorted(f for f in os.listdir(tmp) if f.endswith(".vtt"))
            if not files:
                return ""
            with open(os.path.join(tmp, files[0]), encoding="utf-8", errors="replace") as f:
                return f.read()

    def preview_clip(self, preview_url: str, name: str, seconds: float):
        target = os.path.join(self.paths.clips, "dl", name + ".mp3")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(net.download(preview_url))
        return self.cut(target, name, seconds)

    def cut(self, source, name, seconds):
        ffmpeg = self.ffmpeg()
        out = os.path.join(self.paths.clips, name + ".wav")
        if not ffmpeg:
            self.log("[links] ffmpeg not found; using the downloaded file uncut")
            return source
        duration = probe_seconds(ffmpeg, source)
        start = max(0.0, min(30.0, (duration - seconds) / 2)) if duration else 0.0
        run_tool([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", source,
                  "-ac", "2", "-ar", "48000", out], timeout=300)
        if os.path.abspath(source) != os.path.abspath(out):
            try:
                os.remove(source)
            except OSError:
                pass
        return out


def probe_seconds(ffmpeg, path):
    """Duration in seconds via the ffprobe next to our ffmpeg (or on PATH); None if unknown."""
    ffprobe = ffprobe_for(ffmpeg)
    if not ffprobe:
        return None
    try:
        out = run_tool([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       timeout=60, check=False, text=True)
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


class Links:
    def __init__(self, db, paths, settings, provider, rules, catalog, library, engine, builder, jobs, events, log=print,
                 ffmpeg=None):
        self.db, self.paths, self.settings, self.provider, self.rules = db, paths, settings, provider, rules
        self.catalog, self.library, self.engine, self.builder = catalog, library, engine, builder
        self.jobs, self.events, self.log = jobs, events, log
        self.fetcher = Fetcher(settings, paths, log, ffmpeg)
        from smcore.sourcelyrics import LyricsFinder
        self.lyrics_finder = LyricsFinder(settings, self.fetcher, log)
        self.studio = None   # set by the app; mashup takes are queued for recording

    # MARK: reads

    def sets(self):
        rows = self.db.all("SELECT * FROM source_sets ORDER BY created DESC")
        for r in rows:
            r["tracks"] = self.db.scalar("SELECT COUNT(*) FROM source_tracks WHERE set_id = ?", (r["id"],))
            latest = self.latest_brief(r["id"])
            r["briefTitle"] = latest["brief"].get("title") if latest else None
        return rows

    def latest_brief(self, set_id):
        return self.db.one("SELECT * FROM style_briefs WHERE set_id = ? ORDER BY version DESC LIMIT 1", (set_id,))

    def get(self, set_id):
        s = self.db.one("SELECT * FROM source_sets WHERE id = ?", (set_id,))
        if not s:
            return None
        s["tracks"] = self.db.all("SELECT * FROM source_tracks WHERE set_id = ? ORDER BY position", (set_id,))
        s["options"] = s.get("options") or {}
        if s["options"].get("realLyrics"):
            found = {r["track_id"]: r for r in self.db.all(
                "SELECT l.* FROM source_lyrics l JOIN source_tracks t ON t.id = l.track_id WHERE t.set_id = ?", (set_id,))}
            for t in s["tracks"]:
                t["lyrics"] = found.get(t["id"]) or {"status": "pending"}
        s["briefs"] = self.db.all("SELECT * FROM style_briefs WHERE set_id = ? ORDER BY version DESC", (set_id,))
        s["artists"] = [self.library.artist_card(a) for a in
                        self.db.all("SELECT * FROM artists WHERE source_set_id = ? ORDER BY created", (set_id,))]
        albums = self.db.all(
            "SELECT al.* FROM albums al JOIN style_briefs b ON b.id = al.brief_id WHERE b.set_id = ? ORDER BY al.created",
            (set_id,))
        s["albums"] = [self.library.album_card(a) for a in albums]
        if s.get("mode") == "mashup":
            # Every take (one per plan version and song), newest first, with its plan version.
            takes = []
            for a in albums:
                for t in self.db.all("SELECT id, data_json FROM tracks WHERE album_id = ? ORDER BY number DESC", (a["id"],)):
                    view = self.library.track(t["id"])
                    if view:
                        view["mashup"] = (t.get("data") or {}).get("mashup") or {}
                        takes.append(view)
            s["takes"] = takes
        return s

    # MARK: create + analyse

    def create(self, urls, note="", mode=None, title=None, kind=None, real_lyrics=False):
        """kind "mashup": one new song (or a few, if the note asks) blending the sources. The audio is
        always fetched and analysed for it, whatever the default link mode is."""
        urls = [u.strip() for u in urls if u and u.strip()]
        if not urls:
            raise ValueError("Paste at least one link.")
        mode = "mashup" if kind == "mashup" else (mode or self.settings.section("links").get("mode") or "metadata")
        set_id = new_id("ss_")
        options = {"realLyrics": True} if mode == "mashup" and real_lyrics else {}
        self.db.insert("source_sets", {"id": set_id, "title": title, "note": note or "", "mode": mode,
                                       "status": "analysing", "options_json": options, "created": time.time()})
        what = "Listening to the tracks for a mashup" if mode == "mashup" else "Analysing links"
        job = self.jobs.start("links", what, lambda j: self.analyse(j, set_id, urls), {"setId": set_id})
        return {"setId": set_id, "job": job}

    def analyse(self, job, set_id, urls):
        try:
            return self._analyse(job, set_id, urls)
        except Exception as error:
            self.db.update("source_sets", set_id, {"status": "error"})
            self.events.emit("links", setId=set_id, error=str(error))
            raise

    def _analyse(self, job, set_id, urls):
        cfg = self.settings.section("links")
        limit = int(cfg.get("maxTracks") or 50)
        entries = []
        for i, url in enumerate(urls):
            job.progress(0.02 + 0.1 * i / len(urls), f"Reading link {i + 1} of {len(urls)}")
            expanded, resolver = self.fetcher.expand(url, limit - len(entries))
            entries.extend((u, m, resolver) for u, m in expanded)
            if len(entries) >= limit:
                break
        mode = (self.db.one("SELECT mode FROM source_sets WHERE id = ?", (set_id,)) or {}).get("mode") or "metadata"
        if mode == "mashup" and len(entries[:limit]) < 2:
            raise ValueError("A mashup needs at least two tracks (two links, or a playlist with two or more songs).")
        for position, (url, meta, resolver) in enumerate(entries[:limit], start=1):
            self.db.insert("source_tracks", {"id": new_id("st_"), "set_id": set_id, "position": position, "url": url,
                                             "resolver": resolver, "title": meta.get("title"),
                                             "channel": meta.get("channel") or meta.get("uploader"),
                                             "duration": meta.get("duration"), "thumbnail": meta.get("thumbnail"),
                                             "metadata_json": meta, "status": "listed"})
        self.events.emit("links", setId=set_id)
        rows = self.db.all("SELECT * FROM source_tracks WHERE set_id = ? ORDER BY position", (set_id,))
        for n, row in enumerate(rows):
            job.progress(0.12 + 0.68 * n / max(1, len(rows)), f"Analysing track {row['position']} of {len(rows)}")
            self.analyse_track(row, mode)
            self.events.emit("links", setId=set_id)
        if mode == "mashup" and self.real_lyrics(set_id):
            for n, row in enumerate(rows):
                job.progress(0.80 + 0.02 * n / max(1, len(rows)), f"Finding the lyrics of track {row['position']}")
                self.fetch_lyrics(row["id"])
                self.events.emit("links", setId=set_id)
        if mode == "mashup":
            job.progress(0.82, "Planning the mashup")
            brief = self.write_mashup(set_id)
            self.db.update("source_sets", set_id, {"status": "ready", "title": self.db.one(
                "SELECT title FROM source_sets WHERE id = ?", (set_id,))["title"] or brief["brief"].get("title")})
            job.progress(0.92, "Writing the mashup song")
            made = self.make_mashup(brief["id"])
            self.events.emit("links", setId=set_id)
            return dict(made, setId=set_id, briefId=brief["id"])
        job.progress(0.85, "Writing the style brief")
        brief = self.write_brief(set_id, instruction=None)
        self.db.update("source_sets", set_id, {"status": "ready", "title": self.db.one(
            "SELECT title FROM source_sets WHERE id = ?", (set_id,))["title"] or brief["brief"].get("title")})
        self.events.emit("links", setId=set_id)
        return {"setId": set_id, "briefId": brief["id"]}

    def analyse_track(self, row, mode):
        meta = row.get("metadata") or {}
        values = {}
        try:
            if row["resolver"] == "yt-dlp" and not meta.get("description"):
                meta, resolver = self.fetcher.metadata(row["url"], meta)
                values.update({"metadata_json": meta, "resolver": resolver, "title": meta.get("title") or row["title"],
                               "channel": meta.get("channel") or meta.get("uploader") or row["channel"],
                               "duration": meta.get("duration") or row["duration"]})
            query = " ".join(x for x in [meta.get("artist") or "", meta.get("track") or meta.get("title") or ""] if x)
            candidates = self.catalog.search_tracks(query) if query else []
            values["catalog_json"] = candidates
            analysis = {}
            if mode in ("audio", "voice", "mashup"):
                analysis = self.audio_analysis(row, meta, candidates)
                if analysis.get("clip"):
                    values["clip_path"] = self.paths.rel(analysis.pop("clip"))
            values["analysis_json"] = analysis
            values["status"] = "analysed"
        except Exception as error:  # noqa: BLE001
            values.update(status="error", error=str(error)[:500])
        self.db.update("source_tracks", row["id"], values)

    def analyzer(self):
        """The engine that listens to clips: the selected one, or the built-in ACE-Step when Lemonade
        is selected (Lemonade has no audio understanding)."""
        from smcore import lemonade
        from smcore.engine_client import EngineError
        studio = getattr(self, "studio", None)
        if not (studio and lemonade.selected(self.settings)):
            return self.engine
        if not studio.builtin_installed():
            raise EngineError("Listening to the audio needs the built-in ACE-Step engine (Lemonade can't analyze "
                              "audio). Install it in Settings › Music engine; the brief uses the page details meanwhile.")
        lemonade.unload(self.settings, studio.secrets, lemonade.model(self.settings), self.log)
        return studio.builtin

    def audio_analysis(self, row, meta, candidates):
        cfg = self.settings.section("links")
        seconds = float(cfg.get("analysisSeconds") or 90)
        out = {}
        try:
            if cfg.get("audioSource") == "preview":
                preview = next((c.get("preview") for c in candidates if c.get("preview")), None)
                if not preview:
                    return {"error": "no catalog preview found for this track"}
                clip = self.fetcher.preview_clip(preview, row["id"], min(seconds, 30))
                out["clipSource"] = "catalog preview of the first candidate (may not be the same recording)"
            else:
                clip = self.fetcher.audio_clip(row["url"], row["id"], seconds)
                out["clipSource"] = "the linked audio"
            out["clip"] = clip
        except Exception as error:  # noqa: BLE001
            return {"error": f"couldn't fetch audio: {error}"[:400]}
        try:
            result = self.analyzer().analyze(clip)
            if isinstance(result.get("lyrics"), str):
                result["lyrics"] = result["lyrics"][:600]  # evidence of language/themes only
            out["model"] = result
        except Exception as error:  # noqa: BLE001
            out["modelError"] = str(error)[:400]
        if not self.settings.section("links").get("keepClips") and row and self.db.one(
                "SELECT mode FROM source_sets WHERE id = ?", (row["set_id"],))["mode"] not in ("voice", "mashup"):
            try:
                os.remove(clip)
            except OSError:
                pass
            out.pop("clip", None)
        return out

    # MARK: the source songs' own lyrics (only for a mashup that asks for them)

    def real_lyrics(self, set_id) -> bool:
        row = self.db.one("SELECT options_json FROM source_sets WHERE id = ?", (set_id,))
        return bool(((row or {}).get("options") or {}).get("realLyrics"))

    def _store_lyrics(self, track_id, values):
        values = dict(values, track_id=track_id, updated=time.time())
        values["detail_json"] = values.pop("detail", None) or {}
        cols = ["track_id", "status", "source", "synced", "language", "text", "synced_text", "edited", "error",
                "detail_json", "updated"]
        from smcore.db import encode
        row = encode({c: values.get(c) for c in cols})
        row["synced"] = 1 if row.get("synced") else 0
        row["edited"] = 1 if row.get("edited") else 0
        self.db.run(f"INSERT OR REPLACE INTO source_lyrics ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    tuple(row[c] for c in cols))

    def lyrics_of(self, track_id):
        return self.db.one("SELECT * FROM source_lyrics WHERE track_id = ?", (track_id,))

    def fetch_lyrics(self, track_id) -> dict:
        """LRCLIB first, then the video's captions; the result (or 'none found') is stored per
        source track. An edited text is replaced only by an explicit Fetch again."""
        row = self.db.one("SELECT * FROM source_tracks WHERE id = ?", (track_id,))
        if not row:
            raise ValueError("No such source track")
        self._store_lyrics(track_id, {"status": "fetching"})
        try:
            found = self.lyrics_finder.find(row.get("metadata") or {}, row["url"])
        except Exception as error:  # noqa: BLE001
            found = {"status": "error", "error": str(error)[:400]}
        self._store_lyrics(track_id, found)
        self.log(f"[links] lyrics for #{row['position']} {row.get('title')!r}: {found['status']}"
                 + (f" ({found.get('source')}{', synced' if found.get('synced') else ''})" if found.get("source") else ""))
        return self.lyrics_of(track_id)

    def refetch_lyrics(self, track_id):
        row = self.db.one("SELECT set_id, position FROM source_tracks WHERE id = ?", (track_id,))
        if not row:
            raise ValueError("No such source track")
        self._store_lyrics(track_id, {"status": "fetching"})
        self.events.emit("links", setId=row["set_id"])

        def run(job):
            out = self.fetch_lyrics(track_id)
            self.events.emit("links", setId=row["set_id"])
            return {"setId": row["set_id"], "status": out["status"]}
        return self.jobs.start("lyrics-fetch", f"Finding the lyrics of track {row['position']}", run,
                               {"setId": row["set_id"]}, key=f"lyrics:{track_id}")

    def save_lyrics(self, track_id, text):
        row = self.db.one("SELECT set_id FROM source_tracks WHERE id = ?", (track_id,))
        if not row:
            raise ValueError("No such source track")
        old = self.lyrics_of(track_id) or {}
        text = (text or "").strip()
        self._store_lyrics(track_id, {"status": "found" if text else "none", "source": old.get("source") or "edited",
                                      "synced": False, "language": old.get("language"), "text": text,
                                      "synced_text": None, "edited": True, "detail": old.get("detail") or {}})
        self.events.emit("links", setId=row["set_id"])
        return self.lyrics_of(track_id)

    def source_lyrics(self, set_id):
        """What the mashup planner gets: each included source's lyrics as plain text."""
        rows = self.db.all("SELECT t.position, l.* FROM source_lyrics l JOIN source_tracks t ON t.id = l.track_id "
                           "WHERE t.set_id = ? AND t.included = 1 ORDER BY t.position", (set_id,))
        out = []
        for r in rows:
            if r["status"] == "found" and (r.get("text") or "").strip():
                item = {"number": r["position"], "source": r.get("source"), "language": r.get("language"),
                        "edited": bool(r.get("edited")), "lyrics": r["text"]}
                if (r.get("detail") or {}).get("captions"):
                    item["captions"] = r["detail"]["captions"]   # "automatic" = speech recognition
                out.append(item)
            elif r["status"] == "instrumental":
                out.append({"number": r["position"], "instrumental": True})
        return out

    # MARK: briefs

    def track_payload(self, set_id):
        rows = self.db.all("SELECT * FROM source_tracks WHERE set_id = ? AND included = 1 ORDER BY position", (set_id,))
        return [{"number": r["position"], "url": r["url"], "metadata": r.get("metadata") or {},
                 "catalogCandidates": r.get("catalog") or [], "audioAnalysis": r.get("analysis") or {},
                 **({"error": r["error"]} if r.get("error") else {})} for r in rows]

    def write_brief(self, set_id, instruction=None, parent=None):
        s = self.db.one("SELECT * FROM source_sets WHERE id = ?", (set_id,))
        data = {"sourceTracks": self.track_payload(set_id), "listenerNote": s.get("note") or "",
                "tasteProfile": (self.library.taste() or {}).get("profile") or {}}
        if self.builder.engine_languages:
            data["musicModelVocalLanguages"] = self.builder.engine_languages
        task = "Write the style brief for these source tracks."
        if parent:
            data["previousBrief"] = parent["brief"]
            data["instruction"] = instruction
            task = "Refine the previous style brief following the listener's instruction."
        brief = self.provider.json(self.rules.system("links"), task + "\n\nData:\n" + json.dumps(data, ensure_ascii=False, indent=1),
                                   BRIEF, name="style_brief")
        version = (self.db.scalar("SELECT MAX(version) FROM style_briefs WHERE set_id = ?", (set_id,)) or 0) + 1
        row = {"id": new_id("sb_"), "set_id": set_id, "version": version, "parent_id": parent["id"] if parent else None,
               "instruction": instruction, "brief_json": brief, "model": self.provider.config().get("textModel"),
               "created": time.time()}
        self.db.insert("style_briefs", row)
        return self.db.one("SELECT * FROM style_briefs WHERE id = ?", (row["id"],))

    def refine(self, set_id, instruction):
        parent = self.latest_brief(set_id)
        if not parent:
            raise ValueError("This set has no brief yet.")
        mode = (self.db.one("SELECT mode FROM source_sets WHERE id = ?", (set_id,)) or {}).get("mode")
        if mode == "mashup":
            def take(job):
                job.progress(0.2, "Revising the mashup")
                b = self.write_mashup(set_id, instruction, parent)
                job.progress(0.8, "Writing the new take")
                made = self.make_mashup(b["id"])
                self.events.emit("links", setId=set_id)
                return dict(made, briefId=b["id"])

            return self.jobs.start("links-refine", f"New take: {instruction[:40]}", take, {"setId": set_id})

        def run(job):
            job.progress(0.2, "Refining the brief")
            b = self.write_brief(set_id, instruction, parent)
            self.events.emit("links", setId=set_id)
            return {"briefId": b["id"]}

        return self.jobs.start("links-refine", f"Refining: {instruction[:40]}", run, {"setId": set_id})

    def delete_set(self, set_id):
        """The set, its briefs and its downloaded clips. Music made from it stays."""
        for r in self.db.all("SELECT clip_path FROM source_tracks WHERE set_id = ?", (set_id,)):
            path = self.paths.abs(r.get("clip_path"))
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self.db.run("DELETE FROM source_sets WHERE id = ?", (set_id,))

    def set_included(self, track_id, included):
        self.db.update("source_tracks", track_id, {"included": 1 if included else 0})

    def generate(self, brief_id, output="artists", count=None, song_count=None, artist_id=None):
        b = self.db.one("SELECT * FROM style_briefs WHERE id = ?", (brief_id,))
        if not b:
            raise ValueError("No such brief")
        plan = b["brief"].get("plan") or []
        if count is None:
            count = max(1, sum(1 for p in plan if p.get("kind") == "artist")) if output == "artists" else 1
        req = {"output": output, "count": count, "styleBrief": b["brief"], "briefId": brief_id,
               "sourceSetId": b["set_id"], "songCount": song_count, "artistId": artist_id,
               "coreShare": 1.0, "adjacentShare": 0.0}
        title = b["brief"].get("title") or "style brief"
        return self.jobs.start("build", f"Making music from “{title}”", lambda j: self.builder.build(j, req), req)

    # MARK: mashups (one new song blending the sources; the AI decides everything about it)

    def write_mashup(self, set_id, instruction=None, parent=None):
        s = self.db.one("SELECT * FROM source_sets WHERE id = ?", (set_id,))
        tracks = self.track_payload(set_id)
        eng = self.settings.section("engine")
        data = {"sourceTracks": tracks, "listenerNote": s.get("note") or "",
                "tasteProfile": (self.library.taste() or {}).get("profile") or {},
                "heardCount": sum(1 for t in tracks if (t.get("audioAnalysis") or {}).get("model")),
                "limits": {"maxSongs": MASHUP_MAX_SONGS, "maxSongSeconds": int(float(eng.get("maxSongMinutes") or 4) * 60)},
                "explicitLyricsAllowed": bool(self.settings.section("library").get("allowExplicit"))}
        if self.builder.engine_languages:
            data["musicModelVocalLanguages"] = self.builder.engine_languages
        task = "Write the mashup plan for these source tracks."
        if self.real_lyrics(set_id):
            data["lyricsMode"] = "real"
            data["sourceLyrics"] = self.source_lyrics(set_id)
            task = "Write the mashup plan for these source tracks, using the songs' real lyrics (sourceLyrics)."
        if parent:
            data["previousPlan"] = parent["brief"]
            data["instruction"] = instruction
            task = "Revise the mashup plan following the listener's instruction."
        plan = self.provider.json(self.rules.system("mashup"), task + "\n\nData:\n" + json.dumps(data, ensure_ascii=False, indent=1),
                                  MASHUP, name="mashup")
        version = (self.db.scalar("SELECT MAX(version) FROM style_briefs WHERE set_id = ?", (set_id,)) or 0) + 1
        row = {"id": new_id("sb_"), "set_id": set_id, "version": version, "parent_id": parent["id"] if parent else None,
               "instruction": instruction, "brief_json": plan, "model": self.provider.config().get("textModel"),
               "created": time.time()}
        self.db.insert("style_briefs", row)
        self.log(f"[links] mashup plan v{version} for {set_id}: {plan.get('title')!r}")
        return self.db.one("SELECT * FROM style_briefs WHERE id = ?", (row["id"],))

    def mashup_album(self, set_id):
        return self.db.one("SELECT al.* FROM albums al JOIN style_briefs b ON b.id = al.brief_id WHERE b.set_id = ? "
                           "ORDER BY al.created LIMIT 1", (set_id,))

    def make_mashup(self, brief_id):
        """Store the plan's song(s) as new takes on the set's mashup single (made on the first plan,
        with its fictional artist) and queue them for recording. Code only copies and caps values."""
        b = self.db.one("SELECT * FROM style_briefs WHERE id = ?", (brief_id,))
        plan, set_id = b["brief"], b["set_id"]
        eng = self.settings.section("engine")
        max_seconds = float(eng.get("maxSongMinutes") or 4) * 60
        numbers = {r["position"] for r in self.db.all("SELECT position FROM source_tracks WHERE set_id = ?", (set_id,))}
        album = self.mashup_album(set_id)
        if album:
            artist = self.db.one("SELECT * FROM artists WHERE id = ?", (album["artist_id"],))
        else:
            artist = self._mashup_artist(plan, b)
            album = self._mashup_single(plan, b, artist)
        start = int(self.db.scalar("SELECT MAX(number) FROM tracks WHERE album_id = ?", (album["id"],)) or 0)
        ids = []
        for offset, song in enumerate((plan.get("songs") or [])[:MASHUP_MAX_SONGS], start=1):
            from smcore.builder import says_instrumental
            instrumental = (bool(song.get("instrumental")) or says_instrumental(song.get("vocal_tags"))
                            or says_instrumental(song.get("lyrics")))
            sources = []
            for src in song.get("sources") or []:
                try:
                    number, share = int(src.get("number")), float(src.get("share") or 0)
                except (TypeError, ValueError):
                    continue
                if number in numbers:
                    sources.append({"number": number, "share": max(0.0, min(1.0, share))})
            bpm = song.get("bpm") if isinstance(song.get("bpm"), int) and 30 <= song["bpm"] <= 300 else None
            seconds = song.get("seconds") if isinstance(song.get("seconds"), int) and song["seconds"] > 0 else None
            track_id = new_id("tr_")
            title = (song.get("title") or plan.get("title") or "Mashup").strip()
            lyrics = "[Instrumental]" if instrumental else (song.get("lyrics") or "")
            vocal = "" if instrumental else (song.get("vocal_tags") or (artist.get("data") or {}).get("vocalTags") or "").strip()
            self.db.insert("tracks", {
                "id": track_id, "album_id": album["id"], "artist_id": artist["id"], "number": start + offset,
                "title": title, "theme": song.get("structure"), "style": (song.get("caption") or "")[:512],
                "lyrics": lyrics[:4096], "is_instrumental": 1 if instrumental else 0, "language": song.get("language"),
                "bpm": bpm, "keyscale": song.get("key") or None, "seed": random.randrange(2**31),
                "target_seconds": min(float(seconds), max_seconds) if seconds else None,
                "reference_track_ids_json": [x["number"] for x in sources if x["share"] > 0],
                "status": "written", "created": time.time(),
                "data_json": {"vocalTags": vocal[:160],
                              "mashup": {"setId": set_id, "briefId": brief_id, "version": b["version"],
                                         "instruction": b.get("instruction"), "sources": sources,
                                         "structure": song.get("structure"),
                                         "lyricsCredits": song.get("lyrics_credits") or [],
                                         "realLyrics": self.real_lyrics(set_id)}}})
            self.db.index("track", track_id, title, artist["name"], lyrics)
            ids.append(track_id)
        if not ids:
            raise RuntimeError("The mashup plan had no songs.")
        self.events.emit("library", album=album["id"])
        if self.studio is not None:
            self.studio.request(ids)
        return {"artistId": artist["id"], "albumId": album["id"], "trackIds": ids}

    def _mashup_artist(self, plan, b):
        a = plan.get("artist") or {}
        name = (a.get("name") or plan.get("title") or "Mashup").strip()
        artist_id = new_id("ar_")
        self.db.insert("artists", {
            "id": artist_id, "name": name, "sort_name": name.lower(), "bio": a.get("concept") or plan.get("summary"),
            "voice": a.get("voice"), "genres_json": a.get("genres") or [], "members_json": [], "visual": a.get("visual"),
            "relation": "core", "source_set_id": b["set_id"], "brief_id": b["id"], "created": time.time(),
            "data_json": {"concept": {"name": name, "concept": a.get("concept")}, "mashup": True,
                          "vocalTags": (a.get("vocal_tags") or "").strip()[:160]}})
        self.db.index("artist", artist_id, name, ", ".join(a.get("genres") or []), a.get("concept") or "")
        try:
            self.builder.portrait(artist_id)
        except Exception as error:  # noqa: BLE001
            self.log(f"[links] mashup portrait failed: {error!r}")
        return self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))

    def _mashup_single(self, plan, b, artist):
        album_id = new_id("al_")
        title = (plan.get("title") or "Mashup").strip()
        position = self.db.scalar("SELECT COUNT(*) FROM albums WHERE artist_id = ?", (artist["id"],))
        self.db.insert("albums", {
            "id": album_id, "artist_id": artist["id"], "title": title, "year": int(time.strftime("%Y")), "kind": "single",
            "concept": plan.get("summary"), "liner_notes": plan.get("interpretation") or plan.get("summary"),
            "cover_brief": plan.get("cover_brief"), "position": position, "status": "ready", "brief_id": b["id"],
            "created": time.time(), "data_json": {"sourceSetId": b["set_id"], "mashup": True}})
        self.db.index("album", album_id, title, artist["name"], plan.get("summary") or "")
        try:
            self.builder.cover(album_id)
        except Exception as error:  # noqa: BLE001
            self.log(f"[links] mashup cover failed: {error!r}")
        return self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
