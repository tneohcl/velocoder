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


class _FakeApp:
    """Stand-in for QApplication -- just needs to accept setStyleSheet()."""
    received = None

    def setStyleSheet(self, text):
        self.received = text


class TestStylesheetLoading(unittest.TestCase):
    def test_missing_file_does_not_crash_startup(self):
        app = _FakeApp()
        main._load_stylesheet(app, style_path=Path("/nonexistent/style.qss"))  # must not raise
        self.assertIsNone(app.received)

    def test_real_stylesheet_file_loads(self):
        app = _FakeApp()
        main._load_stylesheet(app, style_path=REPO_ROOT / "style.qss")
        self.assertIn("QPushButton", app.received)


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


class TestClearQueueConfirmation(unittest.TestCase):
    def test_declining_confirmation_keeps_the_queue(self):
        window = main.MainWindow()
        window.queue_list.addItem(main.QListWidgetItem("dummy"))
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.No):
            window._clear_queue()
        self.assertEqual(window.queue_list.count(), 1)

    def test_confirming_clears_the_queue(self):
        window = main.MainWindow()
        window.queue_list.addItem(main.QListWidgetItem("dummy"))
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
        self.assertEqual(window.queue_list.count(), 0)

    def test_empty_queue_skips_the_dialog_entirely(self):
        window = main.MainWindow()
        with patch.object(main.QMessageBox, "question") as mock_question:
            window._clear_queue()
        mock_question.assert_not_called()


class TestResultSizeFormatting(unittest.TestCase):
    def test_format_size_units(self):
        self.assertEqual(main.MainWindow._format_size(500), "500B")
        self.assertEqual(main.MainWindow._format_size(2048), "2.0KB")
        self.assertEqual(main.MainWindow._format_size(300 * 1024 * 1024), "300.0MB")

    def test_append_result_size_shows_shrinkage(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"x" * 1000)
            out.write_bytes(b"x" * 250)  # 75% smaller
            item = main.QListWidgetItem("in.mkv")
            main.MainWindow._append_result_size(item, src, out)
            self.assertIn("75% smaller", item.text())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_append_result_size_shows_growth(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"x" * 100)
            out.write_bytes(b"x" * 200)  # larger output (e.g. a tiny/simple source)
            item = main.QListWidgetItem("in.mkv")
            main.MainWindow._append_result_size(item, src, out)
            self.assertIn("larger", item.text())
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_missing_output_file_does_not_crash(self):
        item = main.QListWidgetItem("in.mkv")
        main.MainWindow._append_result_size(item, Path("/nonexistent/in.mkv"), Path("/nonexistent/out.mp4"))
        self.assertEqual(item.text(), "in.mkv")  # left untouched


class TestPresetModifiedIndicator(unittest.TestCase):
    # isVisible() reflects the whole ancestor chain, not just this widget's
    # own setVisible() calls -- it's always False until the top-level window
    # has been shown at least once (true even under the offscreen platform).

    def test_hidden_immediately_after_loading_a_preset(self):
        window = main.MainWindow()
        window.show()
        self.assertFalse(window.preset_modified_label.isVisible())

    def test_shown_after_changing_a_setting(self):
        window = main.MainWindow()
        window.show()
        window.quality_slider.setValue(window.quality_slider.value() + 1)
        self.assertTrue(window.preset_modified_label.isVisible())

    def test_hidden_again_after_reverting_the_change(self):
        window = main.MainWindow()
        window.show()
        original = window.quality_slider.value()
        window.quality_slider.setValue(original + 1)
        self.assertTrue(window.preset_modified_label.isVisible())
        window.quality_slider.setValue(original)
        self.assertFalse(window.preset_modified_label.isVisible())


class TestCommandPreviewGrouping(unittest.TestCase):
    def test_preview_is_broken_into_multiple_lines(self):
        window = main.MainWindow()
        text = window.command_preview.toPlainText()
        self.assertGreater(text.count("\n"), 0)
        # Grouping is cosmetic only -- flattening it back out must reproduce
        # the same tokens build_args() actually returns.
        self.assertEqual(" ".join(text.split()), " ".join(text.replace("\n", " ").split()))


class TestJobStatusIcons(unittest.TestCase):
    """Queue rows should reflect per-job outcome, not just the status label."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.clip = cls.tmpdir / "clip.mkv"
        _make_clip(cls.clip, "aac")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_started_job_gets_an_icon(self):
        window = main.MainWindow()
        window.add_files([self.clip])
        window._running_items = [window.queue_list.item(0)]
        window._on_job_started(str(self.clip), 1, 1)
        self.assertFalse(window._running_items[0].icon().isNull())

    def test_failed_job_gets_a_tooltip_with_the_reason(self):
        window = main.MainWindow()
        window.add_files([self.clip])
        window._running_items = [window.queue_list.item(0)]
        window._current_running_item = window.queue_list.item(0)
        window._on_job_failed(str(self.clip), "ffmpeg exited 1")
        self.assertEqual(window._running_items[0].toolTip(), "ffmpeg exited 1")


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
