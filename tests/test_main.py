"""Regression tests for main.py (the PySide6 GUI).

Run with:  python3 -m unittest discover -s tests -v
Runs headless via the "offscreen" Qt platform plugin (set below) -- no
real display needed, and no window is ever actually shown.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import (  # noqa: E402
    QEvent, QEventLoop, QMimeData, QPoint, QPointF, QRect, QSize, Qt, QTimer, QUrl,
)
from PySide6.QtGui import (  # noqa: E402
    QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QFocusEvent, QFont,
    QFontMetrics, QPainter, QPalette, QPixmap, QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QScrollArea, QStyleOptionViewItem, QTabWidget, QWidget  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_app = QApplication.instance() or QApplication([])

import formatting  # noqa: E402
import main  # noqa: E402
import presets  # noqa: E402
import queue_controller  # noqa: E402
import queue_widget  # noqa: E402
import session  # noqa: E402
import theming  # noqa: E402
import worker  # noqa: E402

_session_patches = []


def setUpModule():
    # Every bare MainWindow() construction in this whole file now calls
    # _restore_session (main.py.__init__), which calls session.
    # load_session() -- and every queue mutation schedules session.
    # save_session() a moment later. Unlike QSettings' own scalar keys
    # (theme/geometry/expert-state, tolerated ambient real values
    # elsewhere in this file -- see _empty_qsettings' own docstring), a
    # REAL session.json actually populating the queue on construction
    # would break a huge fraction of this suite's own row-count
    # assertions, not just a handful of "what's the default" tests -- and
    # unlike QSettings, this file has no reason to ever accumulate real
    # ambient state on a dev box that runs the real app for manual
    # verification (screenshots, smoke tests, ...) alongside this suite.
    # So: patched to a safe no-op for every test in this module by
    # default, the same way _app itself is a single shared instance for
    # the whole module -- individual tests that actually exercise this
    # feature override these locally (see TestSessionPersistence below),
    # which cleanly shadows this default just for their own scope.
    _session_patches.append(patch.object(session, "load_session", return_value=None))
    _session_patches.append(patch.object(session, "save_session"))
    for p in _session_patches:
        p.start()


def tearDownModule():
    for p in _session_patches:
        p.stop()
    _session_patches.clear()


@contextmanager
def _empty_qsettings():
    """Patches QSettings.value at the class level to simulate a fresh
    config store with nothing persisted yet. A bare MainWindow() otherwise
    reads whatever this machine's real ~/.config/VeloCoder/VeloCoder.conf
    happens to have -- real ambient state (e.g. the user actually expanded
    Effective Command, or picked a theme, while using the real app) that
    has nothing to do with what a test asserting a *default* means."""
    def _empty_store(key, default=None):
        return default
    with patch.object(main.QSettings, "value", side_effect=_empty_store):
        yield


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


def _add_dummy_item(window, name: str, **overrides) -> "main.QTreeWidgetItem":
    """Add a queue item bypassing add_files() -- no on-disk file needed
    (add_files requires path.is_file()) and no real detection subprocess
    spawned, for tests that only care about the selection/settings sync."""
    job = {"path": Path(name), **window._current_settings()}
    job.update(overrides)
    item = window._make_queue_row(job)
    window.queue_list.addTopLevelItem(item)
    return item


class _FakeApp:
    """Stand-in for QApplication -- needs setStyleSheet() (the whole point
    of _load_stylesheet) and now also palette() (_system_accent_tokens
    reads QPalette.Accent/.Highlight off whatever's passed as app)."""
    received = None

    def setStyleSheet(self, text):
        self.received = text

    def palette(self):
        return QPalette()


class TestStylesheetLoading(unittest.TestCase):
    def test_missing_file_does_not_crash_startup(self):
        app = _FakeApp()
        main._load_stylesheet(app, style_path=Path("/nonexistent/style.qss"))  # must not raise
        self.assertIsNone(app.received)

    def test_real_stylesheet_file_loads(self):
        app = _FakeApp()
        main._load_stylesheet(app, style_path=REPO_ROOT / "style.qss")
        self.assertIn("QPushButton", app.received)

    def test_form_fields_use_focus_visible_not_plain_focus(self):
        # QComboBox/QLineEdit/QSpinBox/QPlainTextEdit's accent-border rule
        # was left behind using plain :focus when every other focusable
        # control (QPushButton/QCheckBox/QSlider/QTreeWidget) was migrated
        # to [focusVisible="true"] -- plain :focus matches window
        # reactivation just as much as real Tab navigation (Qt restores
        # focus to whatever widget had it before deactivation, which
        # still satisfies :focus), so every field in the app showed the
        # accent ring on simple alt-tab-back. Reported live on the Audio
        # track combo specifically. Regression guard on the stylesheet
        # text itself since this is a pure QSS selector bug -- nothing
        # about _FocusVisibleFilter's own Python-side logic (already
        # covered by TestFocusVisibleFilter below) was wrong.
        app = _FakeApp()
        main._load_stylesheet(app, style_path=REPO_ROOT / "style.qss")
        self.assertIn('QComboBox[focusVisible="true"]', app.received)
        self.assertIn('QLineEdit[focusVisible="true"]', app.received)
        self.assertIn('QSpinBox[focusVisible="true"]', app.received)
        self.assertIn('QPlainTextEdit[focusVisible="true"]', app.received)
        self.assertNotIn("QComboBox:focus", app.received)
        self.assertNotIn("QLineEdit:focus", app.received)
        self.assertNotIn("QSpinBox:focus", app.received)
        self.assertNotIn("QPlainTextEdit:focus", app.received)

    def test_disableable_form_fields_have_disabled_style(self):
        # codec_combo.setEnabled(False) (whenever a hardware encoder is
        # selected) and pause_after_check.setEnabled(False) (whenever no
        # run is active) were both functionally correct -- isEnabled()
        # really was False -- but reported as not *looking* disabled in
        # the real app. Root cause: styling a widget's base QComboBox/
        # QCheckBox::indicator rule at all suppresses Fusion's native
        # disabled-dimming fallback unless a :disabled rule is added back
        # explicitly, same gap QPushButton:disabled already existed to
        # close for buttons -- just never extended to form fields because
        # nothing in this app had disabled one before now. Regression
        # guard on the stylesheet text itself, same pattern as the
        # focus-visible test above.
        app = _FakeApp()
        main._load_stylesheet(app, style_path=REPO_ROOT / "style.qss")
        self.assertIn("QComboBox:disabled", app.received)
        self.assertIn("QLineEdit:disabled", app.received)
        self.assertIn("QSpinBox:disabled", app.received)
        self.assertIn("QCheckBox:disabled", app.received)
        self.assertIn("QCheckBox::indicator:disabled", app.received)
        self.assertIn("QCheckBox::indicator:checked:disabled", app.received)

    def test_every_token_gets_substituted(self):
        # A leftover "$TOKEN" is exactly the failure mode a naive
        # shortest-match-first replace() would hit for tokens that are a
        # literal prefix of another (e.g. $BG_CONTROL / $BG_CONTROL_HOVER)
        # -- see _load_stylesheet's own comment on why tokens are sorted
        # longest-first before substitution.
        app = _FakeApp()
        main._load_stylesheet(app, "dark", style_path=REPO_ROOT / "style.qss")
        self.assertNotIn("$", app.received)

    def test_dark_and_light_produce_different_output(self):
        # BG_WINDOW, not ACCENT -- accent is system-derived now (see
        # TestSystemAccentTokens below), the same regardless of Dark/Light,
        # so it's no longer a token that tells the two themes apart.
        # BG_WINDOW still is.
        dark_app, light_app = _FakeApp(), _FakeApp()
        main._load_stylesheet(dark_app, "dark", style_path=REPO_ROOT / "style.qss")
        main._load_stylesheet(light_app, "light", style_path=REPO_ROOT / "style.qss")
        self.assertNotEqual(dark_app.received, light_app.received)
        self.assertIn(theming.themes.DARK["BG_WINDOW"], dark_app.received)
        self.assertIn(theming.themes.LIGHT["BG_WINDOW"], light_app.received)

    def test_unknown_theme_name_falls_back_to_dark(self):
        app = _FakeApp()
        main._load_stylesheet(app, "not-a-real-theme", style_path=REPO_ROOT / "style.qss")
        self.assertIn(theming.themes.DARK["BG_WINDOW"], app.received)


class _AccentFakeApp:
    """Stand-in exposing a controllable palette() -- unlike _FakeApp above,
    these tests care specifically about what QPalette.Accent/.Highlight
    resolves to, not about setStyleSheet()."""

    def __init__(self, accent_color: QColor):
        self._palette = QPalette()
        role = getattr(QPalette, "Accent", QPalette.Highlight)
        self._palette.setColor(role, accent_color)

    def palette(self):
        return self._palette


class TestSystemAccentTokens(unittest.TestCase):
    """_load_stylesheet's ACCENT/etc. tokens come from the desktop's own
    palette now, not a color this app picks on its own -- confirmed on a
    real KDE session that QPalette.Accent already resolves to that
    session's actual configured accent (#308cc6), not a generic Fusion
    default. These test the derivation directly, isolated from any real
    desktop session's actual current accent."""

    def test_derives_accent_from_system_palette(self):
        app = _AccentFakeApp(QColor("#308cc6"))
        tokens = main._system_accent_tokens(app)
        self.assertEqual(tokens["ACCENT"], "#308cc6")

    def test_falls_back_to_a_fixed_blue_when_palette_is_black(self):
        # A real desktop session's accent is never actually pure black --
        # treated as "nothing resolved" rather than a legitimate choice.
        app = _AccentFakeApp(QColor(0, 0, 0))
        tokens = main._system_accent_tokens(app)
        self.assertEqual(tokens["ACCENT"], "#4fa8e0")

    def test_hover_and_pressed_are_distinct_from_the_base_accent(self):
        app = _AccentFakeApp(QColor("#308cc6"))
        tokens = main._system_accent_tokens(app)
        self.assertNotEqual(tokens["ACCENT_HOVER"], tokens["ACCENT"])
        self.assertNotEqual(tokens["ACCENT_PRESSED"], tokens["ACCENT"])

    def test_picks_white_text_for_a_dark_accent(self):
        app = _AccentFakeApp(QColor("#1a1a1a"))
        tokens = main._system_accent_tokens(app)
        self.assertEqual(tokens["TEXT_ON_ACCENT"], "#ffffff")

    def test_picks_near_black_text_for_a_light_accent(self):
        app = _AccentFakeApp(QColor("#f0f0f0"))
        tokens = main._system_accent_tokens(app)
        self.assertEqual(tokens["TEXT_ON_ACCENT"], "#0d1117")

    def test_check_icon_matches_text_on_accent_not_theme(self):
        # The checkmark drawn inside a checked QCheckBox sits directly on
        # the $ACCENT fill, exactly like button text -- it needs the same
        # contrast-partner CHECK_ICON picks as TEXT_ON_ACCENT does, not
        # whichever of check_dark.svg/check_light.svg happened to match
        # the *old*, theme-fixed accent. Confirmed a real bug this way:
        # Dark theme's accent used to be light enough for a near-black
        # checkmark to make sense; once the accent became this session's
        # real system accent for *both* themes, TEXT_ON_ACCENT correctly
        # switched to white regardless of theme, but CHECK_ICON was still
        # keyed off theme name until this fix.
        # A light accent needs dark (check_dark.svg) text/check on top of
        # it; a dark accent needs light (check_light.svg) -- same pairing
        # as TEXT_ON_ACCENT itself, tested above.
        light_accent_app = _AccentFakeApp(QColor("#f0f0f0"))
        self.assertEqual(main._system_accent_tokens(light_accent_app)["CHECK_ICON"], "check_dark.svg")
        dark_accent_app = _AccentFakeApp(QColor("#1a1a1a"))
        self.assertEqual(main._system_accent_tokens(dark_accent_app)["CHECK_ICON"], "check_light.svg")


class TestResolveTheme(unittest.TestCase):
    def test_dark_and_light_pass_through_unchanged(self):
        self.assertEqual(main._resolve_theme("dark"), "dark")
        self.assertEqual(main._resolve_theme("light"), "light")

    def test_system_resolves_light(self):
        with patch.object(main.QApplication, "instance") as mock_instance:
            mock_instance.return_value.styleHints.return_value.colorScheme.return_value = main.Qt.ColorScheme.Light
            self.assertEqual(main._resolve_theme("system"), "light")

    def test_system_resolves_dark_for_anything_else(self):
        # Covers both a real Dark preference and Unknown (a platform that
        # doesn't expose one, or -- confirmed directly -- this app's own
        # offscreen/headless test environment) -- either way, falls back to
        # dark rather than erroring or guessing.
        for scheme in (main.Qt.ColorScheme.Dark, main.Qt.ColorScheme.Unknown):
            with patch.object(main.QApplication, "instance") as mock_instance:
                mock_instance.return_value.styleHints.return_value.colorScheme.return_value = scheme
                self.assertEqual(main._resolve_theme("system"), "dark")


class TestThemeIntegration(unittest.TestCase):
    def test_default_theme_choice_is_system(self):
        # VeloCoder (unlike the sibling TITAN-i Transcoder, which keeps
        # "dark") defaults to following the OS's own light/dark preference
        # -- this fork's whole premise is Mac-style conventions over an
        # app-specific opinion.
        with _empty_qsettings():
            window = main.MainWindow()
        self.assertEqual(window._theme_choice, "system")
        self.assertEqual(window.theme_combo.currentData(), "system")

    def test_no_menu_bar(self):
        # Theme picking moved to a footer combo (see the status-bar tests
        # below) -- a whole menu bar for one three-item setting was more
        # chrome than the setting warranted. Guards against it quietly
        # coming back if this is touched again.
        window = main.MainWindow()
        self.assertEqual(window.menuBar().actions(), [])

    def test_invalid_persisted_choice_falls_back_to_dark(self):
        # __init__ applies this same guard to whatever QSettings hands back
        # -- exercised directly against the real function here rather than
        # via a real corrupted config file, since it's a pure function with
        # nothing QSettings-specific about the fallback logic itself.
        for bogus in (None, "", "not-a-theme", 42, "Dark"):  # case-sensitive too
            self.assertEqual(main._validate_theme_choice(bogus), "dark")

    def test_valid_choices_pass_through_unchanged(self):
        for value in ("dark", "light", "system"):
            self.assertEqual(main._validate_theme_choice(value), value)

    def test_apply_theme_updates_choice_and_persists(self):
        # Mocking setValue rather than using a real (even if scratch)
        # QSettings avoids touching the filesystem at all for this --
        # this only needs to confirm _apply_theme's two effects, not that
        # QSettings itself round-trips a value, which every other persisted
        # bit of window state in this app (geometry, collapsed sections)
        # already relies on unverified at this level too.
        window = main.MainWindow()
        with patch.object(window._qsettings, "setValue") as mock_set:
            window._apply_theme("light")
        self.assertEqual(window._theme_choice, "light")
        mock_set.assert_called_once_with("theme_choice", "light")

    def test_system_theme_signal_only_reapplies_when_choice_is_system(self):
        window = main.MainWindow()
        window._theme_choice = "dark"
        with patch.object(main, "_load_stylesheet") as mock_load:
            window._on_system_theme_changed(main.Qt.ColorScheme.Light)
            mock_load.assert_not_called()
        window._theme_choice = "system"
        with patch.object(main, "_load_stylesheet") as mock_load:
            window._on_system_theme_changed(main.Qt.ColorScheme.Light)
            mock_load.assert_called_once()

    def test_footer_combo_reflects_persisted_choice(self):
        # Exercised through real construction, not by poking _theme_choice
        # after the fact -- the combo's initial selection is set once,
        # during __init__, from whatever QSettings handed back.
        def _fake_value(key, default=None):
            return "light" if key == "theme_choice" else default
        with patch.object(main.QSettings, "value", side_effect=_fake_value):
            window = main.MainWindow()
        self.assertEqual(window.theme_combo.currentData(), "light")

    def test_selecting_the_combo_applies_the_theme(self):
        # Constructed with an isolated (guaranteed-"system") starting
        # choice -- selecting "light" against this machine's real ambient
        # choice would be a no-op (no currentIndexChanged, nothing to
        # assert on) on whatever day that real choice already happens to
        # resolve to "light".
        with _empty_qsettings():
            window = main.MainWindow()
        light_index = window.theme_combo.findData("light")
        with patch.object(window._qsettings, "setValue"), \
             patch.object(main, "_load_stylesheet") as mock_load:
            window.theme_combo.setCurrentIndex(light_index)
        self.assertEqual(window._theme_choice, "light")
        mock_load.assert_called_once()


class TestFuzzyTextColor(unittest.TestCase):
    """theming._fuzzy_text_color backs both the quality/speed/audio-bitrate
    tier captions (main.py) and the empty-queue placeholder text
    (queue_widget.py). Palette roles are set directly on a plain QWidget
    here rather than depending on this machine's real Fusion defaults --
    those turned out to vary (confirmed empirically: identical RGB to
    WindowText, alpha 128, under the offscreen QPA platform used for this
    whole suite) and are exactly the condition each branch below needs to
    control precisely, not incidentally inherit."""

    @staticmethod
    def _widget_with_roles(window_text: QColor, placeholder: QColor):
        from PySide6.QtWidgets import QWidget
        widget = QWidget()
        palette = widget.palette()
        palette.setColor(QPalette.WindowText, window_text)
        palette.setColor(QPalette.PlaceholderText, placeholder)
        widget.setPalette(palette)
        return widget

    def test_translucent_placeholder_keeps_its_alpha_not_silently_dropped(self):
        # The real bug: QColor.name() drops alpha entirely, which used to
        # turn a legitimately-translucent PlaceholderText role back into a
        # fully-opaque, indistinguishable-from-regular-text color.
        widget = self._widget_with_roles(QColor(20, 20, 20), QColor(20, 20, 20, 128))
        result = theming._fuzzy_text_color(widget)
        # A real, directly-usable QColor -- not a string. Reported live as
        # "always black text" in both themes when this used to return a
        # "rgba(...)" CSS string instead: queue_widget.py's paintEvent
        # re-wrapped that string in QColor(...) to get a pen color, and
        # QColor's own string constructor does not understand CSS rgba()
        # syntax at all -- it silently comes back invalid (== black),
        # confirmed directly against this exact string.
        self.assertTrue(result.isValid())
        self.assertEqual((result.red(), result.green(), result.blue(), result.alpha()), (20, 20, 20, 128))

    def test_distinct_solid_placeholder_role_is_honored_as_is(self):
        widget = self._widget_with_roles(QColor(20, 20, 20), QColor(120, 120, 120))
        result = theming._fuzzy_text_color(widget)
        self.assertEqual((result.red(), result.green(), result.blue(), result.alpha()), (120, 120, 120, 255))

    def test_placeholder_identical_to_window_text_falls_back_to_text_secondary(self):
        # The genuine "nothing distinct here at all" case (both RGB and
        # alpha equal) -- falls back to this app's own token rather than
        # rendering fuzzy captions at full text strength.
        widget = self._widget_with_roles(QColor(20, 20, 20), QColor(20, 20, 20))
        with patch.dict(theming._current_theme_palette, {"TEXT_SECONDARY": "#6b7280"}):
            result = theming._fuzzy_text_color(widget)
        self.assertEqual(result.name(), "#6b7280")


class TestStartupOrdering(unittest.TestCase):
    """rc_mode_combo must be populated before any preset gets applied."""

    def test_rc_mode_populated_after_construction(self):
        window = main.MainWindow()
        self.assertIsNotNone(window.rc_mode_combo.currentData())
        self.assertGreater(window.rc_mode_combo.count(), 0)

    def test_quality_label_has_no_none_placeholder(self):
        window = main.MainWindow()
        self.assertNotIn("None", window.quality_label.text())


class TestRateControlButtons(unittest.TestCase):
    """rc_mode_combo is Expert's own real, directly-visible control now --
    RC_MODES' technical labels (constants.py: "ICQ (quality, hardware)",
    "CQP (fixed quantizer)", "VBR (target bitrate)", "CRF (quality)",
    "Target bitrate") shown as-is, not a friendly Quality/File Size/
    Advanced 3-button row (that row was cut entirely: it just duplicated
    Normal's own Mode toggle -- mode_quality_btn/mode_filesize_btn, the
    Quality group -- with different labels, per the final control-
    hierarchy decision that Expert should show real encoder mechanics,
    not a second abstraction). setCurrentText()/setCurrentIndex() on a
    QComboBox are property-style calls, not synthesized click/focus
    events, so unlike the old buttons' .click(), these tests need no
    window.show() or Expert-expansion workaround (see TestX264Codec's own
    docstring for the same reasoning already established there)."""

    def test_selecting_icq_by_text_for_vaapi(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.rc_mode_combo.setCurrentText("VBR (target bitrate)")  # move off the default first
        window.rc_mode_combo.setCurrentText("ICQ (quality, hardware)")
        self.assertEqual(window.rc_mode_combo.currentData(), "ICQ")
        self.assertTrue(window.mode_quality_btn.isChecked())

    def test_selecting_vbr_for_vaapi_checks_normal_file_size_mode(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.rc_mode_combo.setCurrentText("VBR (target bitrate)")
        self.assertEqual(window.rc_mode_combo.currentData(), "VBR")
        self.assertTrue(window.mode_filesize_btn.isChecked())

    def test_selecting_cqp_for_vaapi_leaves_normal_mode_unselected(self):
        # CQP (Advanced) has no Normal-mode equivalent -- neither Mode
        # button should read as selected, same "no exact match" handling
        # the Quality tier buttons already use for an in-between Expert
        # slider value.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.rc_mode_combo.setCurrentText("CQP (fixed quantizer)")
        self.assertEqual(window.rc_mode_combo.currentData(), "CQP")
        self.assertFalse(window.mode_quality_btn.isChecked())
        self.assertFalse(window.mode_filesize_btn.isChecked())

    def test_selecting_bitrate_for_x265_checks_normal_file_size_mode(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        window.rc_mode_combo.setCurrentText("Target bitrate")
        self.assertEqual(window.rc_mode_combo.currentData(), "bitrate")
        self.assertTrue(window.mode_filesize_btn.isChecked())

    def test_cqp_not_offered_for_x265_no_equivalent(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        items = [window.rc_mode_combo.itemText(i) for i in range(window.rc_mode_combo.count())]
        self.assertEqual(items, ["CRF (quality)", "Target bitrate"])

    def test_normal_mode_button_resyncs_combo_across_an_encoder_switch(self):
        # Quality on VAAPI (ICQ) should still read as Normal's Quality
        # Mode after switching to x265 (CRF) -- the *concept* carries
        # over even though the underlying rc_mode value differs per
        # encoder.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.mode_quality_btn.click()
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window.rc_mode_combo.currentData(), "CRF")
        self.assertTrue(window.mode_quality_btn.isChecked())

    def test_switching_off_cqp_to_x265_falls_back_to_a_mode_that_exists(self):
        # CQP has no x265 equivalent -- RC_MODES["libx265"] simply doesn't
        # contain it, so switching encoders away from it lands on whatever
        # index 0 becomes (CRF), which Normal's Quality Mode should reflect.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.rc_mode_combo.setCurrentText("CQP (fixed quantizer)")
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window.rc_mode_combo.currentData(), "CRF")
        self.assertTrue(window.mode_quality_btn.isChecked())


class TestSpeedSliderVisibility(unittest.TestCase):
    """x265 got its own real Speed slider (speed_x265_slider), not just a
    QComboBox, matching VAAPI's speed_slider -- so speed_faster_label/
    speed_thorough_label/speed_tier_label are shared by both and stay
    visible regardless of encoder now; only which *slider* is showing
    actually changes. Was previously the reverse for speed_tier_label
    specifically (hidden for x265, visible only for VAAPI, since x265
    had no slider of its own to caption yet)."""

    def test_x265_slider_shown_and_vaapi_slider_hidden_for_cpu(self):
        # isVisibleTo(window._video_expert_content), not isVisible() --
        # this whole form is never shown at all in this fork (see
        # TestRateControlButtons' own docstring for the full reasoning).
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("CPU")
        self.assertTrue(window.speed_x265_slider.isVisibleTo(window._video_expert_content))
        self.assertFalse(window.speed_slider.isVisibleTo(window._video_expert_content))

    def test_vaapi_slider_shown_and_x265_slider_hidden_for_vaapi(self):
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertTrue(window.speed_slider.isVisibleTo(window._video_expert_content))
        self.assertFalse(window.speed_x265_slider.isVisibleTo(window._video_expert_content))

    def test_shared_labels_stay_visible_regardless_of_encoder(self):
        window = main.MainWindow()
        window.show()
        for encoder in ("CPU", "Intel (iGPU)", "AMD (GPU)"):
            window.encoder_combo.setCurrentText(encoder)
            self.assertTrue(window.speed_faster_label.isVisibleTo(window._video_expert_content), encoder)
            self.assertTrue(window.speed_thorough_label.isVisibleTo(window._video_expert_content), encoder)
            self.assertTrue(window.speed_tier_label.isVisibleTo(window._video_expert_content), encoder)


class TestTargetSizeSettings(unittest.TestCase):
    """quality_value means a target output size in MB, not literal kbps,
    when rc_mode is a bitrate-family mode -- see worker.build_args's
    docstring and TestSizeToBitrate in test_worker.py for the conversion
    itself. This covers the GUI's side of storing/round-tripping it."""

    def test_size_spin_value_flows_into_current_settings(self):
        window = main.MainWindow()
        window.show()
        window.mode_filesize_btn.click()
        window.size_spin.setValue(750)
        self.assertEqual(window._current_settings()["quality_value"], 750)

    def test_apply_settings_to_controls_round_trips_size(self):
        # size_spin lives in the always-shown Quality group now (Normal
        # mode), not Expert -- plain isVisible() is the right check for
        # it (window.show() above makes its real ancestor chain visible,
        # only its own Target Size row's setRowVisible state gates it).
        # quality_slider stays in Expert, collapsed by default in this
        # test (video_expert_group never expanded) -- isVisibleTo(window.
        # _video_expert_content) is still correct for it, same reasoning
        # as TestRateControlButtons' own docstring.
        #
        # rc_mode read from RC_MODE_FRIENDLY for whatever encoder is
        # *actually* the live default here, not hardcoded to "VBR" --
        # real, confirmed bug in this test itself (not app code): "VBR"
        # is specifically VAAPI's bitrate-family rc_mode name, and this
        # dev box's real Intel iGPU makes hevc_vaapi the Automatic
        # default (best_available_engine), so "VBR" was silently valid
        # here by coincidence. A GPU-less machine has no Intel/AMD row in
        # ENCODERS at all (confirmed directly in an Ubuntu 24.04
        # container matching CI, no /dev/dri) -- Automatic falls back to
        # libx265, whose bitrate-family rc_mode is called "bitrate", not
        # "VBR"; forcing settings["encoder"] = "hevc_vaapi" doesn't help
        # either, since encoder_combo has no such item to switch to
        # there. Using the same RC_MODE_FRIENDLY[key]["file_size"] lookup
        # main.py's own Mode-button handlers use keeps this correct
        # regardless of which encoder Automatic actually resolves to.
        window = main.MainWindow()
        window.show()
        settings = window._current_settings()
        settings["rc_mode"] = main.RC_MODE_FRIENDLY[window._current_encoder_key()]["file_size"]
        settings["quality_value"] = 2500
        window._apply_settings_to_controls(settings)
        self.assertEqual(window.size_spin.value(), 2500)
        self.assertTrue(window.size_spin.isVisible())
        self.assertFalse(window.quality_slider.isVisibleTo(window._video_expert_content))


class TestSizeEstimateLabel(unittest.TestCase):
    def test_hidden_in_quality_mode(self):
        window = main.MainWindow()
        self.assertFalse(window.size_estimate_label.isVisible())

    def test_prompts_for_a_file_when_queue_is_empty(self):
        window = main.MainWindow()
        window.show()
        window.mode_filesize_btn.click()
        self.assertIn("Add a video", window.size_estimate_label.text())

    def test_shows_a_real_computed_estimate_for_a_queued_file(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window.mode_filesize_btn.click()
            window.size_spin.setValue(1000)
            text = window.size_estimate_label.text()
        self.assertIn("kbps", text)
        self.assertNotIn("Add a video", text)
        self.assertNotIn("Couldn't read", text)

    def test_a_probe_failure_degrades_to_a_message_instead_of_raising(self):
        # Real, confirmed bug: this call was unguarded, unlike the sibling
        # build_args() call in _update_command_preview just above it in
        # that method, which already degrades to a message. A probe
        # failure here (a stalled network mount, ffprobe genuinely
        # missing) used to propagate straight out of
        # _update_command_preview uncaught -- and since that method runs
        # on every settings change while File Size mode is active with a
        # file queued, it would raise again on every subsequent keystroke.
        # Patched around add_files() itself, not just the later
        # mode_filesize_btn.click() -- _update_command_preview eagerly
        # probes duration for *any* queued file regardless of rc_mode (it
        # feeds build_args' duration_seconds unconditionally), so
        # add_files() alone already populates _preview_duration_cache;
        # patching any later than this would just hit that cache and never
        # call the (patched) function at all.
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            with patch.object(worker, "probe_duration", side_effect=RuntimeError("boom")):
                window.add_files([clip])
                _wait_for_detection(window)
                window.mode_filesize_btn.click()  # must not raise
            self.assertIn("estimate unavailable", window.size_estimate_label.text())

    def test_target_too_small_shows_a_clear_message_not_a_bogus_number(self):
        # target_size_to_bitrate_kbps returning 0 used to just get printed
        # as "≈ 0 kbps video..." -- confirmed directly (see
        # test_worker.TestTargetSizeTooSmallRejected) that build_args()
        # itself now refuses to encode against that derived value at all,
        # so this label should say why before the user gets that far, not
        # describe a number Start would then reject anyway. The real test
        # clip is only ~1s, so even size_spin's own minimum (10MB) is
        # still plenty of bitrate for it -- the duration cache is set
        # directly to simulate a long file instead, the actual condition
        # ("target too small for this length") this is testing.
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window.mode_filesize_btn.click()
            window._preview_duration_cache[clip] = 7200.0  # simulate a 2-hour file
            window.size_spin.setValue(window.size_spin.minimum())
            text = window.size_estimate_label.text()
        self.assertIn("too small", text)
        self.assertNotIn("kbps", text)

    def test_reserves_the_real_source_bitrate_when_audio_will_be_copied(self):
        # Same bug/fix as worker.TestBuildArgsAudioBitrateReservation --
        # this label computes its own estimate independently of
        # build_args (it has to: build_args needs a real file on disk,
        # this label also needs to show something before Convert is ever
        # clicked), so the will_copy_audio fix had to be applied here
        # too, separately. MP4, not MKV -- ffprobe reports a real per-
        # stream bit_rate for MP4-muxed AAC but literally "N/A" for this
        # repo's own MKV test fixture (confirmed directly, see that same
        # worker.py test class's own docstring).
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-c:v", "libx264", "-c:a", "aac", "-b:a", "256k", "-shortest", str(clip)],
                check=True, timeout=30,
            )
            real_kbps = worker.probe_audio_bitrate_kbps(clip)
            self.assertIsNotNone(real_kbps, "MP4 should report a real per-stream bit_rate")
            window.add_files([clip])
            _wait_for_detection(window)
            window.mode_filesize_btn.click()
            window._preview_duration_cache[clip] = 80.0
            window.size_spin.setValue(100)  # triggers the recompute against the cached duration above
            text = window.size_estimate_label.text()
        expected_kbps = worker.target_size_to_bitrate_kbps(100, 80.0, real_kbps)
        wrong_kbps_using_configured_bitrate = worker.target_size_to_bitrate_kbps(
            100, 80.0, worker.audio_bitrate_kbps(window._current_settings()["audio_bitrate"])
        )
        self.assertIn(f"{expected_kbps:,} kbps", text)
        self.assertNotEqual(expected_kbps, wrong_kbps_using_configured_bitrate)


class TestDiagnosticsRelocation(unittest.TestCase):
    """Effective Command and Log no longer sit in the main window at all
    (previously collapsible sections, collapsed by default -- reported
    live that even collapsed, they still permanently occupied layout
    space and read as generic-utility chrome). Both widgets still exist
    and still receive every update exactly as before; only their
    presentation changed -- Copy FFmpeg Command needs no visible widget
    at all (_copy_command_to_clipboard reads _last_preview_args
    directly), and Show Conversion Log reparents log_view into an
    on-demand window instead of it living inline."""

    def test_command_preview_widget_exists_but_is_not_shown_anywhere(self):
        window = main.MainWindow()
        window.show()
        self.assertIsNone(window.command_preview.parent())
        self.assertFalse(window.command_preview.isVisible())

    def test_copy_command_works_without_the_widget_ever_being_shown(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window._copy_command_to_clipboard()
        self.assertIn("ffmpeg", QApplication.clipboard().text())

    def test_log_view_exists_but_is_not_shown_until_requested(self):
        window = main.MainWindow()
        window.show()
        self.assertIsNone(window.log_view.parent())
        self.assertFalse(window.log_view.isVisible())

    def test_show_log_window_reparents_and_shows_the_real_log_view(self):
        window = main.MainWindow()
        window.show()
        window.log_view.appendPlainText("a real log line")
        window._show_log_window()
        self.assertTrue(window._log_window.isVisible())
        self.assertIn("a real log line", window.log_view.toPlainText())
        self.assertIs(window.log_view.parent(), window._log_window)

    def test_show_log_window_reuses_the_same_window_on_repeated_calls(self):
        window = main.MainWindow()
        window.show()
        window._show_log_window()
        first = window._log_window
        window._show_log_window()
        self.assertIs(window._log_window, first)

    def test_settings_dialog_hosts_the_real_theme_combo(self):
        window = main.MainWindow()
        with patch.object(main.QDialog, "exec", return_value=None) as mock_exec:
            window._open_settings_dialog()
        mock_exec.assert_called_once()
        # Reparented into the dialog just shown, not a second combo built
        # fresh -- selecting a theme from it must still reach the exact
        # same currentIndexChanged -> _apply_theme wiring from construction.
        self.assertIsInstance(window.theme_combo.parent(), main.QDialog)

    def test_settings_dialog_reuses_the_same_dialog_on_repeated_opens(self):
        # Regression guard for a live crash: a fresh, uncached QDialog on
        # every open reparented theme_combo (built with no parent of its
        # own, ui_builder.py) into a new C++ parent each time, and that
        # dialog's own lifetime was never tracked -- "RuntimeError:
        # libshiboken: Internal C++ object (QComboBox) already deleted"
        # on a later addRow call. Caching the dialog, same pattern as
        # _show_log_window above, means theme_combo is reparented exactly
        # once, ever.
        window = main.MainWindow()
        with patch.object(main.QDialog, "exec", return_value=None):
            window._open_settings_dialog()
            first = window._settings_dialog
            window._open_settings_dialog()
        self.assertIs(window._settings_dialog, first)

    def test_footer_no_longer_has_theme_or_hardware_widgets(self):
        window = main.MainWindow()
        self.assertFalse(hasattr(window, "hw_status_label"))
        # theme_combo still exists (see the Settings dialog test above)
        # but isn't part of the status bar -- confirmed by the status
        # bar having nothing but Qt's own automatic QSizeGrip in it.
        kids = [type(w).__name__ for w in window.statusBar().findChildren(QWidget)]
        self.assertEqual(kids, ["QSizeGrip"])


class TestCommandPreviewErrorHandling(unittest.TestCase):
    def test_no_vaapi_device_shows_message_not_crash(self):
        window = main.MainWindow()
        # Found by content, not a hardcoded index -- constants.ENCODERS'
        # own order (CPU/Intel/AMD, matching the dropdown) isn't the same
        # thing as "which one is VAAPI," and a previous version of this
        # test hardcoding index 0 for "VAAPI HEVC" broke silently (still
        # ran, just against the CPU encoder instead) the moment that order
        # changed to put CPU first.
        intel_index = next(
            i for i, (encoder, vendor, _) in enumerate(main.ENCODERS)
            if encoder == "hevc_vaapi" and vendor == "intel"
        )
        with patch.object(
            main.worker, "find_render_node",
            side_effect=RuntimeError("no render node found"),
        ):
            window.encoder_combo.setCurrentIndex(intel_index)  # triggers a rebuild + preview
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

    def test_preview_follows_the_selected_row_not_always_row_0(self):
        # Real, confirmed bug: this always probed queue_list.topLevelItem(0)
        # regardless of what's actually selected -- display-only (the real
        # encode always uses each job's own file), but selecting a
        # different row to edit its settings still showed row 0's
        # duration/audio in the preview. aac vs mp3 makes this directly
        # observable: aac is copy-compatible, mp3 forces a transcode.
        aac_clip = self.tmpdir / "aac_clip.mkv"
        _make_clip(aac_clip, "aac")
        window = main.MainWindow()
        window.add_files([aac_clip, self.mp3_clip])
        _wait_for_detection(window)

        window.queue_list.topLevelItem(0).setSelected(True)
        window._update_command_preview()
        self.assertIn("-c:a copy", window.command_preview.toPlainText())

        window.queue_list.topLevelItem(0).setSelected(False)
        window.queue_list.topLevelItem(1).setSelected(True)
        window._update_command_preview()
        preview_text = window.command_preview.toPlainText()
        self.assertIn("-c:a aac", preview_text)
        self.assertNotIn("-c:a copy", preview_text)


class TestClearQueueConfirmation(unittest.TestCase):
    def test_declining_confirmation_keeps_the_queue(self):
        window = main.MainWindow()
        window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.No):
            window._clear_queue()
        self.assertEqual(window.queue_list.topLevelItemCount(), 1)

    def test_confirming_clears_the_queue(self):
        window = main.MainWindow()
        item = main.QTreeWidgetItem(["dummy"])
        item.setData(main.STATUS_COL, main.Qt.UserRole, {"path": Path("dummy.mkv"), **window._current_settings()})
        window.queue_list.addTopLevelItem(item)
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)

    def test_empty_queue_skips_the_dialog_entirely(self):
        window = main.MainWindow()
        with patch.object(main.QMessageBox, "question") as mock_question:
            window._clear_queue()
        mock_question.assert_not_called()


class TestResultSizeFormatting(unittest.TestCase):
    def test_format_size_units(self):
        self.assertEqual(formatting.format_size(500), "500B")
        self.assertEqual(formatting.format_size(2048), "2.0KB")
        self.assertEqual(formatting.format_size(300 * 1024 * 1024), "300.0MB")

    def test_append_result_size_shows_shrinkage(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"x" * 1000)
            out.write_bytes(b"x" * 250)  # 75% smaller
            item = main.QTreeWidgetItem()
            main.MainWindow._append_result_size(item, src, out)
            self.assertIn("75% smaller", item.text(main.RESULT_COL))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_append_result_size_shows_growth(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"x" * 100)
            out.write_bytes(b"x" * 200)  # larger output (e.g. a tiny/simple source)
            item = main.QTreeWidgetItem()
            main.MainWindow._append_result_size(item, src, out)
            self.assertIn("larger", item.text(main.RESULT_COL))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_missing_output_file_does_not_crash(self):
        item = main.QTreeWidgetItem()
        main.MainWindow._append_result_size(item, Path("/nonexistent/in.mkv"), Path("/nonexistent/out.mp4"))
        self.assertEqual(item.text(main.RESULT_COL), "")  # left untouched

    def test_append_result_size_returns_byte_counts_on_success(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"x" * 1000)
            out.write_bytes(b"x" * 250)
            item = main.QTreeWidgetItem()
            result = main.MainWindow._append_result_size(item, src, out)
            self.assertEqual(result, (1000, 250))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_append_result_size_returns_none_on_missing_file(self):
        item = main.QTreeWidgetItem()
        result = main.MainWindow._append_result_size(item, Path("/nonexistent/in.mkv"), Path("/nonexistent/out.mp4"))
        self.assertIsNone(result)

    def test_append_result_size_returns_none_when_input_size_is_zero(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src = tmpdir / "in.mkv"
            out = tmpdir / "out.mp4"
            src.write_bytes(b"")
            out.write_bytes(b"x" * 100)
            item = main.QTreeWidgetItem()
            result = main.MainWindow._append_result_size(item, src, out)
            self.assertIsNone(result)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFormatRunSummary(unittest.TestCase):
    def test_counts_and_sizes(self):
        count_line, size_line = formatting.format_run_summary(4, 0, 12_400_000_000, 4_100_000_000)
        self.assertEqual(count_line, "4 videos converted")
        self.assertIn("→", size_line)
        self.assertIn("smaller", size_line)

    def test_singular_video(self):
        count_line, _ = formatting.format_run_summary(1, 0, 1000, 500)
        self.assertEqual(count_line, "1 video converted")

    def test_zero_input_bytes_omits_size_line(self):
        # Every per-job size stat this run failed (_append_result_size
        # returned None each time) even though jobs still completed --
        # nothing real to compare, not a bogus "100% smaller".
        count_line, size_line = formatting.format_run_summary(2, 0, 0, 0)
        self.assertEqual(count_line, "2 videos converted")
        self.assertEqual(size_line, "")

    def test_failed_count_appends_to_the_count_line(self):
        count_line, _ = formatting.format_run_summary(3, 1, 1000, 500)
        self.assertEqual(count_line, "3 videos converted · 1 failed")


class TestAudioTrackChoices(unittest.TestCase):
    """Track (Normal, promoted from Expert) used to unconditionally offer
    Track 1-4 regardless of how many audio streams the selected file(s)
    actually have -- picking a nonexistent one silently produced audio-
    less output instead of an error (worker.py deliberately skips mapping
    a track that isn't there rather than failing the whole job). Track
    count is already probed (worker.parse_probe_output's own
    audio_track_count, used since before this fix for the "+N more" queue
    subtitle) -- _refresh_audio_track_choices (main.py) now also narrows
    audio_combo to it, keyed off queue_widget.AUDIO_TRACK_COUNT_ROLE."""

    def test_default_with_no_selection_offers_all_four(self):
        window = main.MainWindow()
        self.assertEqual(window.audio_combo.count(), 4)

    def test_selecting_a_single_track_file_narrows_to_one(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        item.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.audio_combo.count(), 1)
        self.assertEqual(window.audio_combo.itemText(0), "Track 1")

    def test_a_file_with_more_tracks_offers_that_many(self):
        # Real ffprobe output only ever comes from add_files's own async
        # probe -- setting AUDIO_TRACK_COUNT_ROLE directly here simulates
        # what a genuine multi-track source would have produced, the same
        # way other tests in this file set _preview_duration_cache
        # directly rather than constructing a real multi-hour clip.
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        item.setData(queue_widget.VIDEO_COL, queue_widget.AUDIO_TRACK_COUNT_ROLE, 3)
        item.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.audio_combo.count(), 3)
        self.assertEqual(
            [window.audio_combo.itemText(i) for i in range(3)],
            ["Track 1", "Track 2", "Track 3"],
        )

    def test_multiple_selected_files_offer_only_the_shared_intersection(self):
        # Two files, 1 and 3 tracks -- offering Track 2 or 3 would fail
        # outright on the 1-track file if a shared Track choice were
        # applied to both selected rows at once (same reasoning
        # _sync_settings_to_selected_queue_items already applies to every
        # other shared setting).
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            _make_clip(clip_a, "aac")
            _make_clip(clip_b, "aac")
            window.add_files([clip_a, clip_b])
            _wait_for_detection(window)
        item_a = window.queue_list.topLevelItem(0)
        item_b = window.queue_list.topLevelItem(1)
        item_a.setData(queue_widget.VIDEO_COL, queue_widget.AUDIO_TRACK_COUNT_ROLE, 1)
        item_b.setData(queue_widget.VIDEO_COL, queue_widget.AUDIO_TRACK_COUNT_ROLE, 3)
        item_a.setSelected(True)
        item_b.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.audio_combo.count(), 1)

    def test_stored_audio_track_past_the_narrowed_list_is_clamped_not_left_unset(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
        job["audio_track"] = 2  # Track 3 -- this file only has one track
        item.setData(queue_widget.STATUS_COL, Qt.UserRole, job)
        item.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.audio_combo.count(), 1)
        self.assertEqual(window.audio_combo.currentIndex(), 0)

    def test_a_newly_added_jobs_stored_track_is_clamped_once_the_real_count_is_known(self):
        # Real, confirmed bug: selecting Track 4 while the queue is empty,
        # then adding a genuinely one-track file, captured audio_track=3
        # straight into that job's own settings (add_files snapshots
        # _current_settings() at add time). _refresh_audio_track_choices
        # only narrows the *visible* combo for a selected row, and does
        # so via blockSignals specifically so it doesn't also write back
        # -- so the job itself kept pointing at a track that doesn't
        # exist even after the real probe landed and revealed the true
        # count, regardless of whether this row was ever selected.
        window = main.MainWindow()
        window.show()
        window.audio_combo.setCurrentIndex(3)  # Track 4, queue still empty
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")  # genuinely one audio track
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["audio_track"], 0)

    def test_deselecting_back_to_nothing_restores_all_four(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        item.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.audio_combo.count(), 1)
        item.setSelected(False)
        window._refresh_audio_track_choices()
        self.assertEqual(window.audio_combo.count(), 4)


