"""Mashups of the actual songs: their real lyrics come from LRCLIB (exact signature, then search,
closest duration) or, failing that, the video's own captions; they're stored per source track,
editable, fetched again on request, fetched only for sets that ask for them, and handed to the
mashup planner as sourceLyrics."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SM_SECRET_STORE"] = "file"
os.environ["GHOSTLABEL_NO_RUNTIME_CHECK"] = "1"

from smcore.sourcelyrics import LyricsFinder, lrc_to_text, query_of, vtt_to_text  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "auto_captions.hi.vtt")

RECORDS = [
    {"id": 1, "trackName": "Mirrors", "artistName": "Zara Kaur", "albumName": "Mirrors & Motion", "duration": 201,
     "instrumental": False, "plainLyrics": "Mirror, mirror on my wall\nTell me I can have it all",
     "syncedLyrics": "[00:12.10] Mirror, mirror on my wall\n[00:15.80] Tell me I can have it all"},
    {"id": 2, "trackName": "Mirrors (Live)", "artistName": "Zara Kaur", "albumName": "Live", "duration": 260,
     "instrumental": False, "plainLyrics": "live version", "syncedLyrics": None},
    {"id": 3, "trackName": "Motion", "artistName": "Naveen", "albumName": "", "duration": 180,
     "instrumental": True, "plainLyrics": None, "syncedLyrics": None},
    {"id": 4, "trackName": "Drift", "artistName": "Naveen", "albumName": "", "duration": 240,
     "instrumental": True, "plainLyrics": "[ocean sounds]", "syncedLyrics": None},
    {"id": 5, "trackName": "Mirrors", "artistName": "Someone Else", "albumName": "", "duration": 200,
     "instrumental": False, "plainLyrics": "a different song with the same title", "syncedLyrics": None},
]


class FakeLRCLIB:
    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
                outer.requests.append((url.path, q, self.headers.get("User-Agent")))
                if url.path == "/api/get":
                    hits = [r for r in RECORDS if r["trackName"].lower() == q.get("track_name", "").lower()
                            and r["artistName"].lower() == q.get("artist_name", "").lower()
                            and (not q.get("duration") or abs(r["duration"] - int(q["duration"])) <= 2)]
                    return self.reply(200, hits[0]) if hits else self.reply(404, {"statusCode": 404, "name": "TrackNotFound"})
                if url.path == "/api/search":
                    words = (q.get("q") or q.get("track_name") or "").lower()
                    hits = [r for r in RECORDS if r["trackName"].lower().split(" (")[0] in words
                            or words in r["trackName"].lower()]
                    return self.reply(200, hits)
                self.reply(404, {})

            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Settings:
    def __init__(self, url):
        self.url = url

    def section(self, name):
        return {"lrclibUrl": self.url} if name == "links" else {}


class CaptionFetcher:
    """Stands in for yt-dlp: the video's info lists caption tracks; the chosen one is the fixture."""

    def __init__(self, auto=None, subs=None):
        self.auto, self.subs, self.asked = auto or {}, subs or {}, []

    def info(self, url, flat):
        return {"automatic_captions": self.auto, "subtitles": self.subs, "language": None}

    def captions(self, url, language, automatic):
        self.asked.append((language, automatic))
        with open(FIXTURE, encoding="utf-8") as f:
            return f.read()


class ParsingTests(unittest.TestCase):
    def test_captions_become_plain_lines_without_timing_repeats_or_cues(self):
        with open(FIXTURE, encoding="utf-8") as f:
            text = vtt_to_text(f.read())
        self.assertEqual(text.splitlines(), ["तुम ही हो अब तुम ही हो", "ज़िन्दगी अब तुम ही हो", "चैन भी, मेरा दर्द भी"])

    def test_lrc_timestamps_and_tags_removed(self):
        self.assertEqual(lrc_to_text("[ar: X]\n[00:12.10] One\n[00:15.80][00:40.00] Two"), "One\nTwo")

    def test_query_uses_link_metadata(self):
        q = query_of({"title": "Zara Kaur - Mirrors (Official Video)", "channel": "Zara Kaur - Topic", "track": "Mirrors",
                      "duration": 203})
        self.assertEqual((q["artist"], q["track"], q["duration"]), ("Zara Kaur", "Mirrors", 203.0))


