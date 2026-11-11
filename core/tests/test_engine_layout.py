"""The engine is found from the install layout even when settings.json lost python/env (the
Windows report: python "" and no ACESTEP_PROJECT_ROOT). Windows trees (venv\\Scripts\\python.exe,
venv\\Lib\\site-packages) are simulated on any OS; a real venv checks the .pth link and the
setup's import check end to end with stand-in acestep/torch packages."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["SM_SECRET_STORE"] = "file"

from smcore.bootstrap import Installer  # noqa: E402
from smcore.engine_client import EngineClient, EngineError  # noqa: E402
from smcore.engine_layout import EngineLayout  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Settings  # noqa: E402

FAKE_ACESTEP = os.path.join(REPO, "engine", "tests", "fakes", "acestep")
FAKE_TORCH = os.path.join(REPO, "engine", "tests", "fakes_torch", "torch")
ENGINE_DIR = os.path.join(REPO, "engine")


class Events:
    def emit(self, *a, **k):
        pass


class FakeJob:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.messages = []

    def check(self):
        pass

    def progress(self, fraction, message=None):
        if message:
            self.messages.append(message)

    def log(self, message):
        self.messages.append(message)

    def tail(self, line):
        pass

    def set_flags(self, **flags):
        pass


def touch(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class WindowsLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(os.path.join(self.tmp, "GhostLabel"))
        self.settings = Settings(self.paths)
        eng = self.paths.envs
        self.py = os.path.join(eng, "venv", "Scripts", "python.exe")
        touch(self.py)
        touch(os.path.join(eng, "venv", "Lib", "site-packages", "placeholder.txt"))
        self.ace = os.path.join(eng, "ACE-Step-1.5")
        touch(os.path.join(self.ace, "acestep", "__init__.py"))
        touch(os.path.join(self.ace, "checkpoints", "acestep-v15-turbo", "model.safetensors"), "x" * 10)
        # What his settings.json held after setup: profile kept, python and env gone.
        self.settings.update({"engine": {"kind": "builtin", "backend": "acestep", "profile": "rocm-windows",
                                         "python": "", "env": {}}})
        self.client = EngineClient(self.settings, self.paths, ENGINE_DIR, Events(), log=lambda *_: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_settings_still_launch_the_venv_with_the_ace_root(self):
        cmd, cwd = self.client.command()
        self.assertEqual(cmd[0], self.py)
        self.assertEqual(cwd, ENGINE_DIR)
        env = self.client.environment()
        self.assertEqual(env["ACESTEP_PROJECT_ROOT"], os.path.abspath(self.ace))
        self.assertEqual(env["ACESTEP_ROCM_DTYPE"], "float32")      # ROCm profile defaults come back too
        self.assertEqual(env["GHOSTLABEL_ENGINE_ROOT"], self.paths.envs)

    def test_stale_or_wrong_settings_are_overridden(self):
        self.settings.update({"engine": {"python": sys.executable, "env": {"ACESTEP_PROJECT_ROOT": "C:\\nowhere", "EXTRA": "1"}}})
        cmd, _ = self.client.command()
        self.assertEqual(cmd[0], self.py)
        env = self.client.environment()
        self.assertEqual(env["ACESTEP_PROJECT_ROOT"], os.path.abspath(self.ace))
        self.assertEqual(env["EXTRA"], "1")

    def test_no_venv_is_a_clear_error_except_for_mock(self):
        os.remove(self.py)
        with self.assertRaises(EngineError) as ctx:
            self.client.command()
        self.assertIn("Install engine", str(ctx.exception))
        self.settings.update({"engine": {"backend": "mock"}})
        self.assertEqual(self.client.command()[0][0], sys.executable)

    def test_layout_paths(self):
        layout = EngineLayout(self.paths.envs)
        self.assertEqual(layout.python(), self.py)
        self.assertEqual(layout.site_packages(), os.path.join(self.paths.envs, "venv", "Lib", "site-packages"))
        self.assertTrue(layout.ace_ok())
        path = layout.write_pth()
        self.assertTrue(layout.pth_ok())
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), os.path.abspath(self.ace))
        self.assertEqual(layout.weights()[0], 1)

    def test_save_engine_persists_python_root_and_manifest(self):
        inst = Installer(self.settings, self.paths, ENGINE_DIR, log=lambda *_: None)
        inst.save_engine("rocm-windows", {"ACESTEP_ROCM_DTYPE": "float32"}, {"rocm": "10.0.0"})
        eng = Settings(self.paths).section("engine")
        self.assertEqual(eng["python"], self.py)
        self.assertEqual(eng["env"]["ACESTEP_PROJECT_ROOT"], os.path.abspath(self.ace))
        self.assertEqual(EngineLayout(self.paths.envs).read_manifest()["python"], self.py)


class RealVenvTests(unittest.TestCase):
    """A real venv: before linking, `import acestep` fails exactly like his; after, the setup check
    (the backend's own imports, run from the engine folder with no env) passes; repair fixes settings."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.paths = Paths(os.path.join(cls.tmp, "GhostLabel"))
        venv = os.path.join(cls.paths.envs, "venv")
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", "--system-site-packages", venv], check=True)
        layout = EngineLayout(cls.paths.envs)
        shutil.copytree(FAKE_TORCH, os.path.join(layout.site_packages(), "torch"))
        shutil.copytree(FAKE_ACESTEP, os.path.join(layout.ace, "acestep"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        touch(os.path.join(layout.checkpoints, "acestep-v15-turbo", "model.safetensors"), "x")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_link_check_and_repair(self):
        settings = Settings(self.paths)
        settings.update({"engine": {"kind": "builtin", "backend": "acestep", "profile": "cpu", "python": "", "env": {}}})
        inst = Installer(settings, self.paths, ENGINE_DIR, log=lambda *_: None)
        layout = inst.layout
        if os.path.exists(layout.pth_path()):
            os.remove(layout.pth_path())
        ok, text = inst.check_acestep()
        self.assertFalse(ok)
        self.assertIn("No module named 'acestep'", text)
        inst.link_acestep()
        ok, text = inst.check_acestep()
        self.assertTrue(ok, text)
        self.assertIn(os.path.abspath(layout.ace), text)

        os.remove(layout.pth_path())
        out = inst.repair(FakeJob())
        self.assertIn("linked ACE-Step into the engine environment", out["repaired"])
        eng = Settings(self.paths).section("engine")
        self.assertEqual(eng["python"], layout.python())
        self.assertEqual(eng["env"]["ACESTEP_PROJECT_ROOT"], os.path.abspath(layout.ace))
        self.assertTrue(layout.pth_ok())


class FetchIntoExistingFolderTests(unittest.TestCase):
    def test_source_merges_next_to_existing_checkpoints(self):
        import zipfile
        tmp = tempfile.mkdtemp()
        try:
            paths = Paths(os.path.join(tmp, "GhostLabel"))
            inst = Installer(Settings(paths), paths, ENGINE_DIR, log=lambda *_: None)
            touch(os.path.join(inst.ace, "checkpoints", "keep.safetensors"), "weights")
            zpath = os.path.join(tmp, "src.zip")
            with zipfile.ZipFile(zpath, "w") as z:
                z.writestr("ACE-Step-1.5-main/acestep/__init__.py", "")
                z.writestr("ACE-Step-1.5-main/requirements-rocm.txt", "x\n")
            inst.fetch_file = lambda url, name: zpath
            inst.fetch_acestep()
            self.assertTrue(os.path.isfile(os.path.join(inst.ace, "acestep", "__init__.py")))
            self.assertTrue(os.path.isfile(os.path.join(inst.ace, "requirements-rocm.txt")))
            self.assertTrue(os.path.isfile(os.path.join(inst.ace, "checkpoints", "keep.safetensors")))
            self.assertFalse(os.path.exists(os.path.join(inst.ace, "ACE-Step-1.5-main")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