class TestSettingsScopeLabel(unittest.TestCase):
    """Reported live: nothing in the UI said whether the left panel's
    controls were about to become defaults for a newly-added video, or
    were editing whatever's currently selected in the queue -- both are
    real, frequently-used states with identical-looking controls either
    way. video_scope_label/audio_scope_label (one instance per tab, kept
    in sync by _update_settings_scope_label) make that state explicit."""

    def test_no_selection_reads_as_settings_for_new_videos(self):
        window = main.MainWindow()
        self.assertEqual(window.video_scope_label.text(), "Settings for new videos")
        self.assertEqual(window.audio_scope_label.text(), "Settings for new videos")

    def test_one_selected_names_the_file(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "interview_01.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        window.queue_list.topLevelItem(0).setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.video_scope_label.text(), 'Settings for "interview_01.mkv"')

    def test_multiple_selected_shows_a_count(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            _make_clip(clip_a, "aac")
            _make_clip(clip_b, "aac")
            window.add_files([clip_a, clip_b])
            _wait_for_detection(window)
        window.queue_list.topLevelItem(0).setSelected(True)
        window.queue_list.topLevelItem(1).setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window.video_scope_label.text(), "Settings for 2 selected videos")

    def test_deselecting_back_to_nothing_restores_the_new_videos_text(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        item.setSelected(True)
        window._on_queue_selection_changed()
        item.setSelected(False)
        window._on_queue_selection_changed()
        self.assertEqual(window.video_scope_label.text(), "Settings for new videos")


class TestPartialSettingsSyncOnMultiSelect(unittest.TestCase):
    """Real, reported bug: _sync_settings_to_selected_queue_items used to
    push the *entire* current settings dict onto every selected queue
    item on every single control change. Two files selected together
    with genuinely different settings (the panel only ever shows the
    first one's, see _on_queue_selection_changed) -- nudging just one
    control (AAC Bitrate, say) silently overwrote the *other* file's
    unrelated settings too, not only the one control actually touched.
    Now only the keys that actually changed since the panel last settled
    (this selection's own starting point, tracked in _last_synced_
    settings) get pushed -- everything else about each selected item's
    own settings is left alone."""

    def test_changing_one_control_does_not_touch_an_unrelated_setting(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            _make_clip(clip_a, "aac")
            _make_clip(clip_b, "aac")
            window.add_files([clip_a, clip_b])
            _wait_for_detection(window)
        item_a = window.queue_list.topLevelItem(0)
        item_b = window.queue_list.topLevelItem(1)
        job_a = item_a.data(queue_widget.STATUS_COL, Qt.UserRole)
        job_a["bit_depth"] = 10
        item_a.setData(queue_widget.STATUS_COL, Qt.UserRole, job_a)
        job_b = item_b.data(queue_widget.STATUS_COL, Qt.UserRole)
        job_b["bit_depth"] = 8
        item_b.setData(queue_widget.STATUS_COL, Qt.UserRole, job_b)

        item_a.setSelected(True)
        item_b.setSelected(True)
        window._on_queue_selection_changed()
        self.assertEqual(window._current_settings()["bit_depth"], 10)  # panel shows the first (A)

        window.audio_bitrate_slider.setValue(window.audio_bitrate_slider.maximum())

        self.assertEqual(item_a.data(queue_widget.STATUS_COL, Qt.UserRole)["bit_depth"], 10)
        self.assertEqual(item_b.data(queue_widget.STATUS_COL, Qt.UserRole)["bit_depth"], 8)  # untouched
        expected_bitrate = window._current_settings()["audio_bitrate"]
        self.assertEqual(item_a.data(queue_widget.STATUS_COL, Qt.UserRole)["audio_bitrate"], expected_bitrate)
        self.assertEqual(item_b.data(queue_widget.STATUS_COL, Qt.UserRole)["audio_bitrate"], expected_bitrate)

    def test_a_handler_that_changes_two_keys_together_carries_both(self):
        # Quality tier is a deliberate exception to "only the one control
        # touched" -- clicking it is a single decision that legitimately
        # sets both rc_mode and quality_value together, and both must
        # still propagate as one unit, not just whichever the panel
        # happened to already differ on.
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            _make_clip(clip_a, "aac")
            _make_clip(clip_b, "aac")
            window.add_files([clip_a, clip_b])
            _wait_for_detection(window)
        item_a = window.queue_list.topLevelItem(0)
        item_b = window.queue_list.topLevelItem(1)
        item_a.setSelected(True)
        item_b.setSelected(True)
        window._on_queue_selection_changed()

        window.quality_better_btn.click()

        settings = window._current_settings()
        for item in (item_a, item_b):
            job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
            self.assertEqual(job["rc_mode"], settings["rc_mode"])
            self.assertEqual(job["quality_value"], settings["quality_value"])

    def test_selecting_a_new_item_resets_the_diff_baseline(self):
        # A second, later selection's own starting settings must be the
        # baseline for *that* selection's edits -- not still comparing
        # against whatever the very first selection happened to show,
        # which could make an unrelated, already-true value look like a
        # "change" the moment anything else is edited.
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            _make_clip(clip_a, "aac")
            _make_clip(clip_b, "aac")
            window.add_files([clip_a, clip_b])
            _wait_for_detection(window)
        item_a = window.queue_list.topLevelItem(0)
        item_b = window.queue_list.topLevelItem(1)
        job_b = item_b.data(queue_widget.STATUS_COL, Qt.UserRole)
        job_b["bit_depth"] = 8
        item_b.setData(queue_widget.STATUS_COL, Qt.UserRole, job_b)

        item_a.setSelected(True)
        window._on_queue_selection_changed()
        item_a.setSelected(False)
        item_b.setSelected(True)
        window._on_queue_selection_changed()

        window.audio_bitrate_slider.setValue(window.audio_bitrate_slider.maximum())

        self.assertEqual(item_b.data(queue_widget.STATUS_COL, Qt.UserRole)["bit_depth"], 8)


class TestAudioBitrateSlider(unittest.TestCase):
    """Converted from a QComboBox to a slider (matching Quality/Speed on
    the Video tab) -- the slider's value is an *index* into AUDIO_BITRATES,
    not a kbps number, so the interesting behavior to lock in is the
    round-trip through that index, not just "does a slider move"."""

    def test_default_is_160k(self):
        window = main.MainWindow()
        self.assertEqual(window._current_settings()["audio_bitrate"], "160k")

    def test_slider_value_round_trips_through_current_settings(self):
        window = main.MainWindow()
        window.audio_bitrate_slider.setValue(main.AUDIO_BITRATES.index("256k"))
        self.assertEqual(window._current_settings()["audio_bitrate"], "256k")
        self.assertEqual(window.audio_bitrate_label.text(), "256k")

    def test_apply_settings_sets_the_slider_to_the_matching_index(self):
        window = main.MainWindow()
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "audio_bitrate": "96k"})
        self.assertEqual(window.audio_bitrate_slider.value(), main.AUDIO_BITRATES.index("96k"))

    def test_apply_settings_falls_back_to_160k_for_an_unknown_value(self):
        # A queue job's settings dict could carry a bitrate string that's
        # no longer one of the five real stops -- the old combo's
        # setCurrentText() silently ignored that; .index() would crash
        # without this same defensive fallback in _apply_settings_to_controls.
        window = main.MainWindow()
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "audio_bitrate": "not-a-real-bitrate"})
        self.assertEqual(window.audio_bitrate_slider.value(), main.AUDIO_BITRATES.index("160k"))

    def test_every_bitrate_has_its_own_tier_caption(self):
        window = main.MainWindow()
        for i in range(len(main.AUDIO_BITRATES)):
            window.audio_bitrate_slider.setValue(i)
            self.assertTrue(window.audio_bitrate_tier_label.text())


