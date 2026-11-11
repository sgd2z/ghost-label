"""Provider settings persist: save over HTTP, stop the core process, start a new one on the same
data folder, read back. Also: keyring failures fall back to a labeled file (never abort the save),
a stale second core can't clobber newer settings, an unreadable file is kept aside, and 0.2.0's
data folder is migrated."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, CORE)

from smcore import paths as paths_mod  # noqa: E402
from smcore.paths import Paths  # noqa: E402
from smcore.settings import Secrets, Settings  # noqa: E402

PROVIDER = {"baseUrl": "http://localhost:1234/v1", "textModel": "qwen3-30b", "fastModel": "qwen3-4b",
            "temperature": 0.7, "jsonMode": "json_object"}


class CoreProcess:
    def __init__(self, home, env=None):
        e = dict(os.environ, SM_HOME=home, PYTHONUNBUFFERED="1", **(env or {}))
        self.proc = subprocess.Popen([sys.executable, "-m", "smcore", "--port", "0"], cwd=CORE, env=e,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        line = self.proc.stdout.readline()
        assert line.startswith("SMCORE READY"), line
        info = json.loads(line.split(" ", 2)[2])
        self.base, self.token = f"http://127.0.0.1:{info['port']}", info["token"]

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-SM-Token": self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def stop(self, hard=False):
        self.proc.kill() if hard else self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


class PersistAcrossRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_restart_reload(self):
        env = {"SM_SECRET_STORE": "file"}
        core = CoreProcess(self.home, env)
        r = core.call("PUT", "/api/provider", {"provider": PROVIDER, "apiKey": "sk-test-123"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["settingsPath"], os.path.join(self.home, "settings.json"))
        self.assertTrue(r["warnings"] and "not encrypted" in r["warnings"][0])  # file store is labeled
        core.stop(hard=True)  # like a crash or a kill from the OS: nothing gets a chance to flush
        core = CoreProcess(self.home, env)
        try:
            s = core.call("GET", "/api/settings")
            for k, v in PROVIDER.items():
                self.assertEqual(s["provider"][k], v, k)
            st = core.call("GET", "/api/state")
            self.assertTrue(st["hasApiKey"])
            self.assertTrue(st["providerConfigured"])
            # Unrelated saves later (player volume, engine install) keep the provider.
            core.call("PUT", "/api/settings", {"player": {"volume": 0.5}})
        finally:
            core.stop()
        core = CoreProcess(self.home, env)
        try:
            self.assertEqual(core.call("GET", "/api/settings")["provider"]["baseUrl"], PROVIDER["baseUrl"])
        finally:
            core.stop()

    def test_second_core_on_same_folder_replaces_the_first(self):
        first = CoreProcess(self.home, {"SM_SECRET_STORE": "file"})
        second = CoreProcess(self.home, {"SM_SECRET_STORE": "file"})
        try:
            first.proc.wait(timeout=15)  # stopped by the newer core
            self.assertIsNotNone(first.proc.returncode)
            second.call("PUT", "/api/provider", {"provider": PROVIDER})
        finally:
            second.stop()
            first.proc.stdout.close()


class FakeKeyring:
    def __init__(self, fail_set=False, lossy=False):
        self.store, self.fail_set, self.lossy = {}, fail_set, lossy

    def get_keyring(self):
        return self

    def set_password(self, service, name, value):
        if self.fail_set:
            raise RuntimeError("CredWrite failed")
        self.store[(service, name)] = "" if self.lossy else value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


class SecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def secrets(self, kr):
        s = Secrets(self.paths)
        s.keyring, s.load_error = kr, None
        return s

    def test_keyring_not_installed_is_a_calm_note(self):
        saved_env, saved_mod = os.environ.pop("SM_SECRET_STORE", None), sys.modules.get("keyring")
        sys.modules["keyring"] = None          # import keyring -> ImportError, as when not installed
        try:
            s = Secrets(self.paths)
        finally:
            if saved_mod is None:
                sys.modules.pop("keyring", None)
            else:
                sys.modules["keyring"] = saved_mod
            if saved_env is not None:
                os.environ["SM_SECRET_STORE"] = saved_env
        self.assertTrue(s.not_installed)
        r = s.set("apiKey", "sk-1")
        self.assertIsNone(r.get("warning"))          # no orange error for a missing optional module
        self.assertEqual(s.get("apiKey"), "sk-1")    # kept in secrets.json
        self.assertTrue(os.path.exists(self.paths.secrets_file))

    def test_keyring_ok(self):
        s = self.secrets(FakeKeyring())
        r = s.set("apiKey", "sk-1")
        self.assertIsNone(r["warning"])
        self.assertEqual(s.get("apiKey"), "sk-1")
        self.assertFalse(os.path.exists(self.paths.secrets_file))

    def test_keyring_error_falls_back_to_labeled_file(self):
        for kr in (FakeKeyring(fail_set=True), FakeKeyring(lossy=True)):
            s = self.secrets(kr)
            r = s.set("apiKey", "sk-2")
            self.assertIn("local file", r["warning"])
            self.assertTrue(r["insecure"])
            self.assertEqual(s.get("apiKey"), "sk-2")  # read finds the file copy even with a keyring present
            os.remove(self.paths.secrets_file)

    def test_legacy_service_name_is_read(self):
        kr = FakeKeyring()
        kr.store[("StandardMusicDesktop", "apiKey")] = "sk-old"
        self.assertEqual(self.secrets(kr).get("apiKey"), "sk-old")


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = Paths(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_instance_does_not_clobber(self):
        stale = Settings(self.paths)           # e.g. a core left over from an earlier session
        fresh = Settings(self.paths)
        fresh.update({"provider": PROVIDER})
        stale.update({"engine": {"profile": "rocm-windows"}})  # its in-memory copy has no provider
        back = Settings(self.paths).get()
        self.assertEqual(back["provider"]["baseUrl"], PROVIDER["baseUrl"])
        self.assertEqual(back["engine"]["profile"], "rocm-windows")

    def test_unreadable_file_is_kept_and_reported(self):
        with open(self.paths.settings, "w") as f:
            f.write('{"provider": {"baseUrl": "http://x/v1",')
        s = Settings(self.paths)
        self.assertIn("couldn't be read", s.error)
        self.assertTrue(any(n.startswith("settings.json.unreadable-") for n in os.listdir(self.tmp)))

    def test_migrates_old_data_folder(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        with open(os.path.join(old, "settings.json"), "w") as f:
            json.dump({"provider": PROVIDER}, f)
        new = Paths(os.path.join(self.tmp, "new"))
        orig = paths_mod.legacy_homes
        paths_mod.legacy_homes = lambda: [old]
        try:
            self.assertEqual(paths_mod.migrate_legacy(new), old)
            self.assertIsNone(paths_mod.migrate_legacy(new))  # only once
        finally:
            paths_mod.legacy_homes = orig
        self.assertEqual(Settings(new).get()["provider"]["textModel"], PROVIDER["textModel"])


if __name__ == "__main__":
    unittest.main()


class MigrationFromDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.path.join(self.tmp, "old")
        os.makedirs(self.old)
        with open(os.path.join(self.old, "settings.json"), "w") as f:
            json.dump({"provider": PROVIDER}, f)
        self.orig = paths_mod.legacy_homes
        paths_mod.legacy_homes = lambda: [self.old]

    def tearDown(self):
        paths_mod.legacy_homes = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_provider_written_by_021_is_replaced_by_the_old_one(self):
        new = Paths(os.path.join(self.tmp, "new"))
        from smcore.settings import DEFAULTS
        with open(new.settings, "w") as f:
            json.dump({**DEFAULTS, "player": {"volume": 0.4}}, f)
        self.assertEqual(paths_mod.migrate_legacy(new), self.old)
        s = Settings(new).get()
        self.assertEqual(s["provider"]["baseUrl"], PROVIDER["baseUrl"])
        self.assertEqual(s["player"]["volume"], 0.4)

    def test_a_configured_provider_is_left_alone(self):
        new = Paths(os.path.join(self.tmp, "new2"))
        with open(new.settings, "w") as f:
            json.dump({"provider": {"baseUrl": "https://openrouter.ai/api/v1", "textModel": "x/y"}}, f)
        self.assertIsNone(paths_mod.migrate_legacy(new))
        self.assertEqual(Settings(new).get()["provider"]["textModel"], "x/y")
