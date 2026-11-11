"""Instrumental songs get no lyrics and no singer, whichever step says they're instrumental: the
release plan's flag, an act whose voice is "instrumental", the song writer (instrumental: true or
the "[Instrumental]" marker as lyrics), a mashup plan; and the engine request carries it."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from smcore.builder import Builder, says_instrumental, track_is_instrumental  # noqa: E402
from smcore.db import Database  # noqa: E402
from smcore.studio import Studio  # noqa: E402
from test_studio_retry import Events, Library, Paths, Settings  # noqa: E402


class Provider:
    """Answers by request name; records the data it was given."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def configured(self):
        return True

    def json(self, system, user, schema, name=None, fast=False):
        self.calls.append((name, user))
        return self.replies[name]


class Rules:
    def system(self, name):
        return name


class SettingsAll:
    def section(self, name):
        return {"library": {}, "engine": {"maxSongMinutes": 4}}.get(name, {})


class LibraryStub:
    def taste(self):
        return {}

    def listening_summary(self):
        return {}

    def track(self, t):
        return {"id": t}


class EventsStub:
    def emit(self, *a, **k):
        pass


SUNG = {"style": "dream pop, female vocals", "lyrics": "[Verse]\nla la la", "language": "en"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(os.path.join(self.tmp, "lib.db"))
        now = time.time()
        self.db.insert("artists", {"id": "ar_v", "name": "Zara Kaur", "voice": "warm alto", "created": now})
        self.db.insert("artists", {"id": "ar_i", "name": "Quiet Rooms", "voice": "instrumental",
                                   "data_json": {"vocalTags": "instrumental"}, "created": now})
        for a in ("ar_v", "ar_i"):
            self.db.insert("albums", {"id": "al_" + a, "artist_id": a, "title": "A", "created": now})

    def track(self, tid, artist, **values):
        row = {"id": tid, "album_id": "al_" + artist, "artist_id": artist, "number": 1, "title": tid,
               "status": "planned", "created": time.time()}
        row.update(values)
        self.db.insert("tracks", row)

    def builder(self, replies):
        self.provider = Provider(replies)
        return Builder(self.db, None, SettingsAll(), self.provider, Rules(), LibraryStub(), EventsStub(), log=lambda *_: None)


class MarkerTests(unittest.TestCase):
    def test_marker_is_a_literal_match(self):
        self.assertTrue(says_instrumental("Instrumental"))
        self.assertTrue(says_instrumental(" [Instrumental] "))
        self.assertFalse(says_instrumental("mostly instrumental with a vocal hook"))
        self.assertFalse(says_instrumental("[Verse]\nInstrumental love"))


class LyricsStepTests(Base):
    def test_an_act_without_vocals_gets_no_lyrics_even_if_the_writer_wrote_some(self):
        self.track("t1", "ar_i")
        b = self.builder({"song": SUNG})
        b.write_song("t1")
        t = self.db.one("SELECT * FROM tracks WHERE id = 't1'")
        self.assertEqual((t["lyrics"], t["is_instrumental"]), ("[Instrumental]", 1))
        self.assertIn('"instrumental": true', self.provider.calls[0][1])   # the writer was told

    def test_the_song_writer_can_say_instrumental(self):
        for tid, reply in (("t2", dict(SUNG, instrumental=True)), ("t3", dict(SUNG, lyrics="[Instrumental]"))):
            self.track(tid, "ar_v")
            self.builder({"song": reply}).write_song(tid)
            t = self.db.one("SELECT * FROM tracks WHERE id = ?", (tid,))
            self.assertEqual((t["lyrics"], t["is_instrumental"]), ("[Instrumental]", 1), tid)

    def test_a_sung_song_keeps_its_lyrics(self):
        self.track("t4", "ar_v")
        self.builder({"song": SUNG}).write_song("t4")
        t = self.db.one("SELECT * FROM tracks WHERE id = 't4'")
        self.assertEqual((t["lyrics"], t["is_instrumental"]), ("[Verse]\nla la la", 0))

    def test_release_plan_tracks_of_an_instrumental_act_are_instrumental(self):
        b = self.builder({"album": {"liner_notes": "n", "tracks": [{"title": "One"}, {"title": "Two", "instrumental": False}]}})
        b.cover = lambda album_id: None

        class Job:
            def count(self, **k):
                pass
        artist = self.db.one("SELECT * FROM artists WHERE id = 'ar_i'")
        album = b.make_album(Job(), artist, {"title": "Rooms"}, b.context(), 0, {})
        flags = [r["is_instrumental"] for r in self.db.all("SELECT is_instrumental FROM tracks WHERE album_id = ?", (album,))]
        self.assertEqual(flags, [1, 1])


class Engine:
    def __init__(self):
        self.state, self.jobs = "ready", []

    def status(self):
        return {"state": self.state}

    def ensure_ready(self, on_loading=None):
        return {}

    def generate(self, job, on_progress=None, on_loading=None):
        self.jobs.append(job)
        return {"duration": 1.0, "loudness": -14}


class BuilderStub:
    engine_languages = []

    class provider:
        @staticmethod
        def configured():
            return False

    def vocal_tags(self, artist_id):
        return "female vocals, warm alto"


class RecordingTests(Base):
    def record(self, tid):
        engine = Engine()
        studio = Studio(self.db, Paths(self.tmp), Settings(), engine, BuilderStub(), Library(), Events(), log=lambda *_: None)
        studio.request([tid])
        end = time.time() + 10
        while not engine.jobs and time.time() < end:
            time.sleep(0.05)
        return engine.jobs[0]

    def test_marker_lyrics_record_without_a_singer(self):
        # A song saved with the marker but not the flag (the old path): no vocals in the request.
        self.track("t5", "ar_v", lyrics="[Instrumental]", style="ambient piano", status="written")
        job = self.record("t5")
        self.assertTrue(job["isInstrumental"])
        self.assertEqual(job["voice"], "")
        self.assertNotIn("vocal", job["style"])
        self.assertEqual(self.db.one("SELECT is_instrumental FROM tracks WHERE id = 't5'")["is_instrumental"], 1)

    def test_an_instrumental_act_records_without_vocals(self):
        self.track("t6", "ar_i", lyrics="[Verse]\nwords written before the fix", style="ambient", status="written")
        job = self.record("t6")
        self.assertTrue(job["isInstrumental"])
        self.assertEqual(job["lyrics"], "[Instrumental]")

    def test_a_sung_song_keeps_its_singer(self):
        self.track("t7", "ar_v", lyrics="[Verse]\nla", style="pop", status="written")
        job = self.record("t7")
        self.assertFalse(job["isInstrumental"])
        self.assertIn("female vocals", job["style"])


class MashupTests(unittest.TestCase):
    def test_marker_lyrics_make_an_instrumental_take(self):
        from smcore.app import App
        os.environ["SM_SECRET_STORE"] = "file"
        os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        app = App(home=os.path.join(tmp, "home"), log=lambda *a: None)
        self.addCleanup(app.engine.stop)
        app.studio = None
        app.links.studio = None
        app.db.insert("source_sets", {"id": "ss1", "mode": "mashup", "status": "ready", "created": time.time()})
        app.builder.portrait = lambda a: None
        app.builder.cover = lambda a: None
        plan = {"title": "T", "artist": {"name": "A", "voice": "v", "vocal_tags": "female vocals"},
                "songs": [{"title": "S", "caption": "ambient", "lyrics": "[Instrumental]", "vocal_tags": "female vocals",
                           "sources": []}]}
        app.db.insert("style_briefs", {"id": "sb1", "set_id": "ss1", "version": 1, "brief_json": plan, "created": time.time()})
        made = app.links.make_mashup("sb1")
        t = app.db.one("SELECT * FROM tracks WHERE id = ?", (made["trackIds"][0],))
        self.assertEqual((t["is_instrumental"], t["data"]["vocalTags"]), (1, ""))
        self.assertTrue(track_is_instrumental(t))


if __name__ == "__main__":
    unittest.main()
