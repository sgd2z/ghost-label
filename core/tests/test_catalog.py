"""Artist search ordering (mechanical only): real photo first, exact name (case/diacritics
ignored) next, then Deezer fan count; Deezer's empty-hash placeholder counts as no photo."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smcore import catalog as catalog_mod  # noqa: E402
from smcore.catalog import Catalog  # noqa: E402

PIC = "https://cdn-images.dzcdn.net/images/artist/{}/250x250-000000-80-0-0.jpg"
# Deezer's /search/artist?q=Imagine Dragons as of 2026-11-10 (trimmed to the fields used).
def _with_bare(a):
    return {**a, "picture": f"https://api.deezer.com/artist/{a['id']}/image"}


IMAGINE = [
    {"id": 416239, "name": "Imagine Dragons", "nb_fan": 9562755, "picture_medium": PIC.format("1ba025c23cae3dee14b51152990285fc")},
    {"id": 412453521, "name": "Imagine Dragons", "nb_fan": 3, "picture_medium": PIC.format("")},
    {"id": 52360042, "name": "Imagine Dragons & K.Flay", "nb_fan": 312, "picture_medium": PIC.format("")},
    {"id": 365222752, "name": "Imagine Red Hot Dragons", "nb_fan": 10, "picture_medium": PIC.format("32a153f21a4dd0d9cd694a2d75440f14")},
    {"id": 270518952, "name": "Imagine Killing Dragons", "nb_fan": 11, "picture_medium": PIC.format("741259c8c872f2ac7aabcad268dc900e")},
    {"id": 15032295, "name": "Kygo & Imagine Dragons", "nb_fan": 3001, "picture_medium": PIC.format("")},
    {"id": 137509522, "name": "Image Dragoons", "nb_fan": 324, "picture_medium": PIC.format("e673b53335101e1036d2294f5162f4fe")},
    {"id": 416239, "name": "Imagine Dragons", "nb_fan": 9562755, "picture_medium": PIC.format("1ba025c23cae3dee14b51152990285fc")},
]


class KV:
    def __init__(self):
        self.d = {}

    def kv_get(self, k):
        return self.d.get(k)

    def kv_set(self, k, v):
        self.d[k] = v


class CatalogOrderTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.orig = catalog_mod.net.get_json
        catalog_mod.net.get_json = lambda url, timeout=15: (self.calls.append(url) or {"data": [_with_bare(a) for a in IMAGINE]})
        self.cat = Catalog(KV(), log=lambda *_: None)

    def tearDown(self):
        catalog_mod.net.get_json = self.orig

    def test_imagine_dragons(self):
        hits = self.cat.search_artists("imagine dragons")
        self.assertEqual(hits[0]["id"], 416239)
        self.assertEqual(hits[0]["picture"], PIC.format("1ba025c23cae3dee14b51152990285fc"))
        self.assertEqual(len([h for h in hits if h["id"] == 416239]), 1)          # duplicates dropped
        names = [h["name"] for h in hits]
        # With photos (by fans), then without photos: exact name first, then by fans.
        self.assertEqual(names, ["Imagine Dragons", "Image Dragoons", "Imagine Killing Dragons", "Imagine Red Hot Dragons",
                                 "Imagine Dragons", "Kygo & Imagine Dragons", "Imagine Dragons & K.Flay"])
        self.assertTrue(all(h["picture"] is None for h in hits[4:]))
        self.assertIn("limit=25", self.calls[0])

    def test_exact_match_ignores_case_and_diacritics(self):
        arts = [{"name": "Beyonce Tribute", "fans": 10, "picture": "p"}, {"name": "BEYONCÉ", "fans": 5, "picture": "p"}]
        self.assertEqual(Catalog.order(arts, "beyoncé")[0]["name"], "BEYONCÉ")
        self.assertEqual(Catalog.order(arts, "Beyonce")[0]["name"], "BEYONCÉ")

    def test_placeholder_detection(self):
        self.assertIsNone(Catalog.real_picture(PIC.format("")))
        self.assertIsNone(Catalog.real_picture("https://api.deezer.com/artist/123/image"[:0]))
        self.assertTrue(Catalog.real_picture(PIC.format("abc")))
        a = Catalog.artist({"id": 1, "name": "x", "picture_medium": PIC.format(""), "picture_xl": PIC.format("abc")})
        self.assertEqual(a["picture"], PIC.format("abc"))
        # The bare `picture` field is a redirect that can't tell a placeholder from a photo.
        b = Catalog.artist({"id": 2, "name": "y", "picture": "https://api.deezer.com/artist/2/image", "picture_medium": PIC.format("")})
        self.assertIsNone(b["picture"])


if __name__ == "__main__":
    unittest.main()
