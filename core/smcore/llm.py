"""Generic OpenAI-compatible provider: chat completions with JSON output, plus optional
/images/generations. Works with OpenAI, OpenRouter, LM Studio, Ollama (/v1), llama.cpp server and
anything else that speaks the same shape. No provider-specific features are required: JSON is
requested with json_schema, then json_object, then plain instructions, whichever the server
accepts, and the reply is always parsed, validated and (once) repaired mechanically."""

from __future__ import annotations

import base64
import json
import threading
import time

from collections import deque

from smcore import jsonio, net, placeholder
from smcore.jobs import Cancelled

JSON_MODES = ("json_schema", "json_object", "none")


_lane = threading.local()


class interactive_lane:
    """`with interactive_lane():` marks this thread's requests as ones a person is waiting on."""

    def __enter__(self):
        self.before = getattr(_lane, "interactive", False)
        _lane.interactive = True

    def __exit__(self, *exc):
        _lane.interactive = self.before


def interactive() -> bool:
    return bool(getattr(_lane, "interactive", False))


def is_local_url(url) -> bool:
    """A server on this machine or the local network (mechanical: by host name/address)."""
    import ipaddress
    import urllib.parse
    host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    if host in ("localhost", "") or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


class ProviderError(Exception):
    pass


class Provider:
    def __init__(self, settings, secrets, db=None, log=print):
        self.settings = settings
        self.secrets = secrets
        self.db = db
        self.log = log
        self.mode_cache = {}
        self.sem_lock = threading.Lock()
        self._sem = None
        self._sem_size = None
        self._isem = None
        self.usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "images": 0}
        self.last_image_error = None
        self.calls = {}            # in-flight chat requests, for the UI's "Waiting for <model>…"
        self.calls_lock = threading.Lock()
        self.on_activity = None    # callback(list of in-flight calls)
        self.ended = deque(maxlen=50)   # recently ended calls (with their error, if any)
        self.problem = None        # {model, message, at}: the last request refused for a missing model

    # MARK: config

    def config(self):
        return self.settings.section("provider")

    def base(self, images: bool = False) -> str:
        cfg = self.config()
        url = (cfg.get("imagesBaseUrl") if images else "") or cfg.get("baseUrl") or ""
        return url.rstrip("/")

    def images_separate(self) -> bool:
        """Images go to their own server (Images base URL set and different from the chat one)."""
        cfg = self.config()
        own = (cfg.get("imagesBaseUrl") or "").strip().rstrip("/")
        return bool(own) and own != (cfg.get("baseUrl") or "").strip().rstrip("/")

    def image_key_source(self) -> str:
        """Which key the images endpoint gets: its own; the chat key only when images share the chat
        server (the chat key never goes to a different images server)."""
        if self.secrets.get("imagesApiKey"):
            return "images key"
        if not self.images_separate() and self.secrets.get("apiKey"):
            return "chat key"
        return "none"

    def headers(self, images: bool = False):
        if images:
            source = self.image_key_source()
            key = self.secrets.get("imagesApiKey") if source == "images key" else \
                self.secrets.get("apiKey") if source == "chat key" else ""
        else:
            key = self.secrets.get("apiKey")
        hdrs = {}
        if key:
            hdrs["Authorization"] = f"Bearer {key}"
        if "openrouter.ai" in self.base(images):  # optional attribution headers, harmless elsewhere
            hdrs["HTTP-Referer"] = "https://github.com/sgd2z/ghost-label"
            hdrs["X-Title"] = "Ghost Label"
        return hdrs

    def parallel(self) -> int:
        """How many background requests (creating, writing lyrics, artwork jobs) may be in flight at
        once. Settings › AI provider › Parallel requests; empty = auto: 1 for a server on this
        machine or the local network (one model serving everything), 3 for a remote service."""
        value = str(self.config().get("maxParallel") or "").strip()
        if value:
            try:
                return max(1, min(16, int(float(value))))
            except ValueError:
                pass
        return 1 if is_local_url(self.base()) else 3

    def semaphore(self):
        """Background requests share `parallel()` slots; a request made while the user waits (an
        HTTP handler: onboarding questions, Test connection) has its own slot, so it never queues
        behind a pile of background work."""
        if interactive():
            with self.sem_lock:
                if self._isem is None:
                    self._isem = threading.BoundedSemaphore(1)
                return self._isem
        size = self.parallel()
        with self.sem_lock:
            if self._sem is None or self._sem_size != size:
                self._sem, self._sem_size = threading.BoundedSemaphore(size), size
            return self._sem

    def configured(self) -> bool:
        return bool(self.base() and self.config().get("textModel"))

    # MARK: chat

    def chat(self, messages, model=None, temperature=None, response_format=None, max_tokens=None, timeout=None):
        cfg = self.config()
        body = {"model": model or cfg["textModel"], "messages": messages}
        temp = cfg.get("temperature") if temperature is None else temperature
        if temp is not None:
            body["temperature"] = float(temp)
        if response_format:
            body["response_format"] = response_format
        if max_tokens:
            body["max_tokens"] = int(max_tokens)
        if not self.base():
            raise ProviderError("No AI provider is set up. Open Settings › AI provider.")
        limit = float(timeout or cfg.get("timeoutSeconds") or 180)
        call = self._call_started(body["model"], limit)
        failure = None
        try:
            reply = self._chat(body, limit, call)
            if self.problem and self.problem.get("model") == body["model"]:
                self.problem = None
                self._notify()
            return reply
        except net.HTTPError as error:
            failure = error
            plain = self.missing_model_message(body["model"]) if 400 <= error.status < 500 and error.status not in (401, 403, 429) else None
            if plain:
                self.problem = {"model": body["model"], "message": plain, "at": time.time()}
                self._notify()
                self.log(f"[llm] {plain}")
                raise ProviderError(plain) from None
            raise
        except Exception as error:
            failure = error
            raise
        finally:
            self._call_ended(call, failure)

    def activity(self):
        with self.calls_lock:
            return sorted((dict(c) for c in self.calls.values()), key=lambda c: c["started"])

    def _notify(self):
        if self.on_activity:
            try:
                self.on_activity(self.activity())
            except Exception:  # noqa: BLE001 — UI notifications never break a request
                pass

    _purpose = threading.local()

    def purpose(self, text):
        """`with provider.purpose("photo prompt"):` labels the calls this thread makes, so the UI
        can say what a request is for instead of only which model it waits on."""
        provider = self

        class _Label:
            def __enter__(self):
                self.before = getattr(provider._purpose, "text", None)
                provider._purpose.text = text

            def __exit__(self, *exc):
                provider._purpose.text = self.before
        return _Label()

    def _call_started(self, model, limit, kind="chat", base=None, purpose=None):
        import uuid
        call = {"id": uuid.uuid4().hex[:8], "model": model, "base": base or self.base(), "started": time.time(),
                "timeout": limit, "state": "queued", "kind": kind, "lane": "interactive" if interactive() else "background",
                "purpose": purpose or getattr(self._purpose, "text", None)}
        from smcore.queue import current_owner
        who = current_owner()
        if who:
            call["owner"] = who
        with self.calls_lock:
            self.calls[call["id"]] = call
        self._notify()
        return call

    def _call_ended(self, call, error=None):
        with self.calls_lock:
            self.calls.pop(call["id"], None)
            if error is not None:
                call["error"] = str(error)[:300] or repr(error)
            call["ended"] = time.time()
            self.ended.append(dict(call))
        self._notify()

    def ended_call(self, call_id):
        """How a recent request ended (for the queue's 'recently finished' list)."""
        with self.calls_lock:
            return next((dict(c) for c in reversed(self.ended) if c["id"] == call_id), None)

    def cancel_calls(self, match) -> int:
        """Cancel the queued requests whose owner matches (a cancelled job or recording): they stop
        waiting for a slot at once. Requests already sent run to the end."""
        n = 0
        with self.calls_lock:
            for c in self.calls.values():
                if c["state"] == "queued" and match(c.get("owner") or {}):
                    c["cancelled"] = True
                    n += 1
        return n

    class _Slot:
        """A concurrency slot that a queued request can leave when it is cancelled."""

        def __init__(self, sem, call):
            self.sem, self.call = sem, call

        def __enter__(self):
            from smcore.jobs import Cancelled
            while not self.sem.acquire(timeout=0.25):
                if self.call is not None and self.call.get("cancelled"):
                    raise Cancelled()
            if self.call is not None and self.call.get("cancelled"):
                self.sem.release()
                raise Cancelled()

        def __exit__(self, *exc):
            self.sem.release()

    def slot(self, call=None):
        return self._Slot(self.semaphore(), call)

    def _chat(self, body, limit, call):
        with self.slot(call):
            with self.calls_lock:
                call["state"], call["sent"] = "sent", time.time()
            self._notify()
            last = None
            for attempt in range(3):
                try:
                    reply = net.request("POST", self.base() + "/chat/completions", body, self.headers(), timeout=limit)
                    break
                except (TimeoutError, OSError) as error:
                    if not isinstance(error, TimeoutError) and not isinstance(getattr(error, "reason", None), TimeoutError):
                        last = error
                        if attempt < 2:
                            time.sleep(2 * (attempt + 1))
                            continue
                        raise ProviderError(f"Couldn't reach {self.base()}: {error}") from None
                    # Not retried: a model that is still loading or thinking would just time out again.
                    raise ProviderError(f"No reply from {self.base()} within {int(limit)} s (model \"{body['model']}\"). "
                                        "A local server may still be loading the model; check it, then Retry.") from None
                except net.HTTPError as error:
                    last = error
                    if error.status in (429, 500, 502, 503, 504) and attempt < 2:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise
            else:
                raise last  # pragma: no cover
        self.usage["requests"] += 1
        usage = reply.get("usage") or {}
        self.usage["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        self.usage["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        try:
            message = reply["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("The provider's reply had no message: " + json.dumps(reply)[:300]) from None
        content = message.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content and message.get("reasoning_content"):
            content = message["reasoning_content"]
        return content or ""

    def _mode_key(self, model):
        return f"jsonmode::{self.base()}::{model}"

    def json_mode(self, model):
        forced = self.config().get("jsonMode") or "auto"
        if forced != "auto":
            return forced
        key = self._mode_key(model)
        if key in self.mode_cache:
            return self.mode_cache[key]
        if self.db is not None:
            stored = self.db.kv_get(key)
            if stored in JSON_MODES:
                self.mode_cache[key] = stored
                return stored
        return "json_schema"

    def remember_mode(self, model, mode):
        key = self._mode_key(model)
        self.mode_cache[key] = mode
        if self.db is not None:
            self.db.kv_set(key, mode)

    def json(self, system: str, user: str, schema: dict, name: str = "result", fast: bool = False,
             temperature=None, status=None, timeout=None, check=None):
        """Ask for one JSON document matching `schema`. Returns the validated value.
        status(text) is called before each request (for a live status line in the UI).
        check(value) -> [problems] adds mechanical checks; problems go back to the model once."""
        cfg = self.config()
        say = status or (lambda _t: None)
        model = (cfg.get("fastModel") if fast else None) or cfg["textModel"]
        shape = jsonio.describe(schema)
        system_full = (system.rstrip() + "\n\nReply with a single JSON value and nothing else. "
                       "Its shape:\n" + shape)
        messages = [{"role": "system", "content": system_full}, {"role": "user", "content": user}]
        mode = self.json_mode(model)
        order = list(JSON_MODES[JSON_MODES.index(mode):]) if mode in JSON_MODES else list(JSON_MODES)
        text, error = None, None
        for m in order:
            fmt = None
            if m == "json_schema":
                fmt = {"type": "json_schema", "json_schema": {"name": name, "schema": jsonio.strict_schema(schema),
                                                              "strict": False}}
            elif m == "json_object":
                fmt = {"type": "json_object"}
            try:
                say(f"Asking {model} at {self.base()} (JSON mode: {m})…")
                text = self.chat(messages, model=model, temperature=temperature, response_format=fmt, timeout=timeout)
                if m != mode or (self.config().get("jsonMode") or "auto") == "auto":
                    self.remember_mode(model, m)
                break
            except net.HTTPError as e:
                error = e
                if e.status in (400, 415, 422, 500, 501) and m != "none":
                    self.log(f"[provider] {m} not accepted ({e.status}: {e.body[:200]}); trying the next JSON mode")
                    say(f"{m} not accepted ({e.status}: {e.body[:160]}); trying the next JSON mode…")
                    continue
                raise ProviderError(str(e)) from None
        if text is None:
            raise ProviderError(str(error))
        value, problems = self._validate(text, schema, check)
        if problems:
            self.log(f"[provider] repairing reply: {problems[:4]}")
            say("The reply wasn't valid JSON for this step; asking the model to correct it…")
            fix = self.chat(messages + [
                {"role": "assistant", "content": text[:12000]},
                {"role": "user", "content": "That reply needs fixing. Problems: "
                 + "; ".join(problems[:12]) + "\nReturn the corrected JSON only."}],
                model=model, temperature=0.2, timeout=timeout)
            value, problems = self._validate(fix, schema, check)
            if problems:
                excerpt = " ".join((fix or text or "").split())[:300]
                raise ProviderError("The model's reply didn't match the expected shape: " + "; ".join(problems[:6])
                                    + (f'. It replied: "{excerpt}"' if excerpt else ". It replied with nothing."))
        return value

    @staticmethod
    def _validate(text, schema, check=None):
        try:
            value = jsonio.parse(text)
        except ValueError as error:
            return None, [str(error)]
        value, errors = jsonio.coerce(value, schema)
        if not errors and check:
            errors = list(check(value) or [])
        return value, errors

    # MARK: images

    def image_server(self) -> dict:
        """What the images server says about the image model, cached for a few minutes:
        {"lemonade": bool, "entry": Lemonade's model entry (recipe, recipe_options, size) or None}.
        Lemonade's GET /models/{id} carries `recipe`; other servers' don't (or have no such route)."""
        cfg = self.config()
        key = (self.base(images=True), cfg.get("imageModel"))
        cached = getattr(self, "_image_server", None)
        if cached and cached[0] == key and time.time() - cached[1] < 300:
            return cached[2]
        info = {"lemonade": False, "entry": None}
        if all(key):
            import urllib.parse
            try:
                entry = net.request("GET", key[0] + "/models/" + urllib.parse.quote(key[1], safe=""),
                                    headers=self.headers(images=True), timeout=10)
                if isinstance(entry, dict) and "recipe" in entry:
                    info = {"lemonade": True, "entry": entry}
            except Exception:  # noqa: BLE001 — not listed / no such route: a plain OpenAI-style server
                pass
        self._image_server = (key, time.time(), info)
        return info

    def image_request(self, prompt: str, negative: str = "") -> dict:
        """The images API body. Empty size/steps/cfg settings leave the model's own (Lemonade
        applies each model's recipe options). negative_prompt goes only to Lemonade, which accepts
        it; OpenAI-style servers reject unknown fields."""
        cfg = self.config()
        body = {"model": cfg["imageModel"], "prompt": prompt[:3800], "n": 1}
        if str(cfg.get("imageSize") or "").strip():
            body["size"] = str(cfg["imageSize"]).strip()
        server = self.image_server()
        if server["lemonade"]:
            for key, field, cast in (("imageSteps", "steps", int), ("imageCfgScale", "cfg_scale", float)):
                value = str(cfg.get(key) or "").strip()
                if value:
                    try:
                        body[field] = cast(value)
                    except ValueError:
                        pass
            if negative.strip():
                body["negative_prompt"] = negative.strip()[:1000]
        return body

    def image(self, prompt: str, seed_text: str, style: str = "cover", negative: str = "") -> tuple[bytes, str]:
        """(PNG/JPEG bytes, source) where source is 'provider' or 'placeholder'."""
        cfg = self.config()
        self.last_image_error = None
        self.last_image_request = None
        if not cfg.get("imagesEnabled"):
            self.last_image_error = "image generation is off in Settings"
        elif not cfg.get("imageModel") or not self.base(images=True):
            self.last_image_error = "no image model or images base URL"
        if cfg.get("imagesEnabled") and cfg.get("imageModel") and self.base(images=True):
            limit = float(cfg.get("timeoutSeconds") or 180)
            failure = None
            call = self._call_started(cfg["imageModel"], limit, kind="image", base=self.base(images=True),
                                      purpose={"portrait": "photo"}.get(style, style))
            try:
                body = self.image_request(prompt, negative)
                self.last_image_request = body
                shown = {k: v for k, v in body.items() if k not in ("prompt", "negative_prompt")}
                self.log(f"[images] {style} for {seed_text!r} {shown}: prompt: {body['prompt']}"
                         + (f" | negative: {body['negative_prompt']}" if body.get("negative_prompt") else ""))
                with self.slot(call):
                    self.ensure_image_model(call)
                    with self.calls_lock:
                        call["state"], call["sent"] = "sent", time.time()
                    self._notify()
                    reply = net.request("POST", self.base(images=True) + "/images/generations", body,
                                        self.headers(images=True), timeout=limit)
                item = (reply.get("data") or [{}])[0]
                if item.get("b64_json"):
                    self.usage["images"] += 1
                    return base64.b64decode(item["b64_json"]), "provider"
                if item.get("url"):
                    self.usage["images"] += 1
                    return net.download(item["url"]), "provider"
                self.last_image_error = "the image reply had no data"
                self.log("[provider] image reply had no data; using a placeholder")
            except Cancelled:
                failure = "cancelled"
                raise
            except Exception as error:  # noqa: BLE001 — images are optional
                failure = error
                self.last_image_error = str(error)[:300]
                self.log(f"[images] drawing the {style} for {seed_text!r} failed with {cfg['imageModel']} "
                         f"({error}); using a placeholder")
            finally:
                self._call_ended(call, failure)
        return placeholder.artwork(seed_text, 512, style), "placeholder"

    IMAGE_LOAD_TIMEOUT = 900   # a big image model (Z-Image, Qwen-Image) can take minutes to load

    def ensure_image_model(self, call=None) -> None:
        """Lemonade: load the image model first when /health doesn't list it as loaded (it may have
        been unloaded before a recording), with a generous timeout and a 'loading' activity state,
        so the draw request's own timeout covers only the drawing. Other servers: nothing to do."""
        info = self.image_server()
        if not info.get("lemonade"):
            return
        model = self.config().get("imageModel")
        base = self.base(images=True)
        try:
            health = net.request("GET", base + "/health", headers=self.headers(images=True), timeout=10)
            loaded = {m.get("model_name") for m in health.get("all_models_loaded") or [] if isinstance(m, dict)}
        except Exception as error:  # noqa: BLE001 — the draw request will say what's wrong
            self.log(f"[images] couldn't read {base}/health ({error}); drawing anyway")
            return
        if model in loaded:
            return
        if call is not None:
            with self.calls_lock:
                call["state"] = "loading"
            self._notify()
        self.log(f"[images] loading {model} on {base} (not loaded)")
        start = time.time()
        net.request("POST", base + "/load", {"model_name": model}, self.headers(images=True),
                    timeout=self.IMAGE_LOAD_TIMEOUT)
        self.log(f"[images] {model} loaded in {time.time() - start:.0f} s")

    # MARK: test

    def release_image_model(self) -> bool:
        """Before a recording, when enabled: ask the images server (Lemonade's POST /v1/unload) to drop
        the image model so ACE-Step gets the GPU memory. Servers without /unload just say no."""
        cfg = self.config()
        if not (cfg.get("unloadImageModelBeforeRecording") and cfg.get("imagesEnabled") and cfg.get("imageModel")):
            return False
        try:
            net.request("POST", self.base(images=True) + "/unload", {"model_name": cfg["imageModel"]},
                        self.headers(images=True), timeout=30)
            self.log(f"[provider] unloaded image model {cfg['imageModel']} before recording")
            return True
        except Exception as error:  # noqa: BLE001 — not loaded, or the server has no /unload
            self.log(f"[provider] image model unload skipped: {str(error)[:200]}")
            return False

    def test_images(self) -> dict:
        """The images 'Test' button: free checks only (no image is generated, nothing is billed):
        the images server answers, accepts the key, and lists the image model if it lists models."""
        cfg = self.config()
        out = {"enabled": bool(cfg.get("imagesEnabled")), "baseUrl": self.base(images=True), "model": cfg.get("imageModel"),
               "separate": self.images_separate(), "key": self.image_key_source(), "ok": False}
        if not out["baseUrl"]:
            out["error"] = "No images base URL (and no chat base URL to share)."
            return out
        try:
            from smcore import lemonade
            inv = lemonade.inventory(out["baseUrl"], self.headers(images=True))
            out["ok"] = True
            if inv["lemonade"]:
                # Lemonade: downloaded or loaded models, classified by its labels and recipe.
                out["lemonade"] = True
                out["models"] = len(inv["usable"])
                out["imageModels"] = inv["by_mode"]("image")
                out["loaded"] = out["model"] in inv["loaded"]
                entry = inv["entries"].get(out["model"]) or {}
                opts = entry.get("recipe_options") or {}
                out["modelDefaults"] = {k: opts[k] for k in ("width", "height", "steps", "cfg_scale") if k in opts}
                out["modelSizeGb"] = entry.get("size")
                if out["model"]:
                    check = lemonade.check_model(inv, out["model"], "image")
                    if not check["ok"]:
                        out["ok"], out["error"] = False, check["error"]
                    elif check.get("warning"):
                        out["warning"] = check["warning"]
            else:
                listing = inv["listing"]
                models = [m.get("id") for m in (listing.get("data") or []) if isinstance(m, dict)]
                out["models"] = len(models)
                out["imageModels"] = image_models(listing)
                if models and out["model"] and out["model"] not in models:
                    out["ok"] = False
                    out["error"] = (f"{out['model']} isn't in this server's model list. Models there: "
                                    + ", ".join(models[:12]) + ("…" if len(models) > 12 else ""))
        except Exception as error:  # noqa: BLE001
            out["error"] = str(error)[:300]
        if out["ok"] and not out["enabled"]:
            out["warning"] = "The server answers, but image generation is switched off (tick the box and Save)."
        return out

    def chat_models(self, timeout=8) -> dict:
        """What the Main/Fast model pickers offer, fetched fresh every time: every chat model the
        server can use now (Lemonade: downloaded or loaded), plus whether the saved ones are there.
        {models, loaded, lemonade, textModel: {name, present}, fastModel: {name, present, sameAsMain}, error}."""
        cfg = self.config()
        out = {"baseUrl": self.base(), "models": [], "loaded": [], "lemonade": False, "error": None}
        try:
            from smcore import lemonade
            inv = lemonade.inventory(self.base(), self.headers(), timeout=timeout)
            if inv["lemonade"]:
                out["lemonade"] = True
                out["models"] = inv["by_mode"]("chat")
                out["loaded"] = [m for m in inv["loaded"] if m in out["models"]]
            else:
                listing = inv["listing"]
                ids = [m.get("id") for m in (listing.get("data") or []) if isinstance(m, dict) and m.get("id")]
                images = set(image_models(listing))
                out["models"] = [m for m in ids if m not in images]
        except Exception as error:  # noqa: BLE001 — the pickers still show the saved names
            out["error"] = str(error)[:300]
        listed = out["error"] is None
        main, fast = (cfg.get("textModel") or "").strip(), (cfg.get("fastModel") or "").strip()
        out["textModel"] = {"name": main, "present": (main in out["models"]) if listed and main else None}
        out["fastModel"] = {"name": fast, "sameAsMain": not fast or fast == main,
                            "present": (fast in out["models"]) if listed and fast else None}
        return out

    def missing_model_message(self, model) -> str | None:
        """When a chat request is refused, say plainly if the server simply doesn't have `model`."""
        try:
            listing = self.chat_models(timeout=5)
        except Exception:  # noqa: BLE001
            return None
        if listing["error"] or not listing["models"] or model in listing["models"]:
            return None
        server = "the Lemonade server" if listing["lemonade"] else "the AI server"
        return (f"{model} isn't on {server} at {self.base()}. Pick another model in Settings › AI provider "
                f"(chat models there now: {', '.join(listing['models'][:8])}).")

    def test(self) -> dict:
        """'Test connection': list models, one tiny JSON call, and report what worked."""
        out = {"baseUrl": self.base(), "models": None, "chat": None, "jsonMode": None, "images": None,
               "keyStore": self.secrets.backend_name, "ok": False, "usedModel": self.config().get("textModel")}
        try:
            from smcore import lemonade
            inv = lemonade.inventory(self.base(), self.headers())
            if inv["lemonade"]:
                # Lemonade: every downloaded or loaded model, sorted by what Lemonade serves it as.
                out["lemonade"] = True
                out["models"] = inv["usable"][:200]
                out["imageModels"] = inv["by_mode"]("image")
                out["chatModels"] = inv["by_mode"]("chat")
                out["musicModels"] = inv["by_mode"]("audio-generation")
                out["loadedModels"] = list(inv["loaded"])
            else:
                listing = inv["listing"]
                out["models"] = [m.get("id") for m in (listing.get("data") or []) if isinstance(m, dict)][:200]
                # Other servers that label their models say which make images; the rest are chat.
                out["imageModels"] = image_models(listing)
                out["chatModels"] = [m for m in out["models"] if m not in out["imageModels"]]
            cfg = self.config()
            chat = out["chatModels"]
            missing = [m for m in dict.fromkeys([cfg.get("textModel"), cfg.get("fastModel")]) if m and m not in chat]
            out["missingModels"] = missing
            if missing:
                out["modelWarning"] = (f"{', '.join(missing)} isn't on the server"
                                       + (f" (chat models there: {', '.join(chat[:8])})" if chat else " (it lists no chat models)")
                                       + ". Pick one in the list; nothing was switched for you.")
        except Exception as error:  # noqa: BLE001 — some servers don't list models
            out["modelsError"] = str(error)[:300]
        start = time.time()
        try:
            value = self.json("You check that a connection works.", 'Reply with {"ok": true, "word": "<any word>"}',
                              {"type": "object", "properties": {"ok": {"type": "boolean"}, "word": {"type": "string"}},
                               "required": ["ok"]}, name="check", temperature=0)
            out["chat"] = {"ok": bool(value.get("ok")), "seconds": round(time.time() - start, 2),
                           "model": self.config()["textModel"]}
            out["jsonMode"] = self.json_mode(self.config()["textModel"])
            out["ok"] = True
        except Exception as error:  # noqa: BLE001
            out["chatError"] = str(error)[:500]
        cfg = self.config()
        out["images"] = ((f"enabled: {cfg.get('imageModel', '')} at {self.base(images=True)} ({self.image_key_source()})")
                         if cfg.get("imagesEnabled") else "off (placeholders)")
        return out


def image_models(listing) -> list:
    """Model ids the server itself labels as image models (Lemonade's `labels: ["image"]`)."""
    return [m.get("id") for m in (listing.get("data") or [])
            if isinstance(m, dict) and "image" in (m.get("labels") or [])]