class TestX265SpeedSlider(unittest.TestCase):
    """Converted from a QComboBox (speed_combo) to a slider
    (speed_x265_slider), matching VAAPI's own Speed slider and Audio
    Bitrate's conversion earlier -- same shape of interesting behavior:
    the slider's value is an index into X265_PRESETS, not a value with
    arithmetic meaning of its own, so the round-trip through that index is
    what actually needs locking in."""

    def test_default_is_medium(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window._current_settings()["speed"], "medium")

    def test_slider_value_round_trips_through_current_settings(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        window.speed_x265_slider.setValue(main.X265_PRESETS.index("veryslow"))
        self.assertEqual(window._current_settings()["speed"], "veryslow")
        # The preset name used to have its own separate label next to
        # "Slower" -- discussed directly, dropped as redundant and folded
        # into speed_tier_label's own caption instead, so it isn't lost.
        self.assertIn("(veryslow)", window.speed_tier_label.text())

    def test_apply_settings_sets_the_slider_to_the_matching_index(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "speed": "superfast"})
        self.assertEqual(window.speed_x265_slider.value(), main.X265_PRESETS.index("superfast"))

    def test_apply_settings_falls_back_to_medium_for_an_unknown_value(self):
        # Same defensive-fallback reasoning as Audio Bitrate -- a queue
        # job's settings dict could carry a preset name that isn't one of
        # X265_PRESETS; the old combo's setCurrentText() silently ignored
        # that, .index() would crash without this same fallback.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "speed": "not-a-real-preset"})
        self.assertEqual(window.speed_x265_slider.value(), main.X265_PRESETS.index("medium"))

    def test_every_preset_has_its_own_tier_caption(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        for i in range(len(main.X265_PRESETS)):
            window.speed_x265_slider.setValue(i)
            self.assertTrue(window.speed_tier_label.text())

    def test_fastest_and_slowest_ends_get_the_expected_captions(self):
        # X265_PRESETS is ordered fastest-to-slowest already (ultrafast at
        # index 0, placebo at the end) -- opposite fraction direction from
        # the VAAPI slider's compression_level (1=slowest there), confirmed
        # explicitly here rather than just trusting the caption list order.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        window.speed_x265_slider.setValue(0)
        self.assertIn("Fast", window.speed_tier_label.text())
        window.speed_x265_slider.setValue(len(main.X265_PRESETS) - 1)
        self.assertIn("Maximum effort", window.speed_tier_label.text())

    def test_six_tiers_match_the_vaapi_slider_reversed(self):
        # Speed's caption list was expanded from 3 to 6 tiers (matching
        # Quality's own earlier expansion), reusing the same 6 captions for
        # both engines just in opposite order -- confirmed here rather than
        # assumed, since a copy-paste ordering mistake between the two
        # _tier_label() calls wouldn't show up any other way.
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        x265_captions = []
        for i in range(len(main.X265_PRESETS)):
            window.speed_x265_slider.setValue(i)
            x265_captions.append(window.speed_tier_label.text())

        window.encoder_combo.setCurrentText("Intel (iGPU)")
        vaapi_captions = []
        for i in range(window.speed_slider.minimum(), window.speed_slider.maximum() + 1):
            window.speed_slider.setValue(i)
            vaapi_captions.append(window.speed_tier_label.text())

        # startswith, not equal -- x265's caption now has the raw preset
        # name folded on the end ("... (medium)"), which the VAAPI side
        # has no equivalent of (see _on_speed_x265_slider_changed).
        self.assertTrue(x265_captions[0].startswith(vaapi_captions[-1]))
        self.assertTrue(x265_captions[-1].startswith(vaapi_captions[0]))


class TestX264Codec(unittest.TestCase):
    """libx264 is a second software codec alongside libx265, chosen via
    its own Format -- Codec control (codec_combo), separate from Encoder
    (encoder_combo, "CPU"/"Intel (iGPU)"/"AMD (GPU)" -- which *engine*
    runs the encode). Discussed directly and deliberately split this way
    rather than one flat "CPU (x264)" row in Encoder itself: hardware
    here is HEVC-only (no h264_vaapi wired up), so a codec choice that
    only ever means something for one of the three engine rows reads
    better as its own control than as extra combined rows.

    Both controls resolve together into the one real ffmpeg encoder id
    via _current_encoder_id() -- shares speed_x265_slider with libx265
    (same preset names, confirmed in worker.py), not the VAAPI
    compression_level slider, which _current_settings()/
    _apply_settings_to_controls() originally decided by checking
    `encoder == "libx265"` specifically. That was a real bug for
    libx264: it fell through to the VAAPI branch instead, which several
    of these tests exercise directly.

    Consumer build note: codec_combo was promoted to Normal (the Encoding
    group, alongside Processing) per the final control-hierarchy decision
    -- it's a real, always-shown QComboBox now, not gated behind Expert
    at all, so .setCurrentText() on it here needs no window.show() or
    Expert-expansion workaround regardless of enabled/visible state.
    encoder_combo/tune_combo do still live in the Video tab's Expert
    form (a real collapsible QGroupBox again, window.video_expert_group)
    -- but .setCurrentText()/.setCurrentIndex() are property-style calls,
    not synthesized click/focus events, so they still work regardless of
    Expert's own collapsed state (confirmed directly: isEnabled() and
    the resulting _current_settings() both read correctly without ever
    expanding it) -- unlike QPushButton.click(), which TestRateControl
    Buttons' own docstring documents as a genuine no-op on a collapsed
    Expert's buttons. _shown_window_with_expert_open (TestRateControl
    Buttons, below) is a thin window+show() wrapper here purely by
    convention/naming reuse, not because this class's own tests need
    Expert expanded -- kept that way instead
    of removing the helper and updating every call site for a rename that
    doesn't change behavior.

    One gap remains, unrelated to Expert's own visibility:

    This machine's own best_available_engine() resolves to real
    hardware (confirmed directly: ('hevc_vaapi', 'intel')) -- MainWindow()
    starts on Intel, not CPU/libx265, by the app's own deliberate design
    (Automatic resolves once at construction; see main.py). Any assertion
    here that implicitly wants a CPU/libx265 baseline needs
    encoder_combo.setCurrentText("CPU") first -- it can't rely on that
    being MainWindow()'s own out-of-the-box default, which is genuinely
    machine-dependent."""

    def _shown_window_with_expert_open(self):
        window = main.MainWindow()
        window.show()
        return window

    def test_codec_combo_offers_both(self):
        window = main.MainWindow()
        items = [window.codec_combo.itemText(i) for i in range(window.codec_combo.count())]
        self.assertIn("H.265 (HEVC)", items)
        self.assertIn("H.264 (AVC)", items)

    def test_default_is_libx265(self):
        # Not just "MainWindow() defaults to libx265" -- that's only true
        # on a machine with no hardware acceleration. What's actually
        # being tested (and what's true everywhere): H.265 is CPU's own
        # default codec choice, independent of whatever Automatic
        # resolved at construction.
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window._current_settings()["encoder"], "libx265")

    def test_selecting_h264_sets_libx264_as_the_encoder(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        self.assertEqual(window._current_settings()["encoder"], "libx264")

    def test_codec_combo_disabled_and_forced_to_h265_for_hardware(self):
        # No h264_vaapi wired up -- hardware is HEVC-only here, so the
        # codec choice becomes meaningless (not just irrelevant) the
        # moment a hardware engine is selected.
        window = self._shown_window_with_expert_open()
        window.codec_combo.setCurrentText("H.264 (AVC)")
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertFalse(window.codec_combo.isEnabled())
        self.assertEqual(window.codec_combo.currentText(), "H.265 (HEVC)")
        self.assertEqual(window._current_settings()["encoder"], "hevc_vaapi")

    def test_codec_combo_re_enabled_switching_back_to_cpu(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        window.encoder_combo.setCurrentText("CPU")
        self.assertTrue(window.codec_combo.isEnabled())

    def test_current_settings_reads_speed_from_the_x265_slider_not_vaapi(self):
        # The actual bug: encoder == "libx265" fell through to
        # str(self.speed_slider.value()) (the VAAPI widget) for libx264,
        # producing a bogus compression_level-shaped string instead of a
        # real preset name.
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        window.speed_x265_slider.setValue(main.X265_PRESETS.index("veryslow"))
        self.assertEqual(window._current_settings()["speed"], "veryslow")

    def test_applying_a_libx264_settings_dict_selects_cpu_and_h264(self):
        # The other half of the same bug: _apply_settings_to_controls
        # matched ENCODERS by exact encoder-id string before this fix,
        # which has no "libx264" row at all (constants.py: the CPU row's
        # own id is just a placeholder) -- fell through to index 0
        # (harmlessly landing on CPU by coincidence) without ever
        # touching codec_combo, and separately crashed on
        # int("veryslow") for the speed slider.
        window = main.MainWindow()
        settings = {**window._current_settings(), "encoder": "libx264", "speed": "veryslow"}
        window._apply_settings_to_controls(settings)  # must not raise
        self.assertEqual(window.encoder_combo.currentText(), "CPU")
        self.assertEqual(window.codec_combo.currentText(), "H.264 (AVC)")
        self.assertEqual(window.speed_x265_slider.value(), main.X265_PRESETS.index("veryslow"))

    def test_applying_a_vaapi_settings_dict_does_not_touch_codec_combo_state(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        settings = {**window._current_settings(), "encoder": "hevc_vaapi", "gpu_vendor": "intel",
                    "rc_mode": "ICQ", "quality_value": 26, "speed": "1"}
        window._apply_settings_to_controls(settings)
        self.assertEqual(window.encoder_combo.currentText(), "Intel (iGPU)")
        self.assertFalse(window.codec_combo.isEnabled())

    def test_tune_combo_offers_film_and_stillimage_for_x264(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        items = [window.tune_combo.itemText(i) for i in range(window.tune_combo.count())]
        self.assertIn("film", items)
        self.assertIn("stillimage", items)

    def test_tune_combo_excludes_film_for_x265(self):
        # This exact libx265 build rejects "film" outright (see
        # test_worker.py's TestTune.test_film_is_deliberately_not_offered)
        # -- must never be offered as a choice for x265, unlike x264.
        # tune_combo starts on X265_TUNES unconditionally at construction
        # (ui_builder.py) regardless of encoder/Expert state, so this one
        # genuinely doesn't need the CPU/Expert-open setup the rest of
        # this class needs -- it's checking the plain construction-time
        # default, not anything a real repopulation cascade produced.
        window = main.MainWindow()
        items = [window.tune_combo.itemText(i) for i in range(window.tune_combo.count())]
        self.assertNotIn("film", items)
        self.assertNotIn("stillimage", items)

    def test_switching_from_x264_film_to_x265_resets_tune_to_none(self):
        # "film" isn't valid for x265 at all -- must not silently carry
        # over as some other tune picked by whatever index happened to
        # land there once the list shrinks.
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        window.tune_combo.setCurrentText("film")
        window.codec_combo.setCurrentText("H.265 (HEVC)")
        self.assertEqual(window.tune_combo.currentText(), "None")

    def test_switching_x265_to_x264_preserves_a_shared_tune_value(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.tune_combo.setCurrentText("grain")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        self.assertEqual(window.tune_combo.currentText(), "grain")

    def test_build_args_omits_x265_params_for_x264(self):
        # Full round trip through the GUI layer, not just worker.py's own
        # TestBuildArgsX264 (which constructs the settings dict by hand)
        # -- confirms _current_settings() -> build_args actually connects
        # correctly end to end.
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        settings = window._current_settings()
        args = worker.build_args(
            settings, Path("in.mkv"), Path("out.mp4"),
            probe_audio=False, audio_codec=None, duration_seconds=10.0,
        )
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertNotIn("-x265-params", args)

    def test_h265_better_quality_survives_switch_to_h264(self):
        # Real, reported bug: codec_combo used to wire straight to
        # _on_encoder_changed, which rebuilds rc_mode_combo from scratch
        # and resets currentIndex to 0 -- silently abandoning the user's
        # Quality-tier choice on every codec switch. _on_codec_changed
        # (main.py) now captures/restores it, same shape as
        # _on_processing_choice already does for engine switches.
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.quality_better_btn.click()
        window.codec_combo.setCurrentText("H.264 (AVC)")
        settings = window._current_settings()
        self.assertEqual(settings["encoder"], "libx264")
        self.assertEqual(settings["quality_value"], main.QUALITY_TIERS["libx264"]["better"])
        self.assertTrue(window.quality_better_btn.isChecked())

    def test_h264_better_quality_survives_switch_to_h265(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        window.quality_better_btn.click()
        window.codec_combo.setCurrentText("H.265 (HEVC)")
        settings = window._current_settings()
        self.assertEqual(settings["encoder"], "libx265")
        self.assertEqual(settings["quality_value"], main.QUALITY_TIERS["libx265"]["better"])
        self.assertTrue(window.quality_better_btn.isChecked())

    def test_h265_file_size_800mb_survives_switch_to_h264(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.mode_filesize_btn.click()
        window.size_spin.setValue(800)
        window.codec_combo.setCurrentText("H.264 (AVC)")
        settings = window._current_settings()
        self.assertEqual(settings["encoder"], "libx264")
        self.assertEqual(settings["rc_mode"], "bitrate")
        self.assertEqual(settings["quality_value"], 800)
        self.assertTrue(window.mode_filesize_btn.isChecked())

    def test_h264_file_size_800mb_survives_switch_to_h265(self):
        window = self._shown_window_with_expert_open()
        window.encoder_combo.setCurrentText("CPU")
        window.codec_combo.setCurrentText("H.264 (AVC)")
        window.mode_filesize_btn.click()
        window.size_spin.setValue(800)
        window.codec_combo.setCurrentText("H.265 (HEVC)")
        settings = window._current_settings()
        self.assertEqual(settings["encoder"], "libx265")
        self.assertEqual(settings["rc_mode"], "bitrate")
        self.assertEqual(settings["quality_value"], 800)
        self.assertTrue(window.mode_filesize_btn.isChecked())


class TestAudioDownmix(unittest.TestCase):
    """audio_downmix_check (a checkbox, Expert-only) was replaced by the
    Channels segmented row (Keep Original/Stereo) directly in Normal mode
    -- see ui_builder.py's _build_audio_tab and main.py's
    _on_audio_channels_clicked. audio_channels_stereo_btn.isChecked() is
    the new source of truth _current_settings() reads."""

    def test_default_is_off(self):
        window = main.MainWindow()
        self.assertFalse(window._current_settings()["audio_downmix_stereo"])

    def test_button_round_trips_through_current_settings(self):
        window = main.MainWindow()
        window.audio_channels_stereo_btn.click()
        self.assertTrue(window._current_settings()["audio_downmix_stereo"])

    def test_apply_settings_sets_the_button(self):
        window = main.MainWindow()
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "audio_downmix_stereo": True})
        self.assertTrue(window.audio_channels_stereo_btn.isChecked())

    def test_apply_settings_defaults_to_off_for_an_older_preset_missing_the_key(self):
        # A preset saved before this control existed simply won't have this
        # key -- .get(..., False) in _apply_settings_to_controls, not a bare
        # index, is what keeps that from crashing (same reasoning as the
        # container/tune/deinterlace fallbacks it sits alongside).
        window = main.MainWindow()
        settings = window._current_settings()
        window.audio_channels_stereo_btn.click()
        old_settings = {k: v for k, v in settings.items() if k != "audio_downmix_stereo"}
        window._apply_settings_to_controls(old_settings)
        self.assertTrue(window.audio_channels_keep_btn.isChecked())


class TestComboPopupBackgroundFilter(unittest.TestCase):
    """_ComboPopupBackgroundFilter isn't installed by MainWindow() itself --
    only main() wires it onto the real QApplication -- so each test installs
    its own instance on the shared module-level _app and removes it again in
    tearDown, the same scoping discipline as if this were a fresh app each
    time. Confirmed real popup background/text-style bugs (not reproducible
    under offscreen rendering, only via real screen capture -- see main.py's
    class docstring) drove this filter's existence; what's checkable here
    without real rendering is the underlying property/stylesheet state it
    sets, which is what actually drives that rendering.
    """

    def setUp(self):
        self.filter = main._ComboPopupBackgroundFilter()
        _app.installEventFilter(self.filter)

    def tearDown(self):
        _app.removeEventFilter(self.filter)

    def test_popup_frame_gets_a_background_stylesheet_on_show(self):
        # res_combo, not preset_combo -- this fork has no Presets UI at
        # all (CONSUMER_FORK_PLAN.md), but Bug 1 (the black-bar popup-
        # background fix) is generic to every QComboBox, not preset-
        # specific, so it still needs a real combo to exercise against.
        window = main.MainWindow()
        window.res_combo.showPopup()
        _app.processEvents()
        popup = window.res_combo.view().window()
        self.assertIn(main._current_theme_palette["BG_PANEL"], popup.styleSheet())
        window.res_combo.hidePopup()
        _app.processEvents()

    # test_modified_indicator_is_suppressed_while_popup_is_open and
    # test_modified_indicator_is_restored_after_popup_closes (Bug 2,
    # theming.py's _ComboPopupBackgroundFilter docstring) were removed --
    # both tested QComboBox[modified="true"] styling bleeding into a
    # popup, a state only the (now-removed) preset-modified indicator
    # ever set on any combo. Nothing in this fork sets "modified" on any
    # QComboBox anymore, so that suppress/restore code path in the filter
    # itself is unreachable now, not just untested -- left in place as
    # harmless dead code rather than also touching theming.py in this pass.

    def test_unmodified_combo_is_left_alone(self):
        window = main.MainWindow()
        self.assertFalse(window.res_combo.property("modified"))
        window.res_combo.showPopup()
        _app.processEvents()
        self.assertFalse(window.res_combo.property("modified"))
        self.assertFalse(window.res_combo.property("_popupSuppressedModified"))
        window.res_combo.hidePopup()
        _app.processEvents()
        self.assertFalse(window.res_combo.property("modified"))


class TestComboWheelBlockFilter(unittest.TestCase):
    """_ComboWheelBlockFilter isn't installed by MainWindow() itself --
    only main() wires it onto the real QApplication -- so each test
    installs its own instance and removes it in tearDown, same scoping
    discipline as TestComboPopupBackgroundFilter above. Reported live:
    scrolling the mouse wheel over a combo box changed its value by
    accident, more so now that the left panel itself scrolls.

    Also pins Intel present (same reason TestExpertExpandGrowsWindow/
    TestLeftPanelScrolling do) -- one of these tests below needs Expert's
    expanded content to genuinely need scrolling, which a hardware-less
    machine's now-hidden Processing row can leave enough spare vertical
    room to no longer be true."""

    def setUp(self):
        self.filter = main._ComboWheelBlockFilter()
        _app.installEventFilter(self.filter)
        patcher = patch.object(worker, "find_render_node", side_effect=_find_render_node_for(worker.INTEL_VENDOR_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        _app.removeEventFilter(self.filter)

    @staticmethod
    def _wheel_event(delta=120):
        return QWheelEvent(
            QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, delta),
            Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False,
        )

    def test_wheel_over_a_combo_does_not_change_its_value(self):
        window = main.MainWindow()
        window.show()
        before = window.res_combo.currentIndex()
        QApplication.sendEvent(window.res_combo, self._wheel_event())
        self.assertEqual(window.res_combo.currentIndex(), before)

    def test_wheel_over_a_combo_inside_the_scrollable_panel_still_scrolls_it(self):
        # End-to-end, not just "the combo didn't change" -- the whole
        # point of forwarding to the parent (rather than just swallowing
        # the event outright) is that scrolling the panel still works
        # with the cursor over a combo box, not just that the combo
        # itself stays inert. Expert expanding now grows the window to
        # fit instead of needing to scroll (see TestExpertExpandGrows
        # Window) -- shrunk back down to the collapsed floor afterward
        # specifically to force a real scroll need, the same technique
        # TestLeftPanelScrolling's own fallback test uses.
        window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        left = window.findChild(QWidget, "leftPanel")
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        window.resize(window.width(), left.minimumHeight())
        _app.processEvents()
        self.assertGreater(left.verticalScrollBar().maximum(), 0)  # something to actually scroll
        before_index = window.codec_combo.currentIndex()
        before_scroll = left.verticalScrollBar().value()
        QApplication.sendEvent(window.codec_combo, self._wheel_event(delta=-120))
        self.assertEqual(window.codec_combo.currentIndex(), before_index)
        self.assertNotEqual(left.verticalScrollBar().value(), before_scroll)


class TestFocusVisibleFilter(unittest.TestCase):
    """_FocusVisibleFilter isn't installed by MainWindow() itself -- only
    main() wires it onto the real QApplication -- so each test installs its
    own instance and removes it in tearDown, same as TestComboPopupBackground
    Filter above. window.show() is required here, unlike most other tests in
    this file: a widget that's never been shown doesn't get real FocusIn/
    FocusOut events at all (confirmed directly -- the property stayed
    unset, not just False, without it), so this is one of the few classes
    where skipping show() would make every test pass for the wrong reason.
    Each focus-reason case needs its own clearFocus() first when reusing the
    same widget across assertions in one test -- confirmed directly that
    calling setFocus() again on a widget that already has focus is a no-op,
    generating no new FocusIn to observe.

    res_combo, not deinterlace_check or quality_smaller_btn -- two
    different widgets tried and rejected here, for two different reasons.
    deinterlace_check lives in window._video_expert_content, which this
    fork never shows at all (CONSUMER_FORK_PLAN.md), and setFocus()
    genuinely cannot land on an invisible widget (confirmed directly:
    property("focusVisible") stays None, not just False) -- a fundamental
    Qt behavior, not something a visibility-ignoring check like
    isVisibleTo() (used elsewhere in this file for widgets that stay
    interactive despite being unshown) can work around. quality_smaller_btn
    is visible, but became unusable here for a different reason once the
    Video/Audio tab widget was removed (left/right panel merge): it's now
    the very first focusable widget in the whole window, so Qt hands it
    automatic focus the moment window.show() runs, before setFocus(Tab)
    is ever called -- confirmed directly (app.focusWidget() was already
    quality_smaller_btn right after show()). setFocus() on a widget that
    already has focus is the same documented no-op as reusing one widget
    across assertions above, so it never generated a fresh, observable
    FocusIn at all; property("focusVisible") was just left at whatever
    that earlier auto-focus (a non-Tab reason) had already set it to --
    which happened to be False, making test_tab_focus_is_visible fail
    honestly and test_mouse_focus_is_not_visible pass for the wrong
    reason, the exact assertFalse(...)-is-blind-here failure mode this
    class's own docstring already warns about. Any focusable widget that
    ISN'T the window's natural first tab-stop sidesteps both problems;
    res_combo, well down in the Format group, isn't a candidate for
    Qt's initial auto-focus and isn't inside any hidden container either.
    """

    def setUp(self):
        self.filter = main._FocusVisibleFilter()
        _app.installEventFilter(self.filter)

    def tearDown(self):
        _app.removeEventFilter(self.filter)

    def test_tab_focus_is_visible(self):
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        window.res_combo.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        self.assertTrue(window.res_combo.property("focusVisible"))

    def test_mouse_focus_is_not_visible(self):
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        window.res_combo.setFocus(Qt.FocusReason.MouseFocusReason)
        _app.processEvents()
        self.assertFalse(window.res_combo.property("focusVisible"))

    def test_losing_focus_clears_the_property(self):
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        window.res_combo.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        self.assertTrue(window.res_combo.property("focusVisible"))
        window.res_combo.clearFocus()
        _app.processEvents()
        self.assertFalse(window.res_combo.property("focusVisible"))

    def test_window_activation_focus_events_do_not_crash(self):
        # Real bug, caught by actually running this against a real X11
        # display rather than assuming the widget-focused assumption held:
        # FocusIn/FocusOut also reach plain QWindow objects (the top-level
        # window itself gaining/losing OS-level focus), which have no
        # .style() -- crashed the filter the first time this ran for real.
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        qwindow = window.windowHandle()
        self.assertIsNotNone(qwindow)
        self.assertFalse(self.filter.eventFilter(qwindow, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.ActiveWindowFocusReason)))


class TestCommandPreviewGrouping(unittest.TestCase):
    def test_preview_is_broken_into_multiple_lines(self):
        window = main.MainWindow()
        text = window.command_preview.toPlainText()
        self.assertGreater(text.count("\n"), 0)
        # Grouping is cosmetic only -- flattening it back out must reproduce
        # the same tokens build_args() actually returns.
        self.assertEqual(" ".join(text.split()), " ".join(text.replace("\n", " ").split()))


class TestCopyCommandToClipboard(unittest.TestCase):
    """Real, confirmed bug: the Copy button copied command_preview's own
    *display* text -- _format_preview_text's grouped-onto-several-lines
    form, plain-space-joined with no shell quoting at all. A path
    containing a space pastes as two separate shell arguments that way,
    not one. The clipboard text now comes from shlex.join over the real
    args list this preview was actually built from instead."""

    def test_copied_text_quotes_a_path_with_a_space(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "My Video.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window._copy_command_to_clipboard()
            copied = QApplication.clipboard().text()
        # shlex quotes the *whole* argument the space appears in (the full
        # path), not just the space-containing word in isolation -- the
        # real round-trip check just below is the precise version of this,
        # this one just confirms the space-containing filename ends up
        # inside a quoted argument at all, not split into two bare tokens.
        self.assertIn("My Video.mkv'", copied)

    def test_copied_text_round_trips_through_shlex_split(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "My Video.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window._copy_command_to_clipboard()
            copied = QApplication.clipboard().text()
        self.assertEqual(shlex.split(copied), window._last_preview_args)

    def test_copies_nothing_useful_when_preview_is_unavailable(self):
        window = main.MainWindow()
        window._last_preview_args = []
        window._copy_command_to_clipboard()
        self.assertEqual(QApplication.clipboard().text(), "")


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
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._on_job_started(str(self.clip), 1, 1)
        self.assertFalse(window._running_items[0].icon(main.STATUS_COL).isNull())

    def test_failed_job_gets_a_tooltip_with_the_reason(self):
        window = main.MainWindow()
        window.add_files([self.clip])
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window._on_job_failed(str(self.clip), "ffmpeg exited 1")
        self.assertEqual(window._running_items[0].toolTip(main.STATUS_COL), "ffmpeg exited 1")


class TestQueueWideETA(unittest.TestCase):
    """_queue_eta_seconds rolls the current job's own live ETA (already
    computed in worker.py from real ffmpeg progress) up into a
    whole-queue estimate, applying that same observed speed to each
    not-yet-started row's own probed duration -- item.data(DURATION_COL,
    Qt.UserRole) as set in _on_source_probed, not the formatted display
    text ("1:23:45")."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.clip = cls.tmpdir / "clip.mkv"
        _make_clip(cls.clip, "aac")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _add_row(self, window, duration_seconds=None):
        window.add_files([self.clip])
        item = window.queue_list.topLevelItem(window.queue_list.topLevelItemCount() - 1)
        if duration_seconds is not None:
            item.setData(main.DURATION_COL, main.Qt.UserRole, duration_seconds)
        return item

    def test_no_current_job_eta_yet_returns_none(self):
        # Mirrors _on_job_stats' own "--:--" placeholder before the first
        # real progress tick of a run has landed.
        window = main.MainWindow()
        self.assertIsNone(window._queue_eta_seconds(None, 2.0))

    def test_no_speed_yet_returns_none(self):
        window = main.MainWindow()
        self.assertIsNone(window._queue_eta_seconds(30.0, None))

    def test_zero_speed_returns_none_not_a_divide_by_zero(self):
        window = main.MainWindow()
        self.assertIsNone(window._queue_eta_seconds(30.0, 0.0))

    def test_only_current_job_left_returns_just_its_own_eta(self):
        window = main.MainWindow()
        current = self._add_row(window, 100)
        window._running_items = [current]
        window._current_running_item = current
        self.assertEqual(window._queue_eta_seconds(30.0, 2.0), 30.0)

    def test_adds_estimated_time_for_each_remaining_job(self):
        window = main.MainWindow()
        current = self._add_row(window, 100)
        next_job = self._add_row(window, 60)  # 60s duration, 2.0x speed -> 30s estimated
        window._running_items = [current, next_job]
        window._current_running_item = current
        self.assertEqual(window._queue_eta_seconds(30.0, 2.0), 60.0)

    def test_job_with_no_probed_duration_yet_is_skipped_not_crashed(self):
        window = main.MainWindow()
        current = self._add_row(window, 100)
        still_probing = self._add_row(window, None)  # probe hasn't landed yet
        window._running_items = [current, still_probing]
        window._current_running_item = current
        self.assertEqual(window._queue_eta_seconds(30.0, 2.0), 30.0)

    def test_already_finished_jobs_before_current_are_not_double_counted(self):
        window = main.MainWindow()
        finished = self._add_row(window, 999)
        current = self._add_row(window, 100)
        window._running_items = [finished, current]
        window._current_running_item = current
        self.assertEqual(window._queue_eta_seconds(30.0, 2.0), 30.0)

    def test_on_job_stats_includes_queue_eta_in_the_label(self):
        # eta_label, not stats_label -- this fork's _on_job_stats no
        # longer populates a live fps/bitrate/speed/clock-format-ETA line
        # at all (CONSUMER_FORK_PLAN.md), only the plain-language queue-
        # wide estimate. 30s (current job) + 60s/2.0x (next job) = 60s
        # total -> format_eta_human(60) == "About 1 min remaining".
        window = main.MainWindow()
        current = self._add_row(window, 100)
        next_job = self._add_row(window, 60)
        window._running_items = [current, next_job]
        window._current_running_item = current
        window._on_job_stats({
            "fps": "24", "bitrate": "1200kbits/s", "speed": "2.0x",
            "eta_seconds": 30.0, "speed_multiplier": 2.0,
        })
        self.assertEqual(window.eta_label.text(), "About 1 min remaining")

    def test_on_job_stats_shows_placeholder_before_any_progress(self):
        # No eta_seconds/speed_multiplier yet -> _queue_eta_seconds returns
        # None -> eta_label.setText("") (empty, not a "--:--" placeholder --
        # that was stats_label's own raw-clock-format text, cut along with
        # the rest of that line).
        window = main.MainWindow()
        current = self._add_row(window, 100)
        window._running_items = [current]
        window._current_running_item = current
        window._on_job_stats({"fps": "?", "bitrate": "?", "speed": "?"})
        self.assertEqual(window.eta_label.text(), "")


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
        item = window.queue_list.topLevelItem(0)
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        self.assertTrue(job["deinterlace"])
        # "(interlaced)" lives in the subtitle (VIDEO_SUBTITLE_ROLE) now,
        # not item.text() -- that's the filename, the delegate's title
        # line (queue_widget._VideoCellDelegate).
        self.assertIn("(interlaced)", item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE))

    def test_progressive_file_stays_off(self):
        window = main.MainWindow()
        window.add_files([self.progressive_clip])
        _wait_for_detection(window)
        job = window.queue_list.topLevelItem(0).data(main.STATUS_COL, main.Qt.UserRole)
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
        job = window.queue_list.topLevelItem(0).data(main.STATUS_COL, main.Qt.UserRole)
        self.assertFalse(job["deinterlace"])

    def test_removing_the_item_before_detection_finishes_does_not_crash(self):
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window.queue_list.clear()  # deletes the C++ item object, not just detaches it
        _wait_for_detection(window)  # must not raise from the now-deleted item

    def test_manually_toggling_an_already_queued_items_checkbox_survives_late_detection(self):
        # Different scenario from test_detection_overrides_a_manually_
        # checked_box_when_source_is_progressive above -- that one toggles
        # the checkbox *before* any file exists (nothing selected yet, so
        # nothing is marked as a user override; the new job's starting
        # value is just whatever the checkbox happened to show, which
        # detection is still free to correct). This one selects a file
        # that's *already queued* and edits its checkbox directly -- a
        # real, confirmed race before this fix: the ~20s detector landing
        # after that edit would silently revert it, with nothing to
        # indicate it had happened.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        item = window.queue_list.topLevelItem(0)
        item.setSelected(True)
        window._on_queue_selection_changed()
        window.deinterlace_check.setChecked(False)  # user overrides before detection lands
        window._on_deinterlace_checkbox_changed()
        _wait_for_detection(window)
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        self.assertFalse(job["deinterlace"], "manual override must survive the late detection result")

    def test_convert_defers_while_detection_is_still_pending(self):
        # Real race otherwise: add_files' job snapshot is taken at add
        # time, before the ~20s sample has a result -- clicking Convert
        # immediately could begin encoding with whatever deinterlace value
        # the file started with, not what detection would actually find.
        # Unlike the sibling TITAN-i Transcoder app (which just refuses
        # and asks the user to click Start again), this fork defers and
        # auto-continues once detection lands -- the user already stated
        # their intent by clicking Convert once.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        self.assertTrue(window._detection_processes, "test assumes detection is still in flight")
        window._start()
        self.assertIn("analyzing", window.status_label.text())
        self.assertTrue(window._start_when_ready)
        # Disabled, not left clickable -- this is now a real "preparing"
        # state, not a refusal the user is meant to retry by hand.
        self.assertFalse(window.start_btn.isEnabled())
        _wait_for_detection(window)
        # The deferred Convert actually ran once detection finished, with
        # no second click -- proof it's not just a status message that
        # never resolves into a real run.
        self.assertFalse(window._start_when_ready)
        self.assertFalse(window._queue_editable)
        # The actual point of deferring at all: the job handed to the real
        # queue reflects the *detected* value, not whatever the file
        # started with. Real, confirmed regression otherwise -- an earlier
        # version of this fix called _maybe_begin_ready_conversion() ahead
        # of job["deinterlace"]'s own assignment in _on_interlace_detected,
        # so a Convert click landing on the very last outstanding probe
        # could still begin the run one line too early.
        self.assertTrue(window.queue._jobs[0]["deinterlace"])
        window.queue.stop()  # real encode now in flight -- don't leak it past this test

    def test_preparing_message_counts_videos_not_probe_processes(self):
        # Each video spawns two QProcesses (interlace + source probe) --
        # len(_detection_processes) reported "analyzing 2 file(s)" for a
        # single added video. Confirmed a real, reported bug; this counts
        # _pending_analysis_items (one entry per video) instead.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window._start()
        self.assertIn("1 video", window.status_label.text())
        self.assertNotIn("2 ", window.status_label.text())
        _wait_for_detection(window)
        window.queue.stop()

    def test_adding_a_video_during_preparation_updates_the_pending_count(self):
        # Real gap: add_files() correctly registered the newly-added
        # video against _pending_analysis_items (conversion did end up
        # correctly waiting for it), but never called
        # _reconcile_pending_start_after_mutation() the way Remove/Clear/
        # Undo/Redo all do -- so the "Preparing…" status text stayed
        # stuck at the old, smaller count. A state/UI consistency bug,
        # not another readiness race.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window._start()
        self.assertIn("1 video", window.status_label.text())
        window.add_files([self.progressive_clip])
        self.assertEqual(len(window._pending_analysis_items), 2)
        self.assertIn("2 video", window.status_label.text())
        _wait_for_detection(window)
        self.assertFalse(window._start_when_ready)
        self.assertFalse(window._queue_editable, "both videos should have started converting")
        window.queue.stop()

    def test_adding_a_video_during_preparation_does_not_corrupt_preparing_button_text(self):
        # Regression guard: _update_start_button_label() is called
        # unconditionally by add_files() (a supported mid-preparation
        # workflow, confirmed by the test above), and "Preparing…" is now
        # a phase-owned start_btn label, not the idle count-based one --
        # without the _start_when_ready guard this pass adds, adding a
        # second video here would silently flip the disabled button's text
        # back to "Convert 2 Videos" mid-preparation.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window._start()
        self.assertEqual(window.start_btn.text(), "Preparing…")
        window.add_files([self.progressive_clip])
        self.assertEqual(window.start_btn.text(), "Preparing…")
        _wait_for_detection(window)
        window.queue.stop()

    def test_removing_pending_video_during_preparation_still_converts_the_rest(self):
        # Real gap: _pending_analysis_items was never cleaned up on
        # Remove/Clear/Undo/Redo, so a video removed while its own
        # analysis was still outstanding kept a deferred Convert
        # "preparing" forever, waiting on a video that was never going
        # to run.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip, self.progressive_clip])
        window._start()
        self.assertEqual(len(window._pending_analysis_items), 2)
        removed_item = window.queue_list.topLevelItem(1)
        removed_item.setSelected(True)
        window._remove_selected()
        self.assertEqual(window.queue_list.topLevelItemCount(), 1)
        # The removed video's own bookkeeping entry must be gone too, not
        # just its row -- otherwise this never resolves.
        self.assertEqual(len(window._pending_analysis_items), 1)
        _wait_for_detection(window)
        self.assertFalse(window._start_when_ready)
        self.assertFalse(window._queue_editable, "the remaining video should have started converting")
        window.queue.stop()

    def test_clearing_queue_during_preparation_cancels_the_pending_convert(self):
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window._start()
        self.assertTrue(window._start_when_ready)
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
        self.assertFalse(window._start_when_ready)
        self.assertTrue(window.start_btn.isEnabled())
        self.assertIn("empty", window.status_label.text().lower())
        # The now-irrelevant probes for the cleared video still land
        # eventually -- must not resurrect the cancelled Convert once
        # they do (TranscodeQueue.start([]) would otherwise run against
        # an empty queue the user explicitly cleared).
        _wait_for_detection(window)
        self.assertFalse(window._start_when_ready)
        self.assertTrue(window._queue_editable, "clearing must not have started a run")

    def test_undo_during_preparation_reconciles_the_pending_convert(self):
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window._start()
        self.assertTrue(window._start_when_ready)
        window._undo()  # back to an empty queue
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)
        self.assertFalse(window._start_when_ready)
        self.assertTrue(window.start_btn.isEnabled())

    def test_redo_during_preparation_waits_for_the_restored_videos_own_analysis(self):
        # Real, reported failure mode: _restore_queue_snapshot rebuilds
        # fresh rows (and fresh _pending_analysis_items entries) without
        # first clearing whatever was already in there, so a redo landing
        # while a Convert was already deferred could leave bookkeeping
        # for both stale, detached rows *and* the newly restored ones --
        # "Preparing -- analyzing 4 videos…" for 2 actually-visible ones.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        window.add_files([self.progressive_clip])
        window._undo()  # back down to just the interlaced clip; progressive_clip's own snapshot now sits on the redo stack
        self.assertEqual(window.queue_list.topLevelItemCount(), 1)
        window._start()  # defer Convert while the interlaced clip's own (post-undo, freshly restarted) analysis is still pending
        self.assertTrue(window._start_when_ready)
        self.assertEqual(len(window._pending_analysis_items), 1)

        window._redo()  # brings progressive_clip back too -- both rows rebuilt fresh
        self.assertEqual(window.queue_list.topLevelItemCount(), 2)
        # Exactly 2, not 4 -- the old snapshot's own now-detached bookkeeping
        # must not still be sitting in here alongside the freshly restored rows'.
        self.assertEqual(len(window._pending_analysis_items), 2)
        self.assertTrue(window._start_when_ready, "redo brought back a second video Convert should still wait for")
        self.assertIn("2 video", window.status_label.text())

        _wait_for_detection(window)
        self.assertFalse(window._start_when_ready)
        self.assertFalse(window._queue_editable, "both restored videos should have started converting")
        window.queue.stop()


class TestProbeProcessesFailedToStart(unittest.TestCase):
    """QProcess.finished never fires when the process fails to even start
    (see tests/test_worker.py's TestProcessFailedToStart for the same gap
    already fixed on the main encode process) -- _start_interlace_detection
    and _start_source_probe (queue_controller.py) didn't have the matching
    errorOccurred handling, so a missing/broken ffmpeg or ffprobe install
    left the failed process stuck in _detection_processes forever, and
    _start() refuses to run while that list is non-empty. Confirmed real
    via a PATH pointed at an empty fakebin/ -- both binaries genuinely
    missing, not just one specific tool broken."""

    def test_missing_binaries_does_not_wedge_detection_processes_forever(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        fakebin = Path(tempfile.mkdtemp(prefix="transcoder_test_fakebin_"))
        try:
            clip = tmpdir / "clip.mp4"
            clip.write_bytes(b"not a real video, never actually read")

            window = main.MainWindow()
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fakebin)
            try:
                window.add_files([clip])
                self.assertTrue(
                    window._detection_processes, "test assumes both probes are in flight"
                )
                _wait_for_detection(window, timeout_ms=5000)
            finally:
                os.environ["PATH"] = old_path

            self.assertEqual(
                window._detection_processes, [],
                "a probe that failed to start must not stay queued forever",
            )
            # And Start must not be permanently refused because of it.
            window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
            window.queue_list.topLevelItem(0).setData(
                main.STATUS_COL, main.Qt.UserRole, {"path": clip, **window._current_settings()}
            )
            with patch.object(window.queue, "start") as mock_start:
                window._start()
                mock_start.assert_called_once()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(fakebin, ignore_errors=True)


class TestDisplayPath(unittest.TestCase):
    """formatting.display_path() -- the Save-to field's display form.
    Collapses a path under the real home directory to "~/..." so the
    field reads as a normal user would expect, rather than spelling out
    the full resolved absolute path (which is still what's actually used
    for file operations -- see output_dir, unaffected by this)."""

    def test_path_under_home_collapses_to_tilde(self):
        self.assertEqual(
            formatting.display_path(Path.home() / "Videos" / "transcoded"),
            "~/Videos/transcoded",
        )

    def test_home_itself_collapses_to_bare_tilde(self):
        self.assertEqual(formatting.display_path(Path.home()), "~")

    def test_path_outside_home_is_shown_in_full(self):
        self.assertEqual(
            formatting.display_path(Path("/mnt/other/output")), "/mnt/other/output"
        )


class TestOutputEditNormalization(unittest.TestCase):
    """A manually-typed output path skipped normalization entirely --
    unlike Browse (_pick_output_dir), which only ever hands back a clean
    absolute path from the OS's own directory picker. Two real,
    confirmed consequences: no ~ expansion (Path('~/x') does not expand
    the tilde on its own), and build_args appends the output path as a
    bare final argv element, so a relative path/folder starting with "-"
    gets parsed by ffmpeg as a flag instead of a filename."""

    def test_tilde_expands_to_the_real_home_directory(self):
        window = main.MainWindow()
        window.output_edit.setText("~/some_transcoder_test_subdir")
        window._on_output_edit_changed()
        self.assertEqual(
            window.output_dir, Path.home() / "some_transcoder_test_subdir"
        )
        self.assertNotIn("~", str(window.output_dir))

    def test_field_reflects_the_normalized_path_back(self):
        window = main.MainWindow()
        window.output_edit.setText("~/some_transcoder_test_subdir")
        window._on_output_edit_changed()
        self.assertEqual(window.output_edit.text(), "~/some_transcoder_test_subdir")

    def test_dash_prefixed_relative_path_resolves_to_a_safe_absolute_one(self):
        window = main.MainWindow()
        window.output_edit.setText("-render")
        window._on_output_edit_changed()
        self.assertTrue(window.output_dir.is_absolute())
        self.assertFalse(str(window.output_dir).startswith("-"))

    def test_empty_text_leaves_output_dir_unchanged(self):
        window = main.MainWindow()
        original = window.output_dir
        window.output_edit.setText("   ")
        window._on_output_edit_changed()
        self.assertEqual(window.output_dir, original)


class TestVideoAudioLabels(unittest.TestCase):
    """Pure-logic tests for the queue table's codec/channel friendly-name
    helpers -- worker.parse_probe_output's own field extraction is covered
    in test_worker.TestSourceProbeHelpers; this is just the display layer."""

    def test_known_video_codec_gets_friendly_name(self):
        self.assertEqual(formatting.video_codec_label("hevc"), "HEVC")
        self.assertEqual(formatting.video_codec_label("h264"), "H.264")

    def test_unknown_video_codec_falls_back_to_uppercased_raw_name(self):
        self.assertEqual(formatting.video_codec_label("theora"), "THEORA")

    def test_missing_video_codec_is_a_question_mark(self):
        self.assertEqual(formatting.video_codec_label(None), "?")

    def test_known_audio_codec_gets_friendly_name(self):
        self.assertEqual(formatting.audio_codec_label("eac3"), "E-AC3")

    def test_channel_count_maps_to_surround_label(self):
        self.assertEqual(formatting.audio_channel_label(2), "Stereo")
        self.assertEqual(formatting.audio_channel_label(6), "5.1")

    def test_unusual_channel_count_falls_back_to_raw_number(self):
        self.assertEqual(formatting.audio_channel_label(3), "3ch")

    def test_missing_channel_count_is_a_question_mark(self):
        self.assertEqual(formatting.audio_channel_label(None), "?")


class TestSettingsSummary(unittest.TestCase):
    """formatting.settings_summary() -- the queue table's hover tooltip
    content, distinct from Effective Command's raw ffmpeg argv (that one's
    for a technical reader; this is the friendly version)."""

    @staticmethod
    def _builtin(name: str) -> dict:
        # By name, not a hardcoded index -- a previous version of a test
        # elsewhere in this file hardcoding a builtin_presets.json index
        # broke silently the moment that list's order changed (see
        # TestCommandPreviewErrorHandling's own comment on the same
        # lesson); confirmed directly here too, not just theoretical --
        # inserting x264's own trio into the list shifted "Intel
        # Balanced" from index 4 to index 7 and broke every one of these
        # tests' hardcoded [4] the same way.
        return next(p for p in presets.load_builtin_presets() if p["name"] == name)

    def test_vaapi_quality_mode_job(self):
        job = self._builtin("720p Intel Balanced (Hardware / VAAPI)")
        summary = formatting.settings_summary(job)
        self.assertIn("Intel (iGPU)", summary)
        self.assertIn("ICQ 26", summary)
        self.assertIn("720p", summary)
        self.assertIn("10-bit", summary)
        self.assertIn("MP4", summary)
        self.assertIn("Deinterlace: off", summary)
        self.assertIn("Track 1", summary)
        self.assertIn("copy if compatible", summary)
        self.assertIn("160k", summary)

    def test_cpu_job_omits_gpu_specific_wording(self):
        job = self._builtin("720p CPU Balanced (Software / x265)")
        summary = formatting.settings_summary(job)
        self.assertIn("CPU", summary)
        self.assertIn("CRF 23", summary)

    def test_cpu_x264_job_is_labeled_distinctly_from_x265(self):
        # Real regression otherwise: settings_summary's encoder_label used
        # to be looked up directly in ENCODERS, which no longer has a row
        # for "libx264" at all now that Codec is main.py's own separate
        # Format control (constants.py's CPU row is just a placeholder
        # id) -- would have silently fallen back to the raw string
        # "libx264" instead of a friendly label.
        job = self._builtin("720p CPU Balanced (Software / x264)")
        summary = formatting.settings_summary(job)
        self.assertIn("CPU (x264)", summary)
        self.assertNotIn("libx264", summary)

    def test_target_size_mode_shows_mb_not_a_bare_rc_mode_value(self):
        job = {**self._builtin("720p Intel Balanced (Hardware / VAAPI)"), "rc_mode": "VBR", "quality_value": 800}
        summary = formatting.settings_summary(job)
        self.assertIn("Target size: 800 MB", summary)
        self.assertNotIn("VBR 800", summary)

    def test_deinterlace_on_is_shown(self):
        job = {**self._builtin("720p Intel Balanced (Hardware / VAAPI)"), "deinterlace": True}
        self.assertIn("Deinterlace: on", formatting.settings_summary(job))

    def test_downmix_shown_only_when_checked(self):
        job = self._builtin("720p Intel Balanced (Hardware / VAAPI)")
        self.assertNotIn("downmix", formatting.settings_summary(job))
        job = {**job, "audio_downmix_stereo": True}
        self.assertIn("downmix to stereo", formatting.settings_summary(job))


class TestQueueRowTooltip(unittest.TestCase):
    """The queue table's per-row tooltip -- file path plus a friendly
    settings summary (formatting.settings_summary), not just the bare path
    it used to be."""

    def test_tooltip_includes_both_path_and_settings(self):
        window = main.MainWindow()
        job = {"path": Path("/tmp/some_clip.mkv"), **window._current_settings()}
        item = window._make_queue_row(job)
        window.queue_list.addTopLevelItem(item)
        tooltip = item.toolTip(main.VIDEO_COL)
        self.assertIn(str(job["path"]), tooltip)
        self.assertIn(formatting.settings_summary(job), tooltip)

    def test_tooltip_refreshes_after_a_live_settings_edit(self):
        window = main.MainWindow()
        job = {"path": Path("/tmp/some_clip.mkv"), **window._current_settings()}
        item = window._make_queue_row(job)
        window.queue_list.addTopLevelItem(item)
        item.setSelected(True)
        window.quality_slider.setValue(window.quality_slider.value() + 1)
        updated_job = item.data(main.STATUS_COL, main.Qt.UserRole)
        self.assertIn(formatting.settings_summary(updated_job), item.toolTip(main.VIDEO_COL))


class TestQueueContextMenu(unittest.TestCase):
    """Right-click on a queue row -- Reveal Source File / Reveal Output
    File. Both open the *containing folder* (QDesktopServices.openUrl),
    matching _open_output_dir's own existing behavior exactly, not a
    "select this file" action (not portably available outside a
    dolphin --select-style shell-out).

    Exercises _build_queue_context_menu/_handle_queue_context_action
    directly, never QMenu.exec() (called only from the thin
    _on_queue_context_menu glue, intentionally untested) or
    _on_queue_context_menu itself -- confirmed the hard way that a real
    QMenu.exec() blocks in a real, C++-level modal loop that
    unittest.mock.patch.object cannot intercept, even under the offscreen
    platform, with no timeout of its own."""

    @staticmethod
    def _find_action(menu, text):
        return next(a for a in menu.actions() if a.text() == text)

    def test_reveal_output_is_disabled_before_a_job_completes(self):
        window = main.MainWindow()
        job = {"path": Path("/tmp/clip.mkv"), **window._current_settings()}
        menu = window._build_queue_context_menu(job)
        self.assertFalse(self._find_action(menu, "Reveal Output File").isEnabled())

    def test_reveal_output_is_enabled_once_the_completed_file_exists(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "done.mp4"
            out_file.write_bytes(b"fake output")
            job = {
                "path": Path("/tmp/clip.mkv"), **window._current_settings(),
                "_completed_output_path": str(out_file),
            }
            menu = window._build_queue_context_menu(job)
            self.assertTrue(self._find_action(menu, "Reveal Output File").isEnabled())

    def test_reveal_output_stays_disabled_if_the_file_was_since_deleted(self):
        window = main.MainWindow()
        job = {
            "path": Path("/tmp/clip.mkv"), **window._current_settings(),
            "_completed_output_path": "/tmp/does_not_exist_anymore_12345.mp4",
        }
        menu = window._build_queue_context_menu(job)
        self.assertFalse(self._find_action(menu, "Reveal Output File").isEnabled())

    def test_reveal_source_opens_the_containing_folder(self):
        window = main.MainWindow()
        job = {"path": Path("/tmp/some/dir/clip.mkv"), **window._current_settings()}
        menu = window._build_queue_context_menu(job)
        action = self._find_action(menu, "Reveal Source File")
        with patch.object(queue_controller.QDesktopServices, "openUrl") as mock_open:
            window._handle_queue_context_action(job, action)
        mock_open.assert_called_once()
        self.assertEqual(mock_open.call_args[0][0].toLocalFile(), str(job["path"].parent))

    def test_reveal_output_opens_the_containing_folder(self):
        window = main.MainWindow()
        job = {
            "path": Path("/tmp/clip.mkv"), **window._current_settings(),
            "_completed_output_path": "/tmp/out/clip.mp4",
        }
        menu = window._build_queue_context_menu(job)
        action = self._find_action(menu, "Reveal Output File")
        with patch.object(queue_controller.QDesktopServices, "openUrl") as mock_open:
            window._handle_queue_context_action(job, action)
        mock_open.assert_called_once()
        self.assertEqual(mock_open.call_args[0][0].toLocalFile(), "/tmp/out")

    def test_no_action_chosen_does_nothing(self):
        # The real "dismissed the menu without picking anything" case --
        # QMenu.exec() returns None then.
        window = main.MainWindow()
        job = {"path": Path("/tmp/clip.mkv"), **window._current_settings()}
        with patch.object(queue_controller.QDesktopServices, "openUrl") as mock_open:
            window._handle_queue_context_action(job, None)
        mock_open.assert_not_called()

    def test_completed_output_path_is_recorded_on_job_finished(self):
        window = main.MainWindow()
        job = {"path": Path("/tmp/clip.mkv"), **window._current_settings()}
        item = window._make_queue_row(job)
        window.queue_list.addTopLevelItem(item)
        window._current_running_item = item
        window._on_job_finished("/tmp/clip.mkv", "/tmp/out/clip.mp4")
        updated = item.data(main.STATUS_COL, main.Qt.UserRole)
        self.assertEqual(updated["_completed_output_path"], "/tmp/out/clip.mp4")


class TestRefreshVideoCell(unittest.TestCase):
    """The Video cell's *subtitle* (VIDEO_SUBTITLE_ROLE -- the delegate's
    muted second line, queue_widget._VideoCellDelegate; item.text() itself
    is just the filename, untouched by any of this) depends on three
    independent async results (see _refresh_video_cell's own comment in
    queue_controller.py) that can land in any order -- every order must
    produce the same final subtitle. Raw inputs land on their own roles
    (queue_controller._RAW_VIDEO_LABEL_ROLE/_RAW_AUDIO_LABEL_ROLE), not
    VIDEO_SUBTITLE_ROLE itself -- that's the composed output only."""

    def test_codec_label_landing_first_then_deinterlace_flag(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setData(main.VIDEO_COL, queue_controller._RAW_VIDEO_LABEL_ROLE, "HEVC 1920x1080")
        window._refresh_video_cell(item)
        self.assertEqual(item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE), "HEVC 1920x1080")
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        job["deinterlace"] = True
        item.setData(main.STATUS_COL, main.Qt.UserRole, job)
        window._refresh_video_cell(item)
        self.assertEqual(
            item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE), "HEVC 1920x1080 (interlaced)"
        )

    def test_deinterlace_flag_landing_first_then_codec_label(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        job["deinterlace"] = True
        item.setData(main.STATUS_COL, main.Qt.UserRole, job)
        window._refresh_video_cell(item)
        self.assertIsNone(item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE))  # nothing to show yet
        item.setData(main.VIDEO_COL, queue_controller._RAW_VIDEO_LABEL_ROLE, "HEVC 1920x1080")
        window._refresh_video_cell(item)
        self.assertEqual(
            item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE), "HEVC 1920x1080 (interlaced)"
        )

    def test_progressive_source_gets_no_suffix(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")  # deinterlace defaults False
        item.setData(main.VIDEO_COL, queue_controller._RAW_VIDEO_LABEL_ROLE, "H.264 1280x720")
        window._refresh_video_cell(item)
        self.assertEqual(item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE), "H.264 1280x720")

    def test_audio_label_folds_into_the_same_subtitle(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setData(main.VIDEO_COL, queue_controller._RAW_VIDEO_LABEL_ROLE, "H.264 1280x720")
        item.setData(main.VIDEO_COL, queue_controller._RAW_AUDIO_LABEL_ROLE, "AAC Stereo")
        window._refresh_video_cell(item)
        subtitle = item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE)
        self.assertIn("H.264 1280x720", subtitle)
        self.assertIn("AAC Stereo", subtitle)

    def test_filename_title_is_never_touched_by_any_of_this(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setData(main.VIDEO_COL, queue_controller._RAW_VIDEO_LABEL_ROLE, "HEVC 1920x1080")
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "a.mkv")


class TestVideoCellDelegateElision(unittest.TestCase):
    """_VideoCellDelegate.paint() elides the title/subtitle it draws against
    the cell's live width -- but that has to be paint-only: the tooltip
    (_row_tooltip), settings, and everything else downstream read item.
    text()/VIDEO_SUBTITLE_ROLE directly, not anything paint() produces.
    Confirms a real paint() call, forced against a deliberately narrow rect
    so elision actually engages, leaves the underlying stored strings
    exactly as long as they started."""

    def test_paint_does_not_truncate_the_stored_title_or_subtitle(self):
        window = main.MainWindow()
        long_name = "Very_Long_Production_Master_Final_Really_Actually_Final_v3.mkv"
        item = _add_dummy_item(window, long_name)
        long_subtitle = "3840x2160 HEVC 10-bit  ·  E-AC-3 5.1  ·  deinterlaced"
        item.setData(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE, long_subtitle)

        delegate = window.queue_list.itemDelegateForColumn(main.VIDEO_COL)
        index = window.queue_list.indexFromItem(item, main.VIDEO_COL)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 80, 40)  # deliberately narrower than either string needs

        pixmap = QPixmap(80, 40)
        painter = QPainter(pixmap)
        delegate.paint(painter, option, index)
        painter.end()

        self.assertEqual(item.text(main.VIDEO_COL), long_name)
        self.assertEqual(item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE), long_subtitle)

    def test_paint_actually_draws_the_row_icon(self):
        # Real, confirmed bug caught by screenshot, not by any exception or
        # wrong-data assertion: `icon = opt.icon` captured before `opt.icon
        # = QIcon()` (clearing it so Fusion's own CE_ItemViewItem draw
        # doesn't paint an off-center icon before this delegate paints its
        # own, manually centered one) turned out not to be an independent
        # copy -- confirmed directly that clearing opt.icon afterward also
        # silently nulled this already-captured variable, so the icon never
        # painted at all. Fixed with QIcon(opt.icon), an explicit copy.
        # Regression coverage: sample the pixel where the icon should be
        # and confirm it isn't just background -- not exact-shape fragile,
        # just "something opaque got drawn there". decorationSize is set
        # explicitly below (16x16, matching the real value confirmed via a
        # live app trace) -- a bare hand-built QStyleOptionViewItem() never
        # gets a real one from initStyleOption() outside an actual view
        # paint cycle (confirmed directly: stays Qt's (-1, -1) sentinel
        # regardless of window.show()), which would silently zero out
        # icon_rect's area here regardless of whether the real paint()-time
        # copy bug above is fixed or not.
        window = main.MainWindow()
        window.show()
        item = _add_dummy_item(window, "a.mkv")
        item.setIcon(main.STATUS_COL, window._themed_icon("status_done"))

        delegate = window.queue_list.itemDelegateForColumn(main.VIDEO_COL)
        index = window.queue_list.indexFromItem(item, main.VIDEO_COL)
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, 260, 40)
        option.decorationSize = QSize(16, 16)

        pixmap = QPixmap(260, 40)
        pixmap.fill(Qt.white)
        painter = QPainter(pixmap)
        delegate.paint(painter, option, index)
        painter.end()

        # icon_rect starts at x=4, sized to decorationSize (16x16 default)
        # -- (12, 20) sits inside it regardless of exactly how tall the
        # option's row rect ends up, well clear of the surrounding padding.
        pixel = pixmap.toImage().pixelColor(12, 20)
        self.assertNotEqual(pixel, Qt.white, "expected the status icon to have painted something here")


