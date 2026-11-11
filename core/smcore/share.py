"""Sharing (.glalbum files) and audio export. A share is a zip: manifest.json, artwork/, audio/.
It carries everything needed to keep the artist alive on another machine: the hidden soul and
voice, and each song's lyrics, style and seed, so unrecorded songs record there the same way.
Listening history and favorites never leave the machine. (Own format; see README for the
boundary with the Mac app's .standardalbum.)"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile

from smcore.db import new_id
from smcore.runtime import run_tool
from smcore.studio import safe

FORMAT = "ghost-label-share"
VERSION = 1

ARTIST_FIELDS = ("id", "name", "sort_name", "bio", "soul", "voice", "origin", "formed", "genres", "members", "visual",
                 "relation", "data")
ALBUM_FIELDS = ("id", "title", "year", "kind", "concept", "liner_notes", "cover_brief", "position")
TRACK_FIELDS = ("id", "number", "title", "theme", "style", "lyrics", "is_instrumental", "language", "bpm", "keyscale",
                "explicit", "seed", "target_seconds", "duration", "loudness", "engine")


class Sharing:
    def __init__(self, db, paths, settings, library, events, log=print, ffmpeg=None):
        self.db, self.paths, self.settings, self.library, self.events, self.log = db, paths, settings, library, events, log
        self.ffmpeg = ffmpeg or (lambda: shutil.which("ffmpeg"))   # the app passes Runtime.ffmpeg_path

    def export(self, artist_id, album_ids=None, include_audio=True, dest=None, shared_by=""):
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not artist:
            raise ValueError("No such artist")
        albums = self.db.all("SELECT * FROM albums WHERE artist_id = ? ORDER BY position", (artist_id,))
        if album_ids:
            albums = [a for a in albums if a["id"] in album_ids]
        dest = dest or os.path.join(os.path.expanduser("~"), "Downloads",
                                    safe(artist["name"] + (" - " + albums[0]["title"] if len(albums) == 1 else "")) + ".glalbum")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        manifest = {"format": FORMAT, "version": VERSION, "sharedBy": shared_by, "created": time.time(),
                    "engine": self.settings.section("engine").get("backend"),
                    "artist": {k: artist.get(k) for k in ARTIST_FIELDS}, "albums": []}
        tmp = dest + ".partial"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            portrait = self.paths.abs(artist.get("portrait_path"))
            if portrait and os.path.exists(portrait):
                name = "artwork/artist" + os.path.splitext(portrait)[1]
                z.write(portrait, name)
                manifest["artist"]["portrait"] = name
            for a in albums:
                entry = {k: a.get(k) for k in ALBUM_FIELDS}
                cover = self.paths.abs(a.get("cover_path"))
                if cover and os.path.exists(cover):
                    name = f"artwork/{a['id']}" + os.path.splitext(cover)[1]
                    z.write(cover, name)
                    entry["cover"] = name
                entry["tracks"] = []
                for t in self.db.all("SELECT * FROM tracks WHERE album_id = ? ORDER BY number", (a["id"],)):
                    te = {k: t.get(k) for k in TRACK_FIELDS}
                    audio = self.paths.abs(t.get("audio_path"))
                    if include_audio and t["status"] == "recorded" and audio and os.path.exists(audio):
                        name = f"audio/{t['id']}.m4a"
                        z.write(audio, name, compress_type=zipfile.ZIP_STORED)
                        te["audio"] = name
                    entry["tracks"].append(te)
                manifest["albums"].append(entry)
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        os.replace(tmp, dest)
        return {"path": dest, "albums": len(albums)}

    def preview(self, path):
        token = new_id("sh_")
        folder = os.path.join(self.paths.shares, token)
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                target = os.path.realpath(os.path.join(folder, name))
                if not target.startswith(os.path.realpath(folder) + os.sep):
                    raise ValueError("Unsafe path in share file")
            z.extractall(folder)
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        if manifest.get("format") != FORMAT:
            shutil.rmtree(folder, ignore_errors=True)
            raise ValueError("This isn't a Ghost Label share file.")
        exists = bool(self.db.scalar("SELECT 1 FROM artists WHERE id = ?", (manifest["artist"]["id"],)))
        return {"token": token, "sharedBy": manifest.get("sharedBy"), "artist": manifest["artist"].get("name"),
                "alreadyInLibrary": exists,
                "albums": [{"title": a["title"], "year": a.get("year"), "tracks": len(a["tracks"]),
                            "recorded": sum(1 for t in a["tracks"] if t.get("audio"))} for a in manifest["albums"]]}

    def import_share(self, token):
        folder = os.path.join(self.paths.shares, os.path.basename(token))
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        a = manifest["artist"]
        artist_id = a["id"]
        now = time.time()
        if not self.db.scalar("SELECT 1 FROM artists WHERE id = ?", (artist_id,)):
            portrait = None
            if a.get("portrait"):
                portrait = self._copy(folder, a["portrait"], self.paths.artwork, f"portrait-{artist_id}")
            self.db.insert("artists", {"id": artist_id, "name": a["name"], "sort_name": a.get("sort_name"), "bio": a.get("bio"),
                                       "soul": a.get("soul"), "voice": a.get("voice"), "origin": a.get("origin"),
                                       "formed": a.get("formed"), "genres_json": a.get("genres") or [],
                                       "members_json": a.get("members") or [], "visual": a.get("visual"),
                                       "relation": a.get("relation"), "portrait_path": portrait,
                                       "data_json": dict(a.get("data") or {}, sharedBy=manifest.get("sharedBy")), "created": now})
            self.db.index("artist", artist_id, a["name"], ", ".join(a.get("genres") or []), a.get("bio") or "")
        added = 0
        for al in manifest["albums"]:
            if self.db.scalar("SELECT 1 FROM albums WHERE id = ?", (al["id"],)):
                continue
            cover = self._copy(folder, al["cover"], self.paths.artwork, f"cover-{al['id']}") if al.get("cover") else None
            self.db.insert("albums", {**{k: al.get(k) for k in ALBUM_FIELDS}, "artist_id": artist_id, "cover_path": cover,
                                      "status": "ready", "created": now})
            self.db.index("album", al["id"], al["title"], a["name"], al.get("concept") or "")
            for t in al["tracks"]:
                audio = None
                if t.get("audio"):
                    dest_dir = os.path.join(self.paths.audio, safe(a["name"]) + "-" + artist_id[-6:])
                    audio = self._copy(folder, t["audio"], dest_dir, f"{t['number']:02d} {safe(t['title'])}-{t['id'][-6:]}")
                status = "recorded" if audio else ("written" if t.get("lyrics") else "planned")
                self.db.insert("tracks", {**{k: t.get(k) for k in TRACK_FIELDS}, "album_id": al["id"], "artist_id": artist_id,
                                          "audio_path": audio, "status": status, "created": now})
                self.db.index("track", t["id"], t["title"], a["name"], t.get("lyrics") or "")
            added += 1
        shutil.rmtree(folder, ignore_errors=True)
        self.events.emit("library", artist=artist_id)
        return {"artistId": artist_id, "albumsAdded": added}

    def _copy(self, folder, name, dest_dir, stem):
        source = os.path.join(folder, name)
        if not os.path.exists(source):
            return None
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, stem + os.path.splitext(name)[1])
        shutil.copyfile(source, dest)
        return self.paths.rel(dest)

    def export_audio(self, folder, album_ids=None, artist_id=None):
        rows = []
        if artist_id:
            rows = self.db.all("SELECT id FROM albums WHERE artist_id = ?", (artist_id,))
        ids = list(album_ids or []) + [r["id"] for r in rows]
        if not ids:
            ids = [r["id"] for r in self.db.all("SELECT id FROM albums")]
        ffmpeg = self.ffmpeg()
        written, skipped, failed = 0, 0, 0
        for album_id in ids:
            album = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
            artist = self.db.one("SELECT * FROM artists WHERE id = ?", (album["artist_id"],))
            target_dir = os.path.join(folder, safe(artist["name"]), safe(album["title"]))
            for t in self.db.all("SELECT * FROM tracks WHERE album_id = ? AND status = 'recorded' ORDER BY number", (album_id,)):
                source = self.paths.abs(t["audio_path"])
                dest = os.path.join(target_dir, f"{t['number']:02d} {safe(t['title'])}.m4a")
                if os.path.exists(dest):
                    skipped += 1
                    continue
                os.makedirs(target_dir, exist_ok=True)
                try:
                    if ffmpeg:
                        meta = {"title": t["title"], "artist": artist["name"], "album_artist": artist["name"],
                                "album": album["title"], "track": str(t["number"]), "date": str(album.get("year") or ""),
                                "lyrics": t.get("lyrics") or "", "comment": "Made with Ghost Label"}
                        args = [ffmpeg, "-y", "-loglevel", "error", "-i", source]
                        cover = self.paths.abs(album.get("cover_path"))
                        if cover and os.path.exists(cover):
                            args += ["-i", cover, "-map", "0:a", "-map", "1", "-c:v", "copy" if cover.endswith((".jpg", ".png")) else "mjpeg",
                                     "-disposition:v", "attached_pic"]
                        args += ["-c:a", "copy"]
                        for k, v in meta.items():
                            args += ["-metadata", f"{k}={v}"]
                        run_tool(args + [dest], timeout=120)
                    else:
                        shutil.copyfile(source, dest)
                    written += 1
                except Exception as error:  # noqa: BLE001
                    self.log(f"[export] {dest}: {error}")
                    failed += 1
        return {"folder": folder, "written": written, "skipped": skipped, "failed": failed}
