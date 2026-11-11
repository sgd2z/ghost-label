"""Settings (settings.json) and secrets (OS keyring via `keyring`, else a 0600 file, flagged)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import time

DEFAULTS = {
    "provider": {
        "baseUrl": "https://api.openai.com/v1",
        "textModel": "gpt-4o",
        "fastModel": "gpt-4o-mini",
        "temperature": 0.9,
        "jsonMode": "auto",          # auto | json_schema | json_object | none
        "imagesEnabled": True,
        "imagesBaseUrl": "",          # empty: same as baseUrl
        "imageModel": "gpt-image-1",
        "imageSize": "1024x1024",
        "unloadImageModelBeforeRecording": False,  # off by default; image jobs load the model again when needed   # POST {images}/unload first (Lemonade), frees GPU memory
        # Sent as negative_prompt where the images server takes one (Lemonade); editable in Settings.
        "imageNegativePrompt": "deformed hands, extra fingers, missing fingers, fused fingers, distorted face, "
                               "asymmetric eyes, extra limbs, blurry, out of focus, low quality, jpeg artifacts, "
                               "garbled text, watermark, signature, logo",
        "imageSteps": "",              # empty: the image model's own (Lemonade: its recipe options)
        "imageCfgScale": "",           # empty: the image model's own
        "timeoutSeconds": 180,
        "maxParallel": "",            # background requests at once; empty = auto (1 local server, 3 remote)
    },
    "engine": {
        "kind": "builtin",            # builtin (the bundled engine host) | custom (any protocol engine)
        "backend": "acestep",         # acestep | lemonade (AMD Lemonade Server's ACE-Step-Music) | mock
        "lemonadeUrl": "",            # Lemonade's API base; empty: the AI provider's base URL
        "lemonadeModel": "",          # empty: ACE-Step-Music
        "unloadChatModelBeforeRecording": False,  # Lemonade: free the chat model's memory for each song
        "python": "",                 # engine venv python; filled by the installer
        "customCommand": "",          # custom engine program
        "customArgs": "",
        "modelPath": "",
        "options": {},                # backend --set options
        "env": {},                    # extra environment for the engine
        "quality": 16,                # 8 demo, 16 balanced, 32 studio
        "maxSongMinutes": 4,
        "idleUnloadMinutes": 10,
        "profile": "",                # installer profile used
    },
    "studio": {
        "lookahead": 2,               # songs after the current one the player asks for
        "prefetch": False,            # record unrecorded songs while idle
        "lyricsAhead": 2,             # songs whose lyrics are written ahead of recording
        "artistVoiceReference": False,  # reuse an artist's first recording as reference audio (opt-in)
    },
    "player": {"normalize": True, "volume": 0.9, "autoplay": True, "syncedLyrics": True},
    "library": {"maxAlbumsPerArtist": 3, "tracksPerAlbumHint": "", "allowExplicit": False,
                "tasteRefreshEvery": 10},
    "links": {"mode": "metadata",     # metadata | audio | voice
              "audioSource": "youtube",  # youtube | preview
              "maxTracks": 50, "analysisSeconds": 90, "keepClips": False, "ytdlp": "",
              "lrclibUrl": ""},            # empty = https://lrclib.net (used only for mashups with real lyrics)
    "onboarded": False,
    "explicitAsked": False,
}

SECRET_SERVICE = "GhostLabel"


def merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in ("options", "env"):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class SaveError(RuntimeError):
    pass


def _replace(tmp, dest, attempts=20):
    """os.replace, retried: on Windows an antivirus scanner, the search indexer or a sync client
    briefly holding the file makes it fail with PermissionError."""
    for i in range(attempts):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05 * (i + 1))


def write_json_atomic(path, data, mode=None):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, mode) if mode is not None else os.open(tmp, flags, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class _FileLock:
    """Cross-process lock next to settings.json, so two cores on one data folder (e.g. an orphaned
    one from an earlier session) can't interleave read-merge-write."""

    def __init__(self, path):
        self.path = path
        self.f = None

    def __enter__(self):
        self.f = open(self.path, "a+b")
        deadline = time.time() + 10
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    return self  # proceed unlocked rather than hang the UI
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.f.seek(0)
                msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.f.close()


