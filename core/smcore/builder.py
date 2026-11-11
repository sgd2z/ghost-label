"""Library building: roster -> artist dossiers -> discographies -> albums -> artwork, and songs
(lyrics + style) written just before recording. All creative decisions come from the provider,
guided by the rule files; this module only assembles context, validates shapes, applies the
listener's hard limits (counts, maximum lengths) and stores results."""

from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor

from smcore.db import new_id
from smcore.jobs import Cancelled
from smcore.queue import owner

S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
STRS = {"type": "array", "items": S}
INTS = {"type": "array", "items": I}

ROSTER = {"type": "object", "required": ["artists"], "properties": {"artists": {"type": "array", "items": {
    "type": "object", "required": ["name", "concept"], "properties": {
        "name": S, "concept": {"type": "string", "description": "2-3 sentences: sound, scene, era, place, angle"},
        "relation": {"type": "string", "enum": ["core", "adjacent", "wildcard"]},
        "genres": STRS,
        "draws_on": {"type": "array", "items": I, "description": "source track numbers, if a style brief was given"}}}}}}

DOSSIER = {"type": "object", "required": ["name", "bio", "soul", "voice", "genres"], "properties": {
    "name": S, "bio": S, "soul": S, "voice": S, "origin": S, "formed": S, "genres": STRS,
    "vocal_tags": {"type": "string", "description": "lead vocal as short caption tags, starting with who sings"},
    "members": {"type": "array", "items": {"type": "object", "properties": {"name": S, "role": S}, "required": ["name"]}},
    "visual": S, "languages": STRS}}

DISCOGRAPHY = {"type": "object", "required": ["releases"], "properties": {"releases": {"type": "array", "minItems": 1, "items": {
    "type": "object", "required": ["title", "kind", "concept"], "properties": {
        "title": S, "year": I, "kind": {"type": "string", "enum": ["album", "ep", "single"]}, "concept": S}}}}}

ALBUM = {"type": "object", "required": ["liner_notes", "tracks"], "properties": {
    "liner_notes": S, "cover_brief": S, "tracks": {"type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["title"], "properties": {
            "title": S, "theme": S, "instrumental": B, "language": S, "seconds": I,
            "draws_on": {"type": "array", "items": I}}}}}}

SONG = {"type": "object", "required": ["style", "lyrics"], "properties": {
    "style": {"type": "string", "description": "comma-separated style text for the music model, under 400 characters"},
    "lyrics": S, "language": S, "bpm": {"type": ["integer", "null"]}, "key": {"type": ["string", "null"]},
    "explicit": B,
    "instrumental": {"type": "boolean", "description": "true when this song has no vocals (lyrics then \"[Instrumental]\")"}}}

ART = {"type": "object", "required": ["prompt"], "properties": {
    "prompt": {"type": "string", "description": "the image prompt, most important identity details first"},
    "negative_prompt": {"type": "string", "description": "extra things to avoid for this image (optional)"}}}

RETAKE = {"type": "object", "required": ["style", "lyrics"], "properties": {
    "style": {"type": "string", "description": "the revised style caption, under 400 characters"},
    "lyrics": S, "bpm": {"type": ["integer", "null"]}, "key": {"type": ["string", "null"]},
    "vocal_tags": {"type": "string", "description": "lead-vocal caption tags, or 'instrumental'"},
    "changed": {"type": "string", "description": "one line for the listener: what this take changes"}}}

MAX_FEEDBACK_CHARS = 500     # one piece of listener feedback
MAX_FEEDBACK_ITEMS = 12      # kept per song / per image


def add_feedback(history: dict, text: str, original: dict) -> dict:
    """Append feedback (capped) to a history {original, feedback: [{text, at}]}, keeping the
    original the first time."""
    history = dict(history or {})
    history.setdefault("original", original)
    items = list(history.get("feedback") or [])
    items.append({"text": text.strip()[:MAX_FEEDBACK_CHARS], "at": time.time()})
    history["feedback"] = items[-MAX_FEEDBACK_ITEMS:]
    return history


VOCALS = {"type": "object", "required": ["vocal_tags"], "properties": {
    "vocal_tags": {"type": "string", "maxLength": 160,
                   "description": "comma-separated lead-vocal tags starting with who sings, or 'instrumental'"}}}
