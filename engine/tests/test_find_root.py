"""The ACE-Step backend finds the clone from the Ghost Label layout when nothing is configured."""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine_host.backends.acestep import find_acestep_root  # noqa: E402


class FindRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.engine = os.path.join(self.tmp, "Engine")
        self.ace = os.path.join(self.engine, "ACE-Step-1.5")
        os.makedirs(os.path.join(self.ace, "acestep"))
        open(os.path.join(self.ace, "acestep", "__init__.py"), "w").close()
        self.saved = (os.environ.pop("GHOSTLABEL_ENGINE_ROOT", None), sys.prefix)

    def tearDown(self):
        if self.saved[0] is not None:
            os.environ["GHOSTLABEL_ENGINE_ROOT"] = self.saved[0]
        else:
            os.environ.pop("GHOSTLABEL_ENGINE_ROOT", None)
        sys.prefix = self.saved[1]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_configured_root_wins_when_valid(self):
        self.assertEqual(find_acestep_root(self.ace)[0], self.ace)

    def test_engine_root_env(self):
        os.environ["GHOSTLABEL_ENGINE_ROOT"] = self.engine
        self.assertEqual(find_acestep_root(None)[0], self.ace)
        self.assertEqual(find_acestep_root(os.path.join(self.tmp, "wrong"))[0], self.ace)

    def test_sibling_of_the_venv(self):
        sys.prefix = os.path.join(self.engine, "venv")   # <Engine>/venv -> <Engine>/ACE-Step-1.5
        root, checked = find_acestep_root(None)
        self.assertEqual(root, self.ace)
        self.assertIn(self.ace, checked)


if __name__ == "__main__":
    unittest.main()