class TestMakeQueueRow(unittest.TestCase):
    """_make_queue_row fills in what's known synchronously (file name, size)
    -- everything ffprobe-derived (resolution, duration, codecs) arrives
    later via _on_source_probed, covered end-to-end in
    TestSourceMetadataOnAdd below."""

    def test_file_name_and_size_are_set_immediately(self):
        window = main.MainWindow()
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            clip = tmpdir / "movie.mkv"
            clip.write_bytes(b"x" * 2048)
            job = {"path": clip, **window._current_settings()}
            item = window._make_queue_row(job)
            self.assertEqual(item.text(main.VIDEO_COL), "movie.mkv")
            self.assertEqual(item.text(main.SIZE_COL), "2.0KB")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_output_settings_are_not_shown_in_any_cell(self):
        # The whole point of this table: output settings (encoder, quality,
        # container, ...) live in and edit from the right-hand panel, not
        # duplicated here -- see _make_queue_row's own comment in main.py.
        window = main.MainWindow()
        job = {"path": Path("movie.mkv"), **window._current_settings()}
        item = window._make_queue_row(job)
        all_text = " ".join(item.text(c) for c in range(window.queue_list.columnCount()))
        self.assertNotIn(job["container"], all_text)
        self.assertNotIn(job["encoder"], all_text)

    def test_missing_file_size_does_not_crash(self):
        window = main.MainWindow()
        job = {"path": Path("/nonexistent/movie.mkv"), **window._current_settings()}
        item = window._make_queue_row(job)  # must not raise
        self.assertEqual(item.text(main.SIZE_COL), "")