MAX_VOCAL_TAGS = 160


def with_vocals(style: str, tags: str) -> str:
    """The caption the music model gets: the song's style text plus the artist's vocal tags,
    unless the style already contains them verbatim (a mechanical check, no keyword rules)."""
    style = (style or "").strip().rstrip(",")
    tags = (tags or "").strip().strip(",")
    if not tags or tags.lower() in style.lower():
        return style
    return f"{style}, {tags}" if style else tags


INSTRUMENTAL = "[Instrumental]"


def says_instrumental(value) -> bool:
    """The rules' own marker for "no vocals": a voice line or vocal tags of exactly "instrumental",
    or lyrics of exactly "[Instrumental]". A literal match on the value the AI was told to return,
    not a judgment about the music."""
    v = (value or "").strip().strip(".").strip().lower()
    return v in ("instrumental", "[instrumental]")


def artist_is_instrumental(artist) -> bool:
    if not artist:
        return False
    tags = ((artist.get("data") or {}).get("vocalTags") or "")
    return says_instrumental(artist.get("voice")) or says_instrumental(tags)


def track_is_instrumental(track, artist=None) -> bool:
    """A song is instrumental when its plan said so, its lyrics are the instrumental marker, or its
    act has no vocals (the artist's voice/vocal tags are "instrumental")."""
    return bool(track.get("is_instrumental")) or says_instrumental(track.get("lyrics")) or artist_is_instrumental(artist)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


