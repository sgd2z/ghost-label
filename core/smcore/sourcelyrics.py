"""The real lyrics of a link set's source songs, for a mashup that uses them. Fetched only when
the listener asks for it, from two open sources: LRCLIB (a free, open lyrics database with a
public API; https://lrclib.net/docs) and, failing that, the video's own captions through yt-dlp.
No lyrics websites are scraped.

Matching is mechanical: the title, artist and duration the link already gave us, and the closest
duration among the results. Which lines go into the mashup is the AI's call (rules/mashup.md)."""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse

from smcore import net

LRCLIB = "https://lrclib.net"
MAX_CHARS = 8000                 # per song, so the plan request stays a sensible size
DURATION_TOLERANCE = 20          # seconds: a video can carry a longer intro or outro than the record

TIMESTAMP = re.compile(r"^\s*(\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s*)+")
CUE_TIME = re.compile(r"^\s*(\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
TAG = re.compile(r"<[^>]+>")


def lrc_to_text(lrc: str) -> str:
    """LRC -> plain lines (timestamps and [ar:]/[ti:] header tags removed)."""
    out = []
    for line in (lrc or "").splitlines():
        if re.match(r"^\s*\[[a-z]+:.*\]\s*$", line, re.I):
            continue
        out.append(TIMESTAMP.sub("", line).rstrip())
    return "\n".join(out).strip()


def vtt_to_text(vtt: str) -> str:
    """WebVTT captions -> plain lines: header, cue numbers, timings and markup removed; the rolling
    repeats of auto-captions (each cue repeating the previous line) and annotation-only lines
    such as a bracketed sound cue dropped."""
    lines, last = [], None
    recent = []
    for raw in (vtt or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("WEBVTT", "NOTE", "Kind:", "Language:", "STYLE", "REGION")):
            continue
        if CUE_TIME.match(line) or line.isdigit():
            continue
        line = html.unescape(TAG.sub("", line)).strip()
        if not line:
            continue
        if re.fullmatch(r"[\[(（].*[\])）]", line) or re.fullmatch(r"[\W_]+", line):
            continue      # only an annotation or symbols (♪, a bracketed cue)
        if line == last or line in recent:
            continue      # auto-captions repeat each line in the next cue
        lines.append(line)
        last = line
        recent = (recent + [line])[-3:]
    return "\n".join(lines).strip()


def norm(text) -> str:
    """For comparing names: lower case, bracketed parts removed, punctuation to spaces."""
    text = re.sub(r"[\(\[\{（【][^\)\]\}）】]*[\)\]\}）】]", " ", (text or "").lower())
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def contains(haystack, needle) -> bool:
    """needle's words appear in haystack as a run (word boundaries, after norm())."""
    h, n = norm(haystack), norm(needle)
    return bool(n) and f" {n} " in f" {h} "


def _clean(value):
    return (value or "").strip()


def query_of(meta: dict) -> dict:
    """What the link told us: track, artist, album, duration and the raw title."""
    artist = _clean(meta.get("artist") or meta.get("creator"))
    channel = _clean(meta.get("channel") or meta.get("uploader"))
    if not artist and channel.endswith(" - Topic"):     # YouTube's auto-generated artist channels
        artist = channel[: -len(" - Topic")].strip()
    track = _clean(meta.get("track") or meta.get("alt_title"))
    duration = meta.get("duration")
    try:
        duration = float(duration) if duration else None
    except (TypeError, ValueError):
        duration = None
    return {"track": track, "artist": artist, "album": _clean(meta.get("album")), "duration": duration,
            "title": _clean(meta.get("title") or meta.get("fulltitle")), "channel": channel,
            "language": _clean(meta.get("language")) or None}


class LyricsFinder:
    def __init__(self, settings, fetcher, log=print):
        self.settings, self.fetcher, self.log = settings, fetcher, log

    def base(self):
        return (self.settings.section("links").get("lrclibUrl") or LRCLIB).rstrip("/")

    # MARK: LRCLIB

    def _get(self, path, params):
        url = self.base() + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        for attempt in range(2):
            try:
                return net.request("GET", url, timeout=20)
            except net.HTTPError as error:
                if error.status == 404:
                    return None
                if error.status in (429, 503) and attempt == 0:
                    time.sleep(min(10.0, float(getattr(error, "retry_after", 0) or 3)))
                    continue
                raise
        return None

    def lrclib(self, q: dict):
        """The best LRCLIB record for this track, or None."""
        records = []
        if q["track"] and q["artist"]:
            exact = self._get("/api/get", {"track_name": q["track"], "artist_name": q["artist"], "album_name": q["album"],
                                           "duration": int(round(q["duration"])) if q["duration"] else None})
            if isinstance(exact, dict) and exact.get("id"):
                return exact
            found = self._get("/api/search", {"track_name": q["track"], "artist_name": q["artist"]})
            records += found if isinstance(found, list) else []
        if not records and q["title"]:
            found = self._get("/api/search", {"q": q["title"]})
            records += found if isinstance(found, list) else []
        return self.pick([r for r in records if self.same_song(r, q)], q["duration"])

    @staticmethod
    def same_song(rec, q) -> bool:
        """A search result counts only when its title and its artist both appear in what the link
        says (the track/artist fields, else the video title and channel). Text comparison only."""
        if not isinstance(rec, dict):
            return False
        title_side = " ".join(x for x in (q["track"], q["title"]) if x)
        artist_side = " ".join(x for x in (q["artist"], q["channel"], q["title"]) if x)
        return contains(title_side, rec.get("trackName")) and contains(artist_side, rec.get("artistName"))

    @staticmethod
    def pick(records, duration):
        """With lyrics, then the closest duration (within the tolerance when the duration is known)."""
        usable = [r for r in records if isinstance(r, dict) and (r.get("plainLyrics") or r.get("syncedLyrics") or r.get("instrumental"))]
        if not usable:
            return None
        if not duration:
            return usable[0]
        usable.sort(key=lambda r: abs(float(r.get("duration") or 0) - duration))
        best = usable[0]
        return best if abs(float(best.get("duration") or 0) - duration) <= DURATION_TOLERANCE else None

    # MARK: captions

    def caption_language(self, info: dict, language=None):
        """Which caption track to use: the uploader's own subtitles (in the song's language when
        known), else the automatic captions in the original spoken language ("-orig"), else the
        automatic captions in the song's language."""
        subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v}
        auto = {k: v for k, v in (info.get("automatic_captions") or {}).items() if v}
        lang = language or info.get("language")
        if subs:
            if lang:
                for k in subs:
                    if k == lang or k.split("-")[0] == lang:
                        return k, "subtitles"
            return next(iter(subs)), "subtitles"
        orig = [k for k in auto if k.endswith("-orig")]
        if orig:
            return orig[0], "automatic"
        if lang and lang in auto:
            return lang, "automatic"
        return None, None

    def captions(self, url: str, language=None):
        info = self.fetcher.info(url, flat=False) if self.fetcher else None
        if not info:
            return None
        key, kind = self.caption_language(info, language)
        if not key:
            return None
        vtt = self.fetcher.captions(url, key, automatic=(kind == "automatic"))
        text = vtt_to_text(vtt or "")
        if not text:
            return None
        return {"text": text, "language": key.replace("-orig", ""), "kind": kind}

    # MARK: find

    def find(self, meta: dict, url: str) -> dict:
        """{status: found|instrumental|none, source, synced, language, text, synced_text, detail}."""
        q = query_of(meta or {})
        errors = []
        try:
            rec = self.lrclib(q)
        except Exception as error:  # noqa: BLE001 — try the captions
            rec = None
            errors.append(f"LRCLIB: {error}")
        if rec:
            if rec.get("instrumental"):     # LRCLIB's own flag: the record has no words
                return {"status": "instrumental", "source": "lrclib", "detail": self._detail(rec)}
            synced = rec.get("syncedLyrics") or ""
            text = rec.get("plainLyrics") or lrc_to_text(synced)
            return {"status": "found", "source": "lrclib", "synced": bool(synced), "language": q["language"],
                    "text": text[:MAX_CHARS], "synced_text": synced[:MAX_CHARS * 2] or None, "detail": self._detail(rec)}
        try:
            cap = self.captions(url, q["language"]) if url else None
        except Exception as error:  # noqa: BLE001
            cap = None
            errors.append(f"captions: {error}")
        if cap:
            return {"status": "found", "source": "youtube-captions", "synced": False, "language": cap["language"],
                    "text": cap["text"][:MAX_CHARS], "synced_text": None, "detail": {"captions": cap["kind"]}}
        return {"status": "none", "source": None, "detail": {"query": q}, "error": "; ".join(errors)[:400] or None}

    @staticmethod
    def _detail(rec):
        return {"lrclibId": rec.get("id"), "trackName": rec.get("trackName"), "artistName": rec.get("artistName"),
                "albumName": rec.get("albumName"), "duration": rec.get("duration")}