class TestSourceMetadataOnAdd(unittest.TestCase):
    """Dropping a file in should probe its real source properties and fill
    in the queue row -- see TestAutoDetectInterlaceOnAdd above for the
    sibling deinterlace-detection probe this runs alongside."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.clip = cls.tmpdir / "source.mkv"
        _make_clip(cls.clip, "aac")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_probed_row_shows_real_source_properties(self):
        # Video column text is the filename (the delegate's title line,
        # queue_widget._VideoCellDelegate) -- resolution/codec/audio are
        # the subtitle line instead, VIDEO_SUBTITLE_ROLE, not a separate
        # visible column (Audio no longer has one at all -- folded in
        # here alongside video, see _refresh_video_cell in
        # queue_controller.py).
        window = main.MainWindow()
        window.add_files([self.clip])
        _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        self.assertEqual(item.text(main.VIDEO_COL), self.clip.name)
        subtitle = item.data(main.VIDEO_COL, queue_widget.VIDEO_SUBTITLE_ROLE)
        self.assertIn("H.264", subtitle)
        self.assertIn("320x240", subtitle)
        self.assertEqual(item.text(main.DURATION_COL), "0:01")
        self.assertIn("AAC", subtitle)

    def test_probe_does_not_affect_the_jobs_output_settings(self):
        # width/height on the job dict are the chosen OUTPUT target
        # resolution -- the source probe must never overwrite them with the
        # source file's own (unrelated) dimensions.
        window = main.MainWindow()
        settings = window._current_settings()
        window.add_files([self.clip])
        _wait_for_detection(window)
        job = window.queue_list.topLevelItem(0).data(main.STATUS_COL, main.Qt.UserRole)
        self.assertEqual(job["width"], settings["width"])
        self.assertEqual(job["height"], settings["height"])

    def test_removing_the_item_before_probe_finishes_does_not_crash(self):
        window = main.MainWindow()
        window.add_files([self.clip])
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
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        self.assertEqual(job["quality_value"], window.quality_slider.value())

    def test_changing_a_control_does_not_touch_unselected_items(self):
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv")
        item_b = _add_dummy_item(window, "b.mkv")
        item_a.setSelected(True)
        before_b = dict(item_b.data(main.STATUS_COL, main.Qt.UserRole))
        window.quality_slider.setValue(window.quality_slider.value() + 3)
        self.assertEqual(item_b.data(main.STATUS_COL, main.Qt.UserRole), before_b)

    def test_selecting_multiple_differently_configured_items_does_not_homogenize_them(self):
        # Regression: populating controls from item_a on selection must not
        # cascade into overwriting item_b's (deliberately different) settings.
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv", quality_value=20)
        item_b = _add_dummy_item(window, "b.mkv", quality_value=35)
        before_b = dict(item_b.data(main.STATUS_COL, main.Qt.UserRole))
        item_a.setSelected(True)
        item_b.setSelected(True)
        self.assertEqual(item_b.data(main.STATUS_COL, main.Qt.UserRole), before_b)

    def test_multi_select_then_control_change_applies_to_all_selected(self):
        window = main.MainWindow()
        item_a = _add_dummy_item(window, "a.mkv", quality_value=20)
        item_b = _add_dummy_item(window, "b.mkv", quality_value=35)
        item_a.setSelected(True)
        item_b.setSelected(True)
        window.quality_slider.setValue(17)
        self.assertEqual(item_a.data(main.STATUS_COL, main.Qt.UserRole)["quality_value"], 17)
        self.assertEqual(item_b.data(main.STATUS_COL, main.Qt.UserRole)["quality_value"], 17)

    def test_locked_queue_during_a_run_ignores_selection_edits(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setSelected(True)
        window._set_queue_editable(False)
        before = dict(item.data(main.STATUS_COL, main.Qt.UserRole))
        window.quality_slider.setValue(window.quality_slider.value() + 3)
        self.assertEqual(item.data(main.STATUS_COL, main.Qt.UserRole), before)


class TestQueueLockingDuringRun(unittest.TestCase):
    def test_set_queue_editable_toggles_remove_and_clear_only(self):
        # Add Files is deliberately excluded -- see TestLiveAppendDuringRun,
        # it's meant to keep working during a run.
        window = main.MainWindow()
        window._set_queue_editable(False)
        self.assertFalse(window.remove_btn.isEnabled())
        self.assertFalse(window.clear_btn.isEnabled())
        self.assertFalse(window._queue_editable)
        self.assertTrue(window.add_files_btn.isEnabled())
        window._set_queue_editable(True)
        self.assertTrue(window._queue_editable)

    def test_add_files_button_stays_enabled_through_a_run(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        self.assertTrue(window.add_files_btn.isEnabled())

    def test_start_locks_remove_and_clear_before_handing_off_to_the_engine(self):
        window = main.MainWindow()
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
        window.queue_list.topLevelItem(0).setData(main.STATUS_COL, main.Qt.UserRole, job)
        with patch.object(window.queue, "start") as mock_start:
            window._start()
            mock_start.assert_called_once()
        self.assertFalse(window.remove_btn.isEnabled())
        self.assertFalse(window._queue_editable)

    def test_set_queue_editable_also_locks_drag_reordering(self):
        # Real, confirmed gap: reordering rows by dragging was never
        # actually gated by this at all -- Remove/Clear were locked during
        # a run for the same reason (can't affect a job already running or
        # finished without the visible list lying about what's actually
        # executing), but a drag-reorder wasn't, so the on-screen order
        # could end up not matching TranscodeQueue's own fixed execution
        # order, and job_started's position-based _running_items lookup
        # could then attribute a status icon to the wrong row.
        window = main.MainWindow()
        self.assertFalse(window.queue_list.reorder_locked)
        window._set_queue_editable(False)
        self.assertTrue(window.queue_list.reorder_locked)
        window._set_queue_editable(True)
        self.assertFalse(window.queue_list.reorder_locked)

    def test_on_all_finished_unlocks_queue(self):
        window = main.MainWindow()
        window._set_queue_editable(False)
        window._on_all_finished()
        self.assertTrue(window.clear_btn.isEnabled())
        self.assertTrue(window._queue_editable)


class TestPauseResumeGUI(unittest.TestCase):
    """GUI-side wiring for Pause -- worker.py's own request_pause/resume/
    stop-while-paused mechanics are covered directly in test_worker.py's
    TestPauseResume; these confirm the checkbox, button text/state, and
    status label reflect that correctly."""

    def _running_window(self):
        window = main.MainWindow()
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
        window.queue_list.topLevelItem(0).setData(main.STATUS_COL, main.Qt.UserRole, job)
        with patch.object(window.queue, "start"):
            window._start()
        return window

    def test_pause_checkbox_disabled_until_a_run_starts(self):
        window = main.MainWindow()
        self.assertFalse(window.pause_after_check.isEnabled())

    def test_start_enables_the_pause_checkbox(self):
        window = self._running_window()
        self.assertTrue(window.pause_after_check.isEnabled())

    def test_checking_the_box_requests_a_pause(self):
        window = self._running_window()
        with patch.object(window.queue, "request_pause") as mock_request:
            window.pause_after_check.setChecked(True)
            mock_request.assert_called_once()

    def test_unchecking_the_box_cancels_the_request(self):
        window = self._running_window()
        window.pause_after_check.setChecked(True)
        with patch.object(window.queue, "cancel_pause_request") as mock_cancel:
            window.pause_after_check.setChecked(False)
            mock_cancel.assert_called_once()

    def test_on_paused_updates_status_button_and_checkbox(self):
        window = self._running_window()
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window.pause_after_check.setChecked(True)

        window._on_paused()

        self.assertIn("Paused", window.status_label.text())
        self.assertEqual(window.start_btn.text(), "Resume")
        self.assertTrue(window.start_btn.isEnabled())
        self.assertFalse(window.pause_after_check.isEnabled())
        self.assertFalse(window.pause_after_check.isChecked())
        self.assertTrue(window._queue_paused)

    def test_on_paused_status_has_no_stale_stop_after_suffix(self):
        # Real, pre-existing bug: _on_paused only ever fires because the
        # box WAS checked -- _set_status appends its own "will stop after
        # this video" suffix whenever pause_after_check.isChecked() is True
        # at the moment it runs, so if _set_status ran before the box was
        # unchecked, every real pause produced a nonsensical suffix on an
        # already-paused status.
        window = self._running_window()
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window.pause_after_check.setChecked(True)
        window._on_paused()
        self.assertNotIn("will stop after", window.status_label.text())

    def test_on_paused_reports_the_correct_remaining_count(self):
        window = self._running_window()
        a, b, c = (main.QTreeWidgetItem(["a"]), main.QTreeWidgetItem(["b"]), main.QTreeWidgetItem(["c"]))
        window._running_items = [a, b, c]
        window._current_running_item = a  # b and c still remaining
        window._on_paused()
        self.assertIn("2 file(s) remaining", window.status_label.text())

    def test_queue_stays_locked_while_paused(self):
        window = self._running_window()
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window._on_paused()
        self.assertFalse(window._queue_editable)

    def test_clicking_start_while_paused_calls_resume_not_a_fresh_start(self):
        window = self._running_window()
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window._on_paused()
        with patch.object(window.queue, "resume") as mock_resume, \
             patch.object(window.queue, "start") as mock_start:
            window._start()
            mock_resume.assert_called_once()
            mock_start.assert_not_called()
        self.assertFalse(window._queue_paused)
        # "Converting…", not a recomputed "Convert 1 Video" -- resuming
        # goes straight back into the converting phase (_apply_run_phase_
        # visuals("converting")), same as a fresh _begin_conversion() would,
        # not back to the idle/count-based label.
        self.assertEqual(window.start_btn.text(), "Converting…")

    def test_on_all_finished_resets_pause_state(self):
        window = self._running_window()
        window._running_items = [window.queue_list.topLevelItem(0)]
        window._current_running_item = window.queue_list.topLevelItem(0)
        window._on_paused()
        window._on_all_finished()
        self.assertFalse(window._queue_paused)
        self.assertEqual(window.start_btn.text(), "Convert 1 Video")
        self.assertFalse(window.pause_after_check.isEnabled())

    def test_cancel_while_genuinely_paused_says_stopped_not_complete(self):
        # Real, reported-live bug: TranscodeQueue.stop() (worker.py) isn't
        # always async -- with no live process to terminate (genuinely
        # paused between jobs), it emits all_finished synchronously, right
        # there inside stop(), via a plain same-thread connection to
        # _on_all_finished. _stop() used to set _run_cancelled *after*
        # calling queue.stop(), so that synchronous handler ran and read
        # _run_cancelled while it was still False -- a real Cancel click
        # while paused could report "✓ Conversion Complete" (or "Idle")
        # instead of a cancellation. Uses the real queue.stop() here
        # (not mocked) since the synchronous-emission path is the exact
        # thing under test.
        window = self._running_window()
        window.queue._paused = True  # genuinely paused between jobs, no live process
        window._stop()
        self.assertTrue(window._run_cancelled)
        self.assertEqual(window.status_label.text(), "Conversion Stopped")


class TestRunPhaseVisuals(unittest.TestCase):
    """_apply_run_phase_visuals (queue_controller.py) is the one function
    that makes start_btn/stop_btn/open_folder_btn/progress_bar/eta_label/
    stats_label reflect a given phase -- these lock in the exact widget
    state each phase produces, confirmed by screenshot during design."""

    def test_idle_hides_progress_eta_stats_and_stop(self):
        window = main.MainWindow()
        window.show()
        window._apply_run_phase_visuals("idle")
        self.assertFalse(window.progress_bar.isVisible())
        self.assertFalse(window.eta_label.isVisible())
        self.assertFalse(window.stats_label.isVisible())
        self.assertFalse(window.stop_btn.isVisible())
        self.assertFalse(window.open_folder_btn.isVisible())
        self.assertTrue(window.start_btn.isEnabled())

    def test_preparing_shows_indeterminate_progress_and_hides_stop(self):
        window = main.MainWindow()
        window.show()
        window._apply_run_phase_visuals("preparing")
        self.assertTrue(window.progress_bar.isVisible())
        self.assertEqual(window.progress_bar.minimum(), 0)
        self.assertEqual(window.progress_bar.maximum(), 0)  # indeterminate
        self.assertFalse(window.eta_label.isVisible())
        self.assertFalse(window.stats_label.isVisible())
        self.assertFalse(window.stop_btn.isVisible())  # no cancel path for analysis today
        self.assertEqual(window.start_btn.text(), "Preparing…")
        self.assertFalse(window.start_btn.isEnabled())

    def test_converting_shows_determinate_progress_and_cancel(self):
        window = main.MainWindow()
        window.show()
        window._apply_run_phase_visuals("preparing")  # leaves the bar indeterminate
        window._apply_run_phase_visuals("converting")
        self.assertEqual(window.progress_bar.minimum(), 0)
        self.assertEqual(window.progress_bar.maximum(), 1000)  # un-stuck from preparing's marquee
        self.assertTrue(window.progress_bar.isVisible())
        self.assertTrue(window.eta_label.isVisible())
        # stats_label stays hidden through "converting" in this fork --
        # no live technical line (CONSUMER_FORK_PLAN.md); it's still used,
        # and shown, for the finished-run size/savings summary instead
        # (see the "finished" phase elsewhere in this test class).
        self.assertFalse(window.stats_label.isVisible())
        self.assertTrue(window.stop_btn.isVisible())
        self.assertEqual(window.stop_btn.text(), "Cancel")
        self.assertEqual(window.start_btn.text(), "Converting…")
        self.assertFalse(window.start_btn.isEnabled())

    def test_paused_leaves_progress_value_frozen(self):
        window = main.MainWindow()
        window.show()
        window._apply_run_phase_visuals("converting")
        window.progress_bar.setValue(630)
        window._apply_run_phase_visuals("paused")
        self.assertEqual(window.progress_bar.value(), 630)
        self.assertEqual(window.start_btn.text(), "Resume")
        self.assertTrue(window.start_btn.isEnabled())
        self.assertTrue(window.stop_btn.isVisible())
        self.assertEqual(window.stop_btn.text(), "Cancel")

    def test_finished_hides_progress_and_shows_open_folder(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 4
        window._run_total_input_bytes = 12_400_000_000
        window._run_total_output_bytes = 4_100_000_000
        window._apply_run_phase_visuals("finished")
        self.assertFalse(window.progress_bar.isVisible())
        self.assertTrue(window.open_folder_btn.isVisible())
        self.assertTrue(window.open_folder_btn.isEnabled())
        self.assertFalse(window.stop_btn.isVisible())
        self.assertTrue(window.start_btn.isEnabled())
        self.assertEqual(window.eta_label.text(), "4 videos converted")
        self.assertIn("→", window.stats_label.text())


class TestRunTotalsAccumulation(unittest.TestCase):
    def test_on_job_finished_accumulates_run_totals(self):
        window = main.MainWindow()
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        try:
            src1, out1 = tmpdir / "a_in.mkv", tmpdir / "a_out.mp4"
            src2, out2 = tmpdir / "b_in.mkv", tmpdir / "b_out.mp4"
            src1.write_bytes(b"x" * 1000)
            out1.write_bytes(b"x" * 400)
            src2.write_bytes(b"x" * 2000)
            out2.write_bytes(b"x" * 600)
            item1 = _add_dummy_item(window, "a.mkv")
            window._current_running_item = item1
            window._on_job_finished(str(src1), str(out1))
            item2 = _add_dummy_item(window, "b.mkv")
            window._current_running_item = item2
            window._on_job_finished(str(src2), str(out2))
            self.assertEqual(window._run_completed_count, 2)
            self.assertEqual(window._run_total_input_bytes, 3000)
            self.assertEqual(window._run_total_output_bytes, 1000)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_on_job_finished_still_counts_completion_when_size_stat_fails(self):
        # A transient stat() failure on one output file shouldn't uncount
        # a video that genuinely finished converting -- _run_completed_
        # count and the byte totals are deliberately independent.
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        window._current_running_item = item
        window._on_job_finished("/nonexistent/in.mkv", "/nonexistent/out.mp4")
        self.assertEqual(window._run_completed_count, 1)
        self.assertEqual(window._run_total_input_bytes, 0)
        self.assertEqual(window._run_total_output_bytes, 0)

    def test_begin_conversion_resets_run_totals(self):
        window = main.MainWindow()
        window._run_completed_count = 3
        window._run_total_input_bytes = 999
        window._run_total_output_bytes = 111
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
        window.queue_list.topLevelItem(0).setData(main.STATUS_COL, main.Qt.UserRole, job)
        with patch.object(window.queue, "start"):
            window._begin_conversion()
        self.assertEqual(window._run_completed_count, 0)
        self.assertEqual(window._run_total_input_bytes, 0)
        self.assertEqual(window._run_total_output_bytes, 0)


class TestOnAllFinishedSummary(unittest.TestCase):
    def test_shows_summary_when_jobs_completed(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 4
        window._on_all_finished()
        self.assertTrue(window.open_folder_btn.isVisible())
        self.assertEqual(window.status_label.text(), "✓ Conversion Complete")

    def test_falls_back_to_idle_when_nothing_completed(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 0
        window._on_all_finished()
        self.assertFalse(window.open_folder_btn.isVisible())
        self.assertEqual(window.status_label.text(), "Idle")
        # Reported live: "Idle" reads as internal state-machine language,
        # not product language -- hidden rather than shown, same as the
        # rest of this run-status area already collapses to nothing at
        # rest (see TestHideIdleStatus below for the dedicated coverage).
        self.assertFalse(window.status_label.isVisible())

    def test_zero_success_cancelled_run_says_stopped_not_idle(self):
        # Real gap: cancelling before the first job finished (or a
        # zero-success run generally) fell all the way back to "Idle"
        # unconditionally -- accurate for "nothing was ever queued",
        # misleading for "the user just cancelled" or "every job failed".
        # No finished-summary controls either way (nothing to summarize
        # with 0 successes), just a truthful status line instead of a
        # blank one.
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 0
        window._run_cancelled = True
        window._on_all_finished()
        self.assertFalse(window.open_folder_btn.isVisible())
        self.assertEqual(window.status_label.text(), "Conversion Stopped")

    def test_zero_success_all_failed_says_failed_not_idle(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 0
        window._run_failed_count = 3
        window._on_all_finished()
        self.assertFalse(window.open_folder_btn.isVisible())
        self.assertEqual(window.status_label.text(), "Conversion Failed")

    def test_partial_completion_still_shows_summary(self):
        # Deliberate, non-obvious choice: 2-of-4 completed then cancelled
        # (or the rest failed) still shows "2 videos converted" -- a real
        # result worth showing, not discarded just because the run didn't
        # finish everything it started with.
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 2
        window._on_all_finished()
        self.assertTrue(window.open_folder_btn.isVisible())
        self.assertEqual(window.eta_label.text(), "2 videos converted")

    def test_stop_sets_the_cancelled_flag_for_a_real_click(self):
        # Confirms _stop() itself (not just a hand-set flag) is what marks
        # a run cancelled -- the full real path a Cancel click takes.
        window = main.MainWindow()
        window.show()
        job = {"path": Path("dummy.mkv"), **window._current_settings()}
        window.queue_list.addTopLevelItem(main.QTreeWidgetItem(["dummy"]))
        window.queue_list.topLevelItem(0).setData(main.STATUS_COL, main.Qt.UserRole, job)
        with patch.object(window.queue, "start"):
            window._start()
        self.assertFalse(window._run_cancelled)
        with patch.object(window.queue, "stop"):
            window._stop()
        self.assertTrue(window._run_cancelled)

    def test_cancelled_run_says_stopped_not_complete(self):
        # Real, confirmed gap: _run_completed_count > 0 alone can't tell
        # "the whole run succeeded" from "the user cancelled after some
        # jobs already finished" -- both used to say "✓ Conversion
        # Complete", which is misleading for the latter.
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 2
        window._run_cancelled = True
        window._on_all_finished()
        self.assertEqual(window.status_label.text(), "Conversion Stopped")

    def test_run_with_failures_says_completed_with_issues(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 3
        window._run_failed_count = 1
        window._on_all_finished()
        self.assertEqual(window.status_label.text(), "Completed with Issues")
        self.assertEqual(window.eta_label.text(), "3 videos converted · 1 failed")

    def test_cancelled_takes_priority_over_failed_in_the_heading(self):
        # A job failed, then the user cancelled before the rest finished --
        # "the user stopped it" is the more relevant fact to lead with.
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 1
        window._run_failed_count = 1
        window._run_cancelled = True
        window._on_all_finished()
        self.assertEqual(window.status_label.text(), "Conversion Stopped")

    def test_full_success_still_says_complete(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 4
        window._run_failed_count = 0
        window._run_cancelled = False
        window._on_all_finished()
        self.assertEqual(window.status_label.text(), "✓ Conversion Complete")
        self.assertEqual(window.eta_label.text(), "4 videos converted")


class TestVideoAudioTabAlignment(unittest.TestCase):
    """Reported live and confirmed by direct measurement: Video and Audio's
    tab pages are forced to the same overall height (QStackedWidget sizes
    every page to the tallest one), but only _build_audio_tab's outer
    layout had a trailing addStretch() to absorb the resulting surplus
    space. Video's surplus had nowhere to go but into its own items,
    inflating video_scope_label past its own sizeHint (confirmed: rendered
    22px vs a 17px sizeHint at a 1240x900 window) and pushing Encoding's
    top down out of alignment with Audio's card below its own,
    correctly-unstretched, scope label. This checks the actual geometry
    relationship -- first card top relative to its tab page -- not one
    magic Y coordinate, so it stays meaningful across DPI/theme/font
    changes."""

    def test_first_card_top_matches_between_video_and_audio_tabs(self):
        window = main.MainWindow()
        window.resize(1240, 900)
        window.show()
        for _ in range(5):
            QApplication.processEvents()

        video_label_bottom = window.video_scope_label.geometry().bottom()
        encoding_group = window.video_scope_label.parentWidget().layout().itemAt(1).widget()
        video_gap = encoding_group.geometry().top() - video_label_bottom

        tabs = window.findChild(QTabWidget)
        tabs.setCurrentIndex(1)
        for _ in range(5):
            QApplication.processEvents()

        audio_label_bottom = window.audio_scope_label.geometry().bottom()
        audio_group = window.audio_scope_label.parentWidget().layout().itemAt(1).widget()
        audio_gap = audio_group.geometry().top() - audio_label_bottom

        self.assertEqual(window.video_scope_label.height(), window.video_scope_label.sizeHint().height())
        self.assertEqual(window.audio_scope_label.height(), window.audio_scope_label.sizeHint().height())
        self.assertEqual(video_gap, audio_gap)


class TestHideIdleStatus(unittest.TestCase):
    """Reported live: "Idle" reads as internal state-machine language,
    not product language, and permanently occupying the status line gave
    real status (Preparing/Converting N of M/Conversion Complete/...)
    less visual weight than it should have -- especially since this
    whole run-status area already collapses to nothing else at rest
    (progress_bar/eta_label/stats_label, _apply_run_phase_visuals's
    "idle" branch). _set_status now hides status_label specifically for
    the bare word, not for any other status text."""

    def test_idle_status_is_hidden(self):
        # No longer machine-dependent -- __init__ used to also call
        # _maybe_note_no_hardware(), which overwrote this status on a
        # no-GPU box (e.g. CI) before that startup notice was removed
        # entirely (CPU-only Automatic is a silent, valid outcome now,
        # not something to greet a non-technical user with).
        window = main.MainWindow()
        window.show()
        self.assertEqual(window.status_label.text(), "Idle")
        self.assertFalse(window.status_label.isVisible())

    def test_a_real_status_is_shown(self):
        window = main.MainWindow()
        window.show()
        window._set_status("Queue is empty")
        self.assertTrue(window.status_label.isVisible())

    def test_idle_with_stop_after_current_video_checked_is_still_shown(self):
        # Genuinely informative even at rest -- a pending one-shot intent
        # the user just set -- so the general "Idle" hiding rule must not
        # apply here.
        window = main.MainWindow()
        window.show()
        window.pause_after_check.setChecked(True)
        window._set_status("Idle")
        self.assertTrue(window.status_label.isVisible())
        self.assertIn("will stop after this video", window.status_label.text())


class TestFinishedSummaryDismissal(unittest.TestCase):
    """The finished-run summary (open_folder_btn visible, progress/eta/
    stats repurposed) should disappear the moment the queue's content
    changes -- _refresh_idle_controls (queue_controller.py) is what every
    queue-mutation call site uses instead of touching _apply_run_phase_
    visuals directly, specifically so this stays correct without also
    stomping a live run's button state."""

    def _finished_window(self):
        window = main.MainWindow()
        window.show()
        window._run_completed_count = 1
        window._on_all_finished()
        self.assertTrue(window.open_folder_btn.isVisible())  # sanity check
        return window

    def test_adding_a_file_dismisses_the_finished_summary(self):
        window = self._finished_window()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        self.assertFalse(window.open_folder_btn.isVisible())

    def test_removing_a_row_dismisses_the_finished_summary(self):
        window = self._finished_window()
        item = _add_dummy_item(window, "a.mkv")
        item.setSelected(True)
        window._remove_selected()
        self.assertFalse(window.open_folder_btn.isVisible())

    def test_clear_queue_dismisses_the_finished_summary(self):
        window = self._finished_window()
        _add_dummy_item(window, "a.mkv")
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
        self.assertFalse(window.open_folder_btn.isVisible())

    def test_undo_dismisses_the_finished_summary(self):
        window = self._finished_window()
        _add_dummy_item(window, "a.mkv")
        window._push_undo_snapshot()
        item2 = _add_dummy_item(window, "b.mkv")
        item2.setSelected(True)
        window._remove_selected()
        window._undo()
        self.assertFalse(window.open_folder_btn.isVisible())

    def test_dismissing_the_summary_also_resets_status_label(self):
        # Real, confirmed bug: _apply_run_phase_visuals("idle") hides the
        # summary's own eta_label/stats_label and relabels start_btn, but
        # never touched status_label -- "✓ Conversion Complete" could keep
        # showing above a queue that had already moved on to a new file.
        window = self._finished_window()
        self.assertEqual(window.status_label.text(), "✓ Conversion Complete")
        _add_dummy_item(window, "a.mkv")
        window._refresh_idle_controls()
        self.assertEqual(window.status_label.text(), "Idle")

    def test_ordinary_idle_mutation_does_not_clobber_an_unrelated_status(self):
        # The other half of the same fix: unconditionally resetting
        # status_label to "Idle" on every _refresh_idle_controls() call
        # would have its own bug -- adding a file to a never-yet-run queue
        # also reaches this method, and status_label may legitimately be
        # holding some other status set independently of the queue's own
        # empty/non-empty state at that point (an arbitrary example here,
        # not tied to any one real message -- the no-hardware startup
        # notice this test used to use as its example was removed
        # entirely). Only a genuine finished-summary dismissal should
        # reset it.
        window = main.MainWindow()
        window.show()
        window._set_status("Some other status")
        _add_dummy_item(window, "a.mkv")
        window._refresh_idle_controls()
        self.assertEqual(window.status_label.text(), "Some other status")


