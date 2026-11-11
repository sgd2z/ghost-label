"""Where the installer puts the engine, and how to find it again without trusting settings.json.

<home>/Engine/
  venv/                       the engine's Python (Windows: Scripts\\python.exe, else bin/python)
  ACE-Step-1.5/acestep/       ACE-Step source (not pip-installed; found via a .pth file and env)
  ACE-Step-1.5/checkpoints/   model weights
  ghostlabel-engine.json      what the last successful setup produced (python, root, env, profile)

Both layouts are checked on every OS so a Windows tree can be tested anywhere."""

from __future__ import annotations

import glob
import json
import os

PTH_NAME = "ghostlabel-acestep.pth"
MANIFEST = "ghostlabel-engine.json"
ROCM_WINDOWS_ENV = {"ACESTEP_ROCM_DTYPE": "float32", "ACESTEP_LM_BACKEND": "pt", "MIOPEN_FIND_MODE": "FAST",
                    "TORCH_COMPILE_BACKEND": "eager", "TOKENIZERS_PARALLELISM": "false"}


class EngineLayout:
    def __init__(self, envs_dir: str):
        self.root = envs_dir
        self.venv = os.path.join(envs_dir, "venv")
        self.ace = os.path.join(envs_dir, "ACE-Step-1.5")
        self.checkpoints = os.path.join(self.ace, "checkpoints")
        self.manifest_path = os.path.join(envs_dir, MANIFEST)

    # MARK: python

    def python_candidates(self):
        win = os.path.join(self.venv, "Scripts", "python.exe")
        posix = [os.path.join(self.venv, "bin", n) for n in ("python", "python3")]
        return [win] + posix if os.name == "nt" else posix + [win]

    def default_python(self) -> str:
        """The path the installer creates on this OS (whether or not it exists yet)."""
        return self.python_candidates()[0]

    def python(self) -> str | None:
        """The engine venv's interpreter if it exists."""
        return next((p for p in self.python_candidates() if os.path.isfile(p)), None)

    def site_packages(self) -> str | None:
        win = os.path.join(self.venv, "Lib", "site-packages")
        if os.path.isdir(win):
            return win
        found = sorted(glob.glob(os.path.join(self.venv, "lib", "python3*", "site-packages")))
        return found[-1] if found else None

    # MARK: ACE-Step

    @staticmethod
    def is_ace_root(path) -> bool:
        return bool(path) and os.path.isfile(os.path.join(path, "acestep", "__init__.py"))

    def ace_ok(self) -> bool:
        return self.is_ace_root(self.ace)

    def pth_path(self):
        site = self.site_packages()
        return os.path.join(site, PTH_NAME) if site else None

    def write_pth(self) -> str | None:
        """Make `import acestep` work in the venv regardless of cwd or environment (a plain path
        line in a .pth file; no pip/uv editable install, which would re-resolve torch)."""
        path = self.pth_path()
        if not path:
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(os.path.abspath(self.ace) + "\n")
        return path

    def pth_ok(self) -> bool:
        path = self.pth_path()
        if not path or not os.path.isfile(path):
            return False
        with open(path, encoding="utf-8") as f:
            return os.path.normcase(f.read().strip()) == os.path.normcase(os.path.abspath(self.ace))

    def weights(self):
        """(files, bytes) of model weights present (no network)."""
        n = size = 0
        for dirpath, _dirs, files in os.walk(self.checkpoints):
            if ".cache" in dirpath:
                continue
            for f in files:
                if f.endswith((".safetensors", ".bin", ".pt", ".ckpt")):
                    n += 1
                    size += os.path.getsize(os.path.join(dirpath, f))
        return n, size

    # MARK: manifest

    def read_manifest(self) -> dict:
        try:
            with open(self.manifest_path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_manifest(self, data: dict):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.manifest_path)

    def default_env(self, profile=None) -> dict:
        """Environment the engine needs from this install: the ACE-Step root plus the profile's
        variables (from the manifest, else derived from the profile)."""
        m = self.read_manifest()
        env = dict(m.get("env") or {})
        if not env and (profile or m.get("profile")) == "rocm-windows":
            env.update(ROCM_WINDOWS_ENV)
        if self.ace_ok():
            env["ACESTEP_PROJECT_ROOT"] = os.path.abspath(self.ace)
        return env
