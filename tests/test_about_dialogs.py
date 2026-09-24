"""Regression tests for about_dialogs.py -- AboutDialog, SystemInfoDialog,
LicensesDialog. Integration coverage (menu action wiring, the actual
_show_about_dialog call site) lives in tests/test_main.py; this file
tests these three dialogs directly, the same split test_session.py/
test_main.py already use for session.py.

Run with:  python3 -m unittest discover -s tests -v
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_app = QApplication.instance() or QApplication([])

import about_dialogs  # noqa: E402
from constants import APP_NAME, APP_ORGANIZATION, APP_VERSION  # noqa: E402
from worker import ProcessingBackend, UnusableGpu  # noqa: E402


class TestSystemInfoText(unittest.TestCase):
    def test_shows_mocked_cpu_only(self):
        text = about_dialogs.system_info_text([ProcessingBackend("cpu", "CPU")], "Dark", "6.1.1")
        self.assertIn("Available Processing:\n- CPU\n", text)
        self.assertNotIn("Intel", text)
        self.assertNotIn("AMD", text)

    def test_shows_mocked_intel_amd_cpu(self):
        backends = [
            ProcessingBackend("cpu", "CPU"),
            ProcessingBackend("intel", "Intel"),
            ProcessingBackend("amd", "AMD"),
        ]
        text = about_dialogs.system_info_text(backends, "Dark", "6.1.1")
        self.assertIn("- CPU", text)
        self.assertIn("- Intel", text)
        self.assertIn("- AMD", text)

    def test_no_unusable_gpus_means_no_unavailable_section(self):
        text = about_dialogs.system_info_text([ProcessingBackend("cpu", "CPU")], "Dark", "7.0")
        self.assertNotIn("Unavailable", text)

    def test_unusable_gpu_is_listed_without_ffmpegs_raw_reason(self):
        # The reason is ffmpeg's own error text and can name a device
        # path -- debug log only, never copied System Information.
        text = about_dialogs.system_info_text(
            [ProcessingBackend("cpu", "CPU"), ProcessingBackend("amd", "AMD")], "Dark", "7.0",
            [UnusableGpu("intel", "Intel", "[hevc_vaapi] profile not supported on /dev/dri/renderD128")],
        )
        self.assertIn("Unavailable Processing:\n- Intel (found, but its video driver can't encode)\n", text)
        self.assertNotIn("/dev/dri", text)
        self.assertNotIn("hevc_vaapi", text)

    def test_includes_app_name_version_theme_and_ffmpeg_version(self):
        text = about_dialogs.system_info_text([ProcessingBackend("cpu", "CPU")], "Light", "7.0")
        self.assertIn(f"{APP_NAME} {APP_VERSION}", text)
        self.assertIn("Theme: Light", text)
        self.assertIn("FFmpeg: 7.0", text)

    def test_missing_ffmpeg_shows_not_found_not_a_crash(self):
        text = about_dialogs.system_info_text([ProcessingBackend("cpu", "CPU")], "Dark", None)
        self.assertIn("FFmpeg: Not found", text)

    def test_contains_no_paths_usernames_or_queue_filenames(self):
        # The actual guarantee this function makes: its only inputs are
        # app constants, platform.*, each backend's own display_name,
        # and the theme name -- none of which can ever be a filesystem
        # path, a username, or a queued video's filename, so this checks
        # the promise holds for a batch of suspicious substrings a real
        # regression might introduce (e.g. someone later passes a Path
        # in by mistake).
        text = about_dialogs.system_info_text(
            [ProcessingBackend("cpu", "CPU"), ProcessingBackend("intel", "Intel")], "Dark", "7.0",
        )
        for forbidden in ("/home/", "/Users/", "C:\\", ".mp4", ".mkv", ".mov", "queue"):
            self.assertNotIn(forbidden, text)


class TestSystemInfoDialog(unittest.TestCase):
    def test_copy_button_copies_exactly_the_displayed_text(self):
        dialog = about_dialogs.SystemInfoDialog(
            None, [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        dialog._copy()
        self.assertEqual(QApplication.clipboard().text(), dialog._text)

    def test_copied_text_contains_no_paths_or_queue_filenames(self):
        dialog = about_dialogs.SystemInfoDialog(
            None, [ProcessingBackend("cpu", "CPU"), ProcessingBackend("amd", "AMD")], "Light", "6.0",
        )
        dialog._copy()
        copied = QApplication.clipboard().text()
        for forbidden in ("/home/", "/Users/", "C:\\", ".mp4", ".mkv", ".mov"):
            self.assertNotIn(forbidden, copied)


class TestLicensesDialog(unittest.TestCase):
    def test_opens_and_mentions_ffmpeg_attribution(self):
        dialog = about_dialogs.LicensesDialog(None)
        self.assertEqual(dialog.windowTitle(), "Licenses")
        # findChild covers any QPlainTextEdit inside, regardless of a
        # future internal restructuring of this dialog's own layout.
        from PySide6.QtWidgets import QPlainTextEdit
        text_view = dialog.findChild(QPlainTextEdit)
        self.assertIsNotNone(text_view)
        self.assertIn("FFmpeg", text_view.toPlainText())


class TestAboutDialog(unittest.TestCase):
    def test_shows_the_central_app_version(self):
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        from PySide6.QtWidgets import QLabel
        labels_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn(APP_VERSION, labels_text)
        self.assertIn(APP_NAME, labels_text)

    def test_shows_the_central_app_organization_in_the_copyright_line(self):
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        from PySide6.QtWidgets import QLabel
        labels_text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn(APP_ORGANIZATION, labels_text)

    def test_is_a_compact_fixed_size_dialog(self):
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        # Fixed, not resizable -- a small identity card, not a workspace.
        self.assertEqual(dialog.minimumSize(), dialog.maximumSize())

    def test_system_information_button_opens_system_info_dialog(self):
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        # Patches the class, not just .exec -- clicking must actually
        # construct a real SystemInfoDialog(...), not skip straight to
        # some other path. .exec is patched on the class's own return
        # value so the real modal event loop never runs in a test.
        with patch.object(about_dialogs, "SystemInfoDialog") as mock_cls:
            btn = next(b for b in dialog.findChildren(QPushButton) if b.text() == "System Information…")
            btn.click()
        mock_cls.return_value.exec.assert_called_once()

    def test_licenses_button_opens_licenses_dialog(self):
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        with patch.object(about_dialogs, "LicensesDialog") as mock_cls:
            btn = next(b for b in dialog.findChildren(QPushButton) if b.text() == "Licenses…")
            btn.click()
        mock_cls.return_value.exec.assert_called_once()

    def test_escape_closes_the_dialog(self):
        # QDialog's own default keyPressEvent handles this (reject() on
        # Escape) -- nothing in AboutDialog overrides it, so this locks
        # the behavior in rather than trusting it stays that way by
        # accident through a future change.
        dialog = about_dialogs.AboutDialog(
            None, QIcon(), [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        dialog.show()
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())


class TestEscapeClosesChildDialogs(unittest.TestCase):
    def test_escape_closes_system_info_dialog(self):
        dialog = about_dialogs.SystemInfoDialog(
            None, [ProcessingBackend("cpu", "CPU")], "Dark", "7.0",
        )
        dialog.show()
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())

    def test_escape_closes_licenses_dialog(self):
        dialog = about_dialogs.LicensesDialog(None)
        dialog.show()
        QTest.keyClick(dialog, Qt.Key_Escape)
        self.assertFalse(dialog.isVisible())


if __name__ == "__main__":
    unittest.main()
