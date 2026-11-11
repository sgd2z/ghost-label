"""Small runtime tools the app needs whatever way it was started (installer, or `npm start` from
the repo): ffmpeg, which the engine uses to save each song as .m4a.

At core start we look for it (Ghost Label's own copy, PATH, winget, the usual folders). On Windows,
if it's missing, a pinned portable build (sha256-checked) is downloaded in the background into
the app data folder (Engine/tools/ffmpeg), shown as a job in Activity. macOS can encode with the
built-in afconvert; on macOS/Linux without ffmpeg we log a one-line install hint."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading

EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
INSTALL_HINT = {
    "darwin": "brew install ffmpeg",
    "linux": "sudo apt install ffmpeg   (or your distribution's package manager)",
}


def own_ffmpeg(engine_root):
    return os.path.join(engine_root, "tools", "ffmpeg", "bin", EXE)


def candidates(engine_root, configured=None):
    out = [own_ffmpeg(engine_root), os.path.join(engine_root, "ffmpeg", "bin", EXE)]
    for explicit in (os.environ.get("MUSIC_ENGINE_FFMPEG"), configured):
        if explicit:
            out.insert(0, explicit)
    found = shutil.which("ffmpeg")
    if found:
        out.append(found)
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        out.append(os.path.join(local, "Microsoft", "WinGet", "Links", EXE))
        packages = os.path.join(local, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(packages):
            for pkg in sorted(os.listdir(packages)):
                if "ffmpeg" in pkg.lower():
                    for dirpath, _dirs, files in os.walk(os.path.join(packages, pkg)):
                        if EXE in files:
                            out.append(os.path.join(dirpath, EXE))
        out += [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "ffmpeg", "bin", EXE),
                r"C:\ffmpeg\bin\ffmpeg.exe",
                os.path.join(os.path.expanduser("~"), "scoop", "shims", EXE),
                os.path.join(os.environ.get("ChocolateyInstall", r"C:\ProgramData\chocolatey"), "bin", EXE)]
    else:
        out += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
    return out


NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_tool(args, timeout=300, check=True, text=False):
    """Run ffmpeg/ffprobe without a console window popping up on Windows; output is captured."""
    return subprocess.run(args, capture_output=True, timeout=timeout, check=check, creationflags=NO_WINDOW,
                          **({"text": True, "encoding": "utf-8", "errors": "replace"} if text else {}))


def runs(path):
    try:
        return run_tool([path, "-hide_banner", "-version"], timeout=30, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffprobe_for(ffmpeg):
    """ffprobe next to the ffmpeg we use (portable builds ship both), else PATH."""
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(sibling):
            return sibling
    return shutil.which("ffprobe")


def find_ffmpeg(engine_root, configured=None):
    seen = set()
    for c in candidates(engine_root, configured):
        if c and c not in seen and os.path.isfile(c):
            seen.add(c)
            if runs(c):
                return c
    return None


class Runtime:
    """The one place every ffmpeg user (engine encoding, link clips, reference audio, export) asks
    for ffmpeg, so they all get the same binary the startup check found or installed."""

    def __init__(self, installer, jobs, log=print):
        self.installer, self.jobs, self.log = installer, jobs, log
        self.ffmpeg = None
        self.lock = threading.Lock()

    def configured(self):
        env = (self.installer.settings.section("engine").get("env") or {})
        return env.get("MUSIC_ENGINE_FFMPEG") or None

    def ffmpeg_path(self):
        """The ffmpeg to use now: the one found at start (if it's still there), else a fresh search
        (Ghost Label's own copy, the configured path, PATH, winget, the usual folders); None if none."""
        with self.lock:
            if self.ffmpeg and os.path.isfile(self.ffmpeg):
                return self.ffmpeg
            self.ffmpeg = find_ffmpeg(self.installer.root, self.configured())
            return self.ffmpeg

    def ffprobe_path(self):
        return ffprobe_for(self.ffmpeg_path())

    def check(self):
        """Called at core start (on a thread): find ffmpeg, else fetch it on Windows as a job."""
        self.ffmpeg = None
        self.ffmpeg_path()
        if self.ffmpeg:
            self.log(f"[runtime] ffmpeg: {self.ffmpeg}")
            return None
        if os.name == "nt":
            return self.ensure()
        if sys.platform == "darwin":
            self.log("[runtime] ffmpeg not found; songs are encoded with macOS's afconvert "
                     f"(for ffmpeg: {INSTALL_HINT['darwin']})")
        else:
            self.log(f"[runtime] ffmpeg not found; songs can't be saved until it's installed: {INSTALL_HINT['linux']}")
        return None

    def ensure(self):
        def run(job):
            from smcore.bootstrap import Installer
            # Its own installer object, so this never shares job state with an engine install.
            inst = Installer(self.installer.settings, self.installer.paths, self.installer.engine_dir,
                             self.installer.uv, self.log)
            inst.job = job
            try:
                path = inst.download_ffmpeg()
            finally:
                inst.job = None
            self.ffmpeg = path
            return {"ffmpeg": path}

        return self.jobs.start("runtime", "Installing ffmpeg (used to save songs)", run, {"tool": "ffmpeg"},
                               key="runtime-ffmpeg")