class FinderTests(unittest.TestCase):
    def setUp(self):
        self.lrc = FakeLRCLIB()
        self.addCleanup(self.lrc.close)

    def finder(self, fetcher=None):
        return LyricsFinder(Settings(self.lrc.url), fetcher or CaptionFetcher(), lambda *a: None)

    def test_exact_signature_first_and_names_the_client(self):
        out = self.finder().find({"artist": "Zara Kaur", "track": "Mirrors", "duration": 202}, "https://v")
        self.assertEqual((out["status"], out["source"], out["synced"]), ("found", "lrclib", True))
        self.assertIn("Tell me I can have it all", out["text"])
        path, q, agent = self.lrc.requests[0]
        self.assertEqual((path, q["duration"]), ("/api/get", "202"))
        self.assertIn("GhostLabel", agent)

    def test_search_picks_the_closest_duration(self):
        # A video a little longer than the record; the live version is much longer.
        out = self.finder().find({"title": "Zara Kaur – Mirrors [video]", "duration": 214}, "https://v")
        self.assertEqual(out["detail"]["lrclibId"], 1)

    def test_too_far_from_any_duration_falls_back_to_captions(self):
        fetcher = CaptionFetcher(auto={"hi-orig": [{}], "en": [{}]})
        out = self.finder(fetcher).find({"title": "Mirrors", "duration": 500}, "https://v")
        self.assertEqual((out["status"], out["source"], out["language"]), ("found", "youtube-captions", "hi"))
        self.assertEqual(fetcher.asked, [("hi-orig", True)])     # the original-language auto captions
        self.assertTrue(out["text"].startswith("तुम ही हो"))

    def test_uploaded_subtitles_in_the_song_language_win(self):
        fetcher = CaptionFetcher(auto={"hi-orig": [{}]}, subs={"en": [{}], "hi": [{}]})
        self.finder(fetcher).find({"title": "Nothing here", "language": "hi"}, "https://v")
        self.assertEqual(fetcher.asked, [("hi", False)])

    def test_lrclib_instrumental_flag_wins_over_any_text(self):
        out = self.finder().find({"artist": "Naveen", "track": "Drift", "duration": 240}, "u")
        self.assertEqual(out["status"], "instrumental")
        self.assertNotIn("text", out)

    def test_search_needs_title_and_artist_to_match(self):
        # Same title, a different artist, closest duration: not this song.
        out = self.finder(CaptionFetcher()).find({"title": "Mirrors (Official Video)", "channel": "Someone Else",
                                                  "duration": 200}, "https://v")
        self.assertEqual(out["detail"]["lrclibId"], 5)
        out = self.finder(CaptionFetcher()).find({"title": "Mirrors (Official Video)", "channel": "Random Uploads",
                                                  "duration": 200}, "https://v")
        self.assertEqual(out["status"], "none")

    def test_instrumental_and_none_found(self):
        self.assertEqual(self.finder().find({"artist": "Naveen", "track": "Motion", "duration": 180}, "u")["status"],
                         "instrumental")
        out = self.finder(CaptionFetcher()).find({"title": "Unknown song"}, "https://v")
        self.assertEqual(out["status"], "none")


