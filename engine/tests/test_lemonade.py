"""The Lemonade backend against a fake Lemonade Server (tests/fake_lemonade.py), through engine.py
exactly as the app runs it."""

import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_engine_host import CAN_ENCODE, FFPROBE, ROOT, EngineProcess, job, probe  # noqa: E402
from fake_lemonade import FakeLemonade, realistic, wav24  # noqa: E402

from engine_host.backends import lemonade  # noqa: E402


class DecodeTests(unittest.TestCase):
    def test_24_bit_wav(self):
        x, sr = lemonade.decode_wav(wav24(0.5, 44100, 2))
        self.assertEqual(sr, 44100)
        self.assertEqual(x.shape, (22050, 2))
        self.assertAlmostEqual(float(np.max(np.abs(x))), 0.5, delta=0.01)

    def test_not_a_wav(self):
        with self.assertRaises(lemonade.LemonadeError):
            lemonade.decode_wav(b'{"error": {}}')

    def test_api_base(self):
        self.assertEqual(lemonade.api_base("http://localhost:13305"), "http://localhost:13305/v1")
        self.assertEqual(lemonade.api_base("http://localhost:13305/api/v1/"), "http://localhost:13305/api/v1")
        self.assertEqual(lemonade.api_base(""), lemonade.DEFAULT_URL)

    def test_errors_say_what_to_do(self):
        self.assertIn("lemonade pull ACE-Step-Music",
                      lemonade.explain(404, {"error": {"type": "model_not_found"}}, "ACE-Step-Music", "u"))
        self.assertIn("couldn't load the music model",
                      lemonade.explain(500, {"error": {"type": "model_load_error", "message": "x"}}, "ACE-Step-Music", "u"))
        self.assertIn("GPU memory", lemonade.explain(502, {"error": {"message": "no output"}}, "m", "u"))


