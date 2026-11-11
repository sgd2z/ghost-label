"""Settings › AI provider model pickers against a Lemonade like Siddharth's: gemma-4 QAT loaded
(from extra_models_dir, its catalog entry saying downloaded:false or no entry at all), an image
and a music model, and the saved Qwen3-Coder removed from the server."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine", "tests"))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from fake_lemonade import FakeLemonade, realistic  # noqa: E402
from smcore.llm import ProviderError  # noqa: E402

GEMMA = "gemma-4-26B-A4B-it-QAT-GGUF"
CODER = "Qwen3-Coder-30B-A3B-Instruct-GGUF"
GEMMA_LOADED = {"model_name": GEMMA, "checkpoint": "D:/models/gemma-4-26B-A4B-it-QAT-Q4_0.gguf", "type": "llm",
                "device": "gpu", "recipe": "llamacpp", "status": "ready", "loaded": True}
IMAGE_LOADED = {"model_name": "Qwen-Image-2.1-Turbo-INT8", "type": "image", "recipe": "thenoise", "loaded": True}
MUSIC_LOADED = {"model_name": "ACE-Step-Music", "type": "audio-generation", "recipe": "acestep", "loaded": True}


def server(gemma_entry):
    catalog, _ = realistic()
    catalog = [m for m in catalog if m["id"] != CODER]          # removed from the server since
    catalog.append({"id": "Qwen-Image-2.1-Turbo-INT8", "recipe": "thenoise", "downloaded": True,
                    "labels": ["image", "edit"]})
    if gemma_entry == "not downloaded":                          # catalog says no, /health says loaded
        catalog.append({"id": GEMMA, "recipe": "llamacpp", "downloaded": False, "labels": ["custom", "vision"],
                        "source": "extra_models_dir"})
    for m in catalog:
        if m["id"] == "ACE-Step-Music":
            m["downloaded"] = True
    return FakeLemonade(catalog=catalog, loaded=[GEMMA_LOADED, IMAGE_LOADED, MUSIC_LOADED])


class ChatModelPickerTests(unittest.TestCase):
    def make_app(self, gemma_entry):
        from smcore.app import App
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.fake = server(gemma_entry)
        self.addCleanup(self.fake.close)
        app = App(home=os.path.join(tmp, "home"), log=lambda *a: None)
        self.addCleanup(app.engine.stop)
        app.settings.update({"provider": {"baseUrl": self.fake.url, "textModel": CODER, "fastModel": "",
                                          "imagesEnabled": False}})
        return app

    def test_loaded_models_are_listed_and_the_missing_saved_one_is_flagged(self):
        for variant in ("not downloaded", "no entry"):
            with self.subTest(variant):
                app = self.make_app(variant)
                r = app.provider.chat_models()
                self.assertIsNone(r["error"])
                self.assertTrue(r["lemonade"])
                self.assertIn(GEMMA, r["models"])
                self.assertIn(GEMMA, r["loaded"])
                self.assertNotIn("Qwen-Image-2.1-Turbo-INT8", r["models"])   # image, not chat
                self.assertNotIn("ACE-Step-Music", r["models"])
                self.assertEqual(r["textModel"], {"name": CODER, "present": False})
                self.assertTrue(r["fastModel"]["sameAsMain"])

    def test_test_connection_names_the_model_and_does_not_switch(self):
        app = self.make_app("not downloaded")
        r = app.provider.test()
        self.assertEqual(r["usedModel"], CODER)
        self.assertFalse(r["ok"])
        self.assertEqual(r["missingModels"], [CODER])
        self.assertIn(f"{CODER} isn't on the server", r["modelWarning"])
        self.assertIn(GEMMA, r["modelWarning"])
        self.assertIn(f"{CODER} isn't on the Lemonade server", r["chatError"])
        self.assertEqual(app.settings.section("provider")["textModel"], CODER)   # never auto-switched

    def test_a_request_to_a_missing_model_fails_plainly_and_shows_in_activity(self):
        app = self.make_app("no entry")
        seen = []
        app.provider.on_activity = lambda calls: seen.append(app.provider.problem)
        with self.assertRaises(ProviderError) as ctx:
            app.provider.json("sys", "hello", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
        self.assertIn(f"{CODER} isn't on the Lemonade server", str(ctx.exception))
        self.assertIn("Settings › AI provider", str(ctx.exception))
        self.assertEqual(app.provider.problem["model"], CODER)
        self.assertTrue(any(p and p["model"] == CODER for p in seen))
        # Picking the loaded model works and clears the problem.
        app.settings.update({"provider": {"textModel": GEMMA}})
        app.provider.problem = {"model": GEMMA, "message": "stale", "at": 0}
        self.assertEqual(app.provider.json("sys", "hello", {"type": "object", "properties": {"ok": {"type": "boolean"}}})["ok"], True)
        self.assertIsNone(app.provider.problem)
        self.assertEqual(self.fake.chat_models[-1], GEMMA)

    def test_fast_model_blank_means_main(self):
        app = self.make_app("no entry")
        app.settings.update({"provider": {"textModel": GEMMA, "fastModel": ""}})
        app.provider.json("sys", "hello", {"type": "object", "properties": {"ok": {"type": "boolean"}}}, fast=True)
        self.assertEqual(self.fake.chat_models[-1], GEMMA)

    def test_route_serves_the_list(self):
        from smcore import server as srv
        app = self.make_app("no entry")
        handler = next(fn for (method, pattern, fn) in srv.ROUTES if method == "GET" and "provider/models" in pattern.pattern)
        self.assertIn(GEMMA, handler(app, {}, {})["models"])


if __name__ == "__main__":
    unittest.main()
