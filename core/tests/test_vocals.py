"""The singer reaches the music model: vocal tags written once per artist and always in the caption."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore.builder import Builder, with_vocals  # noqa: E402
from smcore.db import Database  # noqa: E402


class Provider:
    def __init__(self):
        self.calls = []

    def configured(self):
        return True

    def json(self, system, user, schema, name=None, fast=False):
        self.calls.append((name, user))
        return {"vocal_tags": "female vocals, breathy soprano, intimate"}


class Rules:
    def system(self, name):
        return name


class VocalTests(unittest.TestCase):
    def test_with_vocals_is_mechanical(self):
        self.assertEqual(with_vocals("bollywood pop, sitar", "female vocals, breathy soprano"),
                         "bollywood pop, sitar, female vocals, breathy soprano")
        self.assertEqual(with_vocals("pop, FEMALE VOCALS, breathy soprano", "female vocals, breathy soprano"),
                         "pop, FEMALE VOCALS, breathy soprano")
        self.assertEqual(with_vocals("pop,", ""), "pop")

    def test_tags_are_written_once_from_the_voice_line_and_kept(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        db = Database(os.path.join(tmp, "lib.db"))
        db.insert("artists", {"id": "ar1", "name": "Zara Kaur", "voice": "A warm, breathy soprano with a Mumbai lilt",
                              "bio": "A singer from Mumbai.", "created": time.time()})
        provider = Provider()
        b = Builder(db, None, None, provider, Rules(), None, None, log=lambda *_: None)
        self.assertEqual(b.vocal_tags("ar1"), "female vocals, breathy soprano, intimate")
        self.assertIn("breathy soprano with a Mumbai lilt", provider.calls[0][1])
        self.assertEqual(b.vocal_tags("ar1"), "female vocals, breathy soprano, intimate")
        self.assertEqual(len(provider.calls), 1)  # kept with the artist: same singer on every song
        self.assertEqual(db.one("SELECT data_json FROM artists WHERE id='ar1'")["data"]["vocalTags"],
                         "female vocals, breathy soprano, intimate")


if __name__ == "__main__":
    unittest.main()
