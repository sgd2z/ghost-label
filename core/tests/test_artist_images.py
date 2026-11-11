"""Artist photos and covers: the AI gets the whole dossier and the target image model, the
final prompt is logged, Lemonade gets a negative prompt and the model's own size and steps,
and the activity names the image model while an image is drawn."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "engine", "tests"))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from fake_lemonade import FakeLemonade, realistic  # noqa: E402
from fakes import FakeProvider  # noqa: E402
from smcore import lemonade  # noqa: E402

BIO = ("Zara Kaur grew up in Southall, West London, the daughter of Punjabi Sikh parents; she sings "
       "Punjabi and English over garage beats.")


class ArtistImageTests(unittest.TestCase):
    def setUp(self):
        from smcore.app import App
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.chat = FakeProvider()
        self.addCleanup(self.chat.close)
        self.lines = []
        self.app = App(home=os.path.join(self.tmp, "home"), log=self.lines.append)
        self.addCleanup(self.app.engine.stop)
        self.app.settings.update({"provider": {"baseUrl": self.chat.url + "/v1", "textModel": "fake-text",
                                               "fastModel": "fake-fast", "imagesEnabled": True, "imageModel": "fake-image"}})
        self.app.db.insert("artists", {
            "id": "ar_z", "name": "Zara Kaur", "bio": BIO, "origin": "Southall, London", "formed": "2021",
            "voice": "warm female mezzo, Punjabi and English", "genres_json": ["UK garage", "Punjabi pop"],
            "members_json": [{"name": "Zara Kaur", "role": "vocals"}],
            "visual": "late-20s British Punjabi woman, long dark hair, gold jhumkas, streetwear",
            "data_json": {"vocalTags": "female vocals, warm mezzo", "languages": ["pa", "en"]}, "created": time.time()})

    def art_request(self):
        for r in reversed(self.chat.requests):
            for m in r["body"].get("messages") or []:
                if m.get("role") == "user" and "Write the image prompt" in str(m.get("content")):
                    return m["content"]
        self.fail("no image-prompt request")

    def test_the_prompt_writer_gets_the_whole_dossier_and_the_target_model(self):
        result = self.app.builder.portrait("ar_z")
        self.assertEqual(result["source"], "provider")
        user = self.art_request()
        data = json.loads(user.split("Data:\n", 1)[1])
        artist = data["artist"]
        for key in ("bio", "origin", "genres", "members", "visual", "voice", "vocalTags", "languages"):
            self.assertIn(key, artist)
        self.assertIn("Punjabi Sikh", artist["bio"])
        self.assertEqual(data["imageModel"]["model"], "fake-image")
        self.assertEqual(data["kind"], "press photo")
        image_body = next(r["body"] for r in reversed(self.chat.requests) if r["path"].endswith("/images/generations"))
        self.assertEqual(image_body["prompt"], result["prompt"])
        self.assertNotIn("negative_prompt", image_body)        # not a server that takes one
        self.assertTrue(any("[images] portrait for 'Zara Kaur'" in line and "prompt:" in line for line in self.lines))
        stored = self.app.db.one("SELECT portrait_prompt FROM artists WHERE id = 'ar_z'")["portrait_prompt"]
        self.assertEqual(stored, result["prompt"])
        # The rules carry the guidance (identity first, contemporary press photo, hands, length).
        rules = self.app.rules.system("artwork")
        for phrase in ("heritage", "press", "hands", "77 tokens", "negative_prompt"):
            self.assertIn(phrase, rules)

    def test_lemonade_gets_negative_prompt_and_model_defaults_and_activity_names_the_image_model(self):
        catalog, loaded = realistic()
        fake = FakeLemonade(catalog=catalog, loaded=loaded)
        self.addCleanup(fake.close)
        fake.delay = 0.8
        self.app.settings.update({"provider": {"imagesBaseUrl": fake.url, "imageModel": "SDXL-Turbo", "imageSize": ""}})
        seen = []

        def watch():
            for _ in range(40):
                seen.extend(c for c in self.app.llm_activity() if c["kind"] == "image")
                time.sleep(0.05)
        t = threading.Thread(target=watch)
        t.start()
        result = self.app.builder.portrait("ar_z")
        t.join()
        self.assertEqual(result["source"], "provider", self.app.provider.last_image_error)
        body = fake.posts("/images/generations")[-1]
        self.assertEqual(body["model"], "SDXL-Turbo")
        self.assertNotIn("size", body)                      # Lemonade uses SDXL-Turbo's own 512x512
        self.assertNotIn("steps", body)
        self.assertIn("deformed hands", body["negative_prompt"])
        self.assertTrue(seen and seen[0]["model"] == "SDXL-Turbo")
        data = json.loads(self.art_request().split("Data:\n", 1)[1])
        self.assertEqual(data["imageModel"]["size"], "512x512")
        self.assertEqual(data["imageModel"]["family"], "sd-cpp")
        # Settings override the model's own steps/CFG and the negative prompt is editable.
        self.app.settings.update({"provider": {"imageSteps": "6", "imageCfgScale": "1.5",
                                               "imageNegativePrompt": "extra fingers"}})
        self.app.builder.portrait("ar_z")
        body = fake.posts("/images/generations")[-1]
        self.assertEqual((body["steps"], body["cfg_scale"]), (6, 1.5))
        self.assertTrue(body["negative_prompt"].startswith("extra fingers"))

    def test_thenoise_models_get_the_negative_prompt_and_calls_say_what_they_are_for(self):
        catalog, loaded = realistic()
        catalog = list(catalog) + [{"id": "Qwen-Image-2.1-Turbo-INT8", "recipe": "thenoise", "downloaded": True,
                                    "labels": ["image", "edit"], "recipe_options": {
                                        "steps": 8, "cfg_scale": 1, "width": 1440, "height": 1440}}]
        fake = FakeLemonade(catalog=catalog, loaded=loaded)
        self.addCleanup(fake.close)
        fake.delay = 0.5
        self.app.settings.update({"provider": {"imagesBaseUrl": fake.url, "imageModel": "Qwen-Image-2.1-Turbo-INT8",
                                               "imageSize": ""}})
        seen = []
        stop = threading.Event()

        def watch():
            while not stop.is_set():
                seen.extend((c["kind"], c["purpose"], c["model"]) for c in self.app.llm_activity())
                time.sleep(0.02)
        t = threading.Thread(target=watch)
        t.start()
        try:
            self.app.builder.portrait("ar_z")
            album = {"id": "al_z", "artist_id": "ar_z", "title": "Southall Nights", "kind": "single",
                     "position": 0, "created": time.time()}
            self.app.db.insert("albums", album)
            self.app.builder.cover("al_z")
        finally:
            stop.set()
            t.join()
        body = fake.posts("/images/generations")[0]
        self.assertEqual(body["model"], "Qwen-Image-2.1-Turbo-INT8")
        self.assertIn("deformed hands", body["negative_prompt"])
        self.assertNotIn("size", body)
        self.assertIn(("image", "photo", "Qwen-Image-2.1-Turbo-INT8"), seen)
        self.assertIn(("image", "cover", "Qwen-Image-2.1-Turbo-INT8"), seen)
        # The chat call that writes the prompt is labelled too (only checked if it was caught in flight).
        chat_purposes = {p for k, p, m in seen if k == "chat"}
        self.assertTrue(chat_purposes <= {"photo prompt", "cover prompt"}, chat_purposes)
        self.assertEqual(self.app.provider.activity(), [])

    def test_prompt_calls_are_labelled(self):
        labels = []
        real = self.app.provider._call_started

        def spy(*a, **k):
            call = real(*a, **k)
            labels.append((call["kind"], call["purpose"]))
            return call
        self.app.provider._call_started = spy
        self.app.builder.portrait("ar_z")
        self.assertIn(("chat", "photo prompt"), labels)
        self.assertIn(("image", "photo"), labels)
        labels.clear()
        self.app.provider.json("sys", "Plan nothing", {"type": "object", "properties": {}}, name="x")
        self.assertEqual(labels, [("chat", None)])   # unlabelled outside the prompt step

    def add_album(self):
        self.app.db.insert("albums", {"id": "al_m", "artist_id": "ar_z", "title": "Mirrors & Motion", "kind": "album",
                                      "cover_brief": "two mirrors facing each other on a dark stage", "position": 0,
                                      "created": time.time()})

    def test_cover_prompt_writer_gets_the_exact_text(self):
        self.add_album()
        self.app.builder.cover("al_m")
        user = [m["content"] for r in self.chat.requests for m in r["body"].get("messages") or []
                if m.get("role") == "user" and "Write the image prompt for this record cover" in str(m.get("content"))][-1]
        data = json.loads(user.split("Data:\n", 1)[1])
        self.assertEqual(data["coverText"], {"title": "Mirrors & Motion", "artist": "Zara Kaur"})
        rules = self.app.rules.system("artwork")
        self.assertIn("coverText", rules)
        self.assertIn('"Mirrors & Motion"', rules)

    def test_cover_fallback_quotes_title_and_artist(self):
        self.add_album()

        def fail(*a, **k):
            raise RuntimeError("No reply within 180 s")
        self.app.builder.ask = fail
        result = self.app.builder.cover("al_m")
        self.assertIn('"Mirrors & Motion"', result["prompt"])
        self.assertIn('"Zara Kaur"', result["prompt"])
        self.assertIn("two mirrors facing each other", result["prompt"])
        body = next(r["body"] for r in reversed(self.chat.requests) if r["path"].endswith("/images/generations"))
        self.assertEqual(body["prompt"], result["prompt"])
        self.assertTrue(any("[images] writing the record cover prompt failed" in l for l in self.lines))
        self.assertTrue(any("[images] cover for" in l and '"Mirrors & Motion"' in l for l in self.lines))

    def test_upscalers_are_not_image_models(self):
        self.assertEqual(lemonade.mode({"labels": ["upscaling", "on-demand", "image"]}), "upscaling")
        self.assertEqual(lemonade.mode({"recipe": "sd-cpp", "labels": ["image"]}), "image")


if __name__ == "__main__":
    unittest.main()
