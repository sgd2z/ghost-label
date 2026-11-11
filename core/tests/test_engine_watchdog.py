"""Core's side of engine liveness: loading heartbeats, the silence watchdog, exits shown as errors."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore.engine_client import EngineClient, EngineError  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Settings  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Events:
    def __init__(self):
        self.seen = []

    def emit(self, kind, **data):
        self.seen.append((kind, data))


SCRIPTS = {
    # Two loading heartbeats, then silence (a native hang).
    "hang": """
        import json, sys, time
        sys.stdin.readline()
        for i in range(2):
            print(json.dumps({"loading": {"stage": "import acestep.handler", "elapsed": i}}), flush=True)
            time.sleep(0.2)
        time.sleep(60)
    """,
    # Dies while loading, after saying why on stderr.
    "die": """
        import json, sys, time
        sys.stdin.readline()
        print(json.dumps({"loading": {"stage": "Loading the music model", "elapsed": 0}}), flush=True)
        print("RuntimeError: HIP out of memory. Tried to allocate 2.00 GiB", file=sys.stderr, flush=True)
        sys.exit(3)
    """,
    # Loads with heartbeats and answers ready.
    "ok": """
        import json, sys, time
        sys.stdin.readline()
        for i in range(3):
            print(json.dumps({"loading": {"stage": "step %d" % i, "elapsed": i}}), flush=True)
            time.sleep(0.1)
        print(json.dumps({"ready": {"loadSeconds": 1, "capabilities": {"maxSeconds": 60}}}), flush=True)
        sys.stdin.readline()
    """,
}


class WatchdogTests(unittest.TestCase):
    def client(self, name):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        script = os.path.join(tmp, name + ".py")
        with open(script, "w") as f:
            f.write(textwrap.dedent(SCRIPTS[name]))
        paths = Paths(os.path.join(tmp, "GhostLabel"))
        settings = Settings(paths)
        settings.update({"engine": {"kind": "custom", "customCommand": script, "python": sys.executable}})
        self.events = Events()
        c = EngineClient(settings, paths, os.path.join(REPO, "engine"), self.events, log=lambda *_: None)
        c.LOAD_SILENCE = 2
        self.addCleanup(c.stop_quietly)
        return c

    def test_silence_after_heartbeats_is_an_error_naming_the_step(self):
        c = self.client("hang")
        start = time.time()
        with self.assertRaises(EngineError) as ctx:
            c.ensure_ready(timeout=60)
        self.assertLess(time.time() - start, 15)
        self.assertIn("stopped responding while loading", str(ctx.exception))
        self.assertIn("import acestep.handler", str(ctx.exception))
        self.assertEqual(c.state, "failed")

    def test_exit_while_loading_shows_its_last_message(self):
        c = self.client("die")
        with self.assertRaises(EngineError) as ctx:
            c.ensure_ready(timeout=60)
        text = str(ctx.exception)
        self.assertIn("exited unexpectedly (code 3)", text)
        self.assertIn("HIP out of memory", text)
        self.assertEqual(c.state, "failed")

    def test_loading_heartbeats_reach_callback_and_events(self):
        c = self.client("ok")
        stages = []
        caps = c.ensure_ready(timeout=60, on_loading=lambda info: stages.append(info["stage"]))
        self.assertEqual(caps["maxSeconds"], 60)
        self.assertEqual(stages, ["step 0", "step 1", "step 2"])
        self.assertTrue(any(k == "engine_loading" for k, _ in self.events.seen))
        self.assertIsNone(c.status()["loading"])


if __name__ == "__main__":
    unittest.main()
