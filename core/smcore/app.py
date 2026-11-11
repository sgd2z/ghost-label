"""Wires the services together (one instance per running core)."""

from __future__ import annotations

import os
import sys
import threading
import time

from smcore.bootstrap import Installer, detect
from smcore.builder import Builder
from smcore.catalog import Catalog
from smcore.db import Database
from smcore.engine_client import EngineClient
from smcore.events import Events
from smcore.jobs import JobManager
from smcore.queue import GlobalQueue
from smcore.library import Library
from smcore.links import Links
from smcore.llm import Provider
from smcore.paths import Paths, migrate_legacy
from smcore.rules import Rules
from smcore.settings import DEFAULTS, SaveError, Secrets, Settings
from smcore.share import Sharing
from smcore.studio import Studio

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class App:
    def __init__(self, home=None, engine_dir=None, uv=None, catalog_base=None, log=None):
        self.log_file = None
        self.paths = Paths(home)
        self.log = log or self._log
        self.engine_dir = os.path.abspath(engine_dir or os.environ.get("SM_ENGINE_DIR") or os.path.join(REPO, "engine"))
        self.uv = uv
        self.events = Events()
        self.migrated_from = None
        if home is None and not os.environ.get("SM_HOME"):
            try:
                self.migrated_from = migrate_legacy(self.paths)
            except OSError as error:
                self.log(f"couldn't copy settings from the old data folder: {error}")
        self.settings = Settings(self.paths)
        self.secrets = Secrets(self.paths)
        self.db = Database(self.paths.db)
        self.rules = Rules(self.paths)
        self.provider = Provider(self.settings, self.secrets, self.db, self.log)
        # 0.2.x saved the old default (3 parallel requests) with every provider save; it becomes
        # "auto" once, so a local server gets one background request at a time.
        if not self.db.kv_get("migrated.maxParallelAuto"):
            if (self.settings.stored.get("provider") or {}).get("maxParallel") == 3:
                try:
                    self.settings.update({"provider": {"maxParallel": ""}})
                except Exception as error:  # noqa: BLE001
                    self.log(f"[settings] couldn't switch Parallel requests to auto: {error}")
            self.db.kv_set("migrated.maxParallelAuto", True)
        self.provider.on_activity = lambda calls: self.events.emit("llm-activity", calls=self.llm_activity(calls),
                                                                   problem=self.provider.problem)
        self.library = Library(self.db, self.paths, self.events, self.log)
        self.catalog = Catalog(self.db, self.log, catalog_base or os.environ.get("SM_CATALOG_BASE"))
        self.jobs = JobManager(self.db, self.events, self.log)
        self.builder = Builder(self.db, self.paths, self.settings, self.provider, self.rules, self.library, self.events, self.log)
        self.installer = Installer(self.settings, self.paths, self.engine_dir, uv, self.log)
        self.installer.secrets = self.secrets
        from smcore.runtime import Runtime
        self.runtime = Runtime(self.installer, self.jobs, self.log)
        ffmpeg = self.runtime.ffmpeg_path   # every ffmpeg user resolves it the same way
        self.engine = EngineClient(self.settings, self.paths, self.engine_dir, self.events, self.log)
        self.engine.ffmpeg = ffmpeg
        # The built-in ACE-Step engine, kept apart for songs Lemonade can't make (reference audio).
        self.builtin_engine = EngineClient(self.settings, self.paths, self.engine_dir, self.events, self.log,
                                           backend="acestep", event="engine_builtin")
        self.builtin_engine.ffmpeg = ffmpeg
        self.studio = Studio(self.db, self.paths, self.settings, self.engine, self.builder, self.library, self.events,
                             self.log, ffmpeg=ffmpeg, builtin=self.builtin_engine, secrets=self.secrets)
        from smcore.taste import Taste
        self.taste = Taste(self.db, self.settings, self.provider, self.rules, self.catalog, self.library, self.log)
        self.links = Links(self.db, self.paths, self.settings, self.provider, self.rules, self.catalog, self.library,
                           self.engine, self.builder, self.jobs, self.events, self.log, ffmpeg=ffmpeg)
        self.links.studio = self.studio
        # One view of every AI queue (chat, images, music engine, background tasks) for the pages.
        self.queue = GlobalQueue(self.provider, self.jobs, self.studio, self.settings, self.events, self.db, self.log)
        self.jobs.on_cancel.append(lambda job_id: self.provider.cancel_calls(
            lambda o: o.get("jobId") == job_id))
        self.events.watch(lambda kind: kind in ("job", "studio", "recording", "llm-activity", "track")
                          and self.queue.changed())
        self.sharing = Sharing(self.db, self.paths, self.settings, self.library, self.events, self.log, ffmpeg=ffmpeg)
        if not os.environ.get("GHOSTLABEL_NO_RUNTIME_CHECK"):
            threading.Thread(target=self._check_runtime, daemon=True, name="runtime-check").start()
        self.engine.secrets = self.secrets
        self.builtin_engine.secrets = self.secrets
        self.refreshing = threading.Lock()

    def _log(self, text):
        if self.log_file is None:
            self.log_file = open(os.path.join(self.paths.logs, "core.log"), "a", encoding="utf-8")
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
        print(line, file=sys.stderr, flush=True)
        self.log_file.write(line + "\n")
        self.log_file.flush()

    def llm_activity(self, calls=None):
        """In-flight AI requests with their age in seconds (the UI's clock may differ from ours)."""
        import time as _time
        now = _time.time()
        return [{"model": c["model"], "base": c["base"], "state": c["state"], "timeout": c["timeout"],
                 "kind": c.get("kind") or "chat",
                 "purpose": c.get("purpose") or "", "lane": c.get("lane") or "background", "age": round(now - c["started"], 1)} for c in (self.provider.activity() if calls is None else calls)]

    def save_provider(self, provider, api_key=None, images_key=None):
        allowed = set(DEFAULTS["provider"])
        patch = {k: v for k, v in (provider or {}).items() if k in allowed}
        saved = self.settings.update({"provider": patch})["provider"]
        mismatch = [k for k, v in patch.items() if saved.get(k) != v]
        if mismatch:
            raise SaveError("These settings didn't save: " + ", ".join(mismatch))
        out = {"ok": True, "provider": saved, "settingsPath": self.paths.settings, "warnings": [],
               "keyStore": self.secrets.backend_name, "keyStoreInsecure": self.secrets.insecure}
        for name, value in (("apiKey", api_key), ("imagesApiKey", images_key)):
            if value is None:
                continue
            r = self.secrets.set(name, value)
            if value and self.secrets.get(name) != value:
                raise SaveError("The API key didn't read back after saving.")
            if r.get("warning"):
                out["warnings"].append(r["warning"])
            out["keyStore"], out["keyStoreInsecure"] = r["store"], r["insecure"]
        out["hasApiKey"] = bool(self.secrets.get("apiKey"))
        self.log(f"provider saved to {self.paths.settings} (key store: {out['keyStore']})")
        return out

    def state(self):
        s = self.settings.get()
        return {"onboarded": s.get("onboarded"), "providerConfigured": self.provider.configured(),
                "hasApiKey": bool(self.secrets.get("apiKey")), "hasImagesKey": bool(self.secrets.get("imagesApiKey")), "hasHfToken": bool(self.secrets.get("hfToken")), "keyStore": self.secrets.backend_name,
                "keyStoreInsecure": self.secrets.insecure, "engine": self.engine.status(),
                "engineInstalled": bool(s["engine"].get("python") or self.installer.layout.python()) or s["engine"].get("kind") == "custom",
                "enginePython": self.installer.layout.python(), "builtinInstalled": self.studio.builtin_installed(), "engineAceRoot": self.installer.ace if self.installer.layout.ace_ok() else None,
                "home": self.paths.home, "settingsPath": self.paths.settings, "settingsError": self.settings.error,
                "keyStoreWarning": self.secrets.warning or (self.secrets.load_error if self.secrets.insecure
                                                            and not self.secrets.not_installed else None),
                "keyStoreNote": ("Keys are saved in secrets.json (not encrypted). Install keyring to use "
                                 + ("Windows Credential Manager." if os.name == "nt" else "the OS keychain."))
                if self.secrets.not_installed and not self.secrets.warning else None,
                "migratedFrom": self.migrated_from, "platform": sys.platform, "ytdlp": self.links.fetcher.available(), "ffmpeg": self.runtime.ffmpeg,
                "totals": self.library.home()["totals"]}

    def _check_runtime(self):
        try:
            self.runtime.check()
        except Exception as error:  # noqa: BLE001
            self.log(f"[runtime] check failed: {error!r}")

    # MARK: deleting (stop recording first, then rows and files)

    def delete(self, kind, item_id):
        if kind == "track":
            ids = [item_id]
        elif kind == "album":
            ids = [r["id"] for r in self.db.all("SELECT id FROM tracks WHERE album_id = ?", (item_id,))]
        elif kind == "artist":
            ids = [r["id"] for r in self.db.all("SELECT id FROM tracks WHERE artist_id = ?", (item_id,))]
        else:
            raise ValueError(kind)
        self.studio.drop(ids)
        out = {"track": self.library.delete_track, "album": self.library.delete_album,
               "artist": self.library.delete_artist}[kind](item_id)
        self.events.emit("deleted", kind=kind, id=item_id, trackIds=out["tracks"])
        self.log(f"[library] deleted {kind} {item_id}: {len(out['tracks'])} songs, {out['files']} files")
        return out

    def detect(self):
        return detect()

    def repair_engine(self):
        """Re-runs only the missing setup steps (shares the install's single-flight key)."""
        def run(job):
            self.engine.stop()
            self.builtin_engine.stop()
            return self.installer.repair(job)

        return self.jobs.start("install", "Repairing the engine", run, {"repair": True}, key="engine-install")

    def install_engine(self, profile, rocm="10.0.0", arch="gfx1151", download_models=True):
        """Single-flight: a second request while an install runs gets the running job back."""
        def run(job):
            self.engine.stop()
            self.builtin_engine.stop()
            return self.installer.install(job, profile, rocm, arch, download_models)

        return self.jobs.start("install", f"Setting up the engine ({profile})", run,
                               {"profile": profile, "rocm": rocm, "arch": arch, "downloadModels": download_models},
                               key="engine-install")

    def after_signal(self):
        def run():
            if not self.refreshing.acquire(blocking=False):
                return
            try:
                profile = self.taste.maybe_refresh()
                if profile:
                    self.events.emit("taste", profile=profile)
            except Exception as error:  # noqa: BLE001
                self.log(f"[taste] refresh failed: {error!r}")
            finally:
                self.refreshing.release()
        threading.Thread(target=run, daemon=True).start()
