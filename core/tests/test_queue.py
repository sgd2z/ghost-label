"""The global queue: AI requests grouped by the resource they wait on (chat model, image model,
music engine, background tasks), numbered in the order that resource takes them, tagged with
the page items they belong to, cancellable while queued, and remembered for a while after."""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_responsiveness import SCHEMA, Base  # noqa: E402
from smcore.queue import ordinal, owner  # noqa: E402


class QueueTests(Base):
    chat_delay = 1.5

    def wait_for(self, check, seconds=10):
        deadline = time.time() + seconds
        while time.time() < deadline:
            out = check()
            if out:
                return out
            time.sleep(0.05)
        self.fail("timed out")

    def items(self, resource_prefix=None):
        snap = self.app.queue.snapshot()
        return [i for r in snap["resources"] for i in r["items"]
                if not resource_prefix or r["id"].startswith(resource_prefix)]

    def test_chat_requests_are_numbered_in_order_with_their_owner(self):
        def ask(label, album):
            with owner(kind="lyrics", label=label, albumId=album):
                try:
                    self.app.provider.json("s", label, SCHEMA)
                except Exception:  # noqa: BLE001
                    pass
        threads = []
        for n in range(3):
            t = threading.Thread(target=ask, args=(f"Lyrics {n}", f"al{n}"))
            t.start()
            threads.append(t)
            time.sleep(0.15)
        chat = self.wait_for(lambda: (lambda xs: xs if len(xs) == 3 else None)(self.items("chat:")))
        self.assertEqual([i["state"] for i in chat], ["running", "queued", "queued"])
        self.assertEqual([i["label"] for i in chat], ["Lyrics 0", "Lyrics 1", "Lyrics 2"])
        self.assertEqual([i.get("position") for i in chat], [0, 1, 2])
        self.assertEqual(chat[2]["positionText"], "2nd")
        self.assertEqual(chat[1]["refs"], {"albumId": "al1"})
        self.assertTrue(chat[0]["resourceLabel"].startswith("Chat · fake-text"))
        for t in threads:
            t.join()
        recent = self.app.queue.snapshot()["recent"]
        self.assertTrue(any(r["label"] == "Lyrics 2" and r["state"] == "done" for r in recent))

    def test_cancelling_a_queued_request_cancels_its_job_and_frees_the_slot(self):
        self.app.db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "visual": "singer", "created": time.time()})
        self.app.settings.update({"provider": {"imagesEnabled": True, "imageModel": "img"}})
        blocker = threading.Thread(target=lambda: self.app.provider.json("s", "busy", SCHEMA))
        blocker.start()
        time.sleep(0.2)
        job = self.app.jobs.start("artwork", "New photo: Zara Kaur", lambda j: self.app.builder.portrait("ar1"),
                                  {"artistId": "ar1"})
        queued = self.wait_for(lambda: [i for i in self.items("chat:") if i["state"] == "queued"
                                        and i["refs"].get("artistId") == "ar1"])
        item = queued[0]
        self.assertEqual(item["label"], "Photo of Zara Kaur · photo prompt")
        self.assertEqual(item["refs"]["jobId"], job["id"])
        self.assertEqual(item["cancel"]["what"], "job")
        self.assertTrue(self.app.queue.cancel(item["id"])["ok"])
        row = self.wait_for(lambda: (lambda r: r if r["status"] in ("cancelled", "failed", "finished") else None)(
            self.app.jobs.row(job["id"])))
        self.assertEqual(row["status"], "cancelled")
        self.assertIsNone(self.app.db.one("SELECT portrait_path FROM artists WHERE id = 'ar1'")["portrait_path"])
        blocker.join()
        self.wait_for(lambda: not self.app.jobs.running)
        recent = self.app.queue.snapshot()["recent"]
        self.assertTrue(any(r["id"] == "job:" + job["id"] and r["state"] == "cancelled" for r in recent))

    def test_jobs_and_recordings_have_their_own_queues(self):
        self.app.db.insert("artists", {"id": "ar1", "name": "A", "created": time.time()})
        self.app.db.insert("albums", {"id": "al1", "artist_id": "ar1", "title": "Mirrors & Motion", "kind": "album",
                                      "position": 0, "created": time.time()})
        for n in (1, 2, 3):
            self.app.db.insert("tracks", {"id": f"tr{n}", "album_id": "al1", "artist_id": "ar1", "number": n,
                                          "title": f"Song {n}", "lyrics": "la", "style": "pop", "status": "written",
                                          "created": time.time()})
        self.app.studio.request(["tr1", "tr2", "tr3"])
        rec = self.wait_for(lambda: (lambda xs: xs if len(xs) == 3 and xs[0]["state"] == "running" else None)(
            [i for i in self.items() if i["resource"] == "engine"]))
        self.assertEqual([i["refs"]["trackId"] for i in rec], ["tr1", "tr2", "tr3"])
        self.assertEqual([i.get("position") for i in rec], [0, 1, 2])
        self.assertEqual(rec[1]["refs"]["albumId"], "al1")
        self.assertTrue(rec[0]["resourceLabel"].startswith("Music engine"))
        self.app.queue.cancel("rec:tr3")
        rec = [i for i in self.items() if i["resource"] == "engine"]
        self.assertNotIn("tr3", [i["refs"]["trackId"] for i in rec])
        self.app.studio.cancel("tr1")
        self.app.studio.cancel("tr2")

    def test_queued_job_waits_for_a_worker_and_can_be_cancelled_before_it_starts(self):
        gate = threading.Event()
        started = []
        jobs = [self.app.jobs.start("test", f"Task {n}", lambda j, n=n: (started.append(n), gate.wait(10)))
                for n in range(6)]
        tasks = self.wait_for(lambda: (lambda xs: xs if sum(i["state"] == "running" for i in xs) == 4 else None)(
            [i for i in self.items() if i["resource"] == "tasks"]))
        queued = [i for i in tasks if i["state"] == "queued"]
        self.assertEqual([i["label"] for i in queued], ["Task 4", "Task 5"])
        self.assertEqual([i["position"] for i in queued], [1, 2])
        self.app.queue.cancel(queued[1]["id"])
        gate.set()
        self.wait_for(lambda: not self.app.jobs.running)
        self.assertNotIn(5, started)
        self.assertEqual(self.app.jobs.row(jobs[5]["id"])["status"], "cancelled")

    def test_route_and_event(self):
        got = []
        q = self.app.events.subscribe()
        threading.Thread(target=lambda: self.app.provider.json("s", "x", SCHEMA)).start()
        snap, took = self.call("GET", "/api/queue")
        self.assertLess(took, 1.0)
        self.assertIn("resources", snap)
        deadline = time.time() + 5
        while time.time() < deadline and not got:
            try:
                message = q.get(timeout=0.5)
            except Exception:  # noqa: BLE001
                continue
            if '"type": "queue"' in message:
                got.append(message)
        self.assertTrue(got)
        self.app.events.unsubscribe(q)

    def test_ordinal(self):
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 101)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd", "101st"])


if __name__ == "__main__":
    unittest.main()
