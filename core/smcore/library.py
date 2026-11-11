"""Library reads and small writes: the shapes the UI shows, favorites, plays, search."""

from __future__ import annotations

import os
import time
import urllib.parse

ARTIST_PUBLIC = ("id, name, bio, voice, origin, formed, genres_json, members_json, visual, portrait_path, "
                 "relation, source_set_id, brief_id, seed_artist_id, created")


class Library:
    def __init__(self, db, paths, events, log=print):
        self.db, self.paths, self.events, self.log = db, paths, events, log

    # MARK: decoration

    def media(self, rel):
        return "/media/" + urllib.parse.quote(rel) if rel else None

    def album_card(self, a):
        if a is None:
            return None
        counts = self.db.one("SELECT COUNT(*) AS n, SUM(status = 'recorded') AS rec, SUM(COALESCE(duration, target_seconds, 0)) AS secs "
                             "FROM tracks WHERE album_id = ?", (a["id"],)) or {}
        artist = self.db.one("SELECT id, name FROM artists WHERE id = ?", (a["artist_id"],)) or {}
        return {"id": a["id"], "title": a["title"], "year": a["year"], "kind": a["kind"],
                "artistId": a["artist_id"], "artistName": artist.get("name"),
                "cover": self.media(a.get("cover_path")), "status": a.get("status"),
                "trackCount": counts.get("n") or 0, "recorded": counts.get("rec") or 0,
                "seconds": counts.get("secs") or 0, "favorite": self.is_favorite("album", a["id"]),
                "created": a["created"]}

    def track_view(self, t, album=None, artist=None):
        album = album or self.db.one("SELECT id, title, cover_path, artist_id FROM albums WHERE id = ?", (t["album_id"],)) or {}
        artist = artist or self.db.one("SELECT id, name FROM artists WHERE id = ?", (t["artist_id"],)) or {}
        return {"id": t["id"], "number": t["number"], "title": t["title"], "theme": t.get("theme"),
                "albumId": t["album_id"], "albumTitle": album.get("title"), "artistId": t["artist_id"],
                "artistName": artist.get("name"), "cover": self.media(album.get("cover_path")),
                "status": t["status"], "audio": self.media(t.get("audio_path")), "duration": t.get("duration"),
                "targetSeconds": t.get("target_seconds"), "loudness": t.get("loudness"),
                "lyrics": t.get("lyrics"), "style": t.get("style"), "instrumental": bool(t.get("is_instrumental")) or (t.get("lyrics") or "").strip().lower() == "[instrumental]",
                "language": t.get("language"), "explicit": bool(t.get("explicit")), "error": t.get("error"),
                "favorite": self.is_favorite("track", t["id"]),
                "engine": t.get("engine"), "recordNote": (t.get("data") or {}).get("recordNote"),
                "feedback": [f.get("text") for f in ((t.get("data") or {}).get("retake") or {}).get("feedback") or []]}

    def artist_card(self, a):
        n = self.db.scalar("SELECT COUNT(*) FROM albums WHERE artist_id = ?", (a["id"],))
        return {"id": a["id"], "name": a["name"], "portrait": self.media(a.get("portrait_path")),
                "genres": a.get("genres") or [], "origin": a.get("origin"), "albums": n,
                "favorite": self.is_favorite("artist", a["id"]), "relation": a.get("relation")}

    # MARK: pages

    def home(self):
        recent = self.db.all("SELECT * FROM albums ORDER BY created DESC LIMIT 24")
        played = self.db.all(
            "SELECT DISTINCT al.* FROM play_events p JOIN tracks t ON t.id = p.track_id JOIN albums al ON al.id = t.album_id "
            "ORDER BY p.at DESC LIMIT 12")
        favorites = self.db.all("SELECT al.* FROM favorites f JOIN albums al ON al.id = f.item_id WHERE f.kind = 'album' "
                                "ORDER BY f.created DESC LIMIT 12")
        artists = self.db.all("SELECT * FROM artists ORDER BY created DESC LIMIT 18")
        ready = self.db.all("SELECT al.* FROM albums al WHERE EXISTS (SELECT 1 FROM tracks t WHERE t.album_id = al.id "
                            "AND t.status = 'recorded') ORDER BY al.created DESC LIMIT 12")
        totals = {"artists": self.db.scalar("SELECT COUNT(*) FROM artists"),
                  "albums": self.db.scalar("SELECT COUNT(*) FROM albums"),
                  "tracks": self.db.scalar("SELECT COUNT(*) FROM tracks"),
                  "recorded": self.db.scalar("SELECT COUNT(*) FROM tracks WHERE status = 'recorded'")}
        return {"totals": totals,
                "shelves": [s for s in [
                    {"id": "recent", "title": "New in your library", "albums": [self.album_card(a) for a in recent]},
                    {"id": "ready", "title": "Ready to play", "albums": [self.album_card(a) for a in ready]},
                    {"id": "played", "title": "Recently played", "albums": [self.album_card(a) for a in played]},
                    {"id": "favorites", "title": "Favorite albums", "albums": [self.album_card(a) for a in favorites]},
                ] if s["albums"]],
                "artists": [self.artist_card(a) for a in artists]}

    def artists(self, sort="name"):
        order = "name COLLATE NOCASE" if sort == "name" else "created DESC"
        return [self.artist_card(a) for a in self.db.all(f"SELECT * FROM artists ORDER BY {order}")]

    def albums(self):
        return [self.album_card(a) for a in self.db.all("SELECT * FROM albums ORDER BY created DESC")]

    def songs(self, limit=500):
        rows = self.db.all("SELECT * FROM tracks ORDER BY created DESC, number LIMIT ?", (limit,))
        return [self.track_view(t) for t in rows]

    def artist(self, artist_id):
        a = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not a:
            return None
        albums = self.db.all("SELECT * FROM albums WHERE artist_id = ? ORDER BY position, year, created", (artist_id,))
        top = self.db.all("SELECT t.*, COUNT(p.id) AS plays FROM tracks t LEFT JOIN play_events p ON p.track_id = t.id "
                          "AND p.kind = 'play' WHERE t.artist_id = ? GROUP BY t.id ORDER BY plays DESC, t.number LIMIT 5",
                          (artist_id,))
        seed = self.db.one("SELECT id, name FROM artists WHERE id = ?", (a.get("seed_artist_id"),)) if a.get("seed_artist_id") else None
        out = self.artist_card(a)
        out.update({"bio": a.get("bio"), "voice": a.get("voice"), "formed": a.get("formed"),
                    "members": a.get("members") or [], "visual": a.get("visual"),
                    "albumsList": [self.album_card(x) for x in albums],
                    "topSongs": [self.track_view(t) for t in top], "seedArtist": seed,
                    "sourceSetId": a.get("source_set_id"),
                    "imageFeedback": ((a.get("data") or {}).get("imageFeedback") or {}).get("feedback") or []})
        return out

    def album(self, album_id):
        a = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        if not a:
            return None
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (a["artist_id"],))
        tracks = self.db.all("SELECT * FROM tracks WHERE album_id = ? ORDER BY number", (album_id,))
        out = self.album_card(a)
        out.update({"concept": a.get("concept"), "linerNotes": a.get("liner_notes"),
                    "artist": self.artist_card(artist) if artist else None,
                    "tracks": [self.track_view(t, a, artist) for t in tracks],
                    "imageFeedback": ((a.get("data") or {}).get("imageFeedback") or {}).get("feedback") or []})
        return out

    def track(self, track_id):
        t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        return self.track_view(t) if t else None

    def tracks(self, ids):
        out = []
        for i in ids:
            t = self.track(i)
            if t:
                out.append(t)
        return out

    def search(self, q):
        hits = self.db.search(q)
        out = {"artists": [], "albums": [], "tracks": []}
        for h in hits:
            if h["kind"] == "artist":
                a = self.db.one("SELECT * FROM artists WHERE id = ?", (h["item_id"],))
                if a:
                    out["artists"].append(self.artist_card(a))
            elif h["kind"] == "album":
                a = self.db.one("SELECT * FROM albums WHERE id = ?", (h["item_id"],))
                if a:
                    out["albums"].append(self.album_card(a))
            elif h["kind"] == "track":
                t = self.track(h["item_id"])
                if t:
                    out["tracks"].append(t)
        return out

    # MARK: favorites and plays

    def is_favorite(self, kind, item_id):
        return bool(self.db.scalar("SELECT 1 FROM favorites WHERE kind = ? AND item_id = ?", (kind, item_id)))

    def set_favorite(self, kind, item_id, on: bool):
        if kind not in ("artist", "album", "track"):
            raise ValueError("kind must be artist, album or track")
        if on:
            self.db.run("INSERT OR IGNORE INTO favorites (kind, item_id, created) VALUES (?, ?, ?)", (kind, item_id, time.time()))
        else:
            self.db.run("DELETE FROM favorites WHERE kind = ? AND item_id = ?", (kind, item_id))
        self.db.run("UPDATE taste SET signals_since_refresh = signals_since_refresh + 1 WHERE id = 1")
        self.events.emit("favorite", item=kind, id=item_id, on=on)
        return {"kind": kind, "id": item_id, "favorite": on}

    def favorites(self):
        arts = self.db.all("SELECT a.* FROM favorites f JOIN artists a ON a.id = f.item_id WHERE f.kind = 'artist' ORDER BY f.created DESC")
        albs = self.db.all("SELECT a.* FROM favorites f JOIN albums a ON a.id = f.item_id WHERE f.kind = 'album' ORDER BY f.created DESC")
        trks = self.db.all("SELECT t.* FROM favorites f JOIN tracks t ON t.id = f.item_id WHERE f.kind = 'track' ORDER BY f.created DESC")
        return {"artists": [self.artist_card(a) for a in arts], "albums": [self.album_card(a) for a in albs],
                "tracks": [self.track_view(t) for t in trks]}

    def play_event(self, track_id, kind, position=None):
        if kind not in ("play", "complete", "skip"):
            raise ValueError("kind must be play, complete or skip")
        self.db.run("INSERT INTO play_events (track_id, kind, position, at) VALUES (?, ?, ?, ?)",
                    (track_id, kind, position, time.time()))
        if kind != "play":
            self.db.run("UPDATE taste SET signals_since_refresh = signals_since_refresh + 1 WHERE id = 1")
        return {"ok": True}

    # MARK: context for the AI (data, not judgment)

    def listening_summary(self, limit=40):
        fav_artists = self.db.all("SELECT a.name, a.genres_json FROM favorites f JOIN artists a ON a.id = f.item_id "
                                  "WHERE f.kind = 'artist' ORDER BY f.created DESC LIMIT ?", (limit,))
        fav_albums = self.db.all("SELECT al.title, ar.name AS artist FROM favorites f JOIN albums al ON al.id = f.item_id "
                                 "JOIN artists ar ON ar.id = al.artist_id WHERE f.kind = 'album' ORDER BY f.created DESC LIMIT ?", (limit,))
        fav_tracks = self.db.all("SELECT t.title, t.style, ar.name AS artist FROM favorites f JOIN tracks t ON t.id = f.item_id "
                                 "JOIN artists ar ON ar.id = t.artist_id WHERE f.kind = 'track' ORDER BY f.created DESC LIMIT ?", (limit,))
        counts = self.db.all("SELECT ar.name AS artist, SUM(p.kind = 'play') AS plays, SUM(p.kind = 'complete') AS completes, "
                             "SUM(p.kind = 'skip') AS skips FROM play_events p JOIN tracks t ON t.id = p.track_id "
                             "JOIN artists ar ON ar.id = t.artist_id GROUP BY ar.id ORDER BY plays DESC LIMIT ?", (limit,))
        return {"favoriteArtists": [{"name": r["name"], "genres": r["genres"]} for r in fav_artists],
                "favoriteAlbums": fav_albums, "favoriteSongs": fav_tracks, "listeningByArtist": counts}

    def existing_names(self, limit=400):
        return [r["name"] for r in self.db.all("SELECT name FROM artists ORDER BY created DESC LIMIT ?", (limit,))]

    def taste(self):
        return self.db.one("SELECT * FROM taste WHERE id = 1")

    # MARK: deleting (database rows and the files on disk)

    def _remove_files(self, rels):
        removed = 0
        for rel in rels:
            path = self.paths.abs(rel) if rel else None
            if not path:
                continue
            for p in (path, os.path.splitext(path)[0] + ".unencoded.wav"):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                        removed += 1
                except OSError as error:
                    self.log(f"[library] couldn't delete {p}: {error}")
        return removed

    def _prune_dirs(self, rels):
        """Remove folders left empty (an artist's audio folder), never the library roots."""
        roots = {os.path.abspath(self.paths.audio), os.path.abspath(self.paths.artwork), os.path.abspath(self.paths.home)}
        for rel in rels:
            path = self.paths.abs(rel) if rel else None
            folder = os.path.dirname(os.path.abspath(path)) if path else None
            if folder and folder not in roots and os.path.isdir(folder) and not os.listdir(folder):
                try:
                    os.rmdir(folder)
                except OSError:
                    pass

    def _forget_tracks(self, rows):
        ids = [r["id"] for r in rows]
        for i in ids:
            self.db.unindex("track", i)
            self.db.run("DELETE FROM favorites WHERE kind = 'track' AND item_id = ?", (i,))
            self.db.run("DELETE FROM play_events WHERE track_id = ?", (i,))
            self.db.run("UPDATE artists SET voice_track_id = NULL WHERE voice_track_id = ?", (i,))
        return [r.get("audio_path") for r in rows]

    def delete_track(self, track_id):
        rows = self.db.all("SELECT id, audio_path FROM tracks WHERE id = ?", (track_id,))
        files = self._forget_tracks(rows)
        self.db.run("DELETE FROM tracks WHERE id = ?", (track_id,))
        removed = self._remove_files(files)
        self._prune_dirs(files)
        self.events.emit("library")
        return {"tracks": [r["id"] for r in rows], "files": removed}

    def delete_album(self, album_id):
        album = self.db.one("SELECT id, cover_path FROM albums WHERE id = ?", (album_id,))
        rows = self.db.all("SELECT id, audio_path FROM tracks WHERE album_id = ?", (album_id,))
        files = self._forget_tracks(rows)
        self.db.unindex("album", album_id)
        self.db.run("DELETE FROM favorites WHERE kind = 'album' AND item_id = ?", (album_id,))
        self.db.run("DELETE FROM albums WHERE id = ?", (album_id,))  # tracks go with it (ON DELETE CASCADE)
        files.append((album or {}).get("cover_path"))
        removed = self._remove_files(files)
        self._prune_dirs(files)
        self.events.emit("library")
        return {"tracks": [r["id"] for r in rows], "files": removed}

    def delete_artist(self, artist_id):
        artist = self.db.one("SELECT id, portrait_path FROM artists WHERE id = ?", (artist_id,))
        rows = self.db.all("SELECT id, audio_path FROM tracks WHERE artist_id = ?", (artist_id,))
        files = self._forget_tracks(rows)
        for a in self.db.all("SELECT id, cover_path FROM albums WHERE artist_id = ?", (artist_id,)):
            self.db.unindex("album", a["id"])
            self.db.run("DELETE FROM favorites WHERE kind = 'album' AND item_id = ?", (a["id"],))
            files.append(a.get("cover_path"))
        self.db.unindex("artist", artist_id)
        self.db.run("DELETE FROM favorites WHERE kind = 'artist' AND item_id = ?", (artist_id,))
        self.db.run("DELETE FROM artists WHERE id = ?", (artist_id,))
        files.append((artist or {}).get("portrait_path"))
        removed = self._remove_files(files)
        self._prune_dirs(files)
        self.events.emit("library")
        return {"tracks": [r["id"] for r in rows], "files": removed}
