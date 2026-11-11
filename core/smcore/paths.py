"""Where the app keeps its data. `SM_HOME` overrides everything (tests, screenshots, portable)."""

from __future__ import annotations

import os
import sys

APP_DIR_NAME = "GhostLabel"


def default_home() -> str:
    if os.environ.get("SM_HOME"):
        return os.path.abspath(os.path.expanduser(os.environ["SM_HOME"]))
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return os.path.join(base, APP_DIR_NAME)
    if sys.platform == "darwin":
        return os.path.expanduser(f"~/Library/Application Support/{APP_DIR_NAME}")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "ghost-label")


LEGACY_DIR_NAMES = ("StandardMusicDesktop",)  # 0.2.0 and earlier


def legacy_homes() -> list:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return [os.path.join(base, n) for n in LEGACY_DIR_NAMES]
    if sys.platform == "darwin":
        return [os.path.expanduser(f"~/Library/Application Support/{n}") for n in LEGACY_DIR_NAMES]
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return [os.path.join(base, "standard-music-desktop")]


def _load(path):
    import json
    try:
        with open(path, encoding="utf-8-sig") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _user_provider(data):
    """The provider section if someone actually set it (0.2.1 wrote the defaults into the file)."""
    p = (data or {}).get("provider") or {}
    default = ("https://api.openai.com/v1", "gpt-4o")
    return p if p and (p.get("baseUrl"), p.get("textModel")) != default and p.get("baseUrl") else None


def migrate_legacy(paths) -> str | None:
    """After the rename: bring settings (provider, models, preferences), a fallback key file and
    edited rules over from the old data folder. If settings.json already exists here but holds
    only the default provider, the old folder's provider is copied in. Library and engine stay."""
    import json
    import shutil
    current = _load(paths.settings) if os.path.exists(paths.settings) else None
    if current is not None and _user_provider(current):
        return None
    for old in legacy_homes():
        src = os.path.join(old, "settings.json")
        if os.path.abspath(old) == paths.home or not os.path.exists(src):
            continue
        data = _load(src)
        if data is None:
            continue
        if current is not None:
            provider = _user_provider(data)
            if not provider:
                continue
            current["provider"] = provider
            data = current
        else:
            eng = data.get("engine") if isinstance(data.get("engine"), dict) else {}
            if str(eng.get("python") or "").lower().startswith(os.path.abspath(old).lower()):
                # The engine stays in the old folder; it's set up again here (the docs say to delete the old one).
                data["engine"] = {k: v for k, v in eng.items() if k not in ("python", "profile", "env")}
        tmp = paths.settings + ".migrate.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, paths.settings)
        if os.path.exists(os.path.join(old, "secrets.json")) and not os.path.exists(paths.secrets_file):
            shutil.copy2(os.path.join(old, "secrets.json"), paths.secrets_file)
        rules = os.path.join(old, "Rules")
        if os.path.isdir(rules):
            for f in os.listdir(rules):
                if not os.path.exists(os.path.join(paths.rules, f)):
                    shutil.copy2(os.path.join(rules, f), os.path.join(paths.rules, f))
        return old
    return None


class Paths:
    def __init__(self, home: str | None = None):
        self.home = os.path.abspath(home or default_home())
        self.db = os.path.join(self.home, "Library.sqlite")
        self.settings = os.path.join(self.home, "settings.json")
        self.secrets_file = os.path.join(self.home, "secrets.json")  # only when no OS keyring
        self.artwork = os.path.join(self.home, "Artwork")
        self.audio = os.path.join(self.home, "Audio")
        self.clips = os.path.join(self.home, "Clips")          # link-analysis audio (temporary)
        self.envs = os.path.join(self.home, "Engine")           # engine venv, ACE-Step, models, tools
        self.rules = os.path.join(self.home, "Rules")           # user edits of the source rules
        self.logs = os.path.join(self.home, "Logs")
        self.shares = os.path.join(self.home, "Shares")         # unpacked incoming shares
        for d in (self.home, self.artwork, self.audio, self.clips, self.envs, self.rules, self.logs, self.shares):
            os.makedirs(d, exist_ok=True)

    def rel(self, path: str) -> str:
        """Store paths relative to home so a library can move between machines."""
        path = os.path.abspath(path)
        try:
            rel = os.path.relpath(path, self.home)
        except ValueError:  # another drive on Windows
            return path
        return path if rel.startswith("..") else rel.replace(os.sep, "/")

    def abs(self, rel: str | None) -> str | None:
        if not rel:
            return None
        return rel if os.path.isabs(rel) else os.path.join(self.home, *rel.split("/"))
