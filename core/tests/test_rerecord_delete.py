"""Record again keeps the old take until the new one is saved; deletes stop recording and remove files."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore.db import Database  # noqa: E402
from smcore.engine_client import EngineCancelled, EngineError  # noqa: E402
from smcore.library import Library  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.studio import Studio  # noqa: E402


class Events:
    def __init__(self):
        self.seen = []

    def emit(self, kind, **data):
        self.seen.append((kind, data))


class Settings:
    def section(self, name):
        return {"prefetch": False} if name == "studio" else {}


class Builder:
    engine_languages = []

    def vocal_tags(self, artist_id):
        return "female vocals, breathy soprano"

    class provider:
        @staticmethod
        def configured():
            return False


class Engine:
    """Writes the take after `delay`; `fail` makes it fail; cancel() stops it."""

    def __init__(self, delay=0.2, fail=False):
        self.state, self.delay, self.fail = "ready", delay, fail
        self.jobs, self.stop = [], threading.Event()

    def status(self):
        return {"state": self.state}

    def ensure_ready(self, on_loading=None):
        return {}

    def cancel(self, jid):
        self.stop.set()

    def generate(self, job, on_progress=None, on_loading=None):
        self.jobs.append(job)
        if self.stop.wait(self.delay):
            self.stop.clear()
            raise EngineCancelled()
        if self.fail:
            raise EngineError("the song failed")  # a song-level failure: the engine stays ready
        os.makedirs(os.path.dirname(job["outputPath"]), exist_ok=True)
        with open(job["outputPath"], "wb") as f:
            f.write(b"new take")
        return {"duration": 3.0, "loudness": -14}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = Paths(os.path.join(self.tmp, "home"))
        self.db = Database(os.path.join(self.tmp, "lib.db"))
        self.events = Events()
        self.library = Library(self.db, self.paths, self.events, log=lambda *_: None)
        now = time.time()
        cover = os.path.join(self.paths.artwork, "cover-al1.png")
        portrait = os.path.join(self.paths.artwork, "portrait-ar1.png")
        for p in (cover, portrait):
            with open(p, "wb") as f:
                f.write(b"png")
        self.db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "voice": "warm breathy soprano",
                                   "portrait_path": self.paths.rel(portrait), "created": now})
        self.db.insert("albums", {"id": "al1", "artist_id": "ar1", "title": "A", "cover_path": self.paths.rel(cover),
                                  "created": now})
        self.old = os.path.join(self.paths.audio, "Zara Kaur-ar1", "01 Khoobsurat-xx0001.m4a")
        os.makedirs(os.path.dirname(self.old), exist_ok=True)
        with open(self.old, "wb") as f:
            f.write(b"old take")
        self.db.insert("tracks", {"id": "t1", "album_id": "al1", "artist_id": "ar1", "number": 1, "title": "Khoobsurat",
                                  "style": "bollywood pop", "lyrics": "la", "status": "recorded",
                                  "audio_path": self.paths.rel(self.old), "created": now})
        self.db.insert("tracks", {"id": "t2", "album_id": "al1", "artist_id": "ar1", "number": 2, "title": "Sapne",
                                  "lyrics": "la", "status": "written", "created": now})

    def studio(self, engine):
        return Studio(self.db, self.paths, Settings(), engine, Builder(), self.library, self.events, log=lambda *_: None)

    def wait(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def track(self, i="t1"):
        return self.db.one("SELECT * FROM tracks WHERE id = ?", (i,))


class RerecordTests(Fixture):
    def test_new_take_replaces_old_only_when_saved(self):
        engine = Engine(delay=0.6)
        studio = self.studio(engine)
        studio.rerecord("t1")
        self.assertTrue(self.wait(lambda: engine.jobs))
        self.assertTrue(os.path.exists(self.old))  # still there while the new take records
        self.assertIn("female vocals, breathy soprano", engine.jobs[0]["style"])  # today's caption + tags
        self.assertTrue(self.wait(lambda: self.track()["status"] == "recorded" and self.track()["audio_path"] != self.paths.rel(self.old)))
        new = self.paths.abs(self.track()["audio_path"])
        self.assertEqual(open(new, "rb").read(), b"new take")
        self.assertFalse(os.path.exists(self.old))

    def test_failed_take_keeps_the_old_one(self):
        engine = Engine(fail=True)
        studio = self.studio(engine)
        studio.rerecord("t1")
        self.assertTrue(self.wait(lambda: self.track()["error"]))
        t = self.track()
        self.assertEqual(t["status"], "recorded")
        self.assertEqual(t["audio_path"], self.paths.rel(self.old))
        self.assertIn("Recording again failed", t["error"])
        self.assertTrue(os.path.exists(self.old))


class DeleteTests(Fixture):
    def app_delete(self, studio, kind, item):
        # What App.delete does: stop recording, then delete rows and files.
        ids = {"track": [item], "album": ["t1", "t2"], "artist": ["t1", "t2"]}[kind]
        studio.drop(ids)
        return {"track": self.library.delete_track, "album": self.library.delete_album,
                "artist": self.library.delete_artist}[kind](item)

    def test_delete_song_while_it_records_cancels_and_removes_files(self):
        engine = Engine(delay=30)
        studio = self.studio(engine)
        studio.request(["t2"])
        self.assertTrue(self.wait(lambda: engine.jobs))
        out = self.app_delete(studio, "track", "t2")
        self.assertEqual(out["tracks"], ["t2"])
        self.assertIsNone(self.track("t2"))
        self.assertTrue(self.wait(lambda: studio.current is None))
        self.assertFalse(os.path.exists(engine.jobs[0]["outputPath"]))

    def test_delete_album_and_artist_remove_audio_and_artwork(self):
        studio = self.studio(Engine())
        out = self.app_delete(studio, "album", "al1")
        self.assertEqual(sorted(out["tracks"]), ["t1", "t2"])
        self.assertFalse(os.path.exists(self.old))
        self.assertFalse(os.path.exists(os.path.join(self.paths.artwork, "cover-al1.png")))
        self.assertFalse(os.path.isdir(os.path.dirname(self.old)))  # empty artist folder pruned
        self.assertIsNone(self.db.one("SELECT id FROM albums WHERE id = 'al1'"))
        self.app_delete(studio, "artist", "ar1")
        self.assertIsNone(self.db.one("SELECT id FROM artists WHERE id = 'ar1'"))
        self.assertFalse(os.path.exists(os.path.join(self.paths.artwork, "portrait-ar1.png")))
        self.assertTrue(os.path.isdir(self.paths.audio))  # the library roots stay


if __name__ == "__main__":
    unittest.main()
