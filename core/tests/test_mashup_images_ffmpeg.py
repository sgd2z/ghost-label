"""One ffmpeg for every user of it; mashup reference audio from the AI's shares; images on their own
server and key; New cover / New photo replace the old file."""

from __future__ import annotations

import math
import os
import shutil
import stat
import struct
import sys
import tempfile
import time
import unittest
import wave

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from fakes import FakeProvider  # noqa: E402
from smcore import runtime  # noqa: E402
from smcore.links import Fetcher, probe_seconds  # noqa: E402
from smcore.llm import Provider  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Secrets, Settings  # noqa: E402

FFMPEG = shutil.which("ffmpeg")


def tone(path, seconds, freq=440.0, rate=48000):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            v = int(8000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<hh", v, v)
        w.writeframes(bytes(frames))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.paths = Paths(os.path.join(self.tmp, "home"))
        self.settings = Settings(self.paths)


class FfmpegResolverTests(Base):
    def fake_tool(self, folder, name="ffmpeg"):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    @unittest.skipIf(os.name == "nt", "shell-script stand-in for ffmpeg")
    def test_configured_then_own_copy_and_ffprobe_beside_it(self):
        class Installer:
            root = os.path.join(self.tmp, "Engine")
            settings = self.settings
        configured = self.fake_tool(os.path.join(self.tmp, "custom", "bin"))
        self.fake_tool(os.path.join(self.tmp, "custom", "bin"), "ffprobe")
        self.settings.update({"engine": {"env": {"MUSIC_ENGINE_FFMPEG": configured}}})
        rt = runtime.Runtime(Installer(), jobs=None, log=lambda *_: None)
        self.assertEqual(rt.ffmpeg_path(), configured)
        self.assertEqual(rt.ffprobe_path(), os.path.join(self.tmp, "custom", "bin", "ffprobe"))
        # The configured one disappears: the next use finds Ghost Label's own copy, no restart needed.
        os.remove(configured)
        own = self.fake_tool(os.path.join(Installer.root, "tools", "ffmpeg", "bin"))
        self.assertEqual(rt.ffmpeg_path(), own)

    @unittest.skipUnless(FFMPEG, "needs ffmpeg")
    def test_yt_dlp_gets_our_ffmpeg_and_clips_are_cut_with_it(self):
        seen = {}
        source = os.path.join(self.tmp, "song.wav")
        tone(source, 8)

        class YDL:
            def __init__(self, opts):
                seen["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download):
                shutil.copyfile(source, seen["opts"]["outtmpl"].replace("%(ext)s", "wav"))

        class Module:
            YoutubeDL = YDL

        used = []
        fetcher = Fetcher(self.settings, self.paths, lambda *_: None, ffmpeg=lambda: used.append(1) or FFMPEG)
        fetcher.module = lambda: Module
        clip = fetcher.audio_clip("https://example.test/v", "st_1", 4)
        self.assertEqual(seen["opts"]["ffmpeg_location"], FFMPEG)
        self.assertTrue(used)
        self.assertAlmostEqual(probe_seconds(FFMPEG, clip), 4.0, delta=0.2)
        # The yt-dlp binary gets --ffmpeg-location too.
        args = []
        fetcher.module = lambda: None
        fetcher.ytdlp_binary = lambda: "/bin/yt-dlp"
        fetcher._run_binary = lambda a, timeout=0: (args.extend(a), shutil.copyfile(source, a[a.index("-o") + 1].replace("%(ext)s", "wav")))
        fetcher.audio_clip("https://example.test/v", "st_2", 3)
        self.assertEqual(args[args.index("--ffmpeg-location") + 1], FFMPEG)


@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class MashupReferenceTests(Base):
    def test_sections_follow_the_ai_shares(self):
        from smcore.db import Database
        from smcore.studio import Studio

        db = Database(self.paths.db)
        db.insert("source_sets", {"id": "ss_1", "mode": "mashup", "status": "ready", "created": time.time()})
        for n in (1, 2, 3):
            clip = os.path.join(self.paths.clips, f"st_{n}.wav")
            tone(clip, 60, 220.0 * n)
            db.insert("source_tracks", {"id": f"st_{n}", "set_id": "ss_1", "position": n, "url": "u", "clip_path": self.paths.rel(clip)})

        class Engine:
            state = "ready"

            def status(self):
                return {}

        studio = Studio.__new__(Studio)   # only the reference builder; no studio threads
        studio.db, studio.paths, studio.log, studio.ffmpeg = db, self.paths, (lambda *_: None), (lambda: FFMPEG)
        mashup = {"setId": "ss_1", "sources": [{"number": 1, "share": 0.75}, {"number": 2, "share": 0.25},
                                               {"number": 3, "share": 0.0}]}
        ref = studio.mashup_reference({"id": "tr_x"}, mashup)
        self.assertTrue(ref.endswith("ref-tr_x.wav"))
        self.assertAlmostEqual(probe_seconds(FFMPEG, ref), 60.0, delta=0.5)   # 45 s of #1 + 15 s of #2, none of #3
        self.assertIsNone(studio.mashup_reference({"id": "tr_y"}, {"setId": "ss_1", "sources": [{"number": 3, "share": 0}]}))


class ImageSettingsTests(Base):
    def setUp(self):
        super().setUp()
        self.secrets = Secrets(self.paths)
        self.chat = FakeProvider()
        self.images = FakeProvider(key="sk-images")
        self.addCleanup(self.chat.close)
        self.addCleanup(self.images.close)

    def test_images_use_their_own_server_and_key_never_the_chat_key(self):
        self.secrets.set("apiKey", "lm-studio-key")
        self.settings.update({"provider": {"baseUrl": self.chat.url + "/v1", "textModel": "fake-text",
                                           "imagesEnabled": True, "imagesBaseUrl": self.images.url + "/v1",
                                           "imageModel": "fake-image"}})
        p = Provider(self.settings, self.secrets, None, lambda *_: None)
        self.assertEqual(p.image_key_source(), "none")
        _, source = p.image("a cover", "seed")
        self.assertEqual(source, "placeholder")                       # no image key yet: refused, not leaked
        self.assertIsNone(self.images.requests[-1]["auth"])
        report = p.test_images()
        self.assertFalse(report["ok"])
        self.assertTrue(report["separate"])
        self.secrets.set("imagesApiKey", "sk-images")
        _, source = p.image("a cover", "seed")
        self.assertEqual(source, "provider")
        self.assertEqual(self.images.requests[-1]["auth"], "Bearer sk-images")
        self.assertEqual(self.chat.requests, [])                      # nothing image-related went to the chat server
        report = p.test_images()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["key"], "images key")
        self.assertEqual(len(self.images.requests), 2)                # the test generated no image
        self.settings.update({"provider": {"imageModel": "not-there"}})
        self.assertIn("isn't in this server's model list", p.test_images()["error"])

    def test_shared_server_uses_the_chat_key(self):
        chat = FakeProvider(key="sk-shared")
        self.addCleanup(chat.close)
        self.secrets.set("apiKey", "sk-shared")
        self.settings.update({"provider": {"baseUrl": chat.url + "/v1", "textModel": "t", "imagesEnabled": True,
                                           "imagesBaseUrl": "", "imageModel": "fake-image"}})
        p = Provider(self.settings, self.secrets, None, lambda *_: None)
        self.assertEqual(p.image_key_source(), "chat key")
        self.assertEqual(p.image("x", "seed")[1], "provider")


class LemonadeTests(Base):
    """A Lemonade-style server: chat and images on one local URL, labelled models, POST /unload."""

    def test_lists_split_by_label_and_image_model_unloads_before_recording(self):
        server = FakeProvider()
        self.addCleanup(server.close)
        secrets = Secrets(self.paths)
        self.settings.update({"provider": {"baseUrl": server.url + "/v1", "textModel": "fake-text", "imagesEnabled": True,
                                           "imagesBaseUrl": "", "imageModel": "fake-image", "imageSize": "512x512"}})
        p = Provider(self.settings, secrets, None, lambda *_: None)
        report = p.test()
        self.assertEqual(report["imageModels"], ["fake-image"])
        self.assertEqual(report["chatModels"], ["fake-text", "fake-fast"])
        self.assertEqual(p.test_images()["imageModels"], ["fake-image"])
        self.assertFalse(p.release_image_model())                     # off unless asked for
        self.settings.update({"provider": {"unloadImageModelBeforeRecording": True}})
        self.assertTrue(p.release_image_model())
        self.assertEqual(server.requests[-1]["path"], "/v1/unload")
        self.assertEqual(server.requests[-1]["body"], {"model_name": "fake-image"})
        _, source = p.image("cover", "seed")
        self.assertEqual(source, "provider")
        self.assertEqual(server.requests[-1]["body"]["size"], "512x512")


class NewArtworkTests(unittest.TestCase):
    def test_new_cover_and_photo_replace_the_files(self):
        from smcore.app import App
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        fake = FakeProvider()
        self.addCleanup(fake.close)
        app = App(home=os.path.join(tmp, "home"), log=lambda *_: None)
        self.addCleanup(app.engine.stop)
        app.settings.update({"provider": {"baseUrl": fake.url + "/v1", "textModel": "fake-text", "fastModel": "fake-fast",
                                          "imagesEnabled": True, "imageModel": "fake-image"}})
        app.db.insert("artists", {"id": "ar_1", "name": "Night Bus", "visual": "neon", "created": time.time()})
        app.db.insert("albums", {"id": "al_1", "artist_id": "ar_1", "title": "Late", "kind": "single",
                                 "cover_brief": "a bus at night", "created": time.time()})
        first = app.builder.cover("al_1")
        old = app.paths.abs(app.db.one("SELECT cover_path FROM albums WHERE id = 'al_1'")["cover_path"])
        time.sleep(0.01)
        again = app.builder.cover("al_1")
        new = app.paths.abs(app.db.one("SELECT cover_path FROM albums WHERE id = 'al_1'")["cover_path"])
        self.assertEqual((first["source"], again["source"]), ("provider", "provider"))
        self.assertNotEqual(old, new)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        photo = app.builder.portrait("ar_1")
        self.assertEqual(photo["source"], "provider")
        app.settings.update({"provider": {"imagesEnabled": False}})
        off = app.builder.portrait("ar_1")
        self.assertEqual(off["source"], "placeholder")
        self.assertIn("off", off["error"])
        self.assertEqual(len([f for f in os.listdir(app.paths.artwork) if f.startswith("portrait-ar_1")]), 1)


if __name__ == "__main__":
    unittest.main()
