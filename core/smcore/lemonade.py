"""AMD Lemonade Server as the music engine (Settings › Music engine › Backend: Lemonade).

The songs themselves are made by the engine host's `lemonade` backend (engine/engine_host/
backends/lemonade.py); this module is core's side: which server and key to use, the "Test music
engine" button, and asking Lemonade to unload models so the GPU memory goes where it's needed."""

from __future__ import annotations

import urllib.parse

from smcore import net

DEFAULT_URL = "http://localhost:13305/v1"
DEFAULT_MODEL = "ACE-Step-Music"
PULL = "lemonade pull ACE-Step-Music"


def _norm(url):
    url = (url or "").strip().rstrip("/")
    if url and urllib.parse.urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


# Lemonade deploys a model in one mode, named by its labels ("chat" first, then the first mode
# label); a model whose labels name none takes its recipe's default mode. Mirrors Lemonade's
# find_deployment_mode() (include/lemon/model_types.h) and each backend's supported_modes.
MODE_LABELS = {"embeddings": "embedding", "embedding": "embedding", "reranking": "reranking",
               "transcription": "transcription", "image": "image", "tts": "tts",
               "audio-generation": "audio-generation", "classification": "classification",
               "classifier": "classification", "3d": "3d"}
RECIPE_MODES = {"sd-cpp": "image", "thenoise": "image", "acestep": "audio-generation",
                "thinksound": "audio-generation", "openmoss": "tts", "kokoro": "tts",
                "whispercpp": "transcription", "moonshine": "transcription", "trellis": "3d",
                "onnxruntime": "classification"}


def mode(entry) -> str:
    """What a Lemonade model entry serves: chat, image, audio-generation, embedding, …
    Upscalers carry the image label too but only enlarge images, so they count as upscaling."""
    labels = [str(x) for x in (entry.get("labels") or [])]
    if "upscaling" in labels:
        return "upscaling"
    if "chat" in labels:
        return "chat"
    for label in labels:
        if label in MODE_LABELS:
            return MODE_LABELS[label]
    return RECIPE_MODES.get(entry.get("recipe") or "", "chat")


def is_lemonade(listing) -> bool:
    """A /models answer from Lemonade: its entries carry `recipe` (OpenAI's and others' don't)."""
    data = (listing or {}).get("data") or []
    return any(isinstance(m, dict) and "recipe" in m for m in data)


def inventory(base, headers, first=None, timeout=20) -> dict:
    """Everything a Lemonade server can use now, from its own answers:
    - GET /models lists only models whose `downloaded` flag is set;
    - GET /models?show_all=true lists the whole catalog with that flag;
    - GET /health lists what is loaded (a loaded model is usable even if a list misses it).
    `first`: an already fetched plain /models answer. Non-Lemonade servers: {"lemonade": False}."""
    listing = first if first is not None else net.request("GET", base + "/models", headers=headers, timeout=timeout)
    if not is_lemonade(listing):
        return {"lemonade": False, "listing": listing}
    entries = {m["id"]: m for m in listing.get("data") or [] if isinstance(m, dict) and m.get("id")}
    try:
        catalog = net.request("GET", base + "/models?show_all=true", headers=headers, timeout=timeout)
        for m in catalog.get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                entries.setdefault(m["id"], m)
    except Exception:  # noqa: BLE001 — the plain list is enough
        pass
    loaded = {}
    try:
        health = net.request("GET", base + "/health", headers=headers, timeout=timeout)
        for m in health.get("all_models_loaded") or []:
            if isinstance(m, dict) and m.get("model_name"):
                loaded[m["model_name"]] = m
        if health.get("model_loaded") and health["model_loaded"] not in loaded:
            loaded[health["model_loaded"]] = {"model_name": health["model_loaded"]}
    except Exception:  # noqa: BLE001
        pass
    usable = [i for i, m in entries.items() if m.get("downloaded") is not False or i in loaded]
    usable += [i for i in loaded if i not in entries]
    return {"lemonade": True, "entries": entries, "loaded": loaded, "usable": usable,
            "by_mode": lambda wanted: [i for i in usable if (mode(entries[i]) if i in entries
                                                              else _loaded_mode(loaded[i])) == wanted]}


def _loaded_mode(m):
    """Mode of a loaded model that no list entry describes, from /health's `type` (then recipe)."""
    kind = m.get("type") or ""
    known = {"llm": "chat", "vlm": "chat", "chat": "chat", "image": "image", "audio-generation": "audio-generation",
             "embedding": "embedding", "reranking": "reranking", "transcription": "transcription", "tts": "tts"}
    return known.get(kind) or RECIPE_MODES.get(m.get("recipe") or "", "chat")


