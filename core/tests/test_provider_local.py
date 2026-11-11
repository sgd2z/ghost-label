"""Provider behaviour with small local servers (LM Studio-like): unsupported JSON modes, think
tags, truncated JSON, timeouts that must not hang or retry, unknown models, and status lines."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SM_SECRET_STORE"] = "file"

from smcore import jsonio  # noqa: E402
from smcore.llm import Provider, ProviderError  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Secrets, Settings  # noqa: E402

SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
          "required": ["question"]}


class LocalServer:
    """replies: list of (status, body-or-callable, delay) consumed per chat request."""

    def __init__(self, replies, models=("qwen3-4b",)):
        self.replies, self.requests, self.models = list(replies), [], list(models)
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, status, obj):
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.send(200, {"data": [{"id": m} for m in outer.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                status, content, delay = outer.replies.pop(0) if outer.replies else (200, '{"question": "fallback"}', 0)
                time.sleep(delay)
                if callable(content):
                    status, content = content(body)
                try:
                    if status != 200:
                        self.send(status, {"error": content})
                    else:
                        self.send(200, {"choices": [{"message": {"role": "assistant", "content": content}}]})
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class LocalProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(self.tmp)
        self.settings = Settings(self.paths)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def provider(self, srv, **extra):
        self.settings.update({"provider": {"baseUrl": srv.url, "textModel": "qwen3-4b", "fastModel": "qwen3-4b", **extra}})
        return Provider(self.settings, Secrets(self.paths), None, log=lambda *_: None)

    def test_lm_studio_style_json_modes_think_tags_and_truncation(self):
        lm_reject = (400, "'response_format.type' must be 'json_schema' or 'text'", 0)
        srv = LocalServer([(400, "json_schema unsupported by this model", 0), lm_reject,
                           (200, '<think>They like {rock}. Hmm</think>{"question": "Which decade?", "options": ["80s", "90s", "20', 0)])
        try:
            status = []
            value = self.provider(srv).json("sys", "user", SCHEMA, status=status.append)
            self.assertEqual(value["question"], "Which decade?")
            self.assertEqual(value["options"], ["80s", "90s", "20"])
            self.assertEqual([r.get("response_format", {}).get("type") for r in srv.requests], ["json_schema", "json_object", None])
            self.assertTrue(any("not accepted (400" in s and "json_schema" in s for s in status), status)
        finally:
            srv.close()

    def test_timeout_is_reported_quickly_and_not_retried(self):
        srv = LocalServer([(200, '{"question": "late"}', 4)])
        try:
            p = self.provider(srv, jsonMode="none")
            start = time.time()
            with self.assertRaises(ProviderError) as ctx:
                p.json("sys", "user", SCHEMA, timeout=1)
            self.assertLess(time.time() - start, 3)
            self.assertIn("No reply", str(ctx.exception))
            self.assertIn("qwen3-4b", str(ctx.exception))
            self.assertEqual(len(srv.requests), 1)
        finally:
            srv.close()

    def test_unknown_model_error_is_shown(self):
        srv = LocalServer([(404, "Model 'qwen3-4b' not found. Load it in LM Studio.", 0)])
        try:
            with self.assertRaises(ProviderError) as ctx:
                self.provider(srv).json("sys", "user", SCHEMA)
            self.assertIn("not found", str(ctx.exception))
            self.assertEqual(len(srv.requests), 1)
        finally:
            srv.close()

    def test_garbage_twice_shows_the_reply(self):
        srv = LocalServer([(200, "I think you like rock music!", 0), (200, "Sorry, here you go: rock", 0)])
        try:
            with self.assertRaises(ProviderError) as ctx:
                self.provider(srv, jsonMode="none").json("sys", "user", SCHEMA)
            self.assertIn("It replied", str(ctx.exception))
            self.assertIn("rock", str(ctx.exception))
        finally:
            srv.close()

    def test_connection_test_flags_a_model_name_not_on_the_server(self):
        srv = LocalServer([(200, '{"ok": true}', 0)], models=("qwen/qwen3-4b",))
        try:
            r = self.provider(srv).test()
            self.assertIn("qwen/qwen3-4b", r.get("modelWarning", ""))
        finally:
            srv.close()


class QuestionShapeTests(unittest.TestCase):
    def test_question_without_done_is_still_asked(self):
        from smcore.taste import Taste

        class P:
            def json(self, *a, **k):
                return {"question": "What tempo?", "options": [{"label": "Slow"}, {"label": "Fast"}]}

        class R:
            def system(self, _):
                return "sys"

        t = Taste(None, None, P(), R(), None, None)
        q = t.next_question([{"name": "x", "reaction": "like"}], [])
        self.assertFalse(q["done"])
        self.assertEqual(q["question"], "What tempo?")

        P.json = lambda self, *a, **k: {}
        self.assertTrue(t.next_question([], [])["done"])


class QuestionCheckTests(unittest.TestCase):
    def test_mechanical_checks(self):
        from smcore.taste import question_problems
        reactions = [{"name": "Punjabi Songs", "reaction": "love"}, {"name": "Pocahontas", "reaction": "like"}]
        ok = {"question": "Do you like calm or lively music?", "options": [{"label": "Calm"}, {"label": "Lively"}]}
        self.assertEqual(question_problems(ok, reactions), [])
        self.assertEqual(question_problems({"done": True}, reactions), [])
        copied = {"question": "Big hooks or slow vocals?", "options": [{"label": "punjabi songs"}, {"label": "Pocahontas"}]}
        self.assertTrue(any("reactions list" in p for p in question_problems(copied, reactions)))
        self.assertTrue(question_problems({"question": "x?", "options": [{"label": "Only one"}]}, reactions))
        self.assertTrue(question_problems({"question": "x?", "options": [{"label": "Same"}, {"label": "same"}]}, reactions))
        self.assertTrue(question_problems({"question": "x?", "options": [{"label": "a" * 60}, {"label": "b"}]}, reactions))
        self.assertTrue(question_problems({"question": "q" * 200, "options": [{"label": "a"}, {"label": "b"}]}, reactions))

    def test_copied_names_are_sent_back_once_then_fixed(self):
        bad = json.dumps({"question": "Big hooks or slow vocals?", "options": [{"label": "Punjabi Songs"}, {"label": "Pocahontas"}]})
        good = json.dumps({"question": "Do you like upbeat songs or slow ones?",
                           "options": [{"label": "Upbeat", "detail": "catchy, to sing along"}, "Slow"], "either": "Both"})
        srv = LocalServer([(200, bad, 0), (200, good, 0)])
        try:
            tmp = tempfile.mkdtemp()
            paths = Paths(tmp)
            settings = Settings(paths)
            settings.update({"provider": {"baseUrl": srv.url, "textModel": "m", "fastModel": "m"}})
            from smcore.rules import Rules
            from smcore.taste import Taste
            t = Taste(None, settings, Provider(settings, Secrets(paths), None, log=lambda *_: None), Rules(paths), None, None)
            q = t.next_question([{"name": "Punjabi Songs", "reaction": "love"}, {"name": "Pocahontas", "reaction": "like"}], [])
            self.assertEqual([o["label"] for o in q["options"]], ["Upbeat", "Slow"])
            self.assertEqual(q["either"], "Both")
            self.assertIn("reactions list", srv.requests[1]["messages"][-1]["content"])
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            srv.close()


class ActivityAndStageTests(unittest.TestCase):
    def test_in_flight_calls_are_reported(self):
        seen = []
        srv = LocalServer([(200, '{"a": 1}', 0.3)])
        try:
            tmp = tempfile.mkdtemp()
            paths = Paths(tmp)
            settings = Settings(paths)
            settings.update({"provider": {"baseUrl": srv.url, "textModel": "qwen3-4b", "jsonMode": "none"}})
            p = Provider(settings, Secrets(paths), None, log=lambda *_: None)
            p.on_activity = lambda calls: seen.append([(c["model"], c["state"]) for c in calls])
            p.json("sys", "user", {"type": "object", "properties": {"a": {"type": "integer"}}})
            self.assertIn([("qwen3-4b", "sent")], seen)
            self.assertEqual(seen[-1], [])
            self.assertEqual(p.activity(), [])
            shutil.rmtree(tmp, ignore_errors=True)
        finally:
            srv.close()

    def test_job_stage_and_counts_survive_to_the_final_event(self):
        from smcore.db import Database
        from smcore.jobs import JobManager

        class Ev:
            def __init__(self):
                self.jobs = []

            def emit(self, kind, **data):
                if kind == "job":
                    self.jobs.append(data["job"])

        tmp = tempfile.mkdtemp()
        try:
            ev = Ev()
            jm = JobManager(Database(os.path.join(tmp, "db.sqlite")), ev, log=lambda *_: None)

            def work(job):
                job.stage("roster")
                job.count(artistsTotal=2, artistsPlanned=2)
                job.stage("artists")
                job.count(albumsWritten=1)
                job.count(albumsWritten=1)
                return {"ok": True}

            row = jm.start("build", "t", work)
            for _ in range(100):
                if ev.jobs and ev.jobs[-1]["status"] == "finished":
                    break
                time.sleep(0.05)
            last = ev.jobs[-1]
            self.assertEqual(last["status"], "finished")
            self.assertEqual(last["flags"]["counts"], {"artistsTotal": 2, "artistsPlanned": 2, "albumsWritten": 2})
            self.assertEqual(last["flags"]["stage"], "artists")
            self.assertTrue(row["id"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class JsonRepairTests(unittest.TestCase):
    def test_truncations(self):
        self.assertEqual(jsonio.parse('{"question": "Which", "opt'), {"question": "Which"})
        self.assertEqual(jsonio.parse('{"q": "x", "options": ["a"], "multiple": tr'), {"q": "x", "options": ["a"]})
        self.assertEqual(jsonio.parse("<think>unclosed {thoughts} ... {\"a\": 1}"), {"a": 1})
        self.assertEqual(jsonio.parse("Sure! {\"a\": 1} hope that helps"), {"a": 1})


if __name__ == "__main__":
    unittest.main()
