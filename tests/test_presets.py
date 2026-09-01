"""Regression tests for presets.py (user preset persistence).

Run with:  python3 -m unittest discover -s tests -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import presets  # noqa: E402


class TestUserPresets(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        self.path = self.tmpdir / "user_presets.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(presets.load_user_presets(self.path), [])

    def test_save_then_load_round_trips(self):
        data = [{"name": "My Preset", "encoder": "hevc_vaapi", "quality_value": 30}]
        presets.save_user_presets(data, self.path)
        self.assertEqual(presets.load_user_presets(self.path), data)

    def test_save_overwrites_file_contents(self):
        presets.save_user_presets([{"name": "First"}], self.path)
        presets.save_user_presets([{"name": "Second"}], self.path)
        loaded = presets.load_user_presets(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["name"], "Second")


if __name__ == "__main__":
    unittest.main()