class TestQueueDragReorder(unittest.TestCase):
    """Regression coverage for a real, user-reported bug: dragging a queue
    row and dropping it squarely on top of another row (not near its top/
    bottom edge) made the dragged file vanish from the list. Root cause:
    QTreeWidget's built-in InternalMove drop handling treats a drop "on" a
    row as "make this a child of that row" -- correct for a real tree, but
    this list is deliberately flat (setRootIsDecorated(False), items never
    expanded), so the reparented row silently stopped being drawn.
    DropTreeWidget._reorder_rows() replaces that with a manual top-level-
    only move: every drop, anywhere on a row, is a sibling reorder."""

    @staticmethod
    def _add_rows(window, names):
        # A row without job data crashes _on_queue_selection_changed the
        # moment it gets selected (as every row here does, via
        # setSelected() in _drop_at) -- give each a minimal real job dict,
        # same shape TestQueueLockingDuringRun's fake rows use.
        for name in names:
            item = main.QTreeWidgetItem([name])
            item.setData(main.STATUS_COL, Qt.UserRole,
                         {"path": Path(name), **window._current_settings()})
            window.queue_list.addTopLevelItem(item)

    @staticmethod
    def _names(window):
        return [window.queue_list.topLevelItem(i).text(0)
                for i in range(window.queue_list.topLevelItemCount())]

    @staticmethod
    def _drop_at(window, pos, selected_names):
        queue_list = window.queue_list
        for i in range(queue_list.topLevelItemCount()):
            item = queue_list.topLevelItem(i)
            item.setSelected(item.text(0) in selected_names)
        # QDropEvent only stores a raw pointer to the QMimeData it's given
        # -- it doesn't take ownership -- so the caller has to keep a real
        # Python reference alive for as long as the event is in use, or
        # PySide6 garbage-collects it out from under the event (segfault,
        # confirmed the hard way while writing this test).
        mime = QMimeData()
        event = QDropEvent(QPoint(pos), Qt.MoveAction, mime, Qt.NoButton, Qt.NoModifier)
        queue_list.dropEvent(event)
        return event

    def test_dropping_squarely_on_a_row_does_not_delete_it_reorders_instead(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c"])
        target_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(2))
        self._drop_at(window, target_rect.center(), ["a"])
        self.assertEqual(self._names(window), ["b", "c", "a"])

    def test_dropping_on_the_top_half_of_a_row_inserts_before_it(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c"])
        target_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(0))
        pos = QPoint(target_rect.center().x(), target_rect.top())
        self._drop_at(window, pos, ["c"])
        self.assertEqual(self._names(window), ["c", "a", "b"])

    def test_dropping_below_the_last_row_appends_at_the_end(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c"])
        last_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(2))
        pos = QPoint(last_rect.center().x(), last_rect.bottom() + 50)
        self._drop_at(window, pos, ["a"])
        self.assertEqual(self._names(window), ["b", "c", "a"])

    def test_multi_selected_rows_preserve_relative_order_when_reordered(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c", "d"])
        last_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(3))
        pos = QPoint(last_rect.center().x(), last_rect.bottom() + 50)
        self._drop_at(window, pos, ["a", "c"])
        self.assertEqual(self._names(window), ["b", "d", "a", "c"])

    def test_dropping_a_selected_row_onto_another_selected_row_keeps_every_row(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c"])
        target_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(2))
        self._drop_at(window, target_rect.center(), ["a", "c"])
        self.assertEqual(set(self._names(window)), {"a", "b", "c"})
        self.assertEqual(window.queue_list.topLevelItemCount(), 3)

    def test_reorder_locked_blocks_the_drop_entirely(self):
        window = main.MainWindow()
        window.show()
        self._add_rows(window, ["a", "b", "c"])
        window.queue_list.reorder_locked = True
        target_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(2))
        event = self._drop_at(window, target_rect.center(), ["a"])
        self.assertEqual(self._names(window), ["a", "b", "c"])
        self.assertFalse(event.isAccepted())


