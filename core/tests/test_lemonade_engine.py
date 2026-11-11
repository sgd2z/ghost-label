"""Lemonade Server as the music engine, against a fake Lemonade (engine/tests/fake_lemonade.py):
which server and key, the Test music engine button, a real song through the engine host, and
songs with reference audio going to the built-in engine (or without the reference)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "engine", "tests"))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from fake_lemonade import FakeLemonade, realistic  # noqa: E402
from smcore import lemonade  # noqa: E402
from smcore.engine_client import EngineClient  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Secrets, Settings  # noqa: E402
from smcore.studio import Studio  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


class Events:
    def __init__(self):
        self.seen = []

    def emit(self, kind, **data):
        self.seen.append((kind, data))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = Paths(os.path.join(self.tmp, "home"))
        self.settings = Settings(self.paths)
        self.secrets = Secrets(self.paths)
        self.fake = FakeLemonade()
        self.addCleanup(self.fake.close)


class ServerTests(Base):
    def test_url_and_key_follow_the_ai_provider_unless_overridden(self):
        self.settings.update({"provider": {"baseUrl": "http://localhost:13305/api/v1/", "imagesBaseUrl": "http://img:1/v1"}})
        self.secrets.set("apiKey", "chat-key")
        self.secrets.set("imagesApiKey", "img-key")
        self.assertEqual(lemonade.server(self.settings, self.secrets),
                         ("http://localhost:13305/api/v1", "chat-key", "AI provider base URL"))
        self.settings.update({"engine": {"lemonadeUrl": "http://img:1"}})
        self.assertEqual(lemonade.server(self.settings, self.secrets)[:2], ("http://img:1/v1", "img-key"))
        self.settings.update({"engine": {"lemonadeUrl": "http://other:9/v1"}})
        self.assertEqual(lemonade.server(self.settings, self.secrets)[1], "")   # never another server's key
        self.settings.update({"engine": {"lemonadeUrl": ""}, "provider": {"baseUrl": ""}})
        self.assertEqual(lemonade.server(self.settings)[0], lemonade.DEFAULT_URL)
        self.assertEqual(lemonade.model(self.settings), "ACE-Step-Music")

    def test_music_engine_button(self):
        self.settings.update({"engine": {"lemonadeUrl": self.fake.url}})
        r = lemonade.test(self.settings, self.secrets)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["model"], r["recipe"], r["loaded"]), ("ACE-Step-Music", "acestep", False))
        self.assertEqual(self.fake.posts("/audio/generations"), [])   # a check, not a song
        self.fake.model = "other"
        r = lemonade.test(self.settings, self.secrets)
        self.assertFalse(r["ok"])
        self.assertTrue(r["running"])
        self.assertIn("lemonade pull ACE-Step-Music", r["error"])
        self.fake.model, self.fake.downloaded = "ACE-Step-Music", False
        self.assertIn("lemonade pull", lemonade.test(self.settings, self.secrets)["error"])
        self.settings.update({"engine": {"lemonadeUrl": "http://127.0.0.1:9/v1"}})
        r = lemonade.test(self.settings, self.secrets)
        self.assertFalse(r["running"])
        self.assertIn("Start Lemonade Server", r["error"])

    def test_unload(self):
        self.settings.update({"engine": {"lemonadeUrl": self.fake.url}})
        self.assertTrue(lemonade.unload(self.settings, self.secrets, "ACE-Step-Music", log=lambda *_: None))
        self.assertEqual(self.fake.posts("/unload"), [{"model_name": "ACE-Step-Music"}])


@unittest.skipUnless(FFMPEG, "no ffmpeg")
class EngineTests(Base):
    def test_a_song_through_the_engine_host(self):
        self.settings.update({"engine": {"backend": "lemonade", "lemonadeUrl": self.fake.url},
                              "provider": {"baseUrl": self.fake.url}})
        self.fake.key = "k1"
        self.secrets.set("apiKey", "k1")
        events = Events()
        client = EngineClient(self.settings, self.paths, os.path.join(ROOT, "engine"), events, log=lambda *_: None)
        client.secrets = self.secrets
        client.ffmpeg = lambda: FFMPEG
        self.addCleanup(client.stop)
        self.assertIn("lemonade", " ".join(client.command()[0]))
        self.assertNotIn("k1", " ".join(client.command()[0]))   # the key goes by environment only
        caps = client.ensure_ready()
        self.assertFalse(caps["referenceAudio"])
        out = os.path.join(self.tmp, "song.m4a")
        seen = []
        result = client.generate({"id": "j1", "style": "lofi, warm alto", "lyrics": "[Verse]\nla", "seed": 3,
                                  "steps": 16, "outputPath": out, "maxSeconds": 30, "isInstrumental": False},
                                 on_progress=seen.append)
        self.assertAlmostEqual(result["duration"], 2.0, delta=0.1)
        self.assertTrue(os.path.exists(out))
        self.assertEqual(self.fake.posts("/audio/generations")[0]["duration"], 30)
        self.fake.reply = (500, {"error": {"message": "out of memory", "type": "model_load_error"}})
        with self.assertRaises(Exception) as caught:
            client.generate({"id": "j2", "style": "x", "lyrics": "", "seed": 1, "steps": 16,
                             "outputPath": os.path.join(self.tmp, "b.m4a"), "maxSeconds": 10, "isInstrumental": True})
        self.assertIn("couldn't load the music model", str(caught.exception))

    def test_builtin_stand_in_ignores_the_selected_backend(self):
        self.settings.update({"engine": {"backend": "lemonade", "options": {"x": "1"}}})
        client = EngineClient(self.settings, self.paths, os.path.join(ROOT, "engine"), Events(), backend="acestep")
        try:
            cmd = client.command()[0]
        except Exception as error:  # noqa: BLE001 — no venv here: the built-in engine isn't installed
            self.assertIn("isn't set up", str(error))
            return
        self.assertEqual(cmd[cmd.index("--backend") + 1], "acestep")
        self.assertNotIn("x=1", cmd)


class RealisticListingTests(Base):
    """lemonade_models.json: a custom extra_models_dir GGUF for chat, SDXL-Turbo loaded on the GPU
    though its row says downloaded=false (plain /v1/models leaves it out), ACE-Step-Music pulled."""

    def setUp(self):
        super().setUp()
        self.fake.close()
        catalog, loaded = realistic()
        self.fake = FakeLemonade(catalog=catalog, loaded=loaded)
        self.addCleanup(self.fake.close)
        self.settings.update({"provider": {"baseUrl": self.fake.url, "textModel": "Qwen3-Coder-30B-A3B-Instruct-GGUF",
                                           "imagesEnabled": True, "imagesBaseUrl": "", "imageModel": "SDXL-Turbo"}})
        from smcore.llm import Provider
        self.provider = Provider(self.settings, self.secrets, None, lambda *_: None)

    def test_connection_lists_every_usable_model_by_what_lemonade_serves_it_as(self):
        r = self.provider.test()
        self.assertTrue(r["lemonade"])
        self.assertEqual(r["chatModels"], ["Qwen3-Coder-30B-A3B-Instruct-GGUF"])
        self.assertEqual(r["imageModels"], ["SDXL-Turbo"])          # loaded, so listed despite the flag
        self.assertEqual(r["musicModels"], ["ACE-Step-Music"])
        self.assertNotIn("Qwen3-0.6B-GGUF", r["models"])            # catalog only, not downloaded
        self.assertNotIn("SD-Turbo", r["models"])
        self.assertNotIn("nomic-embed-text-v1-GGUF", r["chatModels"] + r["imageModels"])

    def test_images_check(self):
        r = self.provider.test_images()
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["loaded"])
        self.assertEqual(r["imageModels"], ["SDXL-Turbo"])
        for model, expected in (("SD-Turbo", "lemonade pull SD-Turbo"),
                                ("Qwen3-Coder-30B-A3B-Instruct-GGUF", "is a chat model"),
                                ("Nope", "Image models there: SDXL-Turbo")):
            self.settings.update({"provider": {"imageModel": model}})
            r = self.provider.test_images()
            self.assertFalse(r["ok"])
            self.assertIn(expected, r["error"])
            self.assertNotIn("Qwen3-Coder", r.get("imageModels") or [])   # never a chat model as an image model
        self.fake.catalog = [m for m in self.fake.catalog if m["recipe"] != "sd-cpp"]
        self.fake.loaded = [m for m in self.fake.loaded if m["type"] != "image"]
        r = self.provider.test_images()
        self.assertEqual(r["imageModels"], [])
        self.assertIn("No image models found", r["error"])

    def test_music_check_trusts_what_is_loaded(self):
        self.settings.update({"engine": {"lemonadeUrl": ""}})
        r = lemonade.test(self.settings, self.secrets)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["musicModels"], ["ACE-Step-Music"])
        row = next(m for m in self.fake.catalog if m["id"] == "ACE-Step-Music")
        row["downloaded"] = False
        self.assertIn("lemonade pull ACE-Step-Music", lemonade.test(self.settings, self.secrets)["error"])
        self.fake.loaded.append({"model_name": "ACE-Step-Music", "type": "audio-generation", "recipe": "acestep"})
        r = lemonade.test(self.settings, self.secrets)
        self.assertTrue(r["ok"] and r["loaded"], r)
        self.settings.update({"engine": {"lemonadeModel": "SDXL-Turbo"}})
        self.assertIn("is an image model on this server, not an audio-generation model", lemonade.test(self.settings, self.secrets)["error"])


class InstallerTests(Base):
    def test_lemonade_profile(self):
        from unittest import mock
        from smcore.bootstrap import Installer
        from smcore.engine_layout import EngineLayout
        inst = Installer(self.settings, self.paths, os.path.join(ROOT, "engine"), None, lambda *_: None)
        self.enterContext(mock.patch.object(EngineLayout, "write_manifest", lambda self, data: None))
        full = mock.patch.object(EngineLayout, "ace_ok", lambda self: False)
        full.start()
        inst.save_engine("lemonade", {}, {"selected": "lemonade"})   # a small install: Lemonade only
        eng = self.settings.section("engine")
        self.assertEqual((eng["backend"], eng["profile"]), ("lemonade", "lemonade"))
        self.assertNotIn("ACESTEP_PROJECT_ROOT", eng["env"])
        # On top of a full ROCm install: only Lemonade gets selected; the install keeps its profile.
        self.settings.update({"engine": {"profile": "rocm-windows", "backend": "acestep"}})
        full.stop()
        self.enterContext(mock.patch.object(EngineLayout, "ace_ok", lambda self: True))
        inst.save_engine("lemonade", {}, {"selected": "lemonade"})
        eng = self.settings.section("engine")
        self.assertEqual((eng["backend"], eng["profile"]), ("lemonade", "rocm-windows"))
        # Repairing the built-in engine keeps Lemonade selected.
        inst.save_engine("rocm-windows", {}, {"selected": None})
        self.assertEqual(self.settings.section("engine")["backend"], "lemonade")


class RoutingTests(Base):
    """Studio.route: which engine records a song when Lemonade is selected."""

    class Engine:
        def __init__(self, name, installed=True):
            self.name, self.proc, self.stopped, self.installed = name, None, 0, installed

        def stop(self):
            self.stopped += 1
            self.proc = None

        def layout(self):
            installed = self.installed

            class L:
                def python(self):
                    return "/py" if installed else None

                def ace_ok(self):
                    return installed
            return L()

    def studio(self, ref, installed=True):
        s = Studio.__new__(Studio)
        s.settings, s.secrets, s.log = self.settings, self.secrets, lambda *_: None
        s.engine, s.builtin = self.Engine("lemonade"), self.Engine("acestep", installed)
        s.reference_for = lambda t, album, artist: ref
        return s

    def test_routing(self):
        self.settings.update({"engine": {"backend": "acestep"}})
        s = self.studio("/ref.wav")
        self.assertEqual(s.route({}, {}, {}), (s.engine, None, None))   # built-in selected: unchanged

        self.settings.update({"engine": {"backend": "lemonade", "lemonadeUrl": self.fake.url}})
        engine, ref, note = s.route({}, {}, {})
        self.assertIs(engine, s.builtin)
        self.assertEqual(ref, "/ref.wav")
        self.assertIn("built-in ACE-Step", note)
        self.assertEqual(self.fake.posts("/unload"), [{"model_name": "ACE-Step-Music"}])   # Lemonade's music model out first

        s = self.studio(None)
        s.builtin.proc = object()
        self.settings.update({"engine": {"unloadChatModelBeforeRecording": True}, "provider": {"textModel": "Qwen3-8B-GGUF"}})
        self.assertEqual(s.route({}, {}, {}), (s.engine, None, None))
        self.assertEqual(s.builtin.stopped, 1)   # the built-in engine's memory is freed for Lemonade
        self.assertIn({"model_name": "Qwen3-8B-GGUF"}, self.fake.posts("/unload"))

        s = self.studio("/ref.wav", installed=False)
        engine, ref, note = s.route({}, {}, {})
        self.assertIs(engine, s.engine)
        self.assertIsNone(ref)
        self.assertIn("without its reference audio", note)


if __name__ == "__main__":
    unittest.main()