class LinksTests(unittest.TestCase):
    def setUp(self):
        from smcore.app import App
        self.lrc = FakeLRCLIB()
        self.addCleanup(self.lrc.close)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app = App(home=os.path.join(self.tmp, "home"), log=lambda *a: None)
        self.addCleanup(self.app.engine.stop)
        self.app.settings.update({"links": {"lrclibUrl": self.lrc.url}})
        self.app.links.fetcher.info = lambda url, flat: {"automatic_captions": {"hi-orig": [{}]}}
        self.app.links.fetcher.captions = lambda url, lang, automatic: open(FIXTURE, encoding="utf-8").read()

    def make_set(self, real):
        db = self.app.db
        db.insert("source_sets", {"id": "ss1", "mode": "mashup", "status": "ready", "options_json": {"realLyrics": real} if real else {},
                                  "created": time.time()})
        db.insert("source_tracks", {"id": "st1", "set_id": "ss1", "position": 1, "url": "https://v/1", "title": "Mirrors",
                                    "metadata_json": {"artist": "Zara Kaur", "track": "Mirrors", "duration": 201}})
        db.insert("source_tracks", {"id": "st2", "set_id": "ss1", "position": 2, "url": "https://v/2", "title": "Tum Hi Ho",
                                    "metadata_json": {"title": "Tum Hi Ho (Lyric Video)", "duration": 262}})

    def plan_data(self):
        seen = {}

        def json_call(system, user, schema, name=None, fast=False):
            seen["data"] = json.loads(user.split("Data:\n", 1)[1])
            seen["task"] = user.split("\n", 1)[0]
            return {"title": "T", "summary": "s", "per_track": [], "artist": {"name": "A", "voice": "v", "vocal_tags": "male vocals"},
                    "songs": [{"title": "S", "caption": "pop", "lyrics": "[Verse]\nMirror, mirror", "sources": [],
                               "lyrics_credits": [{"section": "[Verse]", "sources": [1]}]}]}
        self.app.provider.json = json_call
        brief = self.app.links.write_mashup("ss1")
        return seen, brief

    def test_lyrics_stored_edited_and_given_to_the_planner(self):
        self.make_set(real=True)
        self.assertEqual(self.app.links.fetch_lyrics("st1")["source"], "lrclib")
        second = self.app.links.fetch_lyrics("st2")
        self.assertEqual((second["source"], second["language"]), ("youtube-captions", "hi"))
        self.assertEqual(self.app.links.source_lyrics("ss1")[1]["captions"], "automatic")   # the planner can tell
        s = self.app.links.get("ss1")
        self.assertEqual([t["lyrics"]["status"] for t in s["tracks"]], ["found", "found"])
        self.app.links.save_lyrics("st2", "तुम ही हो\nज़िन्दगी अब तुम ही हो")
        self.assertTrue(self.app.links.lyrics_of("st2")["edited"])
        seen, brief = self.plan_data()
        self.assertEqual(seen["data"]["lyricsMode"], "real")
        self.assertIn("real lyrics", seen["task"])
        by_number = {x["number"]: x for x in seen["data"]["sourceLyrics"]}
        self.assertIn("Mirror, mirror on my wall", by_number[1]["lyrics"])
        self.assertEqual(by_number[2]["lyrics"], "तुम ही हो\nज़िन्दगी अब तुम ही हो")
        self.assertTrue(by_number[2]["edited"])
        self.assertIn("automatic speech recognition", self.app.rules.system("mashup"))
        rules = self.app.rules.system("mashup")
        self.assertIn("sourceLyrics", rules)
        self.assertIn("lyrics_credits", rules)
        made = self.app.links.make_mashup(brief["id"])
        track = self.app.db.one("SELECT data_json FROM tracks WHERE id = ?", (made["trackIds"][0],))
        self.assertEqual(track["data"]["mashup"]["lyricsCredits"], [{"section": "[Verse]", "sources": [1]}])

    def test_fetch_again_is_a_job(self):
        self.make_set(real=True)
        job = self.app.links.refetch_lyrics("st1")
        self.assertEqual(job["kind"], "lyrics-fetch")
        deadline = time.time() + 10
        while self.app.jobs.running and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.app.links.lyrics_of("st1")["status"], "found")

    def test_sets_without_the_option_never_fetch_or_send_lyrics(self):
        self.make_set(real=False)
        seen, _ = self.plan_data()
        self.assertNotIn("sourceLyrics", seen["data"])
        self.assertNotIn("lyricsMode", seen["data"])
        self.assertNotIn("lyrics", self.app.links.get("ss1")["tracks"][0])
        self.assertEqual(self.lrc.requests, [])

    def test_create_keeps_the_option_only_for_mashups(self):
        self.app.links.analyse = lambda job, set_id, urls: {}
        a = self.app.links.create(["https://v/1", "https://v/2"], kind="mashup", real_lyrics=True)
        b = self.app.links.create(["https://v/1"], mode="metadata", real_lyrics=True)
        self.assertTrue(self.app.links.real_lyrics(a["setId"]))
        self.assertFalse(self.app.links.real_lyrics(b["setId"]))


if __name__ == "__main__":
    unittest.main()
