"""Control icons (ui/icons.py) and the controls that carry them."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from PySide6.QtCore import QByteArray, QSize, Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea, QToolButton  # noqa: E402

from velocoder.core import ffmpeg  # noqa: E402
from velocoder.ui import main_window, queue_controller, session  # noqa: E402
from velocoder.ui._vendor.odcs_ui import desktop as odcs_desktop  # noqa: E402
from velocoder.ui.queue_widget import RESULT_COL, STATUS_COL  # noqa: E402

_app = QApplication.instance() or QApplication([])
_patches = []
ICON_DIR = REPO_ROOT / "src" / "velocoder" / "ui" / "assets" / "icons"


def _fake_render_node(vendor_id):
    if vendor_id in (ffmpeg.GPU_VENDOR_IDS["intel"], ffmpeg.GPU_VENDOR_IDS["amd"]):
        return f"/dev/dri/renderD-test-{vendor_id}"
    raise RuntimeError("no render node")


def setUpModule():
    _patches.extend([
        patch.object(session, "load_session", return_value=None),
        patch.object(session, "save_session"),
        patch.object(ffmpeg, "validate_hevc_encode", return_value=None),
        patch.object(ffmpeg, "find_render_node", side_effect=_fake_render_node),
        patch.object(odcs_desktop, "accent", return_value=None),
    ])
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


def _center_color(icon: QIcon, mode=QIcon.Normal, state=QIcon.Off):
    image = icon.pixmap(QSize(32, 32), 1.0, mode, state).toImage()
    # The darkest-alpha-weighted pixel would be overkill: the stroke of
    # every glyph here crosses the icon's middle row somewhere.
    for x in range(image.width()):
        color = image.pixelColor(x, image.height() // 2)
        if color.alpha() > 200:
            return color.name()
    return None


class IconFiles(unittest.TestCase):
    def test_every_icon_is_valid_svg_drawn_in_current_color(self):
        names = sorted(ICON_DIR.glob("*.svg"))
        self.assertTrue(names)
        for path in names:
            text = path.read_text()
            self.assertTrue(QSvgRenderer(QByteArray(text.encode())).isValid(), path.name)
            if path.stem not in ("intel", "amd"):  # vendor badges keep their own colors
                self.assertIn("currentColor", text, path.name)


class IconWindowTests(unittest.TestCase):
    def setUp(self):
        self.window = main_window.MainWindow()
        self.addCleanup(self.window.deleteLater)

    def test_processing_row_reads_auto_cpu_intel_amd(self):
        w = self.window
        buttons = [w.processing_auto_btn, w.processing_cpu_btn, w.processing_intel_btn, w.processing_amd_btn]
        self.assertEqual([b.text() for b in buttons], ["Auto", "CPU", "Intel", "AMD"])
        self.assertEqual([b.objectName() for b in buttons], ["segLeft", "segMid", "segMid", "segRight"])
        self.assertIs(buttons[0].parentWidget(), buttons[3].parentWidget())  # one joined row
        # Auto is a one-shot pick, never a state the row can show as checked.
        self.assertFalse(w.processing_auto_btn.isCheckable())
        self.assertFalse(w.processing_auto_btn.icon().isNull())

    def test_theme_switch_retints_icons(self):
        self.window._apply_theme("dark")
        dark = _center_color(self.window.add_files_btn.icon())
        self.window._apply_theme("light")
        light = _center_color(self.window.add_files_btn.icon())
        self.assertIsNotNone(dark)
        self.assertNotEqual(dark, light)

    def test_checked_segment_icon_uses_on_accent_color(self):
        icon = self.window.quality_balanced_btn.icon()
        self.assertNotEqual(_center_color(icon, state=QIcon.On), _center_color(icon, state=QIcon.Off))

    def test_settings_column_fits_at_default_text_size(self):
        self.window.resize(1280, 800)
        self.window.show()
        _app.processEvents()
        scroll = next(s for s in self.window.findChildren(QScrollArea) if s.widget().findChild(type(self.window.processing_auto_btn)))
        self.assertLessEqual(scroll.widget().minimumSizeHint().width(), scroll.viewport().width())

    def test_finished_row_gets_a_show_output_button(self):
        w = self.window
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"\0" * 1000)
            output = Path(tmp) / "out" / "clip.mp4"
            output.parent.mkdir()
            output.write_bytes(b"\0" * 300)
            with patch.object(w, "_start_source_probe"):
                w.add_files([source])
            item = w.queue_list.topLevelItem(0)
            w._running_items = [item]
            w._on_job_started(str(source), 1, 1)
            self.assertIsNone(w.queue_list.itemWidget(item, RESULT_COL))
            w._on_job_finished(str(source), str(output))
            cell = w.queue_list.itemWidget(item, RESULT_COL)
            button = cell.findChild(QToolButton, "revealOutputButton")
            self.assertEqual(button.accessibleName(), "Show output file")
            with patch.object(queue_controller.QDesktopServices, "openUrl") as open_url:
                button.click()
            open_url.assert_called_once()
            self.assertEqual(Path(open_url.call_args[0][0].toLocalFile()), output.parent)
            self.assertEqual(item.data(STATUS_COL, Qt.UserRole)["_completed_output_path"], str(output))


if __name__ == "__main__":
    unittest.main()