class TestQueueDragHoverState(unittest.TestCase):
    """DropTreeWidget's dragActive dynamic property (queue_widget.py's
    _set_drag_active) drives the QSS accent-border rule and the empty-state
    placeholder-text swap -- both purely visual, verified separately by
    screenshot. What's unit-testable here is the state machine itself: does
    the property actually flip at the right moments. Same "QMimeData/event
    outlives the call, keep a real Python reference" rule as
    TestQueueDragReorder's _drop_at above applies to every event
    constructed below."""

    @staticmethod
    def _local_file_mime():
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/some_video.mkv")])
        return mime

    def test_valid_file_drag_sets_dragActive_true_on_enter(self):
        window = main.MainWindow()
        window.show()
        mime = self._local_file_mime()
        event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(event)
        self.assertTrue(window.queue_list.property("dragActive"))

    def test_dragActive_stays_true_through_move(self):
        window = main.MainWindow()
        window.show()
        mime = self._local_file_mime()
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(enter)
        move = QDragMoveEvent(QPoint(20, 20), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragMoveEvent(move)
        self.assertTrue(window.queue_list.property("dragActive"))

    def test_dragActive_clears_on_leave(self):
        window = main.MainWindow()
        window.show()
        mime = self._local_file_mime()
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(enter)
        self.assertTrue(window.queue_list.property("dragActive"))
        window.queue_list.dragLeaveEvent(QDragLeaveEvent())
        self.assertFalse(window.queue_list.property("dragActive"))

    def test_dragActive_clears_on_drop(self):
        window = main.MainWindow()
        window.show()
        mime = self._local_file_mime()
        enter = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(enter)
        self.assertTrue(window.queue_list.property("dragActive"))
        drop = QDropEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dropEvent(drop)
        self.assertFalse(window.queue_list.property("dragActive"))

    def test_non_file_mime_never_sets_dragActive(self):
        # hasUrls() alone isn't the bar -- dropEvent only ever actually acts
        # on isLocalFile() URLs (Path(u.toLocalFile()) for ... if u.isLocalFile()),
        # so the accent border shouldn't promise more than a real drop here
        # would deliver. A URL with no local-file mapping (e.g. a web link)
        # exercises exactly that gap.
        window = main.MainWindow()
        window.show()
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/video.mkv")])
        event = QDragEnterEvent(QPoint(10, 10), Qt.CopyAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(event)
        self.assertFalse(window.queue_list.property("dragActive"))

    def test_empty_mime_never_sets_dragActive(self):
        # No hasUrls() at all -- an internal row-reorder drag, the only
        # other real drag this widget ever sees.
        window = main.MainWindow()
        window.show()
        mime = QMimeData()
        event = QDragEnterEvent(QPoint(10, 10), Qt.MoveAction, mime, Qt.NoButton, Qt.NoModifier)
        window.queue_list.dragEnterEvent(event)
        self.assertFalse(window.queue_list.property("dragActive"))


class TestQueueUndoRedo(unittest.TestCase):
    """Undo/redo covers queue *structure* only -- add files, remove
    selected, clear queue, drag-reorder -- not per-item settings edits or
    preset save/delete. Snapshot-based (each stack entry is the full
    queue's job-dict list at that point, see _queue_snapshot in
    queue_controller.py), not a diff/command stack -- simplest robust
    option given rows carry live UI state (icons, probed column text)
    that's easier to re-derive on restore than replay precisely."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_gui_test_"))
        cls.clip = cls.tmpdir / "clip.mkv"
        _make_clip(cls.clip, "aac")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_undo_reverses_add_files(self):
        window = main.MainWindow()
        window.add_files([self.clip])
        _wait_for_detection(window)
        self.assertEqual(window.queue_list.topLevelItemCount(), 1)
        window._undo()
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)

    def test_redo_reapplies_an_undone_add(self):
        window = main.MainWindow()
        window.add_files([self.clip])
        _wait_for_detection(window)
        window._undo()
        window._redo()
        _wait_for_detection(window)
        self.assertEqual(window.queue_list.topLevelItemCount(), 1)

    def test_undo_reverses_remove_selected(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b", "c"])
        window.queue_list.topLevelItem(1).setSelected(True)
        window._remove_selected()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "c"])
        window._undo()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b", "c"])

    def test_undo_reverses_clear_queue(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b", "c"])
        with patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)
        window._undo()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b", "c"])

    def test_undo_reverses_a_drag_reorder(self):
        window = main.MainWindow()
        window.show()
        TestQueueDragReorder._add_rows(window, ["a", "b", "c"])
        target_rect = window.queue_list.visualItemRect(window.queue_list.topLevelItem(2))
        TestQueueDragReorder._drop_at(window, target_rect.center(), ["a"])
        self.assertEqual(TestQueueDragReorder._names(window), ["b", "c", "a"])
        window._undo()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b", "c"])

    def test_a_new_action_clears_the_redo_stack(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b", "c"])
        window.queue_list.topLevelItem(0).setSelected(True)
        window._remove_selected()
        window._undo()
        self.assertTrue(window._redo_stack, "test assumes there's something to redo")
        window.queue_list.topLevelItem(1).setSelected(True)
        window._remove_selected()  # a new action -- must invalidate the old redo history
        self.assertEqual(window._redo_stack, [])

    def test_undo_and_redo_are_refused_during_a_run(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window.queue_list.topLevelItem(0).setSelected(True)
        window._remove_selected()
        window._set_queue_editable(False)  # simulates a run in progress
        window._undo()
        self.assertEqual(TestQueueDragReorder._names(window), ["b"], "undo must be a no-op mid-run")
        window._set_queue_editable(True)
        window._undo()
        window._set_queue_editable(False)
        window._redo()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b"], "redo must be a no-op mid-run")

    def test_undo_with_nothing_to_undo_does_nothing(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a"])
        window._undo()  # add_rows bypasses add_files, so nothing was ever pushed
        self.assertEqual(TestQueueDragReorder._names(window), ["a"])


class TestDeleteKeyRemovesSelectedQueueItem(unittest.TestCase):
    def test_delete_key_removes_the_selected_row_when_queue_list_has_focus(self):
        window = main.MainWindow()
        window.show()
        window.activateWindow()
        QApplication.instance().processEvents()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window.queue_list.topLevelItem(0).setSelected(True)
        window.queue_list.setFocus()
        QApplication.instance().processEvents()
        QTest.keyClick(window.queue_list, Qt.Key_Delete)
        self.assertEqual(TestQueueDragReorder._names(window), ["b"])

    def test_delete_key_does_not_fire_when_output_edit_has_focus(self):
        # Scoped to queue_list specifically (Qt.WidgetWithChildrenShortcut,
        # not the window-wide default Ctrl+Z/Ctrl+Shift+Z use) -- otherwise
        # Delete/Backspace would misfire as "remove queue item" while
        # actually editing the output-folder text field.
        window = main.MainWindow()
        window.show()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window.queue_list.topLevelItem(0).setSelected(True)
        window.output_edit.setFocus()
        QTest.keyClick(window.output_edit, Qt.Key_Delete)
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b"])


class TestQueueStructureLockedDuringRun(unittest.TestCase):
    """Remove/Clear are normally only reachable via buttons that
    _set_queue_editable(False) disables during a run. The Delete-key
    shortcut reaches _remove_selected directly, bypassing that button
    state -- both methods need their own internal guard so a run's
    _running_items index-based lookup can't be corrupted out from under
    it, regardless of how the call is triggered."""

    def test_delete_key_is_a_no_op_mid_run(self):
        window = main.MainWindow()
        window.show()
        window.activateWindow()
        QApplication.instance().processEvents()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window._set_queue_editable(False)  # simulates a run in progress
        window.queue_list.topLevelItem(0).setSelected(True)
        window.queue_list.setFocus()
        QApplication.instance().processEvents()
        QTest.keyClick(window.queue_list, Qt.Key_Delete)
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b"])

    def test_remove_selected_is_a_no_op_mid_run(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window._set_queue_editable(False)
        window.queue_list.topLevelItem(0).setSelected(True)
        window._remove_selected()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b"])

    def test_clear_queue_is_a_no_op_mid_run(self):
        window = main.MainWindow()
        TestQueueDragReorder._add_rows(window, ["a", "b"])
        window._set_queue_editable(False)
        # The mid-run guard must return before the confirmation dialog is
        # even reached -- patched (and asserted uncalled) rather than
        # trusted, so a regression that removes the guard fails loudly
        # instead of hanging the suite on a real modal with no display to
        # dismiss it.
        with patch.object(main.QMessageBox, "question") as mock_question:
            window._clear_queue()
        mock_question.assert_not_called()
        self.assertEqual(TestQueueDragReorder._names(window), ["a", "b"])


class TestLiveAppendDuringRun(unittest.TestCase):
    """A file added (button or drag-drop -- both go through add_files())
    while a run is already in progress should join that run automatically,
    not just sit in the visible list until Start is clicked again."""

    def test_adding_a_file_mid_run_pushes_it_into_the_running_queue(self):
        window = main.MainWindow()
        window._set_queue_editable(False)  # simulates a run in progress
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            self.assertEqual(len(window.queue._jobs), 0)
            window.add_files([clip])
            _wait_for_detection(window)
        self.assertEqual(len(window.queue._jobs), 1)
        self.assertEqual(window.queue._jobs[0]["path"], clip)
        self.assertEqual(len(window._running_items), 1)

    def test_adding_a_file_mid_run_does_not_corrupt_converting_button_text(self):
        # Regression guard for the same class of gap as TestAutoDetectInter
        # laceOnAdd's mid-preparation version above: add_files() calling
        # _update_start_button_label() unconditionally would silently flip
        # start_btn's disabled "Converting…" text back to a recomputed
        # "Convert N Videos" the instant a file is added mid-run.
        window = main.MainWindow()
        window.show()
        window._apply_run_phase_visuals("converting")
        window._set_queue_editable(False)  # simulates a run in progress
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        self.assertEqual(window.start_btn.text(), "Converting…")
        self.assertTrue(window.stop_btn.isVisible())
        self.assertEqual(window.stop_btn.text(), "Cancel")

    def test_not_added_to_the_engine_when_no_run_is_in_progress(self):
        window = main.MainWindow()
        self.assertTrue(window._queue_editable)  # idle, no run
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
        self.assertEqual(len(window.queue._jobs), 0)
        self.assertEqual(len(window._running_items), 0)

    def test_a_mid_run_add_is_submitted_with_its_real_detected_deinterlace_value(self):
        # add_job() is deferred until both interlace detection and source
        # probe have actually landed for a mid-run add (see
        # _maybe_submit_mid_run_job in queue_controller.py) specifically so
        # this can't race: the job handed to the engine already carries the
        # real detected value, never a since-corrected snapshot.
        window = main.MainWindow()
        window._set_queue_editable(False)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "interlaced.mkv"
            _make_interlaced_clip(clip)
            window.add_files([clip])
            _wait_for_detection(window)
        self.assertEqual(window.queue._jobs[0]["deinterlace"], True)

    def test_mid_run_add_is_not_submitted_to_the_engine_until_both_probes_land(self):
        # The actual race this fixes: previously add_job() ran immediately
        # inside add_files(), so if the currently-running job finished
        # before this file's own ~20s interlace sample did,
        # update_pending_job (worker.py) couldn't help -- its own docstring
        # says it only patches jobs still ahead of the queue's position.
        # Verified directly here rather than just trusting the eventual
        # correct outcome (the test above): immediately after add_files()
        # returns, before either probe has landed, the job must not be in
        # the engine's queue yet at all.
        window = main.MainWindow()
        window._set_queue_editable(False)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            self.assertEqual(
                len(window.queue._jobs), 0,
                "must not be submitted before both probes complete",
            )
            item = window.queue_list.topLevelItem(0)
            self.assertIn(item, window._pending_mid_run_items)
            _wait_for_detection(window)
        self.assertEqual(len(window.queue._jobs), 1)
        self.assertNotIn(item, window._pending_mid_run_items)


class TestLeftPanelFixedWidth(unittest.TestCase):
    # Regression guard for the QSplitter -> QHBoxLayout change
    # (ui_builder.py's _build_ui): the left settings panel is a deliberate
    # design rule now (fixed 456px, right panel absorbs all resizing), not
    # incidental sizing -- this exists so a future edit reintroducing
    # splitter-style drag/resize behavior fails a test instead of silently
    # regressing. 456, not the original 470 -- a density pass tried 410
    # directly and confirmed real clipping by screenshot (the settings
    # content's own sizeHint().width() is 450px regardless of hardware,
    # driven by the Quality-tier row's button labels, not by anything
    # padding/spacing could shrink); 456 leaves a small margin above that
    # measured floor. See ui_builder.py's own comment on this same
    # setFixedWidth call for the full measurement.
    def test_left_panel_is_fixed_at_456(self):
        window = main.MainWindow()
        left = window.findChild(QWidget, "leftPanel")
        self.assertIsNotNone(left)
        self.assertEqual(left.minimumWidth(), 456)
        self.assertEqual(left.maximumWidth(), 456)

    def test_widening_the_window_does_not_change_left_panel_width(self):
        window = main.MainWindow()
        window.show()
        left = window.findChild(QWidget, "leftPanel")
        window.resize(1800, 820)
        self.assertEqual(left.width(), 456)


class TestLeftPanelScrolling(unittest.TestCase):
    """The richer Video tab (Encoding/Quality/Format plus a real,
    reachable Expert section again) can genuinely exceed the window's
    default 820px height once Expert is expanded -- #leftPanel (still
    fixed at 410px wide, see TestLeftPanelFixedWidth above) is a
    QScrollArea now, not a bare QWidget.

    Expanding Expert now grows the window to fit instead, when there's a
    shortfall to fit (see TestExpertExpandGrowsWindow below) -- reported
    live, an explicit revision of this class's own original design
    intent (a comment right above used to say the opposite: "instead of
    ... forcing the window taller"). Scrolling here is now the fallback
    for whatever growing can't cover: the window's minimum height is
    pinned to Expert's *collapsed* height specifically so a user can
    still shrink it back down after expanding (accepting scrolling if
    they do), not because growing was abandoned as the primary
    behavior.

    Pins Intel present (setUp/tearDown below): Processing's own row
    (ui_builder.py) hides itself entirely on a genuinely hardware-less
    machine, which frees up enough of #leftPanel's own vertical space
    that Expert's expanded content can end up fitting within the
    unchanged 820px default after all -- confirmed as the actual cause
    of a real CI-only failure (this dev box always has real hardware, so
    Processing's row is never hidden here, silently masking it locally).
    These tests are about the scroll/grow *mechanism* specifically, not
    about whether Processing happens to be visible, so pinning hardware
    present keeps their vertical-space math the same as before Phase 1's
    hardware-detection work touched it at all, regardless of whatever
    font metrics or margins the machine running them happens to have."""

    def setUp(self):
        patcher = patch.object(worker, "find_render_node", side_effect=_find_render_node_for(worker.INTEL_VENDOR_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_left_panel_is_a_scroll_area(self):
        window = main.MainWindow()
        left = window.findChild(QWidget, "leftPanel")
        self.assertIsInstance(left, QScrollArea)

    def test_no_scrolling_needed_at_default_size_with_expert_collapsed(self):
        window = main.MainWindow()
        window.show()
        left = window.findChild(QWidget, "leftPanel")
        self.assertEqual(left.verticalScrollBar().maximum(), 0)

    def test_shrinking_below_the_collapsed_floor_is_refused(self):
        # _empty_qsettings -- needs Expert to genuinely be collapsed so
        # the floor read below is the real collapsed-height one, not
        # whatever this machine's real config last persisted for
        # video_expert_expanded. Asserts against left.minimumHeight()
        # itself, not the window's starting height (1186x720, main.py's
        # own hardcoded default) -- that default is comfortably taller
        # than the actual floor, so it's the wrong thing to compare a
        # shrink-below-the-floor attempt against.
        with _empty_qsettings():
            window = main.MainWindow()
            window.show()
            _app.processEvents()
            left = window.findChild(QWidget, "leftPanel")
            floor = left.minimumHeight()
        window.resize(window.width(), 50)
        _app.processEvents()
        self.assertEqual(window.height(), floor)

    def test_shrinking_after_expanding_falls_back_to_scrolling(self):
        # The window grew to fit when Expert expanded (TestExpertExpand
        # GrowsWindow below) -- forcing it back down to the (unchanged)
        # collapsed-height floor, still with Expert expanded, is exactly
        # the case this class's own docstring says scrolling still
        # exists for.
        window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        left = window.findChild(QWidget, "leftPanel")
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        collapsed_floor = left.minimumHeight()
        window.resize(window.width(), collapsed_floor)
        _app.processEvents()
        self.assertGreater(left.verticalScrollBar().maximum(), 0)

    def test_horizontal_scrollbar_is_never_shown(self):
        # Content is sized for exactly this fixed 410px width by design
        # -- only vertical overflow (Expert expanded) is a real concern.
        window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        left = window.findChild(QWidget, "leftPanel")
        window.video_expert_group.setChecked(True)
        self.assertEqual(left.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)


class TestExpertExpandGrowsWindow(unittest.TestCase):
    """Reported live, explicitly requested with the tradeoff spelled out:
    expanding Expert should grow the window to fit rather than silently
    starting to scroll -- the user should see what they just expanded
    without an extra resize or noticing a scrollbar appeared. The window
    can still be shrunk back down afterward, just never below Expert's
    *collapsed* height (TestLeftPanelScrolling above covers that floor
    and its scrolling fallback).

    Pins Intel present (setUp/tearDown) for the same reason
    TestLeftPanelScrolling does now -- Processing's own row hiding
    itself on a hardware-less machine frees up enough vertical space
    that these height comparisons can silently stop holding, confirmed
    as a real CI-only failure this dev box's own real hardware always
    masked locally."""

    def setUp(self):
        patcher = patch.object(worker, "find_render_node", side_effect=_find_render_node_for(worker.INTEL_VENDOR_ID))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_expanding_grows_the_window_instead_of_requiring_scrolling(self):
        # CPU processing, not whatever Automatic resolved to -- Expert's
        # content is taller on a software encoder (Tune's own row is
        # only shown there, see _on_encoder_changed), so this needs a
        # deterministic engine rather than depending on this machine's
        # own hardware. _empty_qsettings -- needs Expert to genuinely
        # start collapsed so setChecked(True) below is a real transition;
        # a bare MainWindow() would otherwise restore whatever this
        # machine's real config last persisted for video_expert_expanded,
        # which could make this a silent no-op instead of a real toggle.
        with _empty_qsettings():
            window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        _app.processEvents()
        left = window.findChild(QWidget, "leftPanel")
        # Real, confirmed regression from a density pass: this used to read
        # collapsed_height straight off the untouched default-size window,
        # relying on main.py's own default always being shorter than
        # Expert's expanded content. That relationship isn't guaranteed --
        # it silently flipped on CI once the layout got tight enough that
        # Expert's expanded content fit inside the (still Intel-pinned)
        # default window without growing at all ("760 not greater than
        # 760"), even though this dev box's own font metrics still needed
        # the grow. Pinning the window to left.minimumHeight() (the
        # collapsed floor) first makes growth necessary by construction --
        # expanded content is strictly taller than collapsed content,
        # regardless of what any environment's font metrics happen to be.
        window.resize(window.width(), left.minimumHeight())
        _app.processEvents()
        collapsed_height = window.height()
        window.video_expert_group.setChecked(True)
        # The grow itself is deferred a full event-loop turn (QTimer.
        # singleShot(0, ...) in _on_expert_toggled) -- sizeHint() read
        # synchronously inside that handler still reflects the pre-toggle
        # (collapsed) layout every time, confirmed directly. A single
        # processEvents() call does carry a 0ms singleShot through to
        # completion here.
        _app.processEvents()
        self.assertGreater(window.height(), collapsed_height)
        self.assertEqual(left.verticalScrollBar().maximum(), 0)

    def test_collapsing_again_shrinks_the_window_back_down(self):
        # Reported live: collapsing without also shrinking left a
        # visible gap of empty space below the now-short content, the
        # window having grown to fit Expert's expanded content and
        # simply stayed that size. Restores the exact *pre-expand* height,
        # whatever it was, rather than the bare collapsed floor -- those
        # are only different numbers when the window didn't start out
        # already at the floor. Pinned to left.minimumHeight() before
        # expanding (same reasoning as test_expanding_grows_the_window_
        # instead_of_requiring_scrolling above) rather than reading
        # pre_expand_height off the untouched default window -- a tight
        # enough layout can make Expert's expanded content fit inside the
        # default height with no growth at all, which would make the
        # "confirm it actually grew first" assertion below false on some
        # environments' font metrics even though this class is about the
        # collapse behavior, not about whether a grow happened to be
        # needed at the *default* size specifically. _empty_qsettings --
        # needs Expert to genuinely start collapsed so setChecked(True)
        # below is a real transition that actually grows the window.
        with _empty_qsettings():
            window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        _app.processEvents()
        left = window.findChild(QWidget, "leftPanel")
        window.resize(window.width(), left.minimumHeight())
        _app.processEvents()
        pre_expand_height = window.height()
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        self.assertGreater(window.height(), pre_expand_height)  # confirm it actually grew first
        window.video_expert_group.setChecked(False)
        _app.processEvents()
        self.assertEqual(window.height(), pre_expand_height)

    def test_re_expanding_after_manually_shrinking_grows_again(self):
        window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        _app.processEvents()
        left = window.findChild(QWidget, "leftPanel")
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        window.resize(window.width(), left.minimumHeight())
        _app.processEvents()
        window.video_expert_group.setChecked(False)
        _app.processEvents()
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        self.assertEqual(left.verticalScrollBar().maximum(), 0)

    def test_restoring_a_persisted_expanded_state_grows_the_window_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                main.QSettings, "value",
                side_effect=lambda key, default=None: (
                    "true" if key == "video_expert_expanded" else default
                ),
            ):
                window = main.MainWindow()
                window.processing_cpu_btn.click()
                window.show()
                _app.processEvents()
                left = window.findChild(QWidget, "leftPanel")
                self.assertTrue(window.video_expert_group.isChecked())
                self.assertEqual(left.verticalScrollBar().maximum(), 0)

    def test_a_window_already_tall_enough_is_untouched_by_expanding_or_collapsing(self):
        # Real, confirmed bug in an earlier version of this same fix:
        # collapsing always shrank to the bare floor regardless of *why*
        # the window was its current height -- fine right after this
        # class's own auto-grow, wrong if the user had simply made the
        # window tall themselves beforehand (nothing to undo, so nothing
        # should move).
        with _empty_qsettings():
            window = main.MainWindow()
        window.show()
        window.resize(window.width(), 1000)
        _app.processEvents()
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        self.assertEqual(window.height(), 1000)  # already fit -- no grow needed
        window.video_expert_group.setChecked(False)
        _app.processEvents()
        self.assertEqual(window.height(), 1000)  # nothing to undo -- must stay put

    def test_manually_resizing_while_expanded_replaces_the_auto_grown_height(self):
        # The window auto-grew once, but the user then chose a size of
        # their own while Expert was still open -- collapsing must
        # respect *that* choice, not silently snap back to whatever this
        # class had picked before the user's own resize overrode it.
        with _empty_qsettings():
            window = main.MainWindow()
        window.show()
        window.processing_cpu_btn.click()
        _app.processEvents()
        window.video_expert_group.setChecked(True)
        _app.processEvents()
        self.assertNotEqual(window.height(), 1050)  # sanity: not already there by coincidence
        window.resize(window.width(), 1050)
        _app.processEvents()
        window.video_expert_group.setChecked(False)
        _app.processEvents()
        self.assertEqual(window.height(), 1050)

    def test_restored_tall_geometry_survives_toggling_expert_via_the_restore_path(self):
        # Same invariant as the manual-resize case above, but exercised
        # through _restore_window_state's own restoreGeometry + setChecked
        # call sequence specifically (a real returning-user startup),
        # not a live click -- a previous session's tall window (whether
        # the user resized it or this class had auto-grown it) must
        # survive Expert's restored state being toggled the same way.
        # restoreGeometry() itself is mocked rather than fed a real
        # QByteArray blob -- not what this test is about, and genuine
        # saveGeometry()/restoreGeometry() round-tripping is Qt's own
        # concern, not a real bug surface here.
        fake_settings = {"video_expert_expanded": "true", "window_geometry": b"sentinel"}
        with patch.object(
            main.QSettings, "value",
            side_effect=lambda key, default=None: fake_settings.get(key, default),
        ):
            with patch.object(main.MainWindow, "restoreGeometry", lambda self, geo: self.resize(self.width(), 950)):
                window = main.MainWindow()
        window.show()
        _app.processEvents()
        self.assertTrue(window.video_expert_group.isChecked())
        self.assertEqual(window.height(), 950)
        window.video_expert_group.setChecked(False)
        _app.processEvents()
        self.assertEqual(window.height(), 950)


class TestDynamicProcessingButtons(unittest.TestCase):
    """Processing (ui_builder.py's processing_seg_row) is built from
    worker.detect_available_backends() now instead of three hardcoded
    buttons -- a vendor with no button doesn't just get disabled, it
    never exists as a widget at all, matching the actual product goal
    (don't ask a non-technical user to choose between options that can't
    work). Mocked rather than gated on this machine's real hardware, so
    every combination is actually exercised regardless of what happens to
    be installed wherever this runs."""

    def test_no_gpu_hides_the_whole_processing_row(self):
        # Automatic and a lone CPU segment would always mean the exact
        # same outcome on a machine with no GPU at all -- asking
        # "Processing?" is a decision with only one possible answer, so
        # the row (Automatic included, not just the segmented part)
        # doesn't get shown rather than offering a choice with nothing
        # to actually choose between.
        with patch.object(worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
        window.show()
        self.assertFalse(window.processing_auto_btn.isVisible())
        self.assertFalse(window.processing_cpu_btn.isVisible())
        self.assertIsNone(window.processing_intel_btn)
        self.assertIsNone(window.processing_amd_btn)
        self.assertEqual(len(window.processing_button_group.buttons()), 1)

    def test_intel_only_gets_a_two_segment_row(self):
        with patch.object(main.worker, "find_render_node", side_effect=_find_render_node_for(main.worker.INTEL_VENDOR_ID)):
            window = main.MainWindow()
        self.assertEqual(window.processing_cpu_btn.objectName(), "segLeft")
        self.assertEqual(window.processing_intel_btn.objectName(), "segRight")
        self.assertIsNone(window.processing_amd_btn)
        self.assertEqual(len(window.processing_button_group.buttons()), 2)

    def test_amd_only_gets_a_two_segment_row(self):
        with patch.object(main.worker, "find_render_node", side_effect=_find_render_node_for(main.worker.AMD_VENDOR_ID)):
            window = main.MainWindow()
        self.assertEqual(window.processing_cpu_btn.objectName(), "segLeft")
        self.assertIsNone(window.processing_intel_btn)
        self.assertEqual(window.processing_amd_btn.objectName(), "segRight")
        self.assertEqual(len(window.processing_button_group.buttons()), 2)

    def test_both_vendors_present_gets_the_original_three_segment_row(self):
        with patch.object(
            main.worker, "find_render_node",
            side_effect=_find_render_node_for(main.worker.INTEL_VENDOR_ID, main.worker.AMD_VENDOR_ID),
        ):
            window = main.MainWindow()
        self.assertEqual(window.processing_cpu_btn.objectName(), "segLeft")
        self.assertEqual(window.processing_intel_btn.objectName(), "segMid")
        self.assertEqual(window.processing_amd_btn.objectName(), "segRight")
        self.assertEqual(len(window.processing_button_group.buttons()), 3)

    @staticmethod
    def _intel_encoder_combo_index():
        return next(
            i for i, (encoder, vendor, _label) in enumerate(main.ENCODERS)
            if encoder == "hevc_vaapi" and vendor == "intel"
        )

    def test_encoder_combo_itself_is_not_self_corrected(self):
        # Regression guard: an earlier version of this branch self-
        # corrected encoder_combo's own selection the moment it landed on
        # an unavailable vendor, which broke a wide swath of pre-existing
        # hardware-agnostic UI-cascade tests the instant they ran on a
        # genuinely hardware-less machine (confirmed directly on CI,
        # unmasked by this dev box's own real Intel+AMD hardware hiding it
        # in every prior local run). encoder_combo must stay free-floating
        # UI state -- correction happens only where a job settings dict
        # actually gets captured for real use (add_files, below).
        with patch.object(main.worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
            window.encoder_combo.setCurrentIndex(self._intel_encoder_combo_index())
            self.assertEqual(window._current_encoder_id(), "hevc_vaapi")
            self.assertEqual(window._current_gpu_vendor(), "intel")

    def test_apply_settings_to_controls_does_not_correct_an_unavailable_vendor(self):
        # Same regression guard as above, for the settings -> controls
        # direction -- a great many existing tests apply a hardware-
        # specific settings dict directly and expect it to stick.
        with patch.object(main.worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
            settings = {**main.DEFAULT_SETTINGS, "encoder": "hevc_vaapi", "gpu_vendor": "intel", "speed": "4"}
            window._apply_settings_to_controls(settings)
            self.assertEqual(window._current_encoder_id(), "hevc_vaapi")
            self.assertEqual(window._current_gpu_vendor(), "intel")

    def test_add_files_resolves_an_unavailable_vendor_through_automatic(self):
        # The realistic way a queue item's settings end up naming an
        # unavailable vendor at all: Expert's combo was left on "Intel"
        # (previous two tests) when a video got added -- there's no
        # preset/save-file feature to bring stale settings in from
        # elsewhere (presets.py's own docstring). add_files is one of the
        # real boundaries where that gets corrected (via _effective_
        # current_settings), since it's a place a settings dict becomes a
        # real job that could reach build_args -- editing an already-
        # queued item's settings is the other one, covered separately
        # below (test_editing_a_queued_items_settings_...).
        with patch.object(main.worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
            window.encoder_combo.setCurrentIndex(self._intel_encoder_combo_index())
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()  # add_files only needs path.is_file() -- the
                # correction happens synchronously at job creation, before
                # any async probe/detection subprocess would need a real,
                # decodable video.
                window.add_files([clip])
                job = window.queue_list.topLevelItem(0).data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["encoder"], "libx265")
        self.assertIsNone(job["gpu_vendor"])

    def test_add_files_prefers_the_real_gpu_thats_present(self):
        with patch.object(main.worker, "find_render_node", side_effect=_find_render_node_for(main.worker.AMD_VENDOR_ID)):
            window = main.MainWindow()
            window.encoder_combo.setCurrentIndex(self._intel_encoder_combo_index())
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()
                window.add_files([clip])
                job = window.queue_list.topLevelItem(0).data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["encoder"], "hevc_vaapi")
        self.assertEqual(job["gpu_vendor"], "amd")

    def test_editing_a_queued_items_settings_also_sanitizes_an_unavailable_vendor(self):
        # add_files alone isn't the only place a settings dict becomes a
        # real job -- selecting an already-queued item and then picking
        # an unavailable vendor from Expert's own combo pushes that
        # straight into the item's stored settings via _sync_settings_to_
        # selected_queue_items, a second real path build_args could later
        # fail a job on if it weren't also sanitized. speed must land on
        # "medium" here too, not stay at whatever compression_level
        # string the (rejected) VAAPI pick would have used -- the same
        # family-crossing translation _on_processing_choice already does,
        # now shared via _settings_for_engine_vendor.
        with patch.object(main.worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()
                window.add_files([clip])
                item = window.queue_list.topLevelItem(0)
                item.setSelected(True)
                window.encoder_combo.setCurrentIndex(self._intel_encoder_combo_index())
                job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["encoder"], "libx265")
        self.assertIsNone(job["gpu_vendor"])
        self.assertEqual(job["speed"], "medium")

    def test_editing_a_queued_items_settings_prefers_the_real_gpu_thats_present(self):
        with patch.object(main.worker, "find_render_node", side_effect=_find_render_node_for(main.worker.AMD_VENDOR_ID)):
            window = main.MainWindow()
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()
                window.add_files([clip])
                item = window.queue_list.topLevelItem(0)
                item.setSelected(True)
                window.encoder_combo.setCurrentIndex(self._intel_encoder_combo_index())
                job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["encoder"], "hevc_vaapi")
        self.assertEqual(job["gpu_vendor"], "amd")


class TestSettingsForEngineVendorRcModeTranslation(unittest.TestCase):
    """_settings_for_engine_vendor's rc_mode handling used to only
    translate the Quality-tier case (ICQ/CQP/CRF), leaving the comment
    "File Size... left untouched above" -- true for quality_value (a
    target MB means the same thing on any encoder) but not for rc_mode
    itself, which is a different *name* per family ("VBR" for VAAPI,
    "bitrate" for software). A settings dict corrected from Intel/VBR to
    CPU used to keep rc_mode="VBR" verbatim -- worker.build_args' own
    software-path if/elif only recognizes "CRF"/"bitrate", so neither
    -crf nor -b:v got emitted, silently falling back to libx265's own
    default instead of the requested target size. Confirmed directly
    (see test_reproduces_the_pre_fix_silent_rate_control_drop below)
    before writing the fix these tests otherwise cover."""

    def setUp(self):
        self.window = main.MainWindow()

    def _settings(self, encoder, vendor, rc_mode, quality_value, speed):
        return {**main.DEFAULT_SETTINGS, "encoder": encoder, "gpu_vendor": vendor,
                "rc_mode": rc_mode, "quality_value": quality_value, "speed": speed}

    def test_file_size_mode_name_translates_intel_to_cpu(self):
        settings = self._settings("hevc_vaapi", "intel", "VBR", 500, "4")
        result = self.window._settings_for_engine_vendor(settings, "libx265", None)
        self.assertEqual(result["rc_mode"], "bitrate")
        self.assertEqual(result["quality_value"], 500)  # the MB target itself needs no translation

    def test_file_size_mode_name_translates_cpu_to_intel(self):
        settings = self._settings("libx265", None, "bitrate", 500, "medium")
        result = self.window._settings_for_engine_vendor(settings, "hevc_vaapi", "intel")
        self.assertEqual(result["rc_mode"], "VBR")
        self.assertEqual(result["quality_value"], 500)

    def test_file_size_mode_name_is_unchanged_switching_between_two_vaapi_vendors(self):
        # Intel and AMD both call it "VBR" -- no rename needed, just the
        # vendor itself changes.
        settings = self._settings("hevc_vaapi", "intel", "VBR", 500, "4")
        result = self.window._settings_for_engine_vendor(settings, "hevc_vaapi", "amd")
        self.assertEqual(result["rc_mode"], "VBR")
        self.assertEqual(result["quality_value"], 500)

    def test_expert_only_mode_with_no_equivalent_falls_back_to_quality_balanced(self):
        # CQP (Intel's Advanced-only mode) has no libx265 equivalent at
        # all -- build_args' software if/elif doesn't recognize "CQP"
        # either, so carrying it forward would be exactly the same class
        # of silent-rate-control-drop bug as the File Size case.
        settings = self._settings("hevc_vaapi", "intel", "CQP", 30, "4")
        result = self.window._settings_for_engine_vendor(settings, "libx265", None)
        self.assertEqual(result["rc_mode"], "CRF")
        self.assertEqual(result["quality_value"], main.QUALITY_TIERS["libx265"]["balanced"])

    def test_expert_only_mode_is_preserved_when_the_new_backend_genuinely_supports_it(self):
        # CQP is Intel's *and* AMD's Advanced mode (constants.RC_MODES) --
        # switching between them shouldn't reinterpret it as a quality
        # tier it was never meant to be.
        settings = self._settings("hevc_vaapi", "intel", "CQP", 30, "4")
        result = self.window._settings_for_engine_vendor(settings, "hevc_vaapi", "amd")
        self.assertEqual(result["rc_mode"], "CQP")
        self.assertEqual(result["quality_value"], 30)

    def test_reproduces_the_pre_fix_silent_rate_control_drop(self):
        # Direct proof of the actual symptom, not just the settings dict:
        # the exact pre-fix bug (rc_mode left as "VBR" on a libx265 job)
        # fed straight to build_args emits neither -crf nor -b:v at all.
        buggy_settings = self._settings("libx265", None, "VBR", 500, "medium")
        args = worker.build_args(
            buggy_settings, Path("input.mkv"), Path("output.mp4"),
            probe_audio=False, audio_codec=None, duration_seconds=600.0,
        )
        self.assertNotIn("-crf", args)
        self.assertNotIn("-b:v", args)  # the actual bug: silently drops rate control entirely

    def test_translated_file_size_settings_produce_a_real_bitrate_argument(self):
        # The fix, verified the same way: translated rc_mode reaches
        # build_args as "bitrate", which it does recognize.
        settings = self._settings("hevc_vaapi", "intel", "VBR", 500, "4")
        translated = self.window._settings_for_engine_vendor(settings, "libx265", None)
        args = worker.build_args(
            translated, Path("input.mkv"), Path("output.mp4"),
            probe_audio=False, audio_codec=None, duration_seconds=600.0,
        )
        self.assertIn("-b:v", args)
        self.assertNotIn("-crf", args)
        # -preset must be a real x265 preset name, not a leftover VAAPI
        # compression_level digit -- ffmpeg would reject "-preset 4" for
        # libx265 outright.
        self.assertIn(translated["speed"], main.X265_PRESETS)


class TestHardwareProbedOnceForTheWholeSession(unittest.TestCase):
    """detect_available_backends does a real filesystem probe (find_render_
    node -> /dev/dri/by-path) -- cheap today, but the whole point of
    caching it once in MainWindow.__init__ (_available_backends) rather
    than re-detecting per call site is that this stops being true the
    moment detection means an actual FFmpeg validation encode. Spies on
    the real function (side_effect=the real thing, not a stub) so this
    also exercises genuine detection logic, not just a call-count."""

    def test_automatic_and_both_job_boundary_corrections_reuse_the_startup_snapshot(self):
        real_detect = worker.detect_available_backends
        with patch.object(worker, "detect_available_backends", side_effect=real_detect) as mock_detect, \
             patch.object(worker, "find_render_node", side_effect=RuntimeError("no render node")):
            window = main.MainWindow()
            self.assertEqual(mock_detect.call_count, 1)

            window._on_processing_choice("automatic")
            self.assertEqual(mock_detect.call_count, 1)

            # Forces add_files' own _effective_current_settings()
            # correction (Expert's combo landing on a vendor this CPU-
            # only mock doesn't have) -- one of the two real job
            # boundaries that call best_available_engine() now that
            # encoder_combo itself is deliberately not self-correcting
            # (see TestDynamicProcessingButtons' own regression guards on
            # that).
            intel_index = next(
                i for i, (e, v, _l) in enumerate(main.ENCODERS) if e == "hevc_vaapi" and v == "intel"
            )
            window.encoder_combo.setCurrentIndex(intel_index)
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()
                window.add_files([clip])
            self.assertEqual(mock_detect.call_count, 1)

            # The other real job boundary: syncing a selected queue
            # item's settings from the panel also runs through
            # _effective_current_settings() -- must reuse the same
            # snapshot too, not re-probe.
            item = window.queue_list.topLevelItem(0)
            item.setSelected(True)
            window.encoder_combo.setCurrentIndex(intel_index)
            self.assertEqual(mock_detect.call_count, 1)


class TestUnknownProcessingChoiceRaises(unittest.TestCase):
    def test_unrecognized_choice_raises_rather_than_silently_picking_a_vendor(self):
        # Cheap insurance against a future vendor (NVIDIA) landing on the
        # wrong branch if adding it to _on_processing_choice ever misses
        # a case -- an explicit crash beats a silently wrong encoder.
        window = main.MainWindow()
        with self.assertRaises(ValueError):
            window._on_processing_choice("nvidia")


def _find_render_node_for(*present_vendors: str):
    def _fake(vendor_id):
        if vendor_id in present_vendors:
            return f"/dev/dri/renderD{present_vendors.index(vendor_id)}"
        raise RuntimeError(f"no render node found for PCI vendor {vendor_id}")
    return _fake


class TestSegmentedButtonBoldWidth(unittest.TestCase):
    # Regression guard for a real, reported-live clipping bug: "Better
    # Quality" (the longest label in its row) had its text cut off,
    # specifically once selected -- selection makes the segLeft/segMid/
    # segRight buttons bold (style.qss), and Qt's own sizeHint() doesn't
    # get recomputed for a QSS-only pseudo-state change, so a button sized
    # for its regular-weight text stays that size even once bold needs
    # more room. Every segmented button now reserves its own bold-state
    # width unconditionally (ui_builder.py's _segmented_btn_min_width) --
    # this checks that reservation actually covers the bold text for real
    # buttons in the app, not just that the helper function exists.
    def test_button_width_covers_its_own_bold_text(self):
        # No compat_modern_btn/compat_compatible_btn here -- Compatibility
        # is cut entirely (CONSUMER_FORK_PLAN.md's final control-hierarchy
        # decision, once Codec was promoted to a direct Normal choice), so
        # it doesn't exist as an attribute to check at all. No rc_quality_
        # btn/rc_filesize_btn/rc_advanced_btn either -- that friendly
        # 3-button row was cut from Expert entirely (it just duplicated
        # Normal's own Mode toggle); Expert shows the real rc_mode_combo
        # directly now, a QComboBox with no bold-state width concern.
        # processing_* is back (restored, now Normal-visible) alongside
        # mode_*/audio_handling_*/audio_channels_* (new Normal rows).
        # processing_intel_btn/processing_amd_btn only exist at all when
        # that vendor's hardware was detected (ui_builder.py) -- pinned
        # present here so this test covers all three regardless of
        # whatever GPUs happen to be installed on whatever machine runs
        # it, same reasoning TestRateControlButtons' own docstring gives
        # for not trusting ambient hardware.
        with patch.object(worker, "find_render_node", return_value="/dev/dri/renderD128"):
            window = main.MainWindow()
        window.show()
        for btn in (
            window.processing_cpu_btn, window.processing_intel_btn, window.processing_amd_btn,
            window.mode_quality_btn, window.mode_filesize_btn,
            window.quality_smaller_btn, window.quality_balanced_btn, window.quality_better_btn,
            window.audio_handling_automatic_btn, window.audio_handling_convert_btn,
            window.audio_channels_keep_btn, window.audio_channels_stereo_btn,
        ):
            bold_font = QFont(btn.font())
            bold_font.setWeight(QFont.Weight(600))
            needed = QFontMetrics(bold_font).horizontalAdvance(btn.text()) + 30
            self.assertGreaterEqual(
                btn.width(), needed,
                f"{btn.text()!r} is {btn.width()}px, needs {needed}px for its bold state",
            )


class TestSessionPersistenceSave(unittest.TestCase):
    """Cross-session queue persistence (session.py): session.save_session
    scheduled, debounced, after every real queue/output-folder mutation.
    Restore-on-startup is TestSessionPersistenceRestore below -- this
    class is the save side only. setUpModule's own module-wide patch
    (session.load_session -> None, session.save_session -> a no-op Mock)
    already keeps every *other* test in this file from touching a real
    session.json at all; these tests locally re-patch session.save_session
    with their own Mock to actually inspect what gets written."""

    def test_add_files_schedules_a_debounced_save(self):
        window = main.MainWindow()
        with patch.object(session, "save_session") as mock_save:
            with tempfile.TemporaryDirectory() as tmp:
                clip = Path(tmp) / "clip.mkv"
                clip.touch()
                window.add_files([clip])
            # Debounced -- scheduled, but not yet written.
            self.assertTrue(window._session_save_timer.isActive())
            mock_save.assert_not_called()
            window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()
        jobs, output_dir = mock_save.call_args[0]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(output_dir, window.output_dir)

    def test_remove_selected_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
        window.queue_list.topLevelItem(0).setSelected(True)
        with patch.object(session, "save_session") as mock_save:
            window._remove_selected()
            window._session_save_timer.timeout.emit()
        jobs, _ = mock_save.call_args[0]
        self.assertEqual(jobs, [])

    def test_clear_queue_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
        with patch.object(session, "save_session") as mock_save, \
             patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._clear_queue()
            window._session_save_timer.timeout.emit()
        jobs, _ = mock_save.call_args[0]
        self.assertEqual(jobs, [])

    def test_reorder_schedules_a_save(self):
        # queue_widget.py's _reorder_rows calls exactly this (on_reordered)
        # before moving anything -- see _push_undo_snapshot's own comment
        # on why hooking the save there is still correct despite firing
        # pre-mutation.
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
        with patch.object(session, "save_session") as mock_save:
            window._push_undo_snapshot()
            window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_output_folder_picker_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(session, "save_session") as mock_save, \
                 patch.object(queue_controller.QFileDialog, "getExistingDirectory", return_value=tmp):
                window._pick_output_dir()
                window._session_save_timer.timeout.emit()
        _, output_dir = mock_save.call_args[0]
        self.assertEqual(output_dir, Path(tmp))

    def test_typing_a_new_output_path_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(session, "save_session") as mock_save:
                window.output_edit.setText(tmp)
                window._on_output_edit_changed()
                window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_editing_a_selected_items_settings_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
        window.queue_list.topLevelItem(0).setSelected(True)
        with patch.object(session, "save_session") as mock_save:
            window.quality_smaller_btn.click()
            window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_a_completed_job_is_excluded_from_the_snapshot(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
        item = window.queue_list.topLevelItem(0)
        job = item.data(queue_widget.STATUS_COL, Qt.UserRole)
        job["_completed_output_path"] = "/videos/out.mp4"
        item.setData(queue_widget.STATUS_COL, Qt.UserRole, job)
        self.assertEqual(window._unfinished_queue_snapshot(), [])

    def test_a_job_finishing_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            out = Path(tmp) / "out.mkv"
            out.write_bytes(b"x")
            clip.write_bytes(b"xx")
            window.add_files([clip])
            window._current_running_item = window.queue_list.topLevelItem(0)
            with patch.object(session, "save_session") as mock_save:
                window._on_job_finished(str(clip), str(out))
                window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()
        jobs, _ = mock_save.call_args[0]
        self.assertEqual(jobs, [])  # the just-finished job is now excluded

    def test_a_job_failing_schedules_a_save(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            window.add_files([clip])
            window._current_running_item = window.queue_list.topLevelItem(0)
            with patch.object(session, "save_session") as mock_save:
                window._on_job_failed(str(clip), "boom")
                window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_all_finished_schedules_a_save(self):
        window = main.MainWindow()
        with patch.object(session, "save_session") as mock_save:
            window._on_all_finished()
            window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_debounce_only_writes_once_for_a_rapid_burst(self):
        window = main.MainWindow()
        with patch.object(session, "save_session") as mock_save:
            window._schedule_session_save()
            window._schedule_session_save()
            window._schedule_session_save()
            window._session_save_timer.timeout.emit()
        mock_save.assert_called_once()

    def test_close_event_flushes_synchronously_and_stops_the_timer(self):
        window = main.MainWindow()
        window.show()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            clip.touch()
            with patch.object(session, "save_session") as mock_save:
                window.add_files([clip])
                self.assertTrue(window._session_save_timer.isActive())
                window.close()
        mock_save.assert_called_once()
        self.assertFalse(window._session_save_timer.isActive())


class TestSessionPersistenceRestore(unittest.TestCase):
    """The other half (session.load_session, consulted once in main.py's
    __init__ via _restore_session) -- rebuilds the queue through the same
    _restore_queue_snapshot undo/redo already uses. See
    TestSessionPersistenceSave above for the save side."""

    def _fake_session(self, tmp, paths, output_dir=None, extra=None):
        jobs = []
        for p in paths:
            job = {**main.DEFAULT_SETTINGS, "path": str(p), "gpu_vendor": None}
            if extra:
                job.update(extra)
            jobs.append(job)
        return {
            "version": session.SESSION_VERSION,
            "output_dir": output_dir or tmp,
            "jobs": jobs,
        }

    def test_restores_unfinished_jobs_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            clip_a.touch()
            clip_b.touch()
            fake = self._fake_session(tmp, [clip_a, clip_b])
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            self.assertEqual(window.queue_list.topLevelItemCount(), 2)
            self.assertEqual(window.queue_list.topLevelItem(0).text(queue_widget.VIDEO_COL), "a.mkv")
            self.assertEqual(window.queue_list.topLevelItem(1).text(queue_widget.VIDEO_COL), "b.mkv")

    def test_restored_rows_start_ready_not_failed_or_converting(self):
        # This app never attempts partial ffmpeg resume -- a job caught
        # mid-convert or already marked Failed when the app closed comes
        # back as a fresh "Ready" row, same as _make_queue_row always
        # writes regardless of the persisted dict's own history (there is
        # no persisted status text at all -- only path + settings).
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a.mkv"
            clip.touch()
            fake = self._fake_session(tmp, [clip])
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            self.assertEqual(window.queue_list.topLevelItem(0).text(queue_widget.RESULT_COL), "Ready")

    def test_a_stray_completed_output_path_is_stripped_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a.mkv"
            clip.touch()
            fake = self._fake_session(tmp, [clip], extra={"_completed_output_path": "/x.mp4"})
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            job = window.queue_list.topLevelItem(0).data(queue_widget.STATUS_COL, Qt.UserRole)
            self.assertNotIn("_completed_output_path", job)

    def test_missing_files_are_dropped_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_a.touch()
            missing = Path(tmp) / "gone.mkv"  # never created
            fake = self._fake_session(tmp, [clip_a, missing])
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            self.assertEqual(window.queue_list.topLevelItemCount(), 1)
            self.assertEqual(window.status_label.text(), "Restored 1 video(s) from your last session (1 no longer found)")

    def test_all_files_missing_shows_a_quiet_notice_with_an_empty_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.mkv"
            fake = self._fake_session(tmp, [missing])
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            self.assertEqual(window.queue_list.topLevelItemCount(), 0)
            self.assertEqual(window.status_label.text(), "1 video(s) from your last session could no longer be found")

    def test_restores_the_output_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "custom_output"
            fake = self._fake_session(tmp, [], output_dir=str(out_dir))
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            self.assertEqual(window.output_dir, out_dir)

    def test_no_session_data_leaves_a_fresh_empty_queue(self):
        # The module-wide default (setUpModule) already returns None for
        # every other test in this file -- this test just makes that
        # explicit and documents the "nothing to restore" path directly.
        with patch.object(session, "load_session", return_value=None):
            window = main.MainWindow()
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)
        # "Idle" is status_label's own construction-time default
        # (ui_builder.py) -- _restore_session returns immediately with
        # nothing to restore, so nothing here ever calls _set_status to
        # change it.
        self.assertEqual(window.status_label.text(), "Idle")

    def test_sanitizes_an_unavailable_vendor_through_effective_current_settings(self):
        # The same correction every other real job boundary already goes
        # through (add_files, syncing a selected queue item's settings) --
        # a job saved against last session's Intel hardware can't be
        # trusted blindly if this session's machine no longer has it.
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "a.mkv"
            clip.touch()
            fake = self._fake_session(tmp, [clip], extra={
                "encoder": "hevc_vaapi", "gpu_vendor": "intel", "rc_mode": "ICQ", "quality_value": 26,
            })
            with patch.object(worker, "find_render_node", side_effect=_find_render_node_for(worker.AMD_VENDOR_ID)):
                with patch.object(session, "load_session", return_value=fake):
                    window = main.MainWindow()
            job = window.queue_list.topLevelItem(0).data(queue_widget.STATUS_COL, Qt.UserRole)
        self.assertEqual(job["encoder"], "hevc_vaapi")
        self.assertEqual(job["gpu_vendor"], "amd")

    def test_a_malformed_job_missing_a_required_key_is_skipped_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip_a = Path(tmp) / "a.mkv"
            clip_b = Path(tmp) / "b.mkv"
            clip_a.touch()
            clip_b.touch()
            fake = {
                "version": session.SESSION_VERSION,
                "output_dir": tmp,
                "jobs": [
                    {"path": str(clip_a)},  # missing encoder/rc_mode/etc entirely
                    {**main.DEFAULT_SETTINGS, "path": str(clip_b), "gpu_vendor": None},
                ],
            }
            with patch.object(session, "load_session", return_value=fake):
                window = main.MainWindow()
            # The one genuinely complete job still restores.
            self.assertEqual(window.queue_list.topLevelItemCount(), 1)
            self.assertEqual(window.queue_list.topLevelItem(0).text(queue_widget.VIDEO_COL), "b.mkv")

    def test_an_unexpected_error_during_restore_never_crashes_startup(self):
        # The broad top-level guard in _restore_session -- deliberately
        # not narrowed to a specific exception type, since the whole
        # point is that losing an unfinished queue is a recoverable
        # disappointment and failing to launch at all over it would not
        # be, regardless of what actually goes wrong.
        with patch.object(session, "load_session", side_effect=RuntimeError("disk on fire")):
            window = main.MainWindow()  # must not raise
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)

    def test_unrecognized_session_version_is_treated_as_nothing_to_restore(self):
        # load_session() itself already refuses a version mismatch (see
        # test_session.py) -- this just confirms _restore_session doesn't
        # need its own separate check on top of that contract.
        with patch.object(session, "load_session", return_value=None):
            window = main.MainWindow()
        self.assertEqual(window.queue_list.topLevelItemCount(), 0)


if __name__ == "__main__":
    unittest.main()
