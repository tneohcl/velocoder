"""Regression tests for main.py (the PySide6 GUI).

Run with:  python3 -m unittest discover -s tests -v
Runs headless via the "offscreen" Qt platform plugin (set below) -- no
real display needed, and no window is ever actually shown.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

import main  # noqa: E402


def _make_clip(path: Path, audio_codec: str):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-c:a", audio_codec, "-shortest", str(path)],
        check=True, timeout=30,
    )


class TestStartupOrdering(unittest.TestCase):
    """rc_mode_combo must be populated before any preset gets applied."""

    def test_rc_mode_populated_after_construction(self):
        window = main.MainWindow()
        self.assertIsNotNone(window.rc_mode_combo.currentData())
        self.assertGreater(window.rc_mode_combo.count(), 0)

    def test_quality_label_has_no_none_placeholder(self):
        window = main.MainWindow()
        self.assertNotIn("None", window.quality_label.text())


class TestCommandPreviewErrorHandling(unittest.TestCase):
    def test_no_vaapi_device_shows_message_not_crash(self):
        window = main.MainWindow()
        with patch.object(
            main.worker, "find_render_node",
            side_effect=RuntimeError("no render node found"),
        ):
            window.encoder_combo.setCurrentIndex(0)  # VAAPI HEVC -- triggers a rebuild + preview
            window._update_command_preview()  # must not raise
        self.assertIn("preview unavailable", window.command_preview.toPlainText())


class TestCommandPreviewAudioAccuracy(unittest.TestCase):
    """The preview must reflect a real queued file's actual audio, not guess."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.mp3_clip = cls.tmpdir / "mp3_clip.mkv"
        _make_clip(cls.mp3_clip, "mp3")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_preview_reflects_real_source_codec(self):
        window = main.MainWindow()
        window.add_files([self.mp3_clip])
        preview_text = window.command_preview.toPlainText()
        # mp3 is never copy-compatible -- preview must show the real
        # transcode, not the old hardcoded "aac -> always copy" guess.
        self.assertIn("-c:a aac", preview_text)
        self.assertNotIn("-c:a copy", preview_text)

    def test_empty_queue_omits_audio_flags_rather_than_guessing(self):
        window = main.MainWindow()
        preview_text = window.command_preview.toPlainText()
        self.assertNotIn("-c:a", preview_text)


class TestQueueLockingDuringRun(unittest.TestCase):
    def test_set_queue_editable_toggles_buttons(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        self.assertFalse(window.add_files_btn.isEnabled())
        self.assertFalse(window.remove_btn.isEnabled())
        self.assertFalse(window.clear_btn.isEnabled())
        self.assertFalse(window.apply_btn.isEnabled())
        window._set_queue_editable(True)
        self.assertTrue(window.add_files_btn.isEnabled())
        self.assertTrue(window.apply_btn.isEnabled())

    def test_start_locks_queue_before_handing_off_to_the_engine(self):
        window = main.MainWindow()
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addItem(main.QListWidgetItem("dummy"))
        window.queue_list.item(0).setData(main.Qt.UserRole, job)
        with patch.object(window.queue, "start") as mock_start:
            window._start()
            mock_start.assert_called_once()
        self.assertFalse(window.add_files_btn.isEnabled())
        self.assertFalse(window.apply_btn.isEnabled())

    def test_on_all_finished_unlocks_queue(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        window._on_all_finished()
        self.assertTrue(window.add_files_btn.isEnabled())
        self.assertTrue(window.clear_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