class Builder:
    def __init__(self, db, paths, settings, provider, rules, library, events, log=print):
        self.db, self.paths, self.settings = db, paths, settings
        self.provider, self.rules, self.library, self.events, self.log = provider, rules, library, events, log
        self.engine_languages = []   # filled from the engine's ready.capabilities

    # MARK: context

    def context(self, extra=None) -> dict:
        lib = self.settings.section("library")
        eng = self.settings.section("engine")
        taste = self.library.taste() or {}
        ctx = {
            "listener": {"tasteProfile": taste.get("profile") or {}, "notes": taste.get("notes") or "",
                         "explicitLyricsAllowed": bool(lib.get("allowExplicit"))},
            "listening": self.library.listening_summary(),
            "limits": {"maxSongSeconds": int(float(eng.get("maxSongMinutes") or 4) * 60),
                       "maxReleasesPerArtist": int(lib.get("maxAlbumsPerArtist") or 3)},
        }
        if self.engine_languages:
            ctx["musicModelVocalLanguages"] = self.engine_languages
        if lib.get("tracksPerAlbumHint"):
            ctx["limits"]["tracksPerAlbumHint"] = lib["tracksPerAlbumHint"]
        ctx.update(extra or {})
        return ctx

    def ask(self, rule, task, data, schema, name, fast=False):
        user = task.strip() + "\n\nData:\n" + dumps(data)
        return self.provider.json(self.rules.system(rule), user, schema, name=name, fast=fast)

    # MARK: entry points

    def build(self, job, req: dict) -> dict:
        """req: brief, count, seedArtistId, sourceSetId, briefId, styleBrief, output (artists|album|songs),
        artistId (new release for an existing artist), songCount, coreShare, adjacentShare."""
        output = req.get("output") or "artists"
        extra = {}
        if req.get("brief"):
            extra["brief"] = req["brief"]
        if req.get("styleBrief"):
            extra["styleBrief"] = req["styleBrief"]
        if req.get("seedArtistId"):
            seed = self.db.one("SELECT name, bio, genres_json, voice, origin FROM artists WHERE id = ?", (req["seedArtistId"],))
            if seed:
                extra["moreLikeThis"] = seed
        ctx = self.context(extra)
        made = {"artists": [], "albums": []}
        if req.get("artistId"):
            job.progress(0.05, "Planning a new release")
            album_id = self.new_release(job, req["artistId"], ctx, req, output)
            made["albums"].append(album_id)
            return made
        count = max(1, min(int(req.get("count") or 1), 200))
        job.stage("roster")
        job.count(artistsTotal=count)
        job.progress(0.02, f"Planning {count} artist{'s' if count != 1 else ''}")
        roster = self.plan_roster(job, ctx, count, req)
        job.stage("artists")
        done = [0]
        workers = self.provider.parallel() if hasattr(self.provider, "parallel") else 1

        def one(concept):
            job.check()
            artist_id = self.make_artist(job, concept, ctx, req)
            made["artists"].append(artist_id)
            if output == "artists":
                albums = self.make_discography(job, artist_id, ctx, req)
            else:
                albums = [self.new_release(job, artist_id, ctx, req, output, first=True)]
            made["albums"].extend(albums)
            done[0] += 1
            job.count(artistsDone=1)
            job.progress(0.1 + 0.9 * done[0] / len(roster), f"{done[0]} of {len(roster)} artists ready")

        with ThreadPoolExecutor(max_workers=min(workers, len(roster))) as pool:
            for future in [pool.submit(one, c) for c in roster]:
                try:
                    future.result()
                except Exception as error:  # noqa: BLE001 — one artist failing doesn't sink the batch
                    if job.cancel_event.is_set():
                        raise
                    self.log(f"[builder] artist failed: {error!r}")
                    job.count(artistsFailed=1)
                    made.setdefault("errors", []).append(str(error))
        if not made["artists"] and made.get("errors"):
            raise RuntimeError(made["errors"][0])
        job.stage("done")
        return made

    # MARK: roster

    def plan_roster(self, job, ctx, count, req):
        core = float(req.get("coreShare", 0.6))
        adjacent = float(req.get("adjacentShare", 0.3))
        planned = []
        chunk = 8
        while len(planned) < count:
            job.check()
            n = min(chunk, count - len(planned))
            mix = {"core": round(n * core), "adjacent": round(n * adjacent)}
            mix["wildcard"] = max(0, n - mix["core"] - mix["adjacent"])
            data = dict(ctx, wanted=n, mix=mix, existingArtistNames=self.library.existing_names(),
                        alreadyPlannedThisBatch=[p["name"] for p in planned])
            with owner(kind="roster", label=f"Planning {count} artist{'s' if count != 1 else ''}"):
                result = self.ask("roster", f"Plan {n} new fictional artists.", data, ROSTER, "roster")
            fresh = [a for a in result["artists"] if a.get("name")]
            if not fresh:
                raise RuntimeError("The model planned no artists.")
            planned.extend(fresh[: n])
            job.count(artistsPlanned=len(fresh[: n]))
            job.progress(0.02 + 0.08 * len(planned) / count, f"Planned {len(planned)} of {count} artists")
        return planned[:count]

    # MARK: artists

    def make_artist(self, job, concept, ctx, req) -> str:
        data = dict(ctx, concept=concept)
        with owner(kind="artist", label=f"Artist “{concept.get('name') or 'new artist'}”"):
            d = self.ask("artist", "Write this artist's dossier.", data, DOSSIER, "artist")
        artist_id = new_id("ar_")
        self.db.insert("artists", {
            "id": artist_id, "name": d["name"] or concept["name"], "sort_name": (d["name"] or "").lower(),
            "bio": d.get("bio"), "soul": d.get("soul"), "voice": d.get("voice"), "origin": d.get("origin"),
            "formed": d.get("formed"), "genres_json": d.get("genres") or concept.get("genres") or [],
            "members_json": d.get("members") or [], "visual": d.get("visual"), "relation": concept.get("relation"),
            "source_set_id": req.get("sourceSetId"), "brief_id": req.get("briefId"),
            "seed_artist_id": req.get("seedArtistId"),
            "data_json": {"concept": concept, "languages": d.get("languages") or [],
                          "vocalTags": (d.get("vocal_tags") or "").strip()[:MAX_VOCAL_TAGS]}, "created": time.time()})
        self.db.index("artist", artist_id, d["name"], ", ".join(d.get("genres") or []), d.get("bio") or "")
        self.events.emit("library", artist=artist_id)
        try:
            self.portrait(artist_id)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001
            self.log(f"[builder] portrait failed: {error!r}")
        job.count(artistsWritten=1)
        return artist_id

    def make_discography(self, job, artist_id, ctx, req):
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        limit = ctx["limits"]["maxReleasesPerArtist"]
        data = dict(ctx, artist=self.artist_brief(artist))
        with owner(kind="discography", label=f"Releases for {artist['name']}", artistId=artist_id):
            plan = self.ask("discography", f"Plan this artist's releases (at most {limit}).", data, DISCOGRAPHY, "discography")
        albums = []
        job.count(releasesPlanned=len(plan["releases"][:limit]))
        for position, release in enumerate(plan["releases"][:limit]):
            job.check()
            albums.append(self.make_album(job, artist, release, ctx, position, req))
        return albums

    def new_release(self, job, artist_id, ctx, req, output, first=False):
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not artist:
            raise RuntimeError("No such artist")
        existing = self.db.all("SELECT title, year, kind, concept FROM albums WHERE artist_id = ? ORDER BY position", (artist_id,))
        want = {"album": "album", "songs": "ep"}.get(output, "album")
        task = (f"Plan exactly one new release for this artist (kind {want}" +
                (f", about {req['songCount']} songs" if req.get("songCount") else "") + ").")
        data = dict(ctx, artist=self.artist_brief(artist), existingReleases=existing)
        with owner(kind="release", label=f"New release for {artist['name']}", artistId=artist_id):
            plan = self.ask("discography", task, data, DISCOGRAPHY, "release")
        release = plan["releases"][0]
        job.count(releasesPlanned=1)
        if output == "songs":
            release["kind"] = release.get("kind") or "ep"
        position = len(existing)
        return self.make_album(job, artist, release, ctx, position, req)

    def artist_brief(self, artist):
        return {"name": artist["name"], "bio": artist.get("bio"), "soul": artist.get("soul"),
                "voice": artist.get("voice"), "genres": artist.get("genres"), "origin": artist.get("origin"),
                "formed": artist.get("formed"), "members": artist.get("members"), "visual": artist.get("visual"),
                "languages": (artist.get("data") or {}).get("languages")}

    # MARK: albums

    def make_album(self, job, artist, release, ctx, position, req) -> str:
        data = dict(ctx, artist=self.artist_brief(artist), release=release)
        if req.get("songCount"):
            data["songCount"] = int(req["songCount"])
        with owner(kind="album", label=f"Writing “{release.get('title')}”", artistId=artist["id"]):
            a = self.ask("album", "Write this release: tracklist, liner notes and cover brief.", data, ALBUM, "album")
        album_id = new_id("al_")
        max_seconds = ctx["limits"]["maxSongSeconds"]
        self.db.insert("albums", {
            "id": album_id, "artist_id": artist["id"], "title": release["title"], "year": release.get("year"),
            "kind": release.get("kind") or "album", "concept": release.get("concept"),
            "liner_notes": a.get("liner_notes"), "cover_brief": a.get("cover_brief"), "position": position,
            "status": "ready", "brief_id": req.get("briefId"), "created": time.time(),
            "data_json": {"sourceSetId": req.get("sourceSetId")}})
        self.db.index("album", album_id, release["title"], artist["name"], release.get("concept") or "")
        no_vocals = artist_is_instrumental(artist)
        for number, t in enumerate(a["tracks"], start=1):
            track_id = new_id("tr_")
            seconds = t.get("seconds")
            self.db.insert("tracks", {
                "id": track_id, "album_id": album_id, "artist_id": artist["id"], "number": number,
                "title": t["title"], "theme": t.get("theme"),
                "is_instrumental": 1 if t.get("instrumental") or no_vocals else 0,
                "language": t.get("language"), "seed": random.randrange(2**31),
                "target_seconds": min(float(seconds), max_seconds) if seconds else None,
                "reference_track_ids_json": t.get("draws_on") or [], "status": "planned", "created": time.time()})
            self.db.index("track", track_id, t["title"], artist["name"], t.get("theme") or "")
        self.events.emit("library", album=album_id)
        try:
            self.cover(album_id)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001
            self.log(f"[builder] cover failed: {error!r}")
        job.count(albumsWritten=1, songsPlanned=len(a["tracks"]))
        return album_id

    # MARK: songs

    def vocal_tags(self, artist_id) -> str:
        """The artist's lead vocal as caption tags (AI-written from their voice line, then kept, so
        every song has the same singer). Artists made before this get theirs on first use."""
        a = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        if not a:
            return ""
        data = a.get("data") or {}
        if data.get("vocalTags"):
            return data["vocalTags"]
        if not a.get("voice") or not self.provider.configured():
            return ""
        data = {"artist": {"name": a["name"], "voice": a.get("voice"), "bio": (a.get("bio") or "")[:1500],
                           "members": a.get("members")}}
        try:
            with owner(kind="vocals", label=f"Vocal tags for {a['name']}", artistId=artist_id):
                out = self.ask("vocals", "Write the vocal tags for this artist.", data, VOCALS, "vocals", fast=True)
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001 — the voice line itself still goes in the caption
            self.log(f"[builder] vocal tags failed: {error!r}")
            return ""
        tags = (out.get("vocal_tags") or "").strip()[:MAX_VOCAL_TAGS]
        if tags:
            data["vocalTags"] = tags
            self.db.update("artists", artist_id, {"data_json": data})
            self.log(f"[builder] vocal tags for {a['name']}: {tags}")
        return tags

    def write_song(self, track_id) -> dict:
        t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if not t:
            raise RuntimeError("No such track")
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (t["album_id"],))
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (t["artist_id"],))
        siblings = self.db.all("SELECT number, title, theme, style FROM tracks WHERE album_id = ? ORDER BY number", (t["album_id"],))
        instrumental = track_is_instrumental(t, artist)
        extra = {"artist": self.artist_brief(artist),
                 "release": {"title": album["title"], "kind": album["kind"], "year": album["year"], "concept": album.get("concept")},
                 "tracklist": siblings,
                 "song": {"number": t["number"], "title": t["title"], "theme": t.get("theme"),
                          "instrumental": instrumental, "language": t.get("language"),
                          "targetSeconds": t.get("target_seconds")}}
        if album.get("brief_id"):
            brief = self.db.one("SELECT brief_json FROM style_briefs WHERE id = ?", (album["brief_id"],))
            if brief:
                extra["styleBrief"] = brief["brief"]
                extra["song"]["drawsOnSourceTracks"] = t.get("reference_track_ids") or []
        ctx = self.context(extra)
        with owner(kind="lyrics", label=f"Lyrics for “{t['title']}”", trackId=track_id, albumId=t["album_id"],
                   artistId=t["artist_id"]):
            s = self.ask("song", "Write this song's lyrics and its style text.", ctx, SONG, "song")
        # The plan, the act or the song writer itself (instrumental: true, or the marker as lyrics)
        # can say a song has no vocals; any of them makes it instrumental, and it stays so.
        instrumental = instrumental or bool(s.get("instrumental")) or says_instrumental(s.get("lyrics"))
        lyrics = INSTRUMENTAL if instrumental else s["lyrics"]
        bpm = s.get("bpm") if isinstance(s.get("bpm"), int) and 30 <= s["bpm"] <= 300 else None
        explicit = bool(s.get("explicit")) and bool(ctx["listener"]["explicitLyricsAllowed"])
        self.db.update("tracks", track_id, {"style": s["style"][:512], "lyrics": lyrics[:4096],
                                            "is_instrumental": 1 if instrumental else 0,
                                            "language": s.get("language") or t.get("language"), "bpm": bpm,
                                            "keyscale": s.get("key") or None, "explicit": 1 if explicit else 0,
                                            "status": "written" if t["status"] in ("planned", "failed") else t["status"]})
        self.db.index("track", track_id, t["title"], artist["name"], lyrics)
        self.events.emit("track", track=self.library.track(track_id))
        return self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))

    # MARK: retakes with feedback

    def take_values(self, t) -> dict:
        """What the music model gets for this song: caption, lyrics, tempo, key, vocal tags."""
        return {"style": t.get("style") or "", "lyrics": t.get("lyrics") or "", "bpm": t.get("bpm"),
                "key": t.get("keyscale"), "vocalTags": (t.get("data") or {}).get("vocalTags") or "",
                "language": t.get("language")}

    def retake(self, track_id, feedback: str) -> dict:
        """The listener's feedback for a new take: stored with the song (history), then the AI
        revises the song's ORIGINAL generation values following all feedback so far (rules/retake.md).
        The revision waits on the track as `pending` until the new take is saved."""
        t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if not t:
            raise RuntimeError("No such track")
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (t["artist_id"],))
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (t["album_id"],))
        data = dict(t.get("data") or {})
        history = add_feedback(data.get("retake"), feedback, self.take_values(t))
        instrumental = track_is_instrumental(t, artist)
        request = {"originalPrompt": history["original"], "currentTake": self.take_values(t),
                   "feedback": [f["text"] for f in history["feedback"]], "latestFeedback": history["feedback"][-1]["text"],
                   "song": {"title": t["title"], "theme": t.get("theme"), "instrumental": instrumental},
                   "artist": {"name": artist["name"], "voice": artist.get("voice")},
                   "release": {"title": album["title"]} if album else None,
                   "limits": {"maxStyleCharacters": 512},
                   "explicitLyricsAllowed": bool(self.settings.section("library").get("allowExplicit"))}
        with owner(kind="retake", label=f"New take of “{t['title']}”", trackId=track_id, albumId=t["album_id"],
                   artistId=t["artist_id"]):
            out = self.ask("retake", "Revise this song for a new take, following the listener's feedback.",
                           request, RETAKE, "retake")
        bpm = out.get("bpm") if isinstance(out.get("bpm"), int) and 30 <= out["bpm"] <= 300 else None
        history["pending"] = {"style": (out.get("style") or "")[:512],
                              "lyrics": INSTRUMENTAL if instrumental else (out.get("lyrics") or "")[:4096],
                              "bpm": bpm, "key": out.get("key") or None,
                              "vocalTags": "" if instrumental else (out.get("vocal_tags") or "").strip()[:MAX_VOCAL_TAGS],
                              "changed": (out.get("changed") or "")[:300]}
        history["feedback"][-1]["changed"] = history["pending"]["changed"]
        data["retake"] = history
        self.db.update("tracks", track_id, {"data_json": data})
        self.log(f"[builder] new take of {t['title']!r} with feedback {feedback[:80]!r}: {history['pending']['changed']}")
        return history["pending"]

    def custom_song(self, job, req) -> dict:
        """The listener's own lyrics and style, by one of their artists or a new one invented for it."""
        style = (req.get("style") or "").strip()
        lyrics = (req.get("lyrics") or "").strip()
        instrumental = bool(req.get("instrumental"))
        artist_id = req.get("artistId")
        ctx = self.context({"customSong": {"style": style, "title": req.get("title")}})
        if not artist_id:
            job.progress(0.1, "Inventing an artist for this song")
            roster = self.ask("roster", "Plan 1 new fictional artist who would perform this song.",
                              dict(ctx, wanted=1, existingArtistNames=self.library.existing_names()), ROSTER, "roster")
            artist_id = self.make_artist(job, roster["artists"][0], ctx, req)
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        title = (req.get("title") or "").strip() or "Untitled"
        album_id = new_id("al_")
        position = self.db.scalar("SELECT COUNT(*) FROM albums WHERE artist_id = ?", (artist_id,))
        self.db.insert("albums", {"id": album_id, "artist_id": artist_id, "title": title, "year": int(time.strftime("%Y")),
                                  "kind": "single", "concept": f"A single written by the listener. Style: {style}",
                                  "position": position, "status": "ready", "created": time.time()})
        voice = artist.get("voice") or ""
        full_style = style if instrumental or not voice or voice.lower() in style.lower() else f"{style}, {voice}"
        track_id = new_id("tr_")
        self.db.insert("tracks", {"id": track_id, "album_id": album_id, "artist_id": artist_id, "number": 1,
                                  "title": title, "style": full_style[:512],
                                  "lyrics": "[Instrumental]" if instrumental else lyrics[:4096],
                                  "is_instrumental": 1 if instrumental else 0, "seed": random.randrange(2**31),
                                  "language": (req.get("language") or None),
                                  "status": "written" if (lyrics or instrumental) else "planned",
                                  "data_json": {"custom": True}, "created": time.time()})
        self.db.index("album", album_id, title, artist["name"])
        self.db.index("track", track_id, title, artist["name"], lyrics)
        if not lyrics and not instrumental:
            # The listener gave only a style: the AI writes the words, and their style is kept as given.
            job.progress(0.5, "Writing the lyrics")
            self.write_song(track_id)
            if style:
                self.db.update("tracks", track_id, {"style": full_style[:512]})
        job.progress(0.8, "Designing the cover")
        try:
            self.cover(album_id)
        except Exception as error:  # noqa: BLE001
            self.log(f"[builder] cover failed: {error!r}")
        self.events.emit("library", album=album_id)
        return {"artistId": artist_id, "albumId": album_id, "trackId": track_id}

    # MARK: artwork

    def _save_image(self, name, content: bytes, old_rel=None) -> str:
        """A new file each time (so a regenerated image isn't served from a cache); the previous
        one is removed once the new one is written."""
        ext = ".jpg" if content[:3] == b"\xff\xd8\xff" else ".webp" if content[8:12] == b"WEBP" else ".png"
        path = os.path.join(self.paths.artwork, f"{name}-{int(time.time() * 1000)}{ext}")
        with open(path, "wb") as f:
            f.write(content)
        old = self.paths.abs(old_rel) if old_rel else None
        inside = old and os.path.dirname(os.path.abspath(old)) == os.path.abspath(self.paths.artwork)
        if inside and os.path.abspath(old) != os.path.abspath(path) and os.path.isfile(old):
            try:
                os.remove(old)
            except OSError as error:
                self.log(f"[builder] couldn't remove the old image {old}: {error}")
        return self.paths.rel(path)

    def image_result(self, source, prompt=None):
        out = {"source": source, "model": self.settings.section("provider").get("imageModel"), "prompt": prompt}
        if source != "provider":
            out["error"] = getattr(self.provider, "last_image_error", None)
        return out

    def artist_profile(self, artist) -> dict:
        """Everything known about the act, for whoever pictures it: the dossier as written."""
        data = artist.get("data") or {}
        concept = data.get("concept") or {}
        return {k: v for k, v in {
            "name": artist.get("name"), "bio": artist.get("bio"), "origin": artist.get("origin"),
            "formed": artist.get("formed"), "genres": artist.get("genres"), "members": artist.get("members"),
            "visual": artist.get("visual"), "voice": artist.get("voice"), "vocalTags": data.get("vocalTags"),
            "languages": data.get("languages"), "concept": concept.get("concept") if isinstance(concept, dict) else None,
        }.items() if v}

    def image_target(self) -> dict:
        """The image model the prompt is for, and its defaults where the server says (Lemonade)."""
        cfg = self.settings.section("provider")
        out = {"model": cfg.get("imageModel"), "size": cfg.get("imageSize") or "the model's own"}
        try:
            entry = (self.provider.image_server() or {}).get("entry") or {}
        except Exception:  # noqa: BLE001
            entry = {}
        opts = entry.get("recipe_options") or {}
        if opts.get("width") and opts.get("height") and not cfg.get("imageSize"):
            out["size"] = f"{opts['width']}x{opts['height']}"
        if entry.get("recipe"):
            out["family"] = entry.get("recipe")
        return out

    def image_feedback(self, table, row, feedback, prompt_col):
        """Feedback for a new image: stored per artist/album with the original prompt it revises
        (the stored prompt the first time feedback is given). Returns the history or None."""
        if not (feedback or "").strip():
            return None
        data = dict(row.get("data") or {})
        history = add_feedback(data.get("imageFeedback"), feedback, row.get(prompt_col) or "")
        data["imageFeedback"] = history
        self.db.update(table, row["id"], {"data_json": data})
        row["data"] = data
        return history

    @staticmethod
    def with_feedback(data, fallback, history):
        """Add the original prompt and every piece of feedback to the prompt writer's data; the
        fallback (prompt writer down) is the original prompt with the feedback appended."""
        if not history:
            return data, fallback
        notes = [f["text"] for f in history["feedback"]]
        data = dict(data, previousPrompt=history.get("original") or "", feedback=notes)
        base = history.get("original") or fallback
        return data, (base + ". " + ". ".join(notes)).strip()

    def image_prompt(self, kind, brief, data, fallback=None):
        """(prompt, negative prompt). The artwork rules decide what goes in; this passes the brief,
        everything known about the subject, and which image model will draw it. `fallback` is the
        prompt used when the prompt writer fails (default: the brief)."""
        cfg = self.settings.section("provider")
        negative = (cfg.get("imageNegativePrompt") or "").strip()
        fallback = fallback or brief
        if not cfg.get("imagesEnabled"):
            return fallback, negative
        try:
            with self.provider.purpose("cover prompt" if "cover" in kind else "photo prompt"):
                result = self.ask("artwork", f"Write the image prompt for this {kind}.",
                                  dict(data, brief=brief, kind=kind, imageModel=self.image_target()), ART, "art", fast=True)
            extra = (result.get("negative_prompt") or "").strip()
            if extra:
                negative = ", ".join(x for x in (negative, extra) if x)
            return result["prompt"], negative
        except Cancelled:
            raise
        except Exception as error:  # noqa: BLE001
            self.log(f"[images] writing the {kind} prompt failed with {cfg.get('fastModel') or cfg.get('textModel')} "
                     f"({error!r}); drawing from the brief directly")
            return fallback, negative

    def cover(self, album_id, feedback=None):
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (album_id,))
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (album["artist_id"],))
        history = self.image_feedback("albums", album, feedback, "cover_prompt")
        with owner(kind="cover", label=f"Cover for “{album['title']}”", albumId=album_id, artistId=artist["id"]):
            return self._cover(album, artist, history)

    def _cover(self, album, artist, history=None):
        album_id = album["id"]
        brief = album.get("cover_brief") or album.get("concept") or album["title"]
        text = cover_text(album["title"], artist["name"])
        data, fallback = self.with_feedback({
            "artist": self.artist_profile(artist), "title": album["title"], "release": album["kind"],
            "year": album.get("year"), "concept": album.get("concept"), "coverText": text},
            cover_fallback(brief, text), history)
        prompt, negative = self.image_prompt("record cover", brief, data, fallback=fallback)
        content, source = self.provider.image(prompt, f"{artist['name']} {album['title']}", "cover", negative)
        result = self.image_result(source, prompt)
        rel = self._save_image(f"cover-{album_id}", content, album.get("cover_path"))
        self.db.update("albums", album_id, {"cover_path": rel, "cover_prompt": prompt,
                                            "data_json": dict(album.get("data") or {}, coverSource=source)})
        self.events.emit("library", album=album_id)
        return result

    def portrait(self, artist_id, feedback=None):
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (artist_id,))
        history = self.image_feedback("artists", artist, feedback, "portrait_prompt")
        with owner(kind="photo", label=f"Photo of {artist['name']}", artistId=artist_id):
            return self._portrait(artist, history)

    def _portrait(self, artist, history=None):
        artist_id = artist["id"]
        brief = artist.get("visual") or artist.get("bio") or artist["name"]
        data, fallback = self.with_feedback({"artist": self.artist_profile(artist)}, brief, history)
        prompt, negative = self.image_prompt("press photo", brief, data, fallback=fallback)
        content, source = self.provider.image(prompt, artist["name"], "portrait", negative)
        result = self.image_result(source, prompt)
        rel = self._save_image(f"portrait-{artist_id}", content, artist.get("portrait_path"))
        self.db.update("artists", artist_id, {"portrait_path": rel, "portrait_prompt": prompt,
                                              "data_json": dict(artist.get("data") or {}, portraitSource=source)})
        self.events.emit("library", artist=artist_id)
        return result


def cover_text(title, artist) -> dict:
    """The exact strings a cover may show, passed to the prompt writer as data."""
    return {"title": (title or "").strip(), "artist": (artist or "").strip()}


def cover_fallback(brief, text) -> str:
    """When the prompt writer fails: the brief plus the exact title and artist in quotes (a fixed
    template, so the image model isn't left to invent the lettering)."""
    quoted = " and ".join(f'"{v}"' for v in (text.get("title"), text.get("artist")) if v)
    lead = f"Square album cover with text reading {quoted}" if quoted else "Square album cover"
    lead += ", title across the top, artist name smaller at the bottom" if text.get("title") and text.get("artist") else ""
    return f"{lead}. {brief}".strip()
