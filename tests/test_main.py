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

from PySide6.QtCore import QEvent, QEventLoop, QMimeData, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QDropEvent, QFocusEvent, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_app = QApplication.instance() or QApplication([])

import formatting  # noqa: E402
import main  # noqa: E402
import presets  # noqa: E402
import queue_controller  # noqa: E402
import theming  # noqa: E402
import worker  # noqa: E402


@contextmanager
def _empty_qsettings():
    """Patches QSettings.value at the class level to simulate a fresh
    config store with nothing persisted yet. A bare MainWindow() otherwise
    reads whatever this machine's real ~/.config/TITAN-i/Transcoder.conf
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
    def test_default_theme_choice_is_dark(self):
        with _empty_qsettings():
            window = main.MainWindow()
        self.assertEqual(window._theme_choice, "dark")
        self.assertEqual(window.theme_combo.currentData(), "dark")

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
        # bit of window state in this app (geometry, splitter, collapsed
        # sections) already relies on unverified at this level too.
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
        # Constructed with an isolated (guaranteed-"dark") starting choice
        # -- selecting "light" against this machine's real ambient choice
        # would be a no-op (no currentIndexChanged, nothing to assert on)
        # on whatever day that real choice already happens to be "light".
        with _empty_qsettings():
            window = main.MainWindow()
        light_index = window.theme_combo.findData("light")
        with patch.object(window._qsettings, "setValue"), \
             patch.object(main, "_load_stylesheet") as mock_load:
            window.theme_combo.setCurrentIndex(light_index)
        self.assertEqual(window._theme_choice, "light")
        mock_load.assert_called_once()

    def test_selecting_the_combo_refreshes_save_delete_icons(self):
        # _refresh_themed_icons re-reads the SVG for the new theme -- a
        # stale icon object from construction time would otherwise keep
        # showing the old theme's colors after switching.
        with _empty_qsettings():
            window = main.MainWindow()
        light_index = window.theme_combo.findData("light")
        with patch.object(window._qsettings, "setValue"):
            with patch.object(window, "_refresh_themed_icons") as mock_refresh:
                window.theme_combo.setCurrentIndex(light_index)
        mock_refresh.assert_called_once()


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
    """The Quality / File Size / Advanced buttons are a friendlier view over
    rc_mode_combo (still the actual source of truth) -- see _set_rc_mode /
    _sync_rc_buttons_to_combo in main.py."""

    def test_quality_button_selects_icq_for_vaapi(self):
        window = main.MainWindow()
        window.rc_filesize_btn.click()  # move off the default first
        window.rc_quality_btn.click()
        self.assertEqual(window.rc_mode_combo.currentData(), "ICQ")
        self.assertTrue(window.rc_quality_btn.isChecked())

    def test_file_size_button_selects_vbr_for_vaapi(self):
        window = main.MainWindow()
        window.rc_filesize_btn.click()
        self.assertEqual(window.rc_mode_combo.currentData(), "VBR")
        self.assertTrue(window.rc_filesize_btn.isChecked())

    def test_advanced_button_selects_cqp_for_vaapi(self):
        window = main.MainWindow()
        window.rc_advanced_btn.click()
        self.assertEqual(window.rc_mode_combo.currentData(), "CQP")
        self.assertTrue(window.rc_advanced_btn.isChecked())

    def test_file_size_button_selects_bitrate_for_x265(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        window.rc_filesize_btn.click()
        self.assertEqual(window.rc_mode_combo.currentData(), "bitrate")

    def test_advanced_button_hidden_for_x265_no_cqp_equivalent(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        self.assertFalse(window.rc_advanced_btn.isVisible())

    def test_advanced_button_visible_again_switching_back_to_vaapi(self):
        # isVisible() reflects the whole ancestor chain, not just this
        # widget's own flag -- False for everything until the window itself
        # is shown, regardless of what setVisible() was called with.
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("CPU")
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertTrue(window.rc_advanced_btn.isVisible())

    def test_buttons_resync_to_combo_across_an_encoder_switch(self):
        # Quality on VAAPI (ICQ) should still read as the Quality button
        # after switching to x265 (CRF) -- the *concept* carries over even
        # though the underlying rc_mode value is different per encoder.
        window = main.MainWindow()
        window.rc_quality_btn.click()
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window.rc_mode_combo.currentData(), "CRF")
        self.assertTrue(window.rc_quality_btn.isChecked())

    def test_switching_off_cqp_to_x265_falls_back_to_a_button_that_exists(self):
        # CQP has no x265 equivalent -- RC_MODES["libx265"] simply doesn't
        # contain it, so switching encoders away from it lands on whatever
        # index 0 becomes (CRF), which the Quality button should reflect.
        window = main.MainWindow()
        window.rc_advanced_btn.click()
        window.encoder_combo.setCurrentText("CPU")
        self.assertEqual(window.rc_mode_combo.currentData(), "CRF")
        self.assertTrue(window.rc_quality_btn.isChecked())


class TestFileSizeButtonEndRounding(unittest.TestCase):
    """File Size (#segMid in style.qss) is styled as a middle segment --
    square on both sides -- which is wrong whenever Advanced (#segRight)
    is hidden (any encoder with no CQP equivalent, e.g. x265): File Size
    becomes the row's actual last visible button but stayed visually cut
    off square on the right, since QSS has no selector for "my sibling is
    hidden". Reported live, confirmed by screenshot. Fixed via a "segEnd"
    dynamic property set alongside rc_advanced_btn's own visibility in
    _on_encoder_changed -- these tests check that property directly
    rather than rendered pixels, matching how the sibling "modified"
    combo-box indicator is tested elsewhere in this file."""

    def test_file_size_gets_the_end_rounding_when_advanced_is_hidden(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")  # no CQP equivalent
        self.assertFalse(window.rc_advanced_btn.isVisible())
        self.assertTrue(window.rc_filesize_btn.property("segEnd"))

    def test_file_size_stays_a_plain_middle_segment_when_advanced_is_shown(self):
        # isVisible() reflects the whole ancestor chain, not just this
        # widget's own flag -- show() is needed before True reads back True
        # (see test_advanced_button_visible_again_switching_back_to_vaapi
        # above for the same gotcha; a hidden widget doesn't need it, which
        # is why the "hidden" test above doesn't call show()).
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertTrue(window.rc_advanced_btn.isVisible())
        self.assertFalse(window.rc_filesize_btn.property("segEnd"))

    def test_switching_back_to_vaapi_clears_the_end_rounding(self):
        window = main.MainWindow()
        window.encoder_combo.setCurrentText("CPU")
        self.assertTrue(window.rc_filesize_btn.property("segEnd"))
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertFalse(window.rc_filesize_btn.property("segEnd"))


class TestSpeedSliderVisibility(unittest.TestCase):
    """x265 got its own real Speed slider (speed_x265_slider), not just a
    QComboBox, matching VAAPI's speed_slider -- so speed_faster_label/
    speed_thorough_label/speed_tier_label are shared by both and stay
    visible regardless of encoder now; only which *slider* is showing
    actually changes. Was previously the reverse for speed_tier_label
    specifically (hidden for x265, visible only for VAAPI, since x265
    had no slider of its own to caption yet)."""

    def test_x265_slider_shown_and_vaapi_slider_hidden_for_cpu(self):
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("CPU")
        self.assertTrue(window.speed_x265_slider.isVisible())
        self.assertFalse(window.speed_slider.isVisible())

    def test_vaapi_slider_shown_and_x265_slider_hidden_for_vaapi(self):
        window = main.MainWindow()
        window.show()
        window.encoder_combo.setCurrentText("Intel (iGPU)")
        self.assertTrue(window.speed_slider.isVisible())
        self.assertFalse(window.speed_x265_slider.isVisible())

    def test_shared_labels_stay_visible_regardless_of_encoder(self):
        window = main.MainWindow()
        window.show()
        for encoder in ("CPU", "Intel (iGPU)", "AMD (GPU)"):
            window.encoder_combo.setCurrentText(encoder)
            self.assertTrue(window.speed_faster_label.isVisible(), encoder)
            self.assertTrue(window.speed_thorough_label.isVisible(), encoder)
            self.assertTrue(window.speed_tier_label.isVisible(), encoder)


class TestTargetSizeSettings(unittest.TestCase):
    """quality_value means a target output size in MB, not literal kbps,
    when rc_mode is a bitrate-family mode -- see worker.build_args's
    docstring and TestSizeToBitrate in test_worker.py for the conversion
    itself. This covers the GUI's side of storing/round-tripping it."""

    def test_size_spin_value_flows_into_current_settings(self):
        window = main.MainWindow()
        window.rc_filesize_btn.click()
        window.size_spin.setValue(750)
        self.assertEqual(window._current_settings()["quality_value"], 750)

    def test_apply_settings_to_controls_round_trips_size(self):
        window = main.MainWindow()
        window.show()  # isVisible() is always False pre-show(), see other tests' comments
        settings = window._current_settings()
        settings["rc_mode"] = "VBR"
        settings["quality_value"] = 2500
        window._apply_settings_to_controls(settings)
        self.assertEqual(window.size_spin.value(), 2500)
        self.assertTrue(window.size_spin.isVisible())
        self.assertFalse(window.quality_slider.isVisible())


class TestSizeEstimateLabel(unittest.TestCase):
    def test_hidden_in_quality_mode(self):
        window = main.MainWindow()
        self.assertFalse(window.size_estimate_label.isVisible())

    def test_prompts_for_a_file_when_queue_is_empty(self):
        window = main.MainWindow()
        window.rc_filesize_btn.click()
        self.assertIn("Add a file", window.size_estimate_label.text())

    def test_shows_a_real_computed_estimate_for_a_queued_file(self):
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window.rc_filesize_btn.click()
            window.size_spin.setValue(1000)
            text = window.size_estimate_label.text()
        self.assertIn("kbps", text)
        self.assertNotIn("Add a file", text)
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
        # rc_filesize_btn.click() -- _update_command_preview eagerly
        # probes duration for *any* queued file regardless of rc_mode (it
        # feeds build_args' duration_seconds unconditionally), so
        # add_files() alone already populates _preview_duration_cache;
        # patching any later than this would just hit that cache and never
        # call the (patched) function at all.
        window = main.MainWindow()
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            with patch.object(worker, "probe_duration", side_effect=RuntimeError("boom")):
                window.add_files([clip])
                _wait_for_detection(window)
                window.rc_filesize_btn.click()  # must not raise
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
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mkv"
            _make_clip(clip, "aac")
            window.add_files([clip])
            _wait_for_detection(window)
            window.rc_filesize_btn.click()
            window._preview_duration_cache[clip] = 7200.0  # simulate a 2-hour file
            window.size_spin.setValue(window.size_spin.minimum())
            text = window.size_estimate_label.text()
        self.assertIn("too small", text)
        self.assertNotIn("kbps", text)


class TestCollapsibleSections(unittest.TestCase):
    def test_effective_command_collapsed_by_default(self):
        with _empty_qsettings():
            window = main.MainWindow()
        self.assertFalse(window._command_group.isChecked())
        self.assertFalse(window.command_preview.isVisible())

    def test_log_collapsed_by_default(self):
        # Passes today even without _empty_qsettings (this machine's real
        # log_expanded happens to already be false), but that's luck, not
        # correctness -- isolated the same way as the Effective Command
        # test above so it can't start failing just because someone
        # expanded Log for real while using the app.
        with _empty_qsettings():
            window = main.MainWindow()
        self.assertFalse(window._log_group.isChecked())
        self.assertFalse(window.log_view.isVisible())

    def test_checking_the_group_reveals_its_content(self):
        window = main.MainWindow()
        window.show()  # isVisible() is always False pre-show(), see other tests' comments
        window._command_group.setChecked(True)
        self.assertTrue(window.command_preview.isVisible())

    def test_title_text_shows_arrow_reflecting_state(self):
        with _empty_qsettings():
            window = main.MainWindow()
        self.assertEqual(window._command_group.title(), "Effective Command  ▸")
        window._command_group.setChecked(True)
        self.assertEqual(window._command_group.title(), "Effective Command  ▾")


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


class TestPresetModifiedIndicator(unittest.TestCase):
    # A dynamic property on preset_combo itself (QSS: QComboBox[modified=
    # "true"] recolors its text), not a separate "(modified)" label --
    # that label's appearing/disappearing changed the preset row's width
    # and visibly reflowed the window every time a control was touched
    # (confirmed by screenshot). No window.show() needed here unlike most
    # other visibility-flavored tests in this file: a plain widget property
    # doesn't depend on the ancestor chain the way QWidget.isVisible() does.

    def test_false_immediately_after_loading_a_preset(self):
        window = main.MainWindow()
        self.assertFalse(window.preset_combo.property("modified"))

    def test_true_after_changing_a_setting(self):
        window = main.MainWindow()
        window.quality_slider.setValue(window.quality_slider.value() + 1)
        self.assertTrue(window.preset_combo.property("modified"))

    def test_false_again_after_reverting_the_change(self):
        window = main.MainWindow()
        original = window.quality_slider.value()
        window.quality_slider.setValue(original + 1)
        self.assertTrue(window.preset_combo.property("modified"))
        window.quality_slider.setValue(original)
        self.assertFalse(window.preset_combo.property("modified"))

    def test_false_immediately_after_loading_a_cpu_preset(self):
        # Real, confirmed bug: _current_settings() always includes
        # "gpu_vendor" (None for a non-VAAPI encoder, via
        # _current_gpu_vendor()), but the three built-in CPU presets never
        # define that key at all -- only the six VAAPI presets do. Plain
        # != treated a dict missing a key as different from one where it's
        # explicitly None, so selecting any CPU preset showed "modified"
        # immediately with nothing actually changed. The startup default
        # (see test_false_immediately_after_loading_a_preset above) is a
        # VAAPI preset and never exercised this -- this test selects a CPU
        # one specifically, the case that was actually broken.
        window = main.MainWindow()
        idx = window.preset_combo.findText("720p CPU Balanced (Software / x265)")
        window.preset_combo.setCurrentIndex(idx)
        self.assertFalse(window.preset_combo.property("modified"))

    def test_settings_differ_treats_missing_key_and_explicit_none_the_same(self):
        self.assertFalse(formatting.settings_differ({"a": None}, {}))
        self.assertFalse(formatting.settings_differ({}, {"a": None}))
        self.assertTrue(formatting.settings_differ({"a": 1}, {"a": 2}))
        self.assertTrue(formatting.settings_differ({"a": 1}, {}))


class TestSavePresetAsAndDelete(unittest.TestCase):
    """_save_preset_as/_delete_preset mutate window.presets in memory and
    persist via presets.save_presets() -- patched to a Mock in every test
    here so real file I/O (and the real project's presets.json) is never
    touched, regardless of what this test happens to save/delete."""

    @staticmethod
    def _window():
        with patch.object(main, "load_presets", return_value=presets.load_builtin_presets()):
            return main.MainWindow()

    def test_save_as_appends_a_new_preset_after_the_built_ins(self):
        window = self._window()
        original_count = len(window.presets)
        with patch.object(main, "save_presets") as mock_save, \
                patch.object(main.QInputDialog, "getText", return_value=("My Preset", True)):
            window._save_preset_as()
        self.assertEqual(len(window.presets), original_count + 1)
        self.assertEqual(window.presets[-1]["name"], "My Preset")  # appended, not inserted
        mock_save.assert_called_once_with(window.presets)

    def test_save_as_refuses_a_builtin_name(self):
        window = self._window()
        original_count = len(window.presets)
        with patch.object(main, "save_presets") as mock_save, \
                patch.object(main.QInputDialog, "getText",
                              return_value=("720p Intel Balanced (Hardware / VAAPI)", True)), \
                patch.object(main.QMessageBox, "warning") as mock_warning:
            window._save_preset_as()
        mock_warning.assert_called_once()
        mock_save.assert_not_called()
        self.assertEqual(len(window.presets), original_count)

    def test_save_as_overwrites_an_existing_user_preset_in_place(self):
        window = self._window()
        with patch.object(main, "save_presets"), \
                patch.object(main.QInputDialog, "getText", return_value=("My Preset", True)):
            window._save_preset_as()
        original_count = len(window.presets)
        window.quality_slider.setValue(window.quality_slider.value() + 1)
        new_value = window.quality_slider.value()
        with patch.object(main, "save_presets") as mock_save, \
                patch.object(main.QInputDialog, "getText", return_value=("My Preset", True)), \
                patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._save_preset_as()
        self.assertEqual(len(window.presets), original_count)  # overwritten, not appended again
        saved = next(p for p in window.presets if p["name"] == "My Preset")
        self.assertEqual(saved["quality_value"], new_value)
        mock_save.assert_called_once_with(window.presets)

    def test_delete_removes_a_user_preset(self):
        window = self._window()
        with patch.object(main, "save_presets"), \
                patch.object(main.QInputDialog, "getText", return_value=("My Preset", True)):
            window._save_preset_as()
        original_count = len(window.presets)
        window.preset_combo.setCurrentIndex(window.preset_combo.findText("My Preset"))
        with patch.object(main, "save_presets") as mock_save, \
                patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes):
            window._delete_preset()
        self.assertEqual(len(window.presets), original_count - 1)
        self.assertNotIn("My Preset", {p["name"] for p in window.presets})
        mock_save.assert_called_once_with(window.presets)

    def test_delete_refuses_a_builtin(self):
        window = self._window()
        original_count = len(window.presets)
        window.preset_combo.setCurrentIndex(
            window.preset_combo.findText("720p Intel Balanced (Hardware / VAAPI)")
        )
        with patch.object(main, "save_presets") as mock_save, \
                patch.object(main.QMessageBox, "warning") as mock_warning:
            window._delete_preset()
        mock_warning.assert_called_once()
        mock_save.assert_not_called()
        self.assertEqual(len(window.presets), original_count)


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
        # A hand-edited presets.json could carry a bitrate string that's
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
        # Same defensive-fallback reasoning as Audio Bitrate -- a hand-
        # edited presets.json could carry a preset name that isn't one of
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


class TestAudioDownmix(unittest.TestCase):
    def test_default_is_off(self):
        window = main.MainWindow()
        self.assertFalse(window._current_settings()["audio_downmix_stereo"])

    def test_checkbox_round_trips_through_current_settings(self):
        window = main.MainWindow()
        window.audio_downmix_check.setChecked(True)
        self.assertTrue(window._current_settings()["audio_downmix_stereo"])

    def test_apply_settings_sets_the_checkbox(self):
        window = main.MainWindow()
        settings = window._current_settings()
        window._apply_settings_to_controls({**settings, "audio_downmix_stereo": True})
        self.assertTrue(window.audio_downmix_check.isChecked())

    def test_apply_settings_defaults_to_off_for_an_older_preset_missing_the_key(self):
        # A preset saved before this control existed simply won't have this
        # key -- .get(..., False) in _apply_settings_to_controls, not a bare
        # index, is what keeps that from crashing (same reasoning as the
        # container/tune/deinterlace fallbacks it sits alongside).
        window = main.MainWindow()
        settings = window._current_settings()
        window.audio_downmix_check.setChecked(True)
        old_settings = {k: v for k, v in settings.items() if k != "audio_downmix_stereo"}
        window._apply_settings_to_controls(old_settings)
        self.assertFalse(window.audio_downmix_check.isChecked())


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
        window = main.MainWindow()
        window.preset_combo.showPopup()
        _app.processEvents()
        popup = window.preset_combo.view().window()
        self.assertIn(main._current_theme_palette["BG_PANEL"], popup.styleSheet())
        window.preset_combo.hidePopup()
        _app.processEvents()

    def test_modified_indicator_is_suppressed_while_popup_is_open(self):
        window = main.MainWindow()
        window.preset_combo.setProperty("modified", True)
        window.preset_combo.style().unpolish(window.preset_combo)
        window.preset_combo.style().polish(window.preset_combo)
        window.preset_combo.showPopup()
        _app.processEvents()
        self.assertFalse(window.preset_combo.property("modified"))
        window.preset_combo.hidePopup()
        _app.processEvents()

    def test_modified_indicator_is_restored_after_popup_closes(self):
        window = main.MainWindow()
        window.preset_combo.setProperty("modified", True)
        window.preset_combo.style().unpolish(window.preset_combo)
        window.preset_combo.style().polish(window.preset_combo)
        window.preset_combo.showPopup()
        _app.processEvents()
        window.preset_combo.hidePopup()
        _app.processEvents()
        self.assertTrue(window.preset_combo.property("modified"))

    def test_unmodified_combo_is_left_alone(self):
        window = main.MainWindow()
        self.assertFalse(window.preset_combo.property("modified"))
        window.preset_combo.showPopup()
        _app.processEvents()
        self.assertFalse(window.preset_combo.property("modified"))
        self.assertFalse(window.preset_combo.property("_popupSuppressedModified"))
        window.preset_combo.hidePopup()
        _app.processEvents()
        self.assertFalse(window.preset_combo.property("modified"))


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
        window.deinterlace_check.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        self.assertTrue(window.deinterlace_check.property("focusVisible"))

    def test_mouse_focus_is_not_visible(self):
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        window.deinterlace_check.setFocus(Qt.FocusReason.MouseFocusReason)
        _app.processEvents()
        self.assertFalse(window.deinterlace_check.property("focusVisible"))

    def test_losing_focus_clears_the_property(self):
        window = main.MainWindow()
        window.show()
        _app.processEvents()
        window.deinterlace_check.setFocus(Qt.FocusReason.TabFocusReason)
        _app.processEvents()
        self.assertTrue(window.deinterlace_check.property("focusVisible"))
        window.deinterlace_check.clearFocus()
        _app.processEvents()
        self.assertFalse(window.deinterlace_check.property("focusVisible"))

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
        self.assertIn("(interlaced)", item.text(main.VIDEO_COL))

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

    def test_start_refuses_while_detection_is_still_pending(self):
        # Real race otherwise: add_files' job snapshot is taken at add
        # time, before the ~20s sample has a result -- clicking Start
        # immediately could begin encoding with whatever deinterlace value
        # the file started with, not what detection would actually find.
        window = main.MainWindow()
        window.add_files([self.interlaced_clip])
        self.assertTrue(window._detection_processes, "test assumes detection is still in flight")
        window._start()
        self.assertIn("analyzing", window.status_label.text())
        # _start() must have returned early, before disabling this -- proof
        # it didn't actually launch a job with stale (pre-detection) data.
        self.assertTrue(window.start_btn.isEnabled())
        _wait_for_detection(window)


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
        self.assertEqual(window.output_edit.text(), str(window.output_dir))

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

    def test_vaapi_quality_mode_job(self):
        job = presets.load_builtin_presets()[4]  # 720p Intel Balanced
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
        job = presets.load_builtin_presets()[1]  # 720p CPU Balanced
        summary = formatting.settings_summary(job)
        self.assertIn("CPU", summary)
        self.assertIn("CRF 23", summary)

    def test_target_size_mode_shows_mb_not_a_bare_rc_mode_value(self):
        job = {**presets.load_builtin_presets()[4], "rc_mode": "VBR", "quality_value": 800}
        summary = formatting.settings_summary(job)
        self.assertIn("Target size: 800 MB", summary)
        self.assertNotIn("VBR 800", summary)

    def test_deinterlace_on_is_shown(self):
        job = {**presets.load_builtin_presets()[4], "deinterlace": True}
        self.assertIn("Deinterlace: on", formatting.settings_summary(job))

    def test_downmix_shown_only_when_checked(self):
        job = presets.load_builtin_presets()[4]
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
        tooltip = item.toolTip(main.FILE_COL)
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
        self.assertIn(formatting.settings_summary(updated_job), item.toolTip(main.FILE_COL))


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
    """The Video cell's text depends on two independent async results (see
    _refresh_video_cell's own comment in main.py) that can land in either
    order -- both orders must produce the same final text."""

    def test_codec_label_landing_first_then_deinterlace_flag(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        item.setData(main.VIDEO_COL, main.Qt.UserRole, "HEVC 1920x1080")
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "HEVC 1920x1080")
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        job["deinterlace"] = True
        item.setData(main.STATUS_COL, main.Qt.UserRole, job)
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "HEVC 1920x1080 (interlaced)")

    def test_deinterlace_flag_landing_first_then_codec_label(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")
        job = item.data(main.STATUS_COL, main.Qt.UserRole)
        job["deinterlace"] = True
        item.setData(main.STATUS_COL, main.Qt.UserRole, job)
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "")  # nothing to show yet
        item.setData(main.VIDEO_COL, main.Qt.UserRole, "HEVC 1920x1080")
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "HEVC 1920x1080 (interlaced)")

    def test_progressive_source_gets_no_suffix(self):
        window = main.MainWindow()
        item = _add_dummy_item(window, "a.mkv")  # deinterlace defaults False
        item.setData(main.VIDEO_COL, main.Qt.UserRole, "H.264 1280x720")
        window._refresh_video_cell(item)
        self.assertEqual(item.text(main.VIDEO_COL), "H.264 1280x720")


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
            self.assertEqual(item.text(main.FILE_COL), "movie.mkv")
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
        window = main.MainWindow()
        window.add_files([self.clip])
        _wait_for_detection(window)
        item = window.queue_list.topLevelItem(0)
        self.assertIn("H.264", item.text(main.VIDEO_COL))
        self.assertIn("320x240", item.text(main.VIDEO_COL))
        self.assertEqual(item.text(main.DURATION_COL), "0:01")
        self.assertIn("AAC", item.text(main.AUDIO_COL))

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


if __name__ == "__main__":
    unittest.main()
