"""Record again / New photo / New cover with the listener's feedback: the feedback is kept with the
song or image, the AI revises the ORIGINAL values with all feedback so far, a song's revision only
becomes its own when the new take saves, and an empty answer keeps today's behaviour."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from smcore.studio import Studio  # noqa: E402
from test_instrumental import BuilderStub, Engine  # noqa: E402
from test_studio_retry import Events, Library, Paths, Settings  # noqa: E402


class AppBase(unittest.TestCase):
    def setUp(self):
        from smcore.app import App
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = App(home=os.path.join(self.tmp, "home"), log=lambda *a: None)
        self.addCleanup(self.app.engine.stop)
        now = time.time()
        db = self.app.db
        db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "voice": "warm alto", "visual": "a singer in her 20s",
                              "portrait_prompt": "press photo of Zara Kaur, Punjabi woman in her 20s, denim jacket",
                              "data_json": {"vocalTags": "female vocals, warm alto"}, "created": now})
        db.insert("albums", {"id": "al1", "artist_id": "ar1", "title": "Mirrors & Motion", "kind": "album",
                             "cover_prompt": "two mirrors on a dark stage, text reading \"Mirrors & Motion\"", "created": now})
        db.insert("tracks", {"id": "tr1", "album_id": "al1", "artist_id": "ar1", "number": 1, "title": "Glass",
                             "style": "dream pop, airy synths", "lyrics": "[Verse]\nglass on glass", "bpm": 92,
                             "keyscale": "A minor", "status": "recorded", "created": now})
        self.asked = []

    def answer(self, reply):
        def ask(rule, task, data, schema, name, fast=False):
            self.asked.append((rule, data))
            return reply(rule, data) if callable(reply) else reply
        self.app.builder.ask = ask


class SongRetakeTests(AppBase):
    def test_feedback_is_kept_and_the_original_is_revised_with_all_of_it(self):
        self.answer({"style": "dream pop, airy synths, slower, softer drums", "lyrics": "[Verse]\nglass on glass",
                     "bpm": 80, "key": "A minor", "vocal_tags": "female vocals, warm alto", "changed": "slower, softer drums"})
        pending = self.app.builder.retake("tr1", "slower, softer drums")
        self.assertEqual(pending["bpm"], 80)
        rule, data = self.asked[-1]
        self.assertEqual(rule, "retake")
        self.assertEqual(data["originalPrompt"]["style"], "dream pop, airy synths")
        self.assertEqual(data["feedback"], ["slower, softer drums"])
        t = self.app.db.one("SELECT * FROM tracks WHERE id = 'tr1'")
        self.assertEqual((t["style"], t["bpm"]), ("dream pop, airy synths", 92))     # old take's values stay for now
        # A second note: the original is still the first one, both notes are sent.
        self.app.db.update("tracks", "tr1", {"style": "dream pop, airy synths, slower"})
        self.app.builder.retake("tr1", "now a male voice")
        _, data = self.asked[-1]
        self.assertEqual(data["originalPrompt"]["style"], "dream pop, airy synths")
        self.assertEqual(data["feedback"], ["slower, softer drums", "now a male voice"])
        self.assertEqual(data["latestFeedback"], "now a male voice")
        self.assertEqual(self.app.library.track("tr1")["feedback"], ["slower, softer drums", "now a male voice"])
        self.assertIn("original lyrics unchanged unless the feedback asks", self.app.rules.get("retake"))

    def test_feedback_is_capped(self):
        self.answer({"style": "s", "lyrics": "l"})
        for n in range(20):
            self.app.builder.retake("tr1", f"note {n} " + "x" * 900)
        fb = self.app.db.one("SELECT data_json FROM tracks WHERE id = 'tr1'")["data"]["retake"]["feedback"]
        self.assertEqual(len(fb), 12)
        self.assertTrue(all(len(f["text"]) <= 500 for f in fb))

    def test_route_empty_feedback_records_at_once_and_feedback_is_a_job(self):
        from smcore import server
        handler = next(fn for m, rx, fn in server.ROUTES if m == "POST" and rx.pattern.endswith("/rerecord$"))
        calls = []
        self.app.studio.rerecord = lambda tid: calls.append(tid) or {"ok": True}
        self.assertEqual(handler(self.app, {}, {}, "tr1"), {"ok": True})
        self.answer({"style": "s2", "lyrics": "[Verse]\nglass on glass"})
        job = handler(self.app, {"feedback": "brighter mix"}, {}, "tr1")
        self.assertEqual(job["kind"], "retake")
        end = time.time() + 10
        while self.app.jobs.running and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(calls, ["tr1", "tr1"])
        self.assertEqual(self.app.db.one("SELECT data_json FROM tracks WHERE id='tr1'")["data"]["retake"]["pending"]["style"], "s2")


class StudioTakeTests(unittest.TestCase):
    def setUp(self):
        from smcore.db import Database
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(os.path.join(self.tmp, "lib.db"))
        now = time.time()
        self.db.insert("artists", {"id": "ar1", "name": "Zara", "voice": "alto", "created": now})
        self.db.insert("albums", {"id": "al1", "artist_id": "ar1", "title": "A", "created": now})
        pending = {"style": "dream pop, slower", "lyrics": "[Verse]\nnew line", "bpm": 80, "key": None,
                   "vocalTags": "male vocals, baritone", "changed": "slower, male voice"}
        self.db.insert("tracks", {"id": "tr1", "album_id": "al1", "artist_id": "ar1", "number": 1, "title": "Glass",
                                  "style": "dream pop", "lyrics": "[Verse]\nold line", "bpm": 92, "keyscale": "A minor",
                                  "status": "written", "created": now,
                                  "data_json": {"vocalTags": "female vocals", "retake": {
                                      "original": {}, "feedback": [{"text": "slower, male voice"}], "pending": pending}}})

    def run_take(self, engine):
        studio = Studio(self.db, Paths(self.tmp), Settings(), engine, BuilderStub(), Library(), Events(), log=lambda *_: None)
        studio.request(["tr1"])
        end = time.time() + 10
        while time.time() < end:
            data = self.db.one("SELECT data_json FROM tracks WHERE id='tr1'")["data"]
            if "pending" not in data["retake"]:
                return data
            time.sleep(0.05)
        self.fail("the take never ended")

    def test_the_take_records_the_revision_and_keeps_it_once_saved(self):
        engine = Engine()
        data = self.run_take(engine)
        job = engine.jobs[0]
        self.assertEqual((job["lyrics"], job["bpm"], job["keyscale"]), ("[Verse]\nnew line", 80, "A minor"))
        self.assertIn("male vocals, baritone", job["style"])
        t = self.db.one("SELECT * FROM tracks WHERE id='tr1'")
        self.assertEqual((t["style"], t["lyrics"], t["bpm"], t["keyscale"]), ("dream pop, slower", "[Verse]\nnew line", 80, "A minor"))
        self.assertEqual(data["vocalTags"], "male vocals, baritone")
        self.assertEqual(data["retake"]["feedback"][-1]["outcome"], "recorded")

    def test_a_failed_take_keeps_the_old_values(self):
        class Broken(Engine):
            def generate(self, job, on_progress=None, on_loading=None):
                raise RuntimeError("out of memory")
        data = self.run_take(Broken())
        t = self.db.one("SELECT * FROM tracks WHERE id='tr1'")
        self.assertEqual((t["style"], t["lyrics"], t["bpm"]), ("dream pop", "[Verse]\nold line", 92))
        self.assertEqual(data["vocalTags"], "female vocals")
        self.assertEqual(data["retake"]["feedback"][-1]["outcome"], "failed")


class ImageFeedbackTests(AppBase):
    def setUp(self):
        super().setUp()
        self.app.settings.update({"provider": {"imagesEnabled": True, "imageModel": "img", "baseUrl": "http://127.0.0.1:9/v1"}})
        self.drawn = []
        self.app.provider.image = lambda prompt, seed, style, negative="": (self.drawn.append(prompt) or (b"\x89PNG", "provider"))

    def test_photo_feedback_revises_the_original_prompt_and_accumulates(self):
        self.answer(lambda rule, data: {"prompt": "revised: " + "; ".join(data.get("feedback") or [])})
        self.app.builder.portrait("ar1", "outdoors, golden hour")
        _, data = self.asked[-1]
        self.assertEqual(data["previousPrompt"], "press photo of Zara Kaur, Punjabi woman in her 20s, denim jacket")
        self.assertEqual(data["feedback"], ["outdoors, golden hour"])
        self.app.builder.portrait("ar1", "smiling")
        _, data = self.asked[-1]
        self.assertEqual(data["previousPrompt"], "press photo of Zara Kaur, Punjabi woman in her 20s, denim jacket")
        self.assertEqual(data["feedback"], ["outdoors, golden hour", "smiling"])
        self.assertEqual([f["text"] for f in self.app.library.artist("ar1")["imageFeedback"]], ["outdoors, golden hour", "smiling"])
        self.assertIn("previousPrompt", self.app.rules.get("artwork"))

    def test_no_feedback_is_a_fresh_prompt(self):
        self.answer({"prompt": "fresh"})
        self.app.builder.portrait("ar1")
        _, data = self.asked[-1]
        self.assertNotIn("previousPrompt", data)
        self.assertNotIn("imageFeedback", self.app.db.one("SELECT data_json FROM artists WHERE id='ar1'")["data"])

    def test_cover_fallback_appends_the_feedback_to_the_original(self):
        def fail(*a, **k):
            raise RuntimeError("no reply")
        self.app.builder.ask = fail
        self.app.builder.cover("al1", "make it blue")
        self.assertEqual(self.drawn[-1], 'two mirrors on a dark stage, text reading "Mirrors & Motion". make it blue')


if __name__ == "__main__":
    unittest.main()