def check_model(inv, name, wanted) -> dict:
    """Is `name` usable as a `wanted` model on this Lemonade? {ok, loaded, error}."""
    entry = inv["entries"].get(name)
    loaded = name in inv["loaded"]
    out = {"ok": False, "loaded": loaded}
    if entry is None and not loaded:
        others = inv["by_mode"](wanted)
        out["error"] = (f"{name} isn't on this Lemonade server. Pull it: lemonade pull {name}"
                        + (f". {wanted.capitalize()} models there: {', '.join(others[:12])}" if others
                           else f". No {wanted} models found there."))
        return out
    kind = mode(entry) if entry else _loaded_mode(inv["loaded"][name])
    if kind != wanted:
        an = lambda word: ("an " if word[:1] in "aeiou" else "a ") + word  # noqa: E731
        out["error"] = f"{name} is {an(kind)} model on this server, not {an(wanted)} model."
        return out
    if entry is not None and entry.get("downloaded") is False and not loaded:
        out["error"] = f"Lemonade knows {name} but hasn't downloaded it. In a terminal run: lemonade pull {name}"
        return out
    out["ok"] = True
    if entry is not None and entry.get("downloaded") is False:
        out["warning"] = f"{name} is loaded, though Lemonade's model list marks it as not downloaded."
    return out


def selected(settings) -> bool:
    eng = settings.section("engine")
    return eng.get("kind") != "custom" and eng.get("backend") == "lemonade"


def server(settings, secrets=None):
    """(API base, key, where the URL came from). The override wins; else the AI provider's base URL
    (Lemonade as the chat server), else Lemonade's default address. The key is the one saved for
    that same server, never one meant for another."""
    eng, prov = settings.section("engine"), settings.section("provider")
    chat, images = _norm(prov.get("baseUrl")), _norm(prov.get("imagesBaseUrl"))
    url, source = _norm(eng.get("lemonadeUrl")), "Music engine setting"
    if not url:
        url, source = (chat, "AI provider base URL") if chat else (DEFAULT_URL, "Lemonade's default")
    key = ""
    if secrets is not None:
        if url == chat:
            key = secrets.get("apiKey") or ""
        elif url == images:
            key = secrets.get("imagesApiKey") or ""
    return url, key, source


def model(settings):
    return (settings.section("engine").get("lemonadeModel") or "").strip() or DEFAULT_MODEL


def _headers(key):
    return {"Authorization": f"Bearer {key}"} if key else {}


def _error_text(error):
    import json
    try:
        payload = json.loads(getattr(error, "body", "") or "")
        err = payload.get("error")
        return (err.get("message") if isinstance(err, dict) else str(err)) or str(error)
    except (ValueError, AttributeError):
        return str(error)


def test(settings, secrets=None) -> dict:
    """The "Test music engine" button: is Lemonade up, and is the music model there (downloaded or
    loaded) as an audio-generation model. Generates nothing."""
    url, key, source = server(settings, secrets)
    name = model(settings)
    out = {"ok": False, "url": url, "urlSource": source, "model": name, "pull": PULL.replace(DEFAULT_MODEL, name),
           "running": False, "downloaded": False, "loaded": False}
    try:
        health = net.request("GET", url + "/health", headers=_headers(key), timeout=10)
        out["running"] = True
        out["version"] = health.get("version")
    except net.HTTPError as error:
        out["error"] = ("Lemonade wants an API key: save it as the AI provider key (same server)."
                        if error.status == 401 else f"Lemonade answered {error.status}: {_error_text(error)[:200]}")
        return out
    except Exception as error:  # noqa: BLE001 — not running / wrong address
        out["error"] = f"Nothing answered at {url} ({error}). Start Lemonade Server, or set the Lemonade URL."
        return out
    try:
        inv = inventory(url, _headers(key))
    except Exception as error:  # noqa: BLE001
        out["error"] = f"Lemonade didn't list its models: {str(error)[:200]}"
        return out
    if not inv["lemonade"]:
        out["error"] = f"The server at {url} answers, but it isn't Lemonade (its model list has no recipes)."
        return out
    out["loadedModels"] = list(inv["loaded"])
    out["musicModels"] = inv["by_mode"]("audio-generation")
    entry = inv["entries"].get(name) or {}
    out.update(recipe=entry.get("recipe"), labels=entry.get("labels") or [], sizeGb=entry.get("size"))
    check = check_model(inv, name, "audio-generation")
    out["loaded"] = check["loaded"]
    out["downloaded"] = check["ok"]
    if check.get("warning"):
        out["warning"] = check["warning"]
    if not check["ok"]:
        out["error"] = check["error"]
        return out
    out["ok"] = True
    return out


def unload(settings, secrets, name, log=print) -> bool:
    """POST /unload {model_name}: Lemonade frees that model's memory (it reloads on next use)."""
    if not name:
        return False
    url, key, _ = server(settings, secrets)
    try:
        net.request("POST", url + "/unload", {"model_name": name}, _headers(key), timeout=30)
        log(f"[lemonade] unloaded {name}")
        return True
    except Exception as error:  # noqa: BLE001 — not loaded, or not a Lemonade server
        log(f"[lemonade] unload {name} skipped: {str(error)[:200]}")
        return False
