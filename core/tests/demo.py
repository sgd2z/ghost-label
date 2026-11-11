"""A local demo: a fake OpenAI-compatible server that writes plausible (canned) music text, a fake
Deezer-like catalog, and the mock engine. Nothing leaves this computer.

    python tests/demo.py [--port 8765] [--home /tmp/sm-demo] [--ui ../app/dist/renderer] [--seed]

--seed fills the library (onboarding done, a few artists with albums, one link set) so every
view has something to show. Prints the URL to open."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from fakes import FakeCatalog, FakeProvider, example  # noqa: E402
from smcore import builder, links, taste  # noqa: E402

NAMES = ["Saffron Static", "The Velvet Monsoon", "Nadia Qureshi", "Harbor Lights Orchestra", "Kites Over Lahore",
         "Midnight Rickshaw", "Lumen & Ash", "Tara Venkatesh", "Paper Satellites", "The Glasshouse Choir",
         "Ruhaan", "Cobalt Gardens", "Meera & the Night Bus", "Second Sunrise", "Dust Radio", "Okra Moon"]
TITLES = ["Neon Monsoon", "Letters to Karachi", "Slow Satellites", "Rooftop Chai", "Gold Hour", "Paper Boats",
          "The Long Drive Home", "Saawan", "Static Bloom", "Windows in the Rain", "Afterglow", "Small Hours",
          "Signal Fires", "Chandni", "Low Tide", "Velvet Room", "Northern Line", "Monsoon Radio", "Glass Hearts",
          "Echo Park", "Mehfil", "City of Kites", "Blue Hour", "Lanterns"]
GENRES = [["dream pop", "shoegaze"], ["indie soul", "neo-soul"], ["ghazal", "lo-fi"], ["synthwave", "city pop"],
          ["folk", "chamber pop"], ["trip-hop", "downtempo"], ["Punjabi folk", "electronic"], ["jazz", "R&B"]]
LYRICS = {
    "en": "[Intro]\n\n[Verse 1]\nStreetlights hum a song I used to know\nYour name is written in the window glow\nWe were young and the city was slow\nNow the trains don't stop where we used to go\n\n"
          "[Chorus]\nHold on to the static, hold on to the light\nEvery little signal brings you home tonight\nHold on to the static, don't let it fade\nWe're the only sound the radio made\n\n"
          "[Verse 2]\nPaper boats on a river of rain\nI fold your letters and I try again\nEvery corner keeps a little of the pain\nBut I'd walk them all to hear you say my name\n\n"
          "[Bridge]\nTurn it up, turn it up\nLet the night come in\n\n[Chorus]\nHold on to the static, hold on to the light\nEvery little signal brings you home tonight\n\n[Outro]",
    "ur": "[Verse 1]\nShaam dhali hai, teri yaad aayi\nChaand ne phir se baat chalaayi\nKhidki pe baarish, dil mein tanhaai\nTere bina yeh raat paraayi\n\n"
          "[Chorus]\nAa bhi jaa, ke mausam bulaaye\nDil ki galiyon mein deep jalaaye\nAa bhi jaa, ke raat na jaaye\n\n[Instrumental]\n\n[Verse 2]\nKaagaz ki kashti, paani ka rasta\nTera fasaana, mera vaasta\n\n[Chorus]\nAa bhi jaa, ke mausam bulaaye\n\n[Outro]",
}
rng = random.Random(7)
_release_titles = iter(rng.sample(TITLES, len(TITLES)) + [f"Volume {i}" for i in range(2, 200)])


def pick(seq):
    return seq[rng.randrange(len(seq))]


def content(schema, data, user):
    value = example(schema, data=data)
    if schema is builder.ROSTER:
        n = int(data.get("wanted") or 1)
        used = set(data.get("existingArtistNames") or [])
        free = [x for x in NAMES if x not in used] or [f"Act {time.time():.0f}"]
        value = {"artists": [{"name": free[i % len(free)] + ("" if i < len(free) else f" {i}"),
                              "concept": "A late-night project that blends warm analog synths with South Asian melodies.",
                              "relation": ["core", "adjacent", "wildcard"][i % 3], "genres": pick(GENRES), "draws_on": [1]}
                             for i in range(n)]}
    elif schema is builder.DOSSIER:
        g = pick(GENRES)
        value = {"name": data.get("concept", {}).get("name") if isinstance(data.get("concept"), dict) else None,
                 "bio": "Formed in a rented rehearsal room above a sweet shop, they spent two winters recording on a borrowed tape machine. "
                        "Their first single spread hand to hand on burned CDs before anyone knew their faces.\n\n"
                        "On stage they keep the lights low and the songs long; the records are quieter, built around a voice that sounds like it is telling you a secret.",
                 "soul": "Songs about distance, homesickness and the small rituals that keep people close.",
                 "voice": pick(["breathy female alto, close-mic'd", "warm baritone with a soft rasp", "airy falsetto, layered harmonies"]),
                 "origin": pick(["Lahore", "Bristol", "Mumbai", "Toronto", "Osaka", "Glasgow"]), "formed": str(rng.randint(1994, 2021)),
                 "genres": g, "members": [{"name": "A. Rahman", "role": "vocals"}, {"name": "J. Okafor", "role": "keys, production"}],
                 "visual": "grainy film, sodium streetlights, rain on glass", "languages": ["en"]}
        value["name"] = value["name"] or (data.get("artist") or {}).get("name") or pick(NAMES)
    elif schema is builder.DISCOGRAPHY:
        value = {"releases": [{"title": next(_release_titles), "year": rng.randint(2012, 2025), "kind": "album",
                               "concept": "A record about leaving home and finding it again in other people."}]}
    elif schema is builder.ALBUM:
        n = int(data.get("songCount") or 6)
        titles = rng.sample(TITLES, min(n, len(TITLES)))
        value = {"liner_notes": "Recorded over eleven nights with the windows open, so you can hear the city underneath. "
                                "The strings were tracked in a single take; we kept the mistakes.\n\nFor everyone who ever waited at a bus stop in the rain.",
                 "cover_brief": "a rain-streaked window at night, neon reflections",
                 "tracks": [{"title": t, "theme": pick(["missing someone across an ocean", "a night drive", "first monsoon rain", "an old radio station", "a rooftop party"]),
                             "instrumental": i == n - 1 and n > 4, "language": "ur" if i == 2 else "en", "seconds": rng.choice([150, 180, 200, 210])}
                            for i, t in enumerate(titles)]}
    elif schema is builder.SONG:
        lang = ((data.get("song") or {}).get("language")) or "en"
        value = {"style": pick(["dreamy indie pop, warm analog synths, brushed drums, breathy female vocal, 92 bpm",
                                "lo-fi ghazal, harmonium, tabla, soft rhodes, intimate male vocal",
                                "synthwave, gated drums, chorus guitar, nostalgic, airy vocals"]),
                 "lyrics": LYRICS.get(lang, LYRICS["en"]), "language": lang, "bpm": rng.choice([72, 88, 96, 104]), "key": "A minor", "explicit": False}
    elif schema is builder.ART:
        value = {"prompt": "cover art, " + user[-120:]}
    elif schema is taste.QUESTION:
        if not data.get("answersSoFar"):
            value = {"done": False, "question": "When you put music on at night, what's it for?",
                     "options": [{"label": "Focusing"}, {"label": "Feeling something"}, {"label": "Driving"}, {"label": "Dancing"}],
                     "multiple": True}
        elif len(data["answersSoFar"]) == 1:
            value = {"done": False, "question": "Do you prefer singing or instrumental music?",
                     "options": [{"label": "Singing"}, {"label": "Instrumental"}], "either": "Either is fine"}
        else:
            value = {"done": True}
    elif schema is taste.PROFILE:
        value = {"summary": "Late-night, melodic and a little wistful — South Asian melodies meeting indie and soul.",
                 "loves": "intimate vocals, warm keys, songs about distance", "likes": "slow-burning electronica, ghazal phrasing",
                 "avoids": "aggressive drops, novelty songs", "genres": ["indie pop", "neo-soul", "ghazal", "dream pop"],
                 "eras": ["2010s", "now"], "moods": ["wistful", "warm"], "vocals": "close, breathy", "languages": ["en", "ur", "hi"],
                 "exploration": "open to adjacent scenes"}
    elif schema is links.BRIEF:
        tracks = data.get("sourceTracks") or []
        value = {"title": "Monsoon Static" if not data.get("instruction") else "Monsoon Static (more electronic)",
                 "summary": "Rain-soaked, mid-tempo songs where a ghazal-like vocal line floats over lo-fi electronic production.",
                 "shared": "Slow tempos (70–95 bpm), minor keys, close-mic'd vocals and a lot of reverb; every track leans on a single repeating keyboard figure.",
                 "per_track": [{"number": t["number"], "contributes": pick(["the vocal phrasing and Urdu lyrics", "the dusty drum machine groove", "the harmonium drones", "the bittersweet chord changes"])} for t in tracks],
                 "interpretation": "Your note asks to keep the strings and push the production toward electronic: strings stay as pads, the drums become programmed.",
                 "language_guidance": "Mostly Urdu with English hooks; the music model supports both (ur, en).",
                 "plan": [{"kind": "artist", "idea": "Kites Over Lahore", "description": "A duo making late-night Urdu electronica with live strings.", "draws_on": [1, 2], "blend": "vocals of #1, beats of #2"},
                          {"kind": "release", "idea": "Barsaat EP", "description": "A five-song EP for the first rain.", "draws_on": [1]}]}
    return value


def call(base, token, method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-SM-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"null")


def wait_job(base, token, job, timeout=300):
    end = time.time() + timeout
    while time.time() < end:
        j = next((x for x in call(base, token, "GET", "/api/jobs") if x["id"] == job["id"]), None)
        if j and j["status"] in ("finished", "failed", "cancelled"):
            return j
        time.sleep(0.3)
    raise TimeoutError(job)


def run(args):
    os.makedirs(args.home, exist_ok=True)
    preview = os.path.join(args.home, "preview.wav")
    write_tone(preview)
    provider = FakeProvider(content=content, image_size=512)
    catalog = FakeCatalog(preview)
    os.environ["SM_CATALOG_BASE"] = catalog.url
    from smcore.app import App
    from smcore.server import serve
    app = App(home=args.home, engine_dir=os.path.join(HERE, "..", "..", "engine"))
    server, token = serve(app, port=args.port, token=args.token, ui_dir=os.path.abspath(args.ui))
    import threading
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    call(base, token, "PUT", "/api/settings", {
        "provider": {"baseUrl": provider.url + "/v1", "textModel": "demo-large", "fastModel": "demo-small", "imageModel": "demo-image", "maxParallel": 3},
        "engine": {"python": sys.executable, "backend": "mock", "options": {"delay": "0.15", "seconds": "45"}, "quality": 8, "profile": "mock"},
        "links": {"mode": "metadata", "audioSource": "preview", "analysisSeconds": 5}})
    call(base, token, "PUT", "/api/secret", {"name": "apiKey", "value": "sk-demo"})
    if args.seed and not call(base, token, "GET", "/api/state")["totals"]["artists"]:
        call(base, token, "POST", "/api/onboarding/finish", {"reactions": [{"name": "Real Artist 1", "reaction": "love"}], "answers": [], "notes": "Urdu and Hindi welcome"})
        wait_job(base, token, call(base, token, "POST", "/api/create/build", {"count": 4, "output": "artists", "songCount": 6}))
        made = call(base, token, "POST", "/api/links/sets", {"urls": f"{provider.url}/page/1\n{provider.url}/page/2\n{provider.url}/page/3",
                                                             "note": "the vocals of #1 with the beats of #2, sung in Urdu"})
        wait_job(base, token, made["job"])
        albums = call(base, token, "GET", "/api/albums")
        call(base, token, "POST", "/api/favorite", {"kind": "album", "id": albums[0]["id"], "on": True})
        call(base, token, "POST", "/api/studio/request", {"albumId": albums[0]["id"]})
    print(f"DEMO READY {base}/?token={token}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        app.engine.stop()


def write_tone(path, seconds=3):
    import math
    import struct
    import wave
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 330 * i / 22050))) for i in range(22050 * seconds)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--home", default="/tmp/sm-demo")
    p.add_argument("--ui", default=os.path.join(HERE, "..", "..", "app", "dist", "renderer"))
    p.add_argument("--seed", action="store_true")
    p.add_argument("--token", default="demo")
    run(p.parse_args())
