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

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
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


def _make_progressive_clip(path: Path):
    # Plain testsrc2 has sharp, high-frequency moving edges (color bar
    # boundaries, the checkerboard) that idet itself false-positives on --
    # confirmed empirically: a bare testsrc2 clip reads 100% TFF despite
    # being genuinely progressive, unrelated to this app's own detection
    # logic. A mild blur softens exactly the edges idet is confused by
    # while keeping the same real per-frame motion; verified this gives a
    # clean, unambiguous 100% Progressive read.
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1,gblur=sigma=2",
         "-c:v", "libx264", str(path)],
        check=True, timeout=30,
    )


def _make_interlaced_clip(path: Path):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=60:duration=1",
         "-vf", "tinterlace=interleave_top", "-c:v", "libx264", str(path)],
        check=True, timeout=30,
    )


def _wait_for_detection(window, timeout_ms=15000):
    """Pump the event loop until every in-flight interlace-detection
    QProcess this window started has finished, or the timeout elapses."""
    loop = QEventLoop()
    timeout_timer = QTimer()
    timeout_timer.setSingleShot(True)
    timeout_timer.timeout.connect(loop.quit)

    poll_timer = QTimer()

    def check():
        still_running = any(p.state() != main.QProcess.NotRunning for p in window._detection_processes)
        if not still_running:
            loop.quit()

    poll_timer.timeout.connect(check)
    poll_timer.start(50)
    timeout_timer.start(timeout_ms)
    check()
    loop.exec()
    poll_timer.stop()
    timeout_timer.stop()


def _add_dummy_item(window, name: str, **overrides) -> "main.QListWidgetItem":
    """Add a queue item bypassing add_files() -- no on-disk file needed
    (add_files requires path.is_file()) and no real detection subprocess
    spawned, for tests that only care about the selection/settings sync."""
    job = {"path": Path(name), **window._current_settings()}
    job.update(overrides)
    item = main.QListWidgetItem(window._format_item_text(job))
    item.setData(main.Qt.UserRole, job)
    window.queue_list.addItem(item)
    return item


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


class TestAutoDetectInterlaceOnAdd(unittest.TestCase):
    """Dropping a file in should probe it and flip Deinterlace automatically
    -- see worker.TestIdetHelpers/TestDeinterlace for the detection/fix
    logic itself; this covers the async GUI wiring around it."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.interlaced_clip = cls.tmpdir / "interlaced.mkv"
        _make_interlaced_clip(cls.interlaced_clip)
        cls.progressive_clip = cls.tmpdir / "progressive.mkv"
        _make_progressive_clip(cls.progressive_clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_interlaced_file_gets_deinterlace_enabled_automatically(self):
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        _wait_for_detection(window)
        job = window.queue_list.item(0).data(main.Qt.UserRole)
        self.assertTrue(job["deinterlace"])
        self.assertIn("Deinterlace", window.queue_list.item(0).text())

    def test_progressive_file_stays_off(self):
        window = main.MainWindow()
        window.add_files([self.progressive_clip])
        _wait_for_detection(window)
        job = window.queue_list.item(0).data(main.Qt.UserRole)
        self.assertFalse(job["deinterlace"])

    def test_detection_overrides_a_manually_checked_box_when_source_is_progressive(self):
        # A real override in both directions, not a one-way ratchet --
        # otherwise a progressive file queued after the user manually
        # enabled the checkbox for a previous interlaced file would
        # incorrectly stay marked for deinterlacing.
        window = main.MainWindow()
        window.deinterlace_check.setChecked(True)
        window.add_files([self.progressive_clip])
        _wait_for_detection(window)
        job = window.queue_list.item(0).data(main.Qt.UserRole)
        self.assertFalse(job["deinterlace"])

    def test_removing_the_item_before_detection_finishes_does_not_crash(self):
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window.queue_list.clear()  # deletes the C++ item object, not just detaches it
        _wait_for_detection(window)  # must not raise from the now-deleted item


class TestLiveSelectionEditing(unittest.TestCase):
    """Replaces the old 'Apply Settings to Selected' button: selecting a
    queue row loads its settings into the controls, and changing a control
    while row(s) are selected applies live."""

    def test_selecting_an_item_populates_controls_from_its_settings(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv", quality_value=41)
        item.setSelected(True)
        self.assertEqual(window.quality_slider.value(), 41)

    def test_changing_a_control_applies_live_to_the_selected_item(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setSelected(True)
        window.quality_slider.setValue(window.quality_slider.value() + 3)
        job = item.data(main.Qt.UserRole)
        self.assertEqual(job["quality_value"], window.quality_slider.value())

    def test_changing_a_control_does_not_touch_unselected_items(self):
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv")
        item_b = _add_dummy_item(window, "b.mkv")
        item_a.setSelected(True)
        before_b = dict(item_b.data(main.Qt.UserRole))
        window.quality_slider.setValue(window.quality_slider.value() + 3)
        self.assertEqual(item_b.data(main.Qt.UserRole), before_b)

    def test_selecting_multiple_differently_configured_items_does_not_homogenize_them(self):
        # Regression: populating controls from item_a on selection must not
        # cascade into overwriting item_b's (deliberately different) settings.
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv", quality_value=20)
        item_b = _add_dummy_item(window, "b.mkv", quality_value=35)
        before_b = dict(item_b.data(main.Qt.UserRole))
        item_a.setSelected(True)
        item_b.setSelected(True)
        self.assertEqual(item_b.data(main.Qt.UserRole), before_b)

    def test_multi_select_then_control_change_applies_to_all_selected(self):
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv", quality_value=20)
        item_b = _add_dummy_item(window, "b.mkv", quality_value=35)
        item_a.setSelected(True)
        item_b.setSelected(True)
        window.quality_slider.setValue(17)
        self.assertEqual(item_a.data(main.Qt.UserRole)["quality_value"], 17)
        self.assertEqual(item_b.data(main.Qt.UserRole)["quality_value"], 17)

    def test_locked_queue_during_a_run_ignores_selection_edits(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setSelected(True)
        window._set_queue_editable(False)
        before = dict(item.data(main.Qt.UserRole))
        window.quality_slider.setValue(window.quality_slider.value() + 3)
        self.assertEqual(item.data(main.Qt.UserRole), before)


class TestQueueLockingDuringRun(unittest.TestCase):
    def test_set_queue_editable_toggles_buttons(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        self.assertFalse(window.add_files_btn.isEnabled())
        self.assertFalse(window.remove_btn.isEnabled())
        self.assertFalse(window.clear_btn.isEnabled())
        self.assertFalse(window._queue_editable)
        window._set_queue_editable(True)
        self.assertTrue(window.add_files_btn.isEnabled())
        self.assertTrue(window._queue_editable)

    def test_start_locks_queue_before_handing_off_to_the_engine(self):
        window = main.MainWindow()
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addItem(main.QListWidgetItem("dummy"))
        window.queue_list.item(0).setData(main.Qt.UserRole, job)
        with patch.object(window.queue, "start") as mock_start:
            window._start()
            mock_start.assert_called_once()
        self.assertFalse(window.add_files_btn.isEnabled())
        self.assertFalse(window._queue_editable)

    def test_on_all_finished_unlocks_queue(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        window._on_all_finished()
        self.assertTrue(window.add_files_btn.isEnabled())
        self.assertTrue(window.clear_btn.isEnabled())


if __name__ == "__main__":
    unittest.main()
