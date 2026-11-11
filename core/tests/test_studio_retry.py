"""Studio retry rules: an engine failure doesn't count against a song or skip to the next one,
play/Retry starts the pressed song again, and failures from an earlier run don't stick."""

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
from smcore.engine_client import EngineError  # noqa: E402
from smcore.studio import Studio  # noqa: E402


class Events:
    def emit(self, *a, **k):
        pass


class Settings:
    def section(self, name):
        return {"prefetch": False} if name == "studio" else {}


class Library:
    def track(self, t):
        return {"id": t}

    def tracks(self, ids):
        return [{"id": t} for t in ids]


class Engine:
    """Fails like a broken engine (state 'failed') until fixed; records what it was asked for."""

    def __init__(self):
        self.state = "stopped"
        self.broken = True
        self.asked = []
        self.lock = threading.Lock()

    def status(self):
        return {"state": self.state}

    def ensure_ready(self, on_loading=None):
        if self.broken:
            self.state = "failed"
            raise EngineError("The engine stopped responding while loading the model")
        self.state = "ready"
        return {}

    def generate(self, job, on_progress=None, on_loading=None):
        self.asked.append(job["title"])
        return {"duration": 1.0, "loudness": -14}


class Builder:
    engine_languages = []

    class provider:
        @staticmethod
        def configured():
            return False


class Paths:
    def __init__(self, root):
        self.audio = os.path.join(root, "Audio")

    def rel(self, p):
        return p


class StudioRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(os.path.join(self.tmp, "lib.db"))
        now = time.time()
        self.db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "created": now})
        self.db.insert("albums", {"id": "al1", "artist_id": "ar1", "title": "A", "created": now})
        for n, title in enumerate(["Khoobsurat", "Sapne", "Galiyaan"], 1):
            self.db.insert("tracks", {"id": f"t{n}", "album_id": "al1", "artist_id": "ar1", "number": n,
                                      "title": title, "lyrics": "la", "status": "written", "created": now})
        self.db.update("tracks", "t3", {"status": "failed", "error": "The engine stopped responding."})

    def wait(self, cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_engine_failure_waits_for_play_and_records_track_one_first(self):
        engine = Engine()
        studio = Studio(self.db, Paths(self.tmp), Settings(), engine, Builder(), Library(), Events(), log=lambda *_: None)
        # The earlier run's failure didn't stick.
        self.assertEqual(self.db.one("SELECT status FROM tracks WHERE id='t3'")["status"], "written")
        studio.need(["t1", "t2", "t3"])
        self.assertTrue(self.wait(lambda: studio.blocked))
        time.sleep(0.5)
        # Blocked on the engine: it didn't move on to Sapne, and Khoobsurat wasn't counted as failing.
        self.assertEqual(engine.asked, [])
        self.assertNotIn("t1", studio.attempts)
        self.assertIn("stopped responding", studio.status()["blocked"])
        engine.broken = False
        studio.need(["t1", "t2", "t3"])  # play pressed again
        self.assertTrue(self.wait(lambda: len(engine.asked) >= 1))
        self.assertEqual(engine.asked[0], "Khoobsurat")

    def test_retry_unblocks_a_failed_song(self):
        engine = Engine()
        studio = Studio(self.db, Paths(self.tmp), Settings(), engine, Builder(), Library(), Events(), log=lambda *_: None)
        studio.request(["t3"])
        self.assertTrue(self.wait(lambda: studio.blocked))
        engine.broken = False
        studio.request(["t3"])  # Retry
        self.assertTrue(self.wait(lambda: engine.asked == ["Galiyaan"]))


if __name__ == "__main__":
    unittest.main()
