"""The UI stays usable while the AI is slow: page endpoints answer at once with many creates
queued, background requests are limited (one at a time on a local server) and wait their turn
without that wait counting toward the reply timeout, a request a person is waiting on gets its
own slot, artwork runs as a job, and an unloaded Lemonade image model is loaded before drawing."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine", "tests"))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from fake_lemonade import FakeLemonade  # noqa: E402
from fakes import FakeProvider  # noqa: E402
from smcore import llm, server as srv  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


class Base(unittest.TestCase):
    chat_delay = 2.0

    def setUp(self):
        from smcore.app import App
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.chat = FakeProvider(chat_delay=self.chat_delay)
        self.addCleanup(self.chat.close)
        self.app = App(home=os.path.join(self.tmp, "home"), log=lambda *a: None)
        self.addCleanup(self.app.engine.stop)
        self.app.settings.update({"onboarded": True, "provider": {
            "baseUrl": self.chat.url + "/v1", "textModel": "fake-text", "fastModel": "fake-fast", "imagesEnabled": False},
            "engine": {"kind": "builtin", "backend": "mock", "options": {"load_delay": "30"}}})
        self.server, self.token = srv.serve(self.app, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-SM-Token": self.token, "Content-Type": "application/json"})
        start = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null"), time.time() - start


class SlowModelTests(Base):
    def test_pages_answer_at_once_and_background_work_queues(self):
        for i in range(6):
            job, took = self.call("POST", "/api/create/build", {"brief": f"brief {i}", "count": 1})
            self.assertLess(took, 1.0)
        time.sleep(3)
        for path in ("/api/state", "/api/home", "/api/artists", "/api/albums", "/api/songs", "/api/jobs",
                     "/api/studio", "/api/llm/activity", "/api/settings", "/api/taste", "/api/provider/models"):
            _, took = self.call("GET", path)
            self.assertLess(took, 1.0, path)
        calls = self.app.llm_activity()
        self.assertEqual(self.chat.max_in_flight, 1)                 # local server: one at a time
        self.assertGreaterEqual(sum(c["state"] == "queued" for c in calls), 1)
        self.assertEqual(sum(c["state"] == "sent" for c in calls), 1)
        self.stop_jobs()

    def stop_jobs(self):
        for c in self.app.jobs.list():
            if c["status"] in ("queued", "running"):
                self.app.jobs.cancel(c["id"])
        self.chat.chat_delay = 0
        deadline = time.time() + 60
        while self.app.jobs.running and time.time() < deadline:
            time.sleep(0.2)
    def test_a_request_someone_waits_on_does_not_queue_behind_background_work(self):
        busy = [threading.Thread(target=lambda: self.app.provider.json("s", "background", SCHEMA)) for _ in range(3)]
        for t in busy:
            t.start()
        time.sleep(0.3)
        start = time.time()
        with llm.interactive_lane():
            self.app.provider.json("s", "interactive", SCHEMA)
        self.assertLess(time.time() - start, self.chat_delay + 1.0)   # not behind 3 × 2 s of background
        for t in busy:
            t.join()

    def test_artwork_is_a_job(self):
        self.app.db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "visual": "singer", "created": time.time()})
        row, took = self.call("POST", "/api/artists/ar1/portrait")
        self.assertLess(took, 1.0)
        self.assertEqual(row["kind"], "artwork")
        for _ in range(100):
            job, _ = self.call("GET", f"/api/jobs/{row['id']}")
            if job["status"] in ("finished", "failed"):
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "finished", job.get("error"))
        self.assertIn("source", job["result"])


class TimeoutTests(Base):
    chat_delay = 1.2

    def test_queued_time_does_not_count_toward_the_reply_timeout(self):
        self.app.settings.update({"provider": {"timeoutSeconds": 2, "maxParallel": 1}})
        errors = []

        def one():
            try:
                self.app.provider.json("s", "background", SCHEMA)
            except Exception as error:  # noqa: BLE001
                errors.append(error)
        threads = [threading.Thread(target=one) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])       # 3 × 1.2 s in a row, each within its own 2 s


class ParallelSettingTests(unittest.TestCase):
    def test_auto_and_explicit(self):
        self.assertTrue(llm.is_local_url("http://localhost:13305/v1"))
        self.assertTrue(llm.is_local_url("http://192.168.1.20:8080/v1"))
        self.assertFalse(llm.is_local_url("https://api.openai.com/v1"))

        class S:
            def __init__(self, cfg):
                self.cfg = cfg

            def section(self, _):
                return self.cfg
        p = llm.Provider(S({"baseUrl": "http://localhost:13305/v1", "maxParallel": ""}), None)
        self.assertEqual(p.parallel(), 1)
        p = llm.Provider(S({"baseUrl": "https://api.openai.com/v1", "maxParallel": ""}), None)
        self.assertEqual(p.parallel(), 3)
        p = llm.Provider(S({"baseUrl": "http://localhost:13305/v1", "maxParallel": 2}), None)
        self.assertEqual(p.parallel(), 2)

    def test_old_default_becomes_auto_once(self):
        from smcore.app import App
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        home = os.path.join(tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, "settings.json"), "w") as f:
            json.dump({"provider": {"maxParallel": 3, "unloadImageModelBeforeRecording": True}}, f)
        app = App(home=home, log=lambda *a: None)
        self.addCleanup(app.engine.stop)
        cfg = app.settings.section("provider")
        self.assertEqual(cfg["maxParallel"], "")
        self.assertTrue(cfg["unloadImageModelBeforeRecording"])     # an explicit choice is kept
        app.settings.update({"provider": {"maxParallel": 3}})          # chosen again later: kept
        app2 = App(home=home, log=lambda *a: None)
        self.addCleanup(app2.engine.stop)
        self.assertEqual(app2.settings.section("provider")["maxParallel"], 3)

    def test_unload_image_model_defaults_off(self):
        from smcore.settings import DEFAULTS
        self.assertFalse(DEFAULTS["provider"]["unloadImageModelBeforeRecording"])


class ImageModelLoadTests(unittest.TestCase):
    def test_an_unloaded_image_model_is_loaded_before_drawing(self):
        from smcore.app import App
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = FakeLemonade(catalog=[{"id": "SDXL-Turbo", "recipe": "sd-cpp", "downloaded": True, "labels": ["image"]}],
                            loaded=[])
        self.addCleanup(fake.close)
        fake.load_delay = 0.6
        app = App(home=os.path.join(tmp, "home"), log=lambda *a: None)
        self.addCleanup(app.engine.stop)
        app.settings.update({"provider": {"baseUrl": fake.url, "imagesEnabled": True, "imageModel": "SDXL-Turbo",
                                          "imageSize": ""}})
        states = []
        app.provider.on_activity = lambda calls: states.extend(c["state"] for c in calls if c["kind"] == "image")
        content, source = app.provider.image("a singer", "Zara", "portrait")
        self.assertEqual(source, "provider", app.provider.last_image_error)
        paths = [r[1] for r in fake.requests if r[0] == "POST"]
        self.assertLess(paths.index("/load"), paths.index("/images/generations"))
        self.assertIn("loading", states)
        # Loaded now: no second load.
        app.provider.image("a singer", "Zara", "portrait")
        self.assertEqual([r[1] for r in fake.requests if r[0] == "POST"].count("/load"), 1)


if __name__ == "__main__":
    unittest.main()
