"""Regression tests for presets.py -- this fork keeps only
load_builtin_presets() (see presets.py's own module docstring for why:
it's a fixture-data source for tests like TestSettingsSummary in
test_main.py, not a live app feature -- this fork has no Presets UI and
never reads or writes a user presets.json).

Run with:  python3 -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import presets  # noqa: E402


class TestLoadBuiltinPresets(unittest.TestCase):
    def test_loads_the_real_shipped_file(self):
        # Confirms the real, shipped builtin_presets.json (not a synthetic
        # fixture) is actually valid and has the shape tests that use it
        # as fixture data expect.
        loaded = presets.load_builtin_presets()
        # x265, x264, Intel VAAPI, AMD VAAPI -- 4 encoder profiles, each
        # with its own High/Balanced/Low trio.
        self.assertEqual(len(loaded), 12)
        names = {p["name"] for p in loaded}
        self.assertIn("720p Intel Balanced (Hardware / VAAPI)", names)
        self.assertIn("720p CPU Balanced (Software / x264)", names)

    def test_missing_file_raises_instead_of_silently_returning_empty(self):
        # Deliberately no try/except in load_builtin_presets -- a missing
        # shipped seed file is a packaging problem, not recoverable state.
        with self.assertRaises(OSError):
            presets.load_builtin_presets(Path(tempfile.mkdtemp()) / "nonexistent.json")


if __name__ == "__main__":
    unittest.main()
