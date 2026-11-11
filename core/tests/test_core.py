"""Core tests: JSON handling, the provider client against a fake OpenAI-compatible server, and an
end-to-end run of the whole app over HTTP with the mock engine backend and a fake catalog."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SM_SECRET_STORE"] = "file"

from fakes import FakeCatalog, FakeProvider  # noqa: E402
from smcore import jsonio  # noqa: E402
from smcore.app import App  # noqa: E402
from smcore.llm import Provider  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.server import serve  # noqa: E402
from smcore.settings import Secrets, Settings  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


def tone_wav(path, seconds=3.0, sr=48000):
    import math
    import struct
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = b"".join(struct.pack("<hh", int(8000 * math.sin(i * 0.05)), int(8000 * math.sin(i * 0.05)))
                          for i in range(int(seconds * sr)))
        w.writeframes(frames)


def user_of(r):
    messages = r["body"].get("messages") or []
    return messages[1]["content"] if len(messages) > 1 else ""


class JsonTests(unittest.TestCase):
    def test_parse_variants(self):
        self.assertEqual(jsonio.parse('{"a": 1}'), {"a": 1})
        self.assertEqual(jsonio.parse("<think>{no}</think>```json\n{\"a\": [1, 2,],}\n```"), {"a": [1, 2]})
        self.assertEqual(jsonio.parse("Here you go: {\"a\": {\"b\": \"}\"}} trailing"), {"a": {"b": "}"}})
        self.assertEqual(jsonio.parse("{'x': 1}" if False else '{"ok": True}'), {"ok": True})
        with self.assertRaises(ValueError):
            jsonio.parse("no json at all")

    def test_coerce(self):
        schema = {"type": "object", "required": ["n", "items"], "properties": {
            "n": {"type": "integer"}, "items": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
            "kind": {"type": "string", "enum": ["album", "ep"]}, "flag": {"type": "boolean"}}}
        value, errors = jsonio.coerce({"n": "7 songs", "items": "one", "kind": "EP", "flag": "yes"}, schema)
        self.assertEqual(errors, [])
        self.assertEqual(value, {"n": 7, "items": ["one"], "kind": "ep", "flag": True})
        _, errors = jsonio.coerce({"items": [1, 2, 3]}, schema)
        self.assertIn("$.n: missing", errors)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(self.tmp)
        self.settings = Settings(self.paths)
        self.secrets = Secrets(self.paths)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def provider(self, fake, **cfg):
        self.settings.update({"provider": dict({"baseUrl": fake.url + "/v1", "textModel": "fake-text",
                                                "fastModel": "fake-fast", "imageModel": "fake-image"}, **cfg)})
        return Provider(self.settings, self.secrets, None, lambda *_: None)

    def test_json_schema_then_fallback_and_key(self):
        fake = FakeProvider(reject_json_schema=True, key="sk-test")
        try:
            self.secrets.set("apiKey", "sk-test")
            self.assertTrue(self.secrets.insecure)  # SM_SECRET_STORE=file in tests
            p = self.provider(fake)
            out = p.json("sys", "Reply with {\"ok\": true}", {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                                             "required": ["ok"]})
            self.assertEqual(out["ok"], True)
            formats = [r["body"].get("response_format", {}).get("type") for r in fake.requests]
            self.assertEqual(formats, ["json_schema", "json_object"])
            self.assertTrue(all(r["auth"] == "Bearer sk-test" for r in fake.requests))
            # The working mode is remembered for this server + model.
            p.json("sys", "Reply with {\"ok\": true}", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
            self.assertEqual(fake.requests[-1]["body"]["response_format"]["type"], "json_object")
        finally:
            fake.close()

    def test_repair_round(self):
        from smcore.builder import ROSTER
        fake = FakeProvider(broken_first=True)
        try:
            p = self.provider(fake)
            out = p.json("sys", "Plan 2 new fictional artists.\n\nData:\n{\"wanted\": 2}", ROSTER)
            self.assertEqual(len(out["artists"]), 2)
            self.assertTrue(fake.requests[-1]["body"]["messages"][-1]["content"].startswith("That reply needs fixing"))
        finally:
            fake.close()

    def test_images_b64_url_and_placeholder(self):
        for mode, source in (("b64", "provider"), ("url", "provider"), ("fail", "placeholder")):
            fake = FakeProvider(images=mode)
            try:
                content, got = self.provider(fake).image("a cover", "seed")
                self.assertEqual(got, source, mode)
                self.assertTrue(content.startswith(b"\x89PNG"))
            finally:
                fake.close()
        content, got = self.provider(FakeProvider(), imagesEnabled=False).image("x", "seed")
        self.assertEqual(got, "placeholder")

    def test_connection_report(self):
        fake = FakeProvider()
        try:
            report = self.provider(fake).test()
            self.assertTrue(report["ok"])
            self.assertIn("fake-text", report["models"])
            self.assertEqual(report["jsonMode"], "json_schema")
        finally:
            fake.close()

    def test_local_server_without_key(self):
        fake = FakeProvider()
        try:
            self.provider(fake).json("s", "Reply with {\"ok\": true}", {"type": "object", "properties": {"ok": {"type": "boolean"}}})
            self.assertIsNone(fake.requests[-1]["auth"])
        finally:
            fake.close()


class Client:
    def __init__(self, port, token):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def call(self, method, path, body=None, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-SM-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        if status != expect:
            raise AssertionError(f"{method} {path}: {status} {raw[:500]!r}")
        return json.loads(raw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body or {}, **kw)

    def put(self, path, body=None, **kw):
        return self.call("PUT", path, body or {}, **kw)

    def wait_job(self, job, timeout=120, ok=True):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = {j["id"]: j for j in self.get("/api/jobs")}
            row = rows.get(job["id"])
            if row and row["status"] in ("finished", "failed", "cancelled"):
                if not ok:
                    return row
                if row["status"] != "finished":
                    raise AssertionError(f"job {row['kind']} {row['status']}: {row.get('error')}")
                return row
            time.sleep(0.2)
        raise AssertionError("job timed out")

    def wait(self, fn, timeout=120, what="condition"):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(0.3)
        raise AssertionError("timed out waiting for " + what)


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg for the engine's m4a output")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.preview = os.path.join(cls.tmp, "preview.wav")
        tone_wav(cls.preview)
        cls.fake = FakeProvider()
        cls.catalog = FakeCatalog(cls.preview)
        cls.app = App(home=os.path.join(cls.tmp, "home"), catalog_base=cls.catalog.url, log=lambda *_: None)
        cls.server, cls.token = serve(cls.app)
        import threading
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.c = Client(cls.server.server_address[1], cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.app.engine.stop()
        cls.server.shutdown()
        cls.fake.close()
        cls.catalog.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_full_app_flow(self):
        c = self.c
        # Unauthorized without the token.
        req = urllib.request.Request(c.base + "/api/state")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

        # Settings: provider, key, engine (mock backend under this Python).
        c.put("/api/settings", {"provider": {"baseUrl": self.fake.url + "/v1", "textModel": "fake-text", "fastModel": "fake-fast",
                                              "imageModel": "fake-image", "maxParallel": 2},
                                 "engine": {"python": sys.executable, "backend": "mock", "options": {"delay": "0.05", "seconds": "2"},
                                            "quality": 8},
                                 "library": {"maxAlbumsPerArtist": 1, "tasteRefreshEvery": 2},
                                 "links": {"mode": "voice", "audioSource": "preview", "analysisSeconds": 2}})
        c.put("/api/secret", {"name": "apiKey", "value": "sk-local"})
        state = c.get("/api/state")
        self.assertTrue(state["providerConfigured"] and state["hasApiKey"])
        self.assertTrue(c.post("/api/provider/test")["ok"])

        # Onboarding against the catalog.
        genres = c.get("/api/catalog/genres")
        self.assertIn("Indian Music", [g["name"] for g in genres])
        artists = c.get("/api/catalog/genre/106")
        self.assertEqual(len(artists), 8)
        reactions = [{"name": a["name"], "reaction": "love"} for a in artists[:2]]
        q = c.post("/api/onboarding/next", {"reactions": reactions, "answers": []})
        self.assertFalse(q["done"])
        self.assertEqual([o["label"] for o in q["options"]], ["Calm", "Lively"])
        self.assertEqual(q["options"][0]["detail"], "slow, gentle")
        self.assertEqual(q["either"], "A bit of both")
        sent = self.fake.requests[-1]["body"]["messages"][-1]["content"]
        self.assertIn("lovedArtists", sent)
        q2 = c.post("/api/onboarding/next", {"reactions": reactions, "answers": [{"q": q["question"], "a": ["90s"]}]})
        self.assertTrue(q2["done"])
        profile = c.post("/api/onboarding/finish", {"reactions": reactions, "answers": [], "notes": "more strings",
                                                    "allowExplicit": False})
        self.assertIn("summary", profile)
        self.assertTrue(c.get("/api/settings")["onboarded"])

        # Build two artists with discographies, covers and portraits.
        job = c.post("/api/create/build", {"brief": "melancholy synth ballads", "count": 2})
        c.wait_job(job)
        home = c.get("/api/home")
        self.assertEqual(home["totals"]["artists"], 2)
        self.assertEqual(home["totals"]["albums"], 2)
        self.assertEqual(home["totals"]["tracks"], 6)
        album = c.get("/api/albums/" + home["shelves"][0]["albums"][0]["id"])
        self.assertTrue(album["cover"].startswith("/media/"))
        self.assertTrue(album["artist"]["portrait"])
        self.assertTrue(album["linerNotes"])
        self.assertTrue(all(t["status"] == "planned" for t in album["tracks"]))

        # Studio: record the album; lyrics get written first.
        c.post("/api/studio/request", {"albumId": album["id"]})
        done = c.wait(lambda: all(t["status"] == "recorded" for t in c.get("/api/albums/" + album["id"])["tracks"]),
                      timeout=180, what="album recording")
        self.assertTrue(done)
        album = c.get("/api/albums/" + album["id"])
        t1 = album["tracks"][0]
        self.assertTrue(t1["lyrics"].startswith("[Verse]"))
        self.assertTrue(t1["style"])
        self.assertAlmostEqual(t1["duration"], 2.0, delta=0.2)
        # Media with Range (seeking).
        req = urllib.request.Request(c.base + t1["audio"], headers={"X-SM-Token": c.token, "Range": "bytes=0-99"})
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(len(r.read()), 100)
            self.assertEqual(r.headers["Content-Type"], "audio/mp4")
        self.assertEqual(c.get("/api/engine/detect")["suggested"] in ("cpu", "cuda", "mps", "rocm-windows"), True)
        self.assertTrue(c.get("/api/studio")["engine"]["capabilities"]["analyze"])

        # Player needs: an unrecorded song is recorded when the player asks for it.
        other = [a for a in c.get("/api/albums") if a["id"] != album["id"]][0]
        other_tracks = c.get("/api/albums/" + other["id"])["tracks"]
        c.post("/api/studio/need", {"trackIds": [other_tracks[0]["id"]]})
        c.wait(lambda: c.get("/api/tracks/" + other_tracks[0]["id"])["status"] == "recorded", what="needed song")
        c.post("/api/studio/need", {"trackIds": []})

        # Favorites and plays shape taste (refresh every 2 signals).
        before = len([r for r in self.fake.requests if "Update the listener" in json.dumps(r["body"])])
        c.post("/api/favorite", {"kind": "album", "id": album["id"], "on": True})
        c.post("/api/play-event", {"trackId": t1["id"], "kind": "play"})
        c.post("/api/play-event", {"trackId": t1["id"], "kind": "complete"})
        c.wait(lambda: len([r for r in self.fake.requests if "Update the listener" in json.dumps(r["body"])]) > before,
               what="taste refresh")
        self.assertEqual(len(c.get("/api/favorites")["albums"]), 1)
        roster_req = [r for r in self.fake.requests if user_of(r).startswith("Plan ")]

        # More like this; a new release for an existing artist; a custom song.
        c.wait_job(c.post("/api/create/build", {"seedArtistId": album["artist"]["id"], "count": 1}))
        last_roster = [r for r in self.fake.requests if user_of(r).startswith("Plan ")][-1]
        self.assertIn("moreLikeThis", user_of(last_roster))
        self.assertIn("favoriteAlbums", user_of(last_roster))
        self.assertGreater(len(roster_req), 0)
        c.wait_job(c.post("/api/create/build", {"artistId": album["artist"]["id"], "output": "album"}))
        self.assertEqual(len(c.get("/api/artists/" + album["artist"]["id"])["albumsList"]), 2)
        custom = c.wait_job(c.post("/api/create/custom", {"title": "My Song", "style": "lo-fi, rain", "lyrics": "[Verse]\nmine"}))
        track = c.get("/api/tracks/" + custom["result"]["trackId"])
        self.assertEqual(track["lyrics"], "[Verse]\nmine")
        self.assertIn(track["status"], ("written", "recording", "recorded"))  # custom songs go straight to the studio
        # Style only: the AI writes the lyrics, the listener's style is kept.
        styled = c.wait_job(c.post("/api/create/custom", {"title": "Words Please", "style": "ghazal, harmonium", "language": "ur"}))
        t2 = c.get("/api/tracks/" + styled["result"]["trackId"])
        self.assertTrue(t2["lyrics"])
        self.assertTrue(t2["style"].startswith("ghazal, harmonium"))

        # Search.
        found = c.get("/api/search?q=" + urllib.parse.quote(album["title"].split()[0]))
        self.assertTrue(found["albums"] or found["tracks"])

        # Links: two pages (page-metadata fallback), audio analysis on catalog previews via the engine.
        made = c.post("/api/links/sets", {"urls": f"{self.fake.url}/page/1\n{self.fake.url}/page/2",
                                          "note": "keep the strings, make it more electronic"})
        c.wait_job(made["job"], timeout=180)
        s = c.get("/api/links/sets/" + made["setId"])
        self.assertEqual([t["position"] for t in s["tracks"]], [1, 2])
        self.assertEqual(s["tracks"][0]["title"], "Song 1 - Some Channel")
        self.assertEqual(s["tracks"][0]["catalog"][0]["genres"], ["Films/Games"])
        self.assertEqual(s["tracks"][0]["analysis"]["model"]["caption"], "mock analysis")
        self.assertTrue(s["tracks"][0]["clip_path"])  # voice mode keeps the clip
        self.assertEqual(len(s["briefs"]), 1)
        brief_req = [r for r in self.fake.requests if user_of(r).startswith("Write the style brief")][-1]
        self.assertIn("keep the strings", user_of(brief_req))
        self.assertIn("rules for using the sources", brief_req["body"]["messages"][0]["content"].lower())
        c.wait_job(c.post(f"/api/links/sets/{made['setId']}/refine", {"instruction": "more like track 2, less sad"}))
        s = c.get("/api/links/sets/" + made["setId"])
        self.assertEqual([b["version"] for b in s["briefs"]], [2, 1])
        refine_req = [r for r in self.fake.requests if user_of(r).startswith("Refine")][-1]
        self.assertIn("previousBrief", user_of(refine_req))
        gen = c.post(f"/api/links/briefs/{s['briefs'][0]['id']}/generate", {"output": "songs", "songCount": 2})
        c.wait_job(gen)
        s = c.get("/api/links/sets/" + made["setId"])
        self.assertEqual(len(s["artists"]), 1)
        self.assertEqual(len(s["albums"]), 1)
        linked = c.get("/api/albums/" + s["albums"][0]["id"])
        self.assertEqual(len(linked["tracks"]), 2)
        c.post("/api/studio/request", {"trackIds": [linked["tracks"][0]["id"]]})
        c.wait(lambda: c.get("/api/tracks/" + linked["tracks"][0]["id"])["status"] == "recorded", what="linked song")
        song_req = [r for r in self.fake.requests if user_of(r).startswith("Write this song")][-1]
        self.assertIn("styleBrief", user_of(song_req))

        # Mashup: one new song from two links. The audio is heard even with the metadata-only default,
        # the AI plans it from the mashup rules, the take records with reference audio built from both clips.
        c.put("/api/settings", {"links": {"mode": "metadata"}})
        one = c.post("/api/links/sets", {"urls": f"{self.fake.url}/page/1", "kind": "mashup"})
        failed = c.wait_job(one["job"], timeout=60, ok=False)
        self.assertIn("at least two", failed["error"])
        mash = c.post("/api/links/sets", {"urls": f"{self.fake.url}/page/1\n{self.fake.url}/page/2", "kind": "mashup",
                                          "note": "the groove of #1 with the vocals of #2"})
        c.wait_job(mash["job"], timeout=180)
        m = c.get("/api/links/sets/" + mash["setId"])
        self.assertEqual(m["mode"], "mashup")
        self.assertTrue(all(t["analysis"]["model"]["caption"] == "mock analysis" for t in m["tracks"]))
        self.assertTrue(all(t["clip_path"] for t in m["tracks"]))
        plan_req = [r for r in self.fake.requests if user_of(r).startswith("Write the mashup plan")][-1]
        self.assertIn("keep it transformative", plan_req["body"]["messages"][0]["content"].lower())
        self.assertIn("mock analysis", user_of(plan_req))
        self.assertEqual(len(m["takes"]), 1)
        take = m["takes"][0]
        self.assertEqual(take["mashup"]["sources"], [{"number": 1, "share": 0.5}, {"number": 2, "share": 0.5}])
        c.wait(lambda: c.get("/api/tracks/" + take["id"])["status"] == "recorded", timeout=120, what="mashup take")
        if shutil.which("ffmpeg"):
            self.assertTrue(os.path.exists(os.path.join(self.app.paths.clips, f"ref-{take['id']}.wav")))
        # Iterating: an instruction gives a revised plan and a new take; the first take stays.
        c.wait_job(c.post(f"/api/links/sets/{mash['setId']}/refine", {"instruction": "more like #1, faster"}), timeout=60)
        m = c.get("/api/links/sets/" + mash["setId"])
        self.assertEqual(len(m["takes"]), 2)
        self.assertEqual(m["takes"][0]["mashup"]["version"], 2)
        self.assertEqual(m["takes"][0]["mashup"]["instruction"], "more like #1, faster")
        self.assertEqual(len(m["albums"]), 1)  # takes collect on one single by one artist
        revise_req = [r for r in self.fake.requests if user_of(r).startswith("Revise the mashup plan")][-1]
        self.assertIn("previousPlan", user_of(revise_req))

        # Share an artist to another library and back in; export audio.
        out = c.post("/api/share/export", {"artistId": album["artist"]["id"], "path": os.path.join(self.tmp, "x.glalbum")})
        self.assertTrue(os.path.exists(out["path"]))
        other = App(home=os.path.join(self.tmp, "home2"), log=lambda *_: None)
        try:
            preview = other.sharing.preview(out["path"])
            self.assertEqual(preview["albums"][0]["recorded"], 3)
            result = other.sharing.import_share(preview["token"])
            self.assertEqual(result["albumsAdded"], 2)
            imported = other.library.artist(result["artistId"])
            self.assertTrue(imported["portrait"])
            self.assertEqual(sum(a["recorded"] for a in imported["albumsList"]), 3)
        finally:
            other.engine.stop()
        exported = c.post("/api/export/audio", {"folder": os.path.join(self.tmp, "export"), "albumIds": [album["id"]]})
        self.assertEqual(exported["written"], 3)
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title,album", "-of", "json",
                                os.path.join(self.tmp, "export", os.listdir(os.path.join(self.tmp, "export"))[0],
                                             os.listdir(os.path.join(self.tmp, "export", os.listdir(os.path.join(self.tmp, "export"))[0]))[0],
                                             sorted(os.listdir(os.path.join(self.tmp, "export", os.listdir(os.path.join(self.tmp, "export"))[0],
                                                                            os.listdir(os.path.join(self.tmp, "export", os.listdir(os.path.join(self.tmp, "export"))[0]))[0])))[0])],
                               capture_output=True, text=True) if shutil.which("ffprobe") else None
        if probe:
            self.assertEqual(json.loads(probe.stdout)["format"]["tags"]["album"], album["title"])

        # Rules are editable and resettable.
        rules = c.get("/api/rules")
        self.assertIn("links", [r["name"] for r in rules])
        c.put("/api/rules/song", {"text": "custom song rules"})
        self.assertTrue([r for r in c.get("/api/rules") if r["name"] == "song"][0]["edited"])
        c.post("/api/rules/song/reset")


import urllib.error  # noqa: E402
import urllib.parse  # noqa: E402

if __name__ == "__main__":
    unittest.main()