class Settings:
    """settings.json. Every save re-reads the file under a lock and merges only the patch into it,
    so a stale copy in another process can't overwrite newer values; the write is atomic and read
    back before the save is reported as done. A file that can't be parsed is kept aside, not
    silently replaced with defaults."""

    def __init__(self, paths):
        self.paths = paths
        self.lock = threading.Lock()
        self.error = None
        try:
            self.stored = self._read()
        except SaveError:
            self.stored = {}
        self.data = merge(DEFAULTS, self.stored)

    def _read(self):
        path = self.paths.settings
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8-sig") as f:
                value = json.load(f)
            if not isinstance(value, dict):
                raise ValueError("not a JSON object")
            return value
        except ValueError as error:
            bad = f"{path}.unreadable-{int(time.time())}"
            try:
                shutil.copy2(path, bad)
            except OSError:
                bad = None
            self.error = f"settings.json couldn't be read ({error}); defaults are in use" + (f"; the old file was kept as {bad}" if bad else "")
            return {}
        except OSError as error:
            self.error = f"settings.json couldn't be opened ({error})"
            raise SaveError(self.error) from error

    def get(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def section(self, name):
        with self.lock:
            return copy.deepcopy(self.data.get(name))

    def update(self, patch: dict):
        """Merge patch into settings.json; returns the full settings. Raises SaveError if the file
        couldn't be written or doesn't read back with the patch applied."""
        with self.lock, _FileLock(self.paths.settings + ".lock"):
            try:
                current = self._read() if os.path.exists(self.paths.settings) else {}
            except SaveError:
                current = self.stored
            stored = merge(current, patch)
            try:
                write_json_atomic(self.paths.settings, stored)
                with open(self.paths.settings, encoding="utf-8-sig") as f:
                    back = json.load(f)
            except (OSError, ValueError) as error:
                raise SaveError(f"Couldn't save settings to {self.paths.settings}: {error}") from error
            if merge(back, patch) != back:
                raise SaveError(f"Settings were written to {self.paths.settings} but didn't read back the same.")
            self.stored = back
            self.data = merge(DEFAULTS, back)
            self.error = None
            return copy.deepcopy(self.data)


class Secrets:
    """API keys. Prefers the OS keyring (Windows Credential Manager, macOS Keychain, Secret
    Service); falls back to a user-only file when no keyring backend works (`insecure` = True).
    A write is read back; if the keyring refuses it the key goes to the file and the reason is
    kept in `warning` for the UI."""

    LEGACY_SERVICES = ("StandardMusicDesktop",)  # 0.2.0 and earlier

    def __init__(self, paths):
        self.paths = paths
        self.keyring = None
        self.warning = None
        self.load_error = None
        self.not_installed = False   # keyring isn't installed: a calm note, not an error
        if os.environ.get("SM_SECRET_STORE") == "file":
            self.load_error = "SM_SECRET_STORE=file is set"
        else:
            try:
                import keyring  # type: ignore
                from keyring.backends import fail  # type: ignore
                backend = keyring.get_keyring()
                if isinstance(backend, fail.Keyring) or "fail" in type(backend).__module__ or "null" in type(backend).__module__.lower():
                    self.load_error = f"no usable OS keychain ({type(backend).__name__})"
                else:
                    self.keyring = keyring
            except ImportError as error:   # not installed (or not importable)
                self.not_installed = True
                self.load_error = f"keyring is not installed ({error})"
                self.keyring = None
            except Exception as error:  # noqa: BLE001 — no backend
                self.load_error = f"keyring unavailable: {type(error).__name__}: {error}"
                self.keyring = None

    @property
    def insecure(self) -> bool:
        return self.keyring is None or bool(self._file())

    @property
    def backend_name(self) -> str:
        if self.keyring is None:
            return "local file (not encrypted)"
        try:
            name = type(self.keyring.get_keyring()).__name__
        except Exception:  # noqa: BLE001
            name = "keyring"
        return name + (" + local file (not encrypted)" if self._file() else "")

    def _file(self):
        try:
            with open(self.paths.secrets_file, encoding="utf-8") as f:
                value = json.load(f)
                return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, data):
        if data:
            write_json_atomic(self.paths.secrets_file, data, mode=0o600)
        elif os.path.exists(self.paths.secrets_file):
            os.remove(self.paths.secrets_file)

    def get(self, name: str) -> str:
        if self.keyring is not None:
            try:
                value = self.keyring.get_password(SECRET_SERVICE, name)
                if value:
                    return value
                for legacy in self.LEGACY_SERVICES:
                    value = self.keyring.get_password(legacy, name)
                    if value:
                        return value
            except Exception:  # noqa: BLE001
                pass
        return self._file().get(name, "")

    def set(self, name: str, value: str) -> dict:
        """Returns {"store", "insecure", "warning"}; raises SaveError if it couldn't be stored anywhere."""
        self.warning = None
        data = self._file()
        if self.keyring is not None:
            try:
                if value:
                    self.keyring.set_password(SECRET_SERVICE, name, value)
                    if self.keyring.get_password(SECRET_SERVICE, name) != value:
                        raise RuntimeError("the keychain didn't return the key that was saved")
                else:
                    for service in (SECRET_SERVICE, *self.LEGACY_SERVICES):
                        try:
                            self.keyring.delete_password(service, name)
                        except Exception:  # noqa: BLE001
                            pass
                if name in data:  # an older fallback copy would shadow nothing but is still plaintext
                    data.pop(name)
                    self._write_file(data)
                return {"store": self.backend_name, "insecure": self.insecure, "warning": None}
            except Exception as error:  # noqa: BLE001
                self.warning = (f"The OS keychain refused the key ({type(error).__name__}: {error}); "
                                f"it was saved in a local file instead: {self.paths.secrets_file} (not encrypted).")
        elif value and not self.not_installed:   # not installed: the calm Settings note covers it
            self.warning = (f"No OS keychain is available ({self.load_error or 'unknown'}); the key was saved in a "
                            f"local file: {self.paths.secrets_file} (not encrypted).")
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        try:
            self._write_file(data)
        except OSError as error:
            raise SaveError(f"Couldn't save the key to {self.paths.secrets_file}: {error}") from error
        if value and self._file().get(name) != value:
            raise SaveError(f"The key was written to {self.paths.secrets_file} but didn't read back.")
        return {"store": self.backend_name, "insecure": True, "warning": self.warning}