@unittest.skipUnless(CAN_ENCODE, "no ffmpeg")
class LemonadeEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake = FakeLemonade()
        self.engine = None

    def tearDown(self):
        if self.engine:
            self.engine.close()
        self.fake.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def start(self, **env):
        self.engine = EngineProcess(["--backend", "lemonade"], env=dict({"LEMONADE_URL": self.fake.url}, **env))
        self.engine.send({"load": {"modelPath": ROOT}})
        return self.engine.read_until("ready", timeout=30)

    def test_song_round_trip(self):
        self.fake.delay = 1.5
        ready = self.start()
        caps = ready["ready"]["capabilities"]
        self.assertFalse(caps["referenceAudio"])
        self.assertFalse(caps["analyze"])
        self.assertIn("hi", caps["languages"])
        self.assertEqual(self.fake.posts("/audio/generations"), [])   # checking doesn't load the model
        out = os.path.join(self.tmp, "song.m4a")
        self.engine.send(job(out, vocalLanguage="hi", referenceAudio="/x.wav", bpm=96, keyscale="E minor"))
        done = self.engine.read_until("finished", timeout=60)["finished"]["_0"]
        self.assertAlmostEqual(done["duration"], 2.0, delta=0.1)
        self.assertTrue(os.path.exists(out))
        if FFPROBE:
            self.assertEqual(probe(out)["streams"][0]["codec_name"], "aac")
        body = self.fake.posts("/audio/generations")[0]
        self.assertEqual(body["model"], "ACE-Step-Music")
        self.assertEqual(body["prompt"], "English, indie rock, female vocal, warm alto")
        self.assertTrue(body["lyrics"].startswith("[Verse]"))
        self.assertEqual(body["vocal_language"], "hi")
        self.assertEqual((body["duration"], body["seed"], body["steps"], body["response_format"]), (20, 7, 48, "wav"))
        self.assertNotIn("reference_audio", body)
        progress = [m["progress"]["_1"] for m in self.engine.lines if "progress" in m]
        waiting = [p for p in progress if p["stage"] == "composing"]
        self.assertTrue(waiting and all(p.get("estimated") for p in waiting))   # a timer, not a percentage
        self.assertIn("Lemonade", waiting[0]["detail"])

    def test_instrumental(self):
        self.start()
        self.engine.send(job(os.path.join(self.tmp, "i.m4a"), isInstrumental=True))
        self.engine.read_until("finished", timeout=60)
        self.assertEqual(self.fake.posts("/audio/generations")[0]["lyrics"], "[Instrumental]")

    def test_missing_model_fails_load_with_the_pull_command(self):
        self.fake.model = "something-else"
        self.engine = EngineProcess(["--backend", "lemonade"], env={"LEMONADE_URL": self.fake.url})
        self.engine.send({"load": {"modelPath": ""}})
        failed = self.engine.read_until("failed", timeout=30)["failed"]
        self.assertIn("lemonade pull ACE-Step-Music", failed["message"])

    def test_not_downloaded(self):
        self.fake.downloaded = False
        self.engine = EngineProcess(["--backend", "lemonade"], env={"LEMONADE_URL": self.fake.url})
        self.engine.send({"load": {"modelPath": ""}})
        self.assertIn("hasn't downloaded", self.engine.read_until("failed", timeout=30)["failed"]["message"])

    def test_loaded_model_is_used_even_if_its_row_says_not_downloaded(self):
        catalog, loaded = realistic()
        for m in catalog:
            if m["id"] == "ACE-Step-Music":
                m["downloaded"] = False
        self.fake.close()
        self.fake = FakeLemonade(catalog=catalog, loaded=loaded)
        self.engine = EngineProcess(["--backend", "lemonade"], env={"LEMONADE_URL": self.fake.url})
        self.engine.send({"load": {"modelPath": ""}})
        self.assertIn("lemonade pull ACE-Step-Music", self.engine.read_until("failed", timeout=30)["failed"]["message"])
        self.fake.loaded.append({"model_name": "ACE-Step-Music", "type": "audio-generation"})
        self.engine.send({"load": {"modelPath": ""}})
        self.engine.read_until("ready", timeout=30)

    def test_server_down(self):
        url = self.fake.url
        self.fake.close()
        self.fake = FakeLemonade()
        self.engine = EngineProcess(["--backend", "lemonade"], env={"LEMONADE_URL": url})
        self.engine.send({"load": {"modelPath": ""}})
        self.assertIn("isn't answering", self.engine.read_until("failed", timeout=30)["failed"]["message"])

    def test_load_error_and_out_of_memory_are_surfaced_and_engine_survives(self):
        self.start()
        self.fake.reply = (500, {"error": {"message": "ace-server exited", "type": "model_load_error"}})
        self.engine.send(job(os.path.join(self.tmp, "a.m4a"), jid="a"))
        msg = self.engine.read_until("failed", timeout=30)["failed"]
        self.assertEqual(msg["id"], "a")
        self.assertIn("couldn't load the music model ACE-Step-Music", msg["message"])
        self.fake.reply = (502, {"error": {"message": "no output", "type": "backend_error"}})
        self.engine.send(job(os.path.join(self.tmp, "b.m4a"), jid="b"))
        self.assertIn("GPU memory", self.engine.read_until("failed", timeout=30)["failed"]["message"])
        self.fake.reply = None
        self.engine.send(job(os.path.join(self.tmp, "c.m4a"), jid="c"))
        self.engine.read_until("finished", timeout=60)

    def test_api_key_is_sent(self):
        self.fake.key = "sekret"
        self.start(LEMONADE_API_KEY="sekret")
        self.engine.send(job(os.path.join(self.tmp, "k.m4a")))
        self.engine.read_until("finished", timeout=60)

    def test_cancel_abandons_the_request(self):
        self.fake.delay = 5
        self.start()
        self.engine.send(job(os.path.join(self.tmp, "x.m4a"), jid="x"))
        self.engine.read_until("progress", timeout=10)
        self.engine.send({"cancel": {"id": "x"}})
        self.assertEqual(self.engine.read_until("cancelled", timeout=5)["cancelled"]["id"], "x")


if __name__ == "__main__":
    unittest.main()
