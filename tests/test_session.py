"""Regression tests for session.py -- the plain file-I/O layer for cross-
session queue persistence (no MainWindow/queue_controller involved here,
see test_main.py's TestSessionPersistence for the integration side).

Run with:  python3 -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import session  # noqa: E402


class TestSessionFilePath(unittest.TestCase):
    def test_ends_in_session_json(self):
        self.assertEqual(session.session_file_path().name, "session.json")


class TestSaveAndLoadRoundTrip(unittest.TestCase):
    def test_round_trips_jobs_and_output_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with patch.object(session, "session_file_path", return_value=path):
                jobs = [
                    {"path": Path("/videos/a.mov"), "encoder": "libx265", "gpu_vendor": None, "quality_value": 23},
                    {"path": Path("/videos/b.mov"), "encoder": "hevc_vaapi", "gpu_vendor": "intel", "quality_value": 26},
                ]
                session.save_session(jobs, Path("/videos/out"))
                loaded = session.load_session()
        self.assertEqual(loaded["version"], session.SESSION_VERSION)
        self.assertEqual(loaded["output_dir"], "/videos/out")
        self.assertEqual(len(loaded["jobs"]), 2)
        # path comes back as a plain string -- turning it back into a
        # real Path (and deciding what to do if it no longer exists) is
        # the caller's job, not this module's.
        self.assertEqual(loaded["jobs"][0]["path"], "/videos/a.mov")
        self.assertEqual(loaded["jobs"][1]["gpu_vendor"], "intel")

    def test_creates_parent_directory_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "session.json"
            with patch.object(session, "session_file_path", return_value=path):
                session.save_session([], Path("/videos/out"))
                self.assertTrue(path.exists())

    def test_strips_completed_output_path_defensively(self):
        # Belt-and-suspenders: the caller (queue_controller.py) is
        # expected to already filter completed jobs out entirely before
        # calling this, but this key should never survive a round trip
        # even if one slips through.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with patch.object(session, "session_file_path", return_value=path):
                session.save_session(
                    [{"path": Path("/videos/a.mov"), "_completed_output_path": "/videos/a_out.mov"}],
                    Path("/videos/out"),
                )
                loaded = session.load_session()
        self.assertNotIn("_completed_output_path", loaded["jobs"][0])

    def test_no_leftover_tmp_file_after_a_successful_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with patch.object(session, "session_file_path", return_value=path):
                session.save_session([], Path("/videos/out"))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["session.json"])

    def test_overwrites_a_previous_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            with patch.object(session, "session_file_path", return_value=path):
                session.save_session([{"path": Path("/videos/a.mov")}], Path("/videos/out1"))
                session.save_session([{"path": Path("/videos/b.mov")}], Path("/videos/out2"))
                loaded = session.load_session()
        self.assertEqual(loaded["output_dir"], "/videos/out2")
        self.assertEqual(len(loaded["jobs"]), 1)
        self.assertEqual(loaded["jobs"][0]["path"], "/videos/b.mov")


class TestLoadSessionFailureModes(unittest.TestCase):
    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "does_not_exist.json"
            with patch.object(session, "session_file_path", return_value=path):
                self.assertIsNone(session.load_session())

    def test_corrupt_json_returns_none_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text("{not valid json")
            with patch.object(session, "session_file_path", return_value=path):
                self.assertIsNone(session.load_session())

    def test_a_bare_json_list_instead_of_an_object_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(json.dumps([1, 2, 3]))
            with patch.object(session, "session_file_path", return_value=path):
                self.assertIsNone(session.load_session())

    def test_unrecognized_version_returns_none(self):
        # Never guesses at an unfamiliar format -- "nothing to restore"
        # is always safe, misinterpreting a future (or past) shape isn't.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(json.dumps({"version": 999, "output_dir": "/x", "jobs": []}))
            with patch.object(session, "session_file_path", return_value=path):
                self.assertIsNone(session.load_session())

    def test_missing_version_key_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text(json.dumps({"output_dir": "/x", "jobs": []}))
            with patch.object(session, "session_file_path", return_value=path):
                self.assertIsNone(session.load_session())


if __name__ == "__main__":
    unittest.main()
