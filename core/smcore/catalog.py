"""Real-artist catalog for onboarding and link matching: the public, keyless Deezer API, cached
in the library's kv table. (A clean replacement for the Mac app's bundled catalog.sqlite.)"""

from __future__ import annotations

import re
import time
import unicodedata
import urllib.parse

from smcore import net

BASE = "https://api.deezer.com"
TTL = 7 * 24 * 3600


class Catalog:
    def __init__(self, db, log=print, base=None):
        self.db, self.log = db, log
        self.base = (base or BASE).rstrip("/")

    def get(self, path: str, params=None, ttl=TTL):
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        key = f"deezer:{path}{query}"
        cached = self.db.kv_get(key)
        if cached and time.time() - cached.get("at", 0) < ttl:
            return cached["data"]
        try:
            data = net.get_json(self.base + path + query, timeout=15)
        except Exception as error:  # noqa: BLE001 — offline is fine, onboarding degrades
            self.log(f"[catalog] {path}: {error}")
            return cached["data"] if cached else None
        if isinstance(data, dict) and data.get("error"):
            self.log(f"[catalog] {path}: {data['error']}")
            return None
        self.db.kv_set(key, {"at": time.time(), "data": data})
        return data

    @staticmethod
    def real_picture(url):
        """Deezer's placeholder for an artist without a photo is the image URL with an empty hash
        (".../images/artist//250x250-..."); treat it as no picture."""
        if not url or "/images/artist//" in url or re.search(r"/artist/?$", url):
            return None
        return url

    @staticmethod
    def artist(a):
        # The sized picture_* URLs carry the image hash (empty for the placeholder); the bare
        # `picture` field is an api.deezer.com/artist/<id>/image redirect that hides that.
        pic = next((Catalog.real_picture(a.get(k)) for k in ("picture_medium", "picture_big", "picture_xl", "picture_small")
                    if Catalog.real_picture(a.get(k))), None)
        return {"id": a.get("id"), "name": a.get("name"), "picture": pic, "fans": a.get("nb_fan"), "link": a.get("link")}

    @staticmethod
    def fold(text):
        """Case- and diacritic-insensitive form of a name, for exact-match comparison."""
        text = unicodedata.normalize("NFKD", text or "")
        return " ".join("".join(c for c in text if not unicodedata.combining(c)).casefold().split())

    @staticmethod
    def order(artists, q):
        """Mechanical ordering only: artists with a real photo before ones without; within those,
        exact name matches (ignoring case/diacritics) first; then by Deezer fan count, high to low."""
        target = Catalog.fold(q)
        return sorted(artists, key=lambda a: (a.get("picture") is None, Catalog.fold(a.get("name")) != target,
                                              -(a.get("fans") or 0)))

    def genres(self):
        data = self.get("/genre") or {}
        return [{"id": g["id"], "name": g["name"], "picture": g.get("picture_medium")}
                for g in data.get("data", []) if g.get("id")]

    def genre_artists(self, genre_id, limit=30):
        data = self.get(f"/genre/{int(genre_id)}/artists") or {}
        return [self.artist(a) for a in data.get("data", [])[:limit]]

    def search_artists(self, q, limit=12):
        if len((q or "").strip()) < 2:
            return []
        data = self.get("/search/artist", {"q": q.strip(), "limit": max(limit, 25)}, ttl=24 * 3600) or {}
        seen, out = set(), []
        for a in data.get("data", []):
            if a.get("id") in seen:
                continue
            seen.add(a.get("id"))
            out.append(self.artist(a))
        return self.order(out, q)[:limit]

    def related(self, artist_id, limit=12):
        data = self.get(f"/artist/{int(artist_id)}/related", {"limit": limit}) or {}
        return [self.artist(a) for a in data.get("data", [])]

    def artist_detail(self, artist_id):
        """Top tracks (with album genres) for the AI's context."""
        top = self.get(f"/artist/{int(artist_id)}/top", {"limit": 5}) or {}
        out = []
        for t in top.get("data", []):
            out.append({"title": t.get("title"), "album": (t.get("album") or {}).get("title")})
        return out

    def album_genres(self, album_id):
        data = self.get(f"/album/{int(album_id)}") or {}
        return {"genres": [g.get("name") for g in (data.get("genres") or {}).get("data", [])],
                "release_date": data.get("release_date"), "label": data.get("label"),
                "title": data.get("title")}

    def search_tracks(self, q, limit=5):
        """Candidate real-world matches for a link. Fuzzy on purpose: the AI decides which, if any,
        is the same recording."""
        if not (q or "").strip():
            return []
        data = self.get("/search/track", {"q": q.strip()[:200], "limit": limit}, ttl=24 * 3600) or {}
        out = []
        for i, t in enumerate(data.get("data", [])):
            item = {"title": t.get("title"), "artist": (t.get("artist") or {}).get("name"),
                    "album": (t.get("album") or {}).get("title"), "duration": t.get("duration"),
                    "preview": t.get("preview"), "deezerId": t.get("id"),
                    "artistId": (t.get("artist") or {}).get("id")}
            if i < 3 and (t.get("album") or {}).get("id"):
                item.update({k: v for k, v in self.album_genres(t["album"]["id"]).items() if k != "title"})
            out.append(item)
        return out
