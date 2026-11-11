"""The record-ahead studio. One song records at a time. Demand, in priority order:
1. what the player needs now (the current song and the next few in its queue),
2. songs the listener asked to record (Record album / Record song),
3. optional idle prefetch (newest unrecorded songs, favorites first) when enabled.
Lyrics for upcoming songs are written ahead, in parallel with the recording."""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import traceback

from smcore.builder import with_vocals
from smcore.engine_client import EngineCancelled, EngineError
from smcore.jobs import Cancelled
from smcore.runtime import run_tool

REFERENCE_SECONDS = 60.0   # length of a mashup's reference audio (ACE-Step samples 3 x 10 s of it)
MIN_SECTION = 10.0         # each source with a share gets at least one sampled section's worth


def safe(name: str) -> str:
    cleaned = re.sub(r"[^\w\- ]+", "", name or "", flags=re.UNICODE).strip()
    return (cleaned or "untitled")[:60]


class Studio:
    def __init__(self, db, paths, settings, engine, builder, library, events, log=print, ffmpeg=None,
                 builtin=None, secrets=None):
        self.db, self.paths, self.settings, self.engine = db, paths, settings, engine
        # With Lemonade selected: the built-in ACE-Step engine for songs that need reference audio.
        self.builtin, self.secrets = builtin, secrets
        self.active = engine     # the engine recording the current song
        self.ffmpeg = ffmpeg or (lambda: shutil.which("ffmpeg"))   # the app passes Runtime.ffmpeg_path
        self.builder, self.library, self.events, self.log = builder, library, events, log
        self.lock = threading.Condition()
        self.needs = []          # from the player
        self.requested = []      # from the listener
        self.current = None      # {"trackId", "stage", "overall", "elapsed", "jobId"}
        self.cancelled = set()
        self.writing = set()
        self.paused = False
        self.attempts = {}
        self.blocked = None      # an engine-level failure: wait for the listener before retrying
        # Interrupted recordings and earlier failures go back to the queue on start: a failure from
        # an engine that since got fixed (a hang, a missing module, no GPU memory) mustn't stick.
        self.rerecords = set()   # recorded songs queued to be recorded again (old take kept until then)
        db.run("UPDATE tracks SET status = CASE WHEN audio_path IS NOT NULL THEN 'recorded' "
               "WHEN lyrics IS NULL THEN 'planned' ELSE 'written' END, error = NULL "
               "WHERE status IN ('recording', 'queued', 'failed')")
        threading.Thread(target=self._loop, daemon=True, name="studio").start()
        threading.Thread(target=self._lyrics_loop, daemon=True, name="lyrics-ahead").start()

    # MARK: demand

    def need(self, track_ids):
        with self.lock:
            ids = [t for t in track_ids if t]
            if ids and (not self.needs or ids[0] != self.needs[0] or self.blocked):
                # The listener pressed play on this song: a new try, whatever happened before.
                self.attempts.pop(ids[0], None)
                self.cancelled.discard(ids[0])
                self.blocked = None
            self.needs = ids
            self.lock.notify_all()
        return self.status()

    def request(self, track_ids):
        with self.lock:
            for t in track_ids:
                if t not in self.requested:
                    self.requested.append(t)
                self.attempts.pop(t, None)
                self.cancelled.discard(t)
                self.blocked = None
                row = self.db.one("SELECT status FROM tracks WHERE id = ?", (t,))
                if row and row["status"] in ("planned", "written", "failed"):
                    self.db.update("tracks", t, {"status": "queued", "error": None})
                    self.events.emit("track", track=self.library.track(t))
            self.lock.notify_all()
        return self.status()

    def rerecord(self, track_id):
        """Record a recorded (or failed) song again with today's caption and vocal tags. Its current
        audio stays playable until the new take is saved; a failed take leaves it as it was."""
        row = self.db.one("SELECT status, audio_path FROM tracks WHERE id = ?", (track_id,))
        if not row:
            raise ValueError("No such song")
        with self.lock:
            if self.current and self.current["trackId"] == track_id:
                return self.status()
            if row["audio_path"]:
                self.rerecords.add(track_id)
            self.db.update("tracks", track_id, {"status": "queued", "error": None})
        self.events.emit("track", track=self.library.track(track_id))
        return self.request([track_id])

    def drop(self, track_ids, wait=30.0):
        """Before songs are deleted: take them out of every queue and stop the one recording."""
        ids = set(track_ids)
        with self.lock:
            self.needs = [t for t in self.needs if t not in ids]
            self.requested = [t for t in self.requested if t not in ids]
            self.cancelled |= ids
            self.rerecords -= ids
            current = self.current
        if current and current["trackId"] in ids:
            self.active.cancel(current["jobId"])
            deadline = time.time() + wait
            while time.time() < deadline:
                with self.lock:
                    if not self.current or self.current["trackId"] not in ids:
                        break
                    self.lock.wait(0.2)
        self.events.emit("studio", studio=self.status())

    def cancel(self, track_id):
        with self.lock:
            self.requested = [t for t in self.requested if t != track_id]
            self.needs = [t for t in self.needs if t != track_id]
            self.cancelled.add(track_id)
            current = self.current
        provider = getattr(self.builder, "provider", None)
        if hasattr(provider, "cancel_calls"):
            provider.cancel_calls(lambda o: (o.get("cancel") or {}).get("id") == track_id)
        if current and current["trackId"] == track_id:
            self.active.cancel(current["jobId"])
        row = self.db.one("SELECT status, lyrics FROM tracks WHERE id = ?", (track_id,))
        if row and row["status"] == "queued":
            self.db.update("tracks", track_id, {"status": "written" if row["lyrics"] else "planned"})
            self.events.emit("track", track=self.library.track(track_id))
        return self.status()

    def set_paused(self, paused: bool):
        with self.lock:
            self.paused = bool(paused)
            self.lock.notify_all()
        return self.status()

    def _unrecorded(self, ids):
        out = []
        for t in ids:
            row = self.db.one("SELECT status FROM tracks WHERE id = ?", (t,))
            if row and row["status"] != "recorded" and t not in self.cancelled and self.attempts.get(t, 0) < 2:
                out.append(t)
        return out

    def upcoming(self, limit=10):
        with self.lock:
            needs, requested = list(self.needs), list(self.requested)
        order = self._unrecorded(needs) + [t for t in self._unrecorded(requested) if t not in needs]
        if len(order) < limit and self.settings.section("studio").get("prefetch") and not self.paused:
            rows = self.db.all(
                "SELECT t.id FROM tracks t JOIN albums al ON al.id = t.album_id "
                "LEFT JOIN favorites f ON f.kind = 'album' AND f.item_id = al.id "
                "WHERE t.status IN ('planned', 'written') ORDER BY (f.item_id IS NULL), al.created DESC, t.number LIMIT ?",
                (limit,))
            order += [r["id"] for r in rows if r["id"] not in order and r["id"] not in self.cancelled]
        return order[:limit]

    def status(self):
        with self.lock:
            current = dict(self.current) if self.current else None
        upcoming = self.upcoming(20)
        return {"current": current, "queue": self.library.tracks(upcoming), "paused": self.paused,
                "blocked": self.blocked, "engine": self.engine.status()}

    # MARK: lyrics ahead

    def _lyrics_loop(self):
        while True:
            time.sleep(1.0)
            try:
                ahead = int(self.settings.section("studio").get("lyricsAhead") or 0)
                if ahead <= 0 or not self.builder.provider.configured():
                    continue
                for t in self.upcoming(ahead + 1):
                    row = self.db.one("SELECT status, lyrics FROM tracks WHERE id = ?", (t,))
                    if row and not row["lyrics"] and t not in self.writing:
                        from smcore.queue import owner
                        try:
                            with owner(cancel={"what": "recording", "id": t, "label": "this song"}):
                                self._write(t)
                        except Cancelled:
                            pass   # the song was taken out of the queue while its lyrics waited
            except Exception:  # noqa: BLE001
                self.log(traceback.format_exc())
                time.sleep(10)

    def _write(self, track_id):
        self.writing.add(track_id)
        try:
            self.builder.write_song(track_id)
        finally:
            self.writing.discard(track_id)

    # MARK: recording

    def _loop(self):
        while True:
            with self.lock:
                while True:
                    order = [] if self.paused or self.blocked else self.upcoming(1)
                    if order:
                        break
                    self.lock.wait(5.0)
            track_id = order[0]
            try:
                self.record(track_id)
            except Cancelled:
                # Stopped while its lyrics request waited: back to where it was, not a failure.
                row = self.db.one("SELECT status, lyrics FROM tracks WHERE id = ?", (track_id,))
                if row and row["status"] in ("recording", "queued"):
                    self.db.update("tracks", track_id, {"status": "written" if row["lyrics"] else "planned"})
                    self.events.emit("track", track=self.library.track(track_id))
            except Exception as error:  # noqa: BLE001
                self.log(traceback.format_exc())
                message = str(error) or repr(error)
                if self._restore_take(track_id, f"Recording again failed: {message}"):
                    continue
                if isinstance(error, EngineError) and self.active.state == "failed":
                    # The engine itself failed (load error, exit, hang), not this song: don't count it
                    # against the song or move on to the next one; wait until play/Retry is pressed.
                    with self.lock:
                        self.blocked = message
                else:
                    self.attempts[track_id] = self.attempts.get(track_id, 0) + 1
                self.db.update("tracks", track_id, {"status": "failed", "error": message})
                self.events.emit("track", track=self.library.track(track_id))
                if isinstance(error, EngineError):
                    time.sleep(1)
            finally:
                with self.lock:
                    self.current = None
                    self.requested = [t for t in self.requested if self._unrecorded([t])]
                self.events.emit("studio", studio=self.status())

    def record(self, track_id):
        t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if not t or t["status"] == "recorded":
            return
        from smcore.queue import owner
        with owner(trackId=track_id, albumId=t["album_id"], artistId=t["artist_id"],
                   cancel={"what": "recording", "id": track_id, "label": t["title"]}):
            try:
                return self._record(track_id, t)
            except BaseException:
                self._end_retake(track_id, "failed")   # the old take and its values stay
                raise

    def _end_retake(self, track_id, outcome):
        """A take made from feedback ended: on success its revised values become the song's; either
        way the pending revision is cleared and the feedback notes how it went."""
        row = self.db.one("SELECT data_json FROM tracks WHERE id = ?", (track_id,))
        data = dict((row or {}).get("data") or {})
        history = dict(data.get("retake") or {})
        pending = history.pop("pending", None)
        if not pending:
            return
        values = {}
        if outcome == "recorded":
            values = {"style": pending.get("style"), "lyrics": pending.get("lyrics"),
                      "bpm": pending.get("bpm"), "keyscale": pending.get("key")}
            values = {k: v for k, v in values.items() if v}     # a value the revision left out stays
            if pending.get("vocalTags"):
                data["vocalTags"] = pending["vocalTags"]
        if history.get("feedback"):
            history["feedback"][-1]["outcome"] = outcome
        data["retake"] = history
        values["data_json"] = data
        self.db.update("tracks", track_id, values)

    def _record(self, track_id, t):
        jid = f"{track_id}-{int(time.time())}"
        with self.lock:
            now = time.time() * 1000
            self.current = {"trackId": track_id, "jobId": jid, "stage": "writing", "overall": 0.0, "elapsed": 0.0,
                            "started": now, "since": now}
        self.events.emit("studio", studio=self.status())
        if not t["lyrics"]:
            while track_id in self.writing:
                time.sleep(0.5)
            t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
            if not t["lyrics"]:
                self._write(track_id)
                t = self.db.one("SELECT * FROM tracks WHERE id = ?", (track_id,))
        if track_id in self.cancelled:
            return
        artist = self.db.one("SELECT * FROM artists WHERE id = ?", (t["artist_id"],))
        album = self.db.one("SELECT * FROM albums WHERE id = ?", (t["album_id"],))
        eng = self.settings.section("engine")
        self.db.update("tracks", track_id, {"status": "recording", "error": None})
        self.events.emit("track", track=self.library.track(track_id))
        engine, ref, note = self.route(t, album, artist)
        self.active = engine
        self._note(t, note)
        if engine.state != "ready":
            self._stage(track_id, "loading", detail="Starting the engine")

        def on_loading(info):
            self._stage(track_id, "loading", detail=info.get("stage"), loadElapsed=info.get("elapsed"), emit=False)
            self.events.emit("recording", trackId=track_id,
                             progress={"stage": "loading", "detail": info.get("stage"), "loadElapsed": info.get("elapsed")})

        provider = getattr(self.builder, "provider", None)
        if hasattr(provider, "release_image_model"):
            provider.release_image_model()   # optional: free the local image model's GPU memory first
        caps = engine.ensure_ready(on_loading=on_loading)
        self.builder.engine_languages = caps.get("languages") or self.builder.engine_languages
        out_dir = os.path.join(self.paths.audio, safe(artist["name"]) + "-" + artist["id"][-6:])
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{t['number']:02d} {safe(t['title'])}-{track_id[-6:]}.m4a")
        if t.get("audio_path"):
            # Recording again: a new file, so the old take stays playable until this one succeeds.
            out = os.path.join(out_dir, f"{t['number']:02d} {safe(t['title'])}-{track_id[-6:]}-take{int(time.time())}.m4a")
        max_seconds = float(eng.get("maxSongMinutes") or 4) * 60
        target = min(float(t["target_seconds"] or max_seconds), max_seconds)
        # The singer must be in the caption: the song's style text plus the artist's vocal tags
        # (or, without tags, their voice line), added mechanically if the style left them out.
        # A take from feedback records the AI's revision; it becomes the song's once the take saves.
        pending = ((t.get("data") or {}).get("retake") or {}).get("pending")
        if pending:
            t = dict(t, style=pending.get("style") or t["style"], lyrics=pending.get("lyrics") or t["lyrics"],
                     bpm=pending.get("bpm") or t.get("bpm"), keyscale=pending.get("key") or t.get("keyscale"),
                     data=dict(t.get("data") or {}, vocalTags=pending.get("vocalTags") or (t.get("data") or {}).get("vocalTags")))
        style = t["style"] or ""
        voice = ""
        from smcore.builder import track_is_instrumental
        instrumental = track_is_instrumental(t, artist)
        if instrumental and not t["is_instrumental"]:
            self.db.update("tracks", track_id, {"is_instrumental": 1, "lyrics": "[Instrumental]"})
            t = dict(t, is_instrumental=1, lyrics="[Instrumental]")
            self.log(f"[studio] {t['title']!r} is instrumental (its plan, lyrics or act say so): recorded without vocals")
        if not instrumental:
            # A song may carry its own vocal tags (a mashup take the AI re-voiced); else the artist's.
            voice = (t.get("data") or {}).get("vocalTags") or ""
            if not voice and hasattr(self.builder, "vocal_tags"):
                voice = self.builder.vocal_tags(artist["id"])
            voice = voice or (artist.get("voice") or "")
            if voice.strip().lower() != "instrumental":
                style = with_vocals(style, voice)
        self.log(f"[studio] caption for {t['title']!r}: {style}")
        job = {"id": jid, "style": style, "lyrics": t["lyrics"] or "", "planning": "full",
               "seed": int(t["seed"] or 0), "steps": int(eng.get("quality") or 16),
               "maxSemanticTokens": int(target * 25) + 50, "outputPath": out, "title": t["title"],
               "artist": artist["name"], "voice": voice, "isInstrumental": bool(instrumental),
               "maxSeconds": target}
        languages = caps.get("languages") or []
        if t.get("language") and t["language"] in languages:
            job["vocalLanguage"] = t["language"]
        if t.get("bpm"):
            job["bpm"] = int(t["bpm"])
        if t.get("keyscale"):
            job["keyscale"] = t["keyscale"]
        if caps.get("referenceAudio"):
            ref = ref or self.reference_for(t, album, artist)
            if ref:
                job["referenceAudio"] = ref
                self.log(f"[studio] reference audio for {t['title']!r}: {ref}")
        start = time.time()

        def on_progress(p):
            with self.lock:
                if self.current and self.current["trackId"] == track_id:
                    if p.get("stage") != self.current.get("stage"):
                        self.current["since"] = time.time() * 1000
                    self.current.update(stage=p.get("stage"), overall=p.get("overall", 0.0), elapsed=p.get("elapsed", 0.0),
                                        songSeconds=p.get("estimatedSongSeconds"), detail=p.get("detail"),
                                        tokens=p.get("tokens"), step=p.get("step"), steps=p.get("steps"),
                                        estimated=p.get("estimated"), chunk=p.get("chunk"), chunks=p.get("chunks"))
                    p = dict(p, since=self.current["since"], started=self.current.get("started"))
            self.events.emit("recording", trackId=track_id, progress=p)

        try:
            result = engine.generate(job, on_progress, on_loading=on_loading)
        except EngineCancelled:
            self._end_retake(track_id, "cancelled")
            self._remove_new_take(out, t)
            if not self._restore_take(track_id, None):
                self.db.update("tracks", track_id, {"status": "written"})
                self.events.emit("track", track=self.library.track(track_id))
            return
        if not self.db.one("SELECT id FROM tracks WHERE id = ?", (track_id,)):
            self._remove_new_take(out, t)  # deleted while it was recording
            return
        old = self.paths.abs(t.get("audio_path")) if t.get("audio_path") else None
        self.rerecords.discard(track_id)
        self.db.update("tracks", track_id, {
            "status": "recorded", "audio_path": self.paths.rel(out), "duration": result.get("duration"),
            "loudness": result.get("loudness"), "recorded_at": time.time(), "record_seconds": time.time() - start,
            "engine": self.builtin.backend() if engine is self.builtin else
                      (eng.get("backend") if eng.get("kind") != "custom" else "custom"),
            "error": None})
        if old and os.path.abspath(old) != os.path.abspath(out) and os.path.exists(old):
            try:
                os.remove(old)  # the new take replaced it
            except OSError as error:
                self.log(f"[studio] couldn't remove the old take {old}: {error}")
        if pending:
            self._end_retake(track_id, "recorded")
        if not artist.get("voice_track_id") and not t["is_instrumental"]:
            self.db.update("artists", artist["id"], {"voice_track_id": track_id})
        self.events.emit("track", track=self.library.track(track_id))
        self.events.emit("recorded", trackId=track_id)

    # MARK: which engine (Lemonade, or the built-in one for reference audio)

    def builtin_installed(self):
        layout = self.builtin.layout() if self.builtin else None
        return bool(layout and layout.python() and layout.ace_ok())

    def route(self, t, album, artist):
        """(engine, reference audio, note). Lemonade takes no reference audio, so with Lemonade
        selected a song that has one (a mashup take, voice mode, the artist's voice) goes to the
        built-in ACE-Step engine when it's installed, else to Lemonade without it, with a note.
        Each switch frees the other engine's GPU memory first."""
        from smcore import lemonade
        if not lemonade.selected(self.settings):
            return self.engine, None, None
        ref = self.reference_for(t, album, artist)
        if ref and self.builtin_installed():
            lemonade.unload(self.settings, self.secrets, lemonade.model(self.settings), self.log)
            return self.builtin, ref, ("Recorded with the built-in ACE-Step engine: this song uses reference "
                                       "audio (mashup or voice), which Lemonade can't take.")
        if self.builtin and self.builtin.proc:
            self.log("[studio] stopping the built-in engine to free GPU memory for Lemonade")
            self.builtin.stop()
        eng = self.settings.section("engine")
        if eng.get("unloadChatModelBeforeRecording"):
            lemonade.unload(self.settings, self.secrets, self.settings.section("provider").get("textModel"), self.log)
        if ref:
            return self.engine, None, ("Recorded by Lemonade without its reference audio (Lemonade can't take "
                                       "reference audio). Install the built-in engine in Settings › Music engine "
                                       "to record mashups and voice references with it.")
        return self.engine, None, None

    def _note(self, t, note):
        data = dict(t.get("data") or {})
        if data.get("recordNote") == note:
            return
        data["recordNote"] = note
        if note is None:
            data.pop("recordNote")
        else:
            self.log(f"[studio] {t['title']!r}: {note}")
        self.db.update("tracks", t["id"], {"data_json": data})

    def _stage(self, track_id, stage, emit=True, **extra):
        with self.lock:
            if not self.current or self.current["trackId"] != track_id:
                return
            if self.current.get("stage") != stage:
                self.current["since"] = time.time() * 1000
            self.current.update(stage=stage, **extra)
        if emit:
            self.events.emit("studio", studio=self.status())

    def _restore_take(self, track_id, error):
        """A re-recording that failed or was stopped: the song keeps its previous take."""
        with self.lock:
            was = track_id in self.rerecords
            self.rerecords.discard(track_id)
        row = self.db.one("SELECT audio_path FROM tracks WHERE id = ?", (track_id,))
        path = self.paths.abs(row["audio_path"]) if row and row.get("audio_path") else None
        if not (was and path and os.path.exists(path)):
            return False
        self.db.update("tracks", track_id, {"status": "recorded", "error": error})
        self.events.emit("track", track=self.library.track(track_id))
        return True

    def _remove_new_take(self, out, t):
        if t.get("audio_path") and os.path.abspath(self.paths.abs(t["audio_path"])) == os.path.abspath(out):
            return
        for p in (out, os.path.splitext(out)[0] + ".unencoded.wav"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    # MARK: reference audio (opt-in modes)

    def reference_for(self, t, album, artist):
        """A mashup take: sections of the source clips in the shares the AI chose. Voice mode on a
        links brief: the clips of the source tracks this song draws on. Otherwise, when enabled,
        the artist's own first recording (keeps one voice per artist)."""
        links = self.settings.section("links")
        refs = t.get("reference_track_ids") or []
        mashup = (t.get("data") or {}).get("mashup") or {}
        if mashup.get("setId"):
            return self.mashup_reference(t, mashup)
        brief = self.db.one("SELECT b.set_id, s.mode FROM style_briefs b JOIN source_sets s ON s.id = b.set_id "
                            "WHERE b.id = ?", (album["brief_id"],)) if album.get("brief_id") else None
        if refs and brief and "voice" in (brief.get("mode"), links.get("mode")):
            if brief:
                clips = []
                for number in refs:
                    row = self.db.one("SELECT clip_path FROM source_tracks WHERE set_id = ? AND position = ?",
                                      (brief["set_id"], int(number)))
                    path = self.paths.abs(row["clip_path"]) if row and row["clip_path"] else None
                    if path and os.path.exists(path):
                        clips.append(path)
                if len(clips) == 1:
                    return clips[0]
                if clips:
                    return self.concat(clips, t["id"])
        if self.settings.section("studio").get("artistVoiceReference") and artist.get("voice_track_id"):
            row = self.db.one("SELECT audio_path FROM tracks WHERE id = ? AND status = 'recorded'", (artist["voice_track_id"],))
            path = self.paths.abs(row["audio_path"]) if row else None
            if path and os.path.exists(path) and artist["voice_track_id"] != t["id"]:
                return path
        return None

    def mashup_reference(self, t, mashup):
        """One reference file for ACE-Step (which samples three 10 s sections across it for sound and
        timbre): from each source clip, a centred section as long as its share of REFERENCE_SECONDS.
        The shares are the AI's; this only measures and cuts. Nothing here reaches the output audio."""
        parts = []
        for src in mashup.get("sources") or []:
            share = float(src.get("share") or 0)
            if share <= 0:
                continue
            row = self.db.one("SELECT clip_path FROM source_tracks WHERE set_id = ? AND position = ?",
                              (mashup["setId"], int(src["number"])))
            path = self.paths.abs(row["clip_path"]) if row and row.get("clip_path") else None
            if path and os.path.exists(path):
                parts.append((path, share))
        if not parts:
            return None
        total = sum(share for _, share in parts)
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            return max(parts, key=lambda p: p[1])[0]
        from smcore.links import probe_seconds
        out = os.path.join(self.paths.clips, f"ref-{t['id']}.wav")
        args, chains = [ffmpeg, "-y", "-loglevel", "error"], []
        for i, (path, share) in enumerate(parts):
            length = max(MIN_SECTION, REFERENCE_SECONDS * share / total)
            duration = probe_seconds(ffmpeg, path)
            if duration:
                length = min(length, duration)
            start = max(0.0, (duration - length) / 2) if duration else 0.0
            args += ["-i", path]
            chains.append(f"[{i}:a]atrim=start={start:.2f}:duration={length:.2f},asetpts=PTS-STARTPTS,"
                          f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        graph = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
        try:
            run_tool(args + ["-filter_complex", graph, "-map", "[out]", out], timeout=120)
            return out
        except Exception as error:  # noqa: BLE001
            self.log(f"[studio] couldn't build the mashup reference: {error}")
            return max(parts, key=lambda p: p[1])[0]

    def concat(self, clips, track_id):
        ffmpeg = self.ffmpeg()
        if not ffmpeg:
            return clips[0]
        out = os.path.join(self.paths.clips, f"ref-{track_id}.wav")
        args = [ffmpeg, "-y", "-loglevel", "error"]
        for c in clips:
            args += ["-i", c]
        args += ["-filter_complex", "".join(f"[{i}:a]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1", out]
        try:
            run_tool(args, timeout=120)
            return out
        except Exception as error:  # noqa: BLE001
            self.log(f"[studio] couldn't join reference clips: {error}")
            return clips[0]
