#!/usr/bin/env python3
"""TITAN-i Transcoder: minimal ffmpeg front-end replacing HandBrake QSV."""
import shlex
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QProcess
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTreeWidgetItem, QLabel, QInputDialog, QMessageBox,
)

import worker
import formatting
from constants import (
    ENCODERS, RC_MODES, RC_MODE_FRIENDLY,
    encoder_profile_key, QUALITY_RANGES, X265_PRESETS, RESOLUTIONS,
    AUDIO_BITRATES, BUILTIN_PRESET_NAMES,
)
from presets import load_presets, save_presets
from worker import TranscodeQueue, BITRATE_RC_MODES
from queue_widget import (
    FILE_COL, VIDEO_COL, DURATION_COL, AUDIO_COL, SIZE_COL,
    RESULT_COL, STATUS_COL, QUEUE_COLUMN_HEADERS,
)
from theming import (
    _current_theme_palette, _system_accent_tokens, _load_stylesheet,
    _ComboPopupBackgroundFilter, _FocusVisibleFilter,
    _validate_theme_choice, _resolve_theme, _fuzzy_text_color,
)
from ui_builder import _UiBuilderMixin
from queue_controller import _QueueControllerMixin

class MainWindow(QMainWindow, _UiBuilderMixin, _QueueControllerMixin):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TITAN-i Transcoder")
        self.resize(1240, 820)

        self.presets: list[dict] = load_presets()
        self._res_label = {(r["width"], r["height"]): r["label"] for r in RESOLUTIONS}
        self._preview_audio_cache: dict[tuple, str | None] = {}
        self._preview_audio_channels_cache: dict[tuple, int | None] = {}
        self._preview_duration_cache: dict[Path, float] = {}
        self._last_preview_args: list[str] = []
        self._loaded_preset_settings: dict | None = None
        self._running_items: list[QTreeWidgetItem] = []
        self._current_running_item: QTreeWidgetItem | None = None
        self._syncing_controls_from_selection = False
        self._queue_editable = True
        # Counts remaining outstanding probes (interlace detection + source
        # probe, starts at 2) for a file added while a run is already in
        # progress -- see add_files/_maybe_submit_mid_run_job in
        # queue_controller.py. Only ever populated for a genuinely mid-run
        # add; an item added while idle is never in here.
        self._pending_mid_run_items: dict[QTreeWidgetItem, int] = {}
        # Queue-structure undo/redo (add/remove/clear/reorder) -- see
        # _push_undo_snapshot/_undo/_redo in queue_controller.py. Each
        # entry is a full snapshot (list of job dicts), not a diff.
        self._undo_stack: list[list[dict]] = []
        self._redo_stack: list[list[dict]] = []
        self._detection_processes: list[QProcess] = []  # keep references alive; Qt won't
        self.output_dir = Path.home() / "Videos" / "transcoded"
        self._qsettings = QSettings("TITAN-i", "Transcoder")

        self._theme_choice = _validate_theme_choice(self._qsettings.value("theme_choice", "dark"))
        _load_stylesheet(QApplication.instance(), _resolve_theme(self._theme_choice))
        # "System" needs to react live, not just at launch -- confirmed this
        # signal actually exists and fires on this Qt/PySide6 version before
        # relying on it (see the git history for the real check).
        QApplication.instance().styleHints().colorSchemeChanged.connect(self._on_system_theme_changed)

        self.queue = TranscodeQueue()
        self.queue.job_started.connect(self._on_job_started)
        self.queue.job_progress.connect(self._on_job_progress)
        self.queue.job_stats.connect(self._on_job_stats)
        self.queue.job_log.connect(self._on_job_log)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.job_failed.connect(self._on_job_failed)
        self.queue.all_finished.connect(self._on_all_finished)

        self._build_ui()
        # Ctrl+Z/Ctrl+Shift+Z -- default Qt.WindowShortcut context, fires
        # regardless of which child widget has focus, matching how
        # document-level undo normally behaves. Queue-structure only (add/
        # remove/clear/reorder) -- see _push_undo_snapshot/_undo/_redo in
        # queue_controller.py.
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self._redo)
        # Delete removes the selected queue row(s) -- scoped to queue_list
        # itself (Qt.WidgetWithChildrenShortcut, not the window-wide
        # default above) so Delete/Backspace still edits text normally
        # when output_edit or a dialog field has focus instead of
        # misfiring as "remove queue item".
        delete_shortcut = QShortcut(QKeySequence(Qt.Key_Delete), self.queue_list, self._remove_selected)
        delete_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        # Deferred to here rather than set inline at the end of
        # _build_audio_tab(), same reason quality_slider/speed_slider's real
        # initial values are set later too (via _on_encoder_changed below),
        # not at construction: every slider-changed handler ends in
        # _on_control_changed() -> _sync_settings_to_selected_queue_items(),
        # which reaches self.queue_list -- built later in _build_ui() by
        # _build_right_panel(), after _build_audio_tab() has already run.
        # Confirmed directly: setting it inline crashed with exactly that
        # AttributeError the first time this ran. speed_x265_slider is the
        # same story -- its own valueChanged handler ends in the same
        # _on_control_changed() call, so its default ("medium", matching
        # x265's own default and what the old speed_combo used to start on)
        # is set here too, not inline in _build_video_tab().
        self.audio_bitrate_slider.setValue(AUDIO_BITRATES.index("160k"))
        self.speed_x265_slider.setValue(X265_PRESETS.index("medium"))
        # Must run before _refresh_preset_combo(): it's the only thing that
        # populates rc_mode_combo, and applying a preset while that combo is
        # still empty leaves rc_mode reading back as None.
        self._on_encoder_changed()
        # Explicit select=, not just "whatever's first" -- every launch
        # should land on CPU Balanced regardless of where it sits in the
        # built-in presets' own CPU/Intel/AMD order. Preset selection isn't
        # otherwise persisted across launches at all (unlike window
        # geometry/theme/etc.), so this runs on every single startup, not
        # just a first install.
        self._refresh_preset_combo(select="720p CPU Balanced (Software / x265)")
        self._restore_window_state()

    def closeEvent(self, event):
        self._qsettings.setValue("window_geometry", self.saveGeometry())
        self._qsettings.setValue("splitter_state", self._splitter.saveState())
        self._qsettings.setValue("command_expanded", self._command_group.isChecked())
        self._qsettings.setValue("log_expanded", self._log_group.isChecked())
        super().closeEvent(event)

    def _restore_window_state(self):
        geometry = self._qsettings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        # QSettings round-trips bool through its backing store as the string
        # "true"/"false" on some platforms -- str(...) != "false" rather than
        # a bare truthiness check, so a stored False doesn't come back truthy.
        command_expanded = self._qsettings.value("command_expanded")
        if command_expanded is not None:
            self._command_group.setChecked(str(command_expanded) != "false")
        log_expanded = self._qsettings.value("log_expanded")
        if log_expanded is not None:
            self._log_group.setChecked(str(log_expanded) != "false")
        splitter_state = self._qsettings.value("splitter_state")
        if splitter_state is not None:
            self._splitter.restoreState(splitter_state)

    def _apply_theme(self, choice: str):
        self._theme_choice = choice
        self._qsettings.setValue("theme_choice", choice)
        _load_stylesheet(QApplication.instance(), _resolve_theme(choice))
        self._refresh_themed_icons()
        self._refresh_fuzzy_caption_style()

    def _on_system_theme_changed(self, _scheme):
        if self._theme_choice == "system":
            _load_stylesheet(QApplication.instance(), _resolve_theme("system"))
            self._refresh_themed_icons()
            self._refresh_fuzzy_caption_style()

    def _apply_fuzzy_caption_style(self, label: QLabel):
        # Matches "Drag video files here..." (DropTreeWidget.paintEvent) --
        # both go through theming._fuzzy_text_color so they stay the same
        # kind of secondary/explanatory text, and both fall back the same
        # way if QPalette.PlaceholderText isn't actually distinct from
        # regular text on this session (see that function's docstring).
        # Read fresh each call rather than baked in once, so this stays
        # correct across every theme including "Match System". QSS's
        # `color:` property does understand rgba() -- unlike QColor's own
        # string constructor, which is what tripped this up in
        # queue_widget.py (see _fuzzy_text_color's docstring) -- so
        # formatting it here, for this one QSS-consuming call site, is
        # safe.
        color = _fuzzy_text_color(self)
        rgba = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"
        label.setStyleSheet(f"font-size: 9pt; color: {rgba};")

    def _refresh_fuzzy_caption_style(self):
        for label in (self.quality_tier_label, self.speed_tier_label, self.audio_bitrate_tier_label):
            self._apply_fuzzy_caption_style(label)

    def _themed_icon(self, name: str) -> QIcon:
        theme = _resolve_theme(self._theme_choice)
        return QIcon(str(Path(__file__).parent / "assets" / f"{name}_{theme}.svg"))

    def _refresh_themed_icons(self):
        self.save_btn.setIcon(self._themed_icon("save"))
        self.delete_btn.setIcon(self._themed_icon("delete"))

    def _copy_command_to_clipboard(self):
        # Not self.command_preview.toPlainText() -- that's _format_preview_
        # text's grouped-onto-several-lines *display* form, plain-space-
        # joined with no quoting at all, which reads fine on screen but
        # isn't actually valid shell input: a path containing a space
        # (e.g. "/media/My Video.mov") would paste as two separate
        # arguments, not one. shlex.join over the real args list this
        # preview was actually built from quotes whatever needs it and
        # leaves everything else alone, and flattens to one line -- multi-
        # line would need trailing "\" continuations to paste correctly,
        # which the display form was never written to include.
        QApplication.clipboard().setText(shlex.join(self._last_preview_args))

    # --- cascading settings behavior ---
    def _current_encoder_id(self) -> str:
        return ENCODERS[self.encoder_combo.currentIndex()][0]

    def _current_gpu_vendor(self) -> str | None:
        return ENCODERS[self.encoder_combo.currentIndex()][1]

    def _current_encoder_key(self) -> str:
        """RC_MODES/RC_MODE_FRIENDLY lookup key -- encoder id alone isn't
        specific enough once two GPU vendors share "hevc_vaapi" but support
        different rc_modes (AMD's driver rejects ICQ outright)."""
        return encoder_profile_key(self._current_encoder_id(), self._current_gpu_vendor())

    def _on_encoder_changed(self):
        encoder = self._current_encoder_id()
        encoder_key = self._current_encoder_key()
        is_vaapi = encoder == "hevc_vaapi"

        self.rc_mode_combo.blockSignals(True)
        self.rc_mode_combo.clear()
        for value, label in RC_MODES[encoder_key]:
            self.rc_mode_combo.addItem(label, userData=value)
        self.rc_mode_combo.blockSignals(False)

        # CQP (the Advanced button) has no libx265 equivalent, and AMD's
        # driver has no ICQ to demote it in favor of in the first place.
        advanced_visible = RC_MODE_FRIENDLY[encoder_key]["advanced"] is not None
        self.rc_advanced_btn.setVisible(advanced_visible)
        # File Size (#segMid in style.qss) is styled as a middle segment --
        # square on both sides, the outer two only round their own outer
        # corner -- which is wrong whenever Advanced (#segRight) is hidden:
        # File Size becomes the row's actual last visible button but still
        # renders cut off square on the right, since QSS has no selector
        # for "my sibling is hidden". Reported live, confirmed by
        # screenshot. Same setProperty/unpolish/polish pattern the preset
        # combo's "modified" indicator already uses just below for the
        # same reason: state a QSS selector alone can't express.
        self.rc_filesize_btn.setProperty("segEnd", not advanced_visible)
        self.rc_filesize_btn.style().unpolish(self.rc_filesize_btn)
        self.rc_filesize_btn.style().polish(self.rc_filesize_btn)

        # speed_faster_label/speed_thorough_label/speed_tier_label are
        # shared by both sliders below (same "Faster .. Slower" axis,
        # same 3-tier fuzzy caption vocabulary either way) -- always visible
        # now that x265 has its own real slider too, not just VAAPI.
        self.speed_slider.setVisible(is_vaapi)
        self.speed_x265_slider.setVisible(not is_vaapi)
        if is_vaapi:
            self._on_speed_slider_changed()
        else:
            self._on_speed_x265_slider_changed()
        self.video_form.setRowVisible(self.tune_combo, not is_vaapi)

        # Repopulating above ran with signals blocked (clearing/adding items
        # one at a time would otherwise fire currentIndexChanged repeatedly
        # on a half-built list), so neither of its normal listeners ran --
        # both are called explicitly here instead. Order matters: the
        # buttons read rc_mode_combo's now-settled state, they don't drive it.
        self._on_rc_mode_changed()
        self._sync_rc_buttons_to_combo()

    def _on_rc_mode_changed(self):
        # rc_mode_combo is always populated by this point -- __init__ calls
        # _on_encoder_changed() (the only thing that populates it) before
        # anything that could apply a preset and reach this method.
        rc_mode = self.rc_mode_combo.currentData()
        is_bitrate = rc_mode in BITRATE_RC_MODES
        self.quality_slider.setVisible(not is_bitrate)
        self.quality_label.setVisible(not is_bitrate)
        self.quality_tier_label.setVisible(not is_bitrate)
        self.size_spin.setVisible(is_bitrate)
        self.size_estimate_label.setVisible(is_bitrate)
        if not is_bitrate:
            lo, hi, default = QUALITY_RANGES[rc_mode]
            self.quality_slider.blockSignals(True)
            self.quality_slider.setRange(lo, hi)
            self.quality_slider.setValue(default)
            self.quality_slider.blockSignals(False)
            self._on_quality_changed()
        else:
            self._on_control_changed()

    def _on_quality_changed(self):
        rc_mode = self.rc_mode_combo.currentData()
        self.quality_label.setText(f"{self.quality_slider.value()} ({rc_mode})")
        # Fraction 0 = the lowest ICQ/CQP/CRF number = the *best* quality
        # end (these all share the same lower-is-better convention) -- so
        # the first label here is what belongs at that end, not what reads
        # first. "Movies & TV" and the near-lossless/lighter-footage labels
        # either side of it are the original 3-tier set, kept as the
        # anchors readers may already recognize; the other 3 fill in the
        # gaps a plain 3-way split left too wide. Trimmed to a short
        # "Label -- descriptor" pair per tier -- the original wordier
        # phrasing read as too long next to Speed's captions once both
        # sat in the same size fuzzyGroup box.
        self.quality_tier_label.setText(formatting.tier_label(
            formatting.fraction_of(self.quality_slider),
            "Archival -- near-lossless",
            "High quality -- crisp detail",
            "Movies & TV -- general purpose",
            "Streaming -- efficient",
            "Lighter footage -- more compression",
            "Heavy compression -- visible quality loss",
        ))
        self._on_control_changed()

    def _set_rc_mode(self, value: str):
        index = next(
            (i for i in range(self.rc_mode_combo.count()) if self.rc_mode_combo.itemData(i) == value), None
        )
        if index is not None:
            self.rc_mode_combo.setCurrentIndex(index)

    def _sync_rc_buttons_to_combo(self):
        # Keeps the visible buttons correct no matter what actually changed
        # rc_mode_combo underneath -- a button click, an encoder switch
        # repopulating it, or a preset/queue-selection load -- rather than
        # scattering a sync call across every one of those call sites.
        value = self.rc_mode_combo.currentData()
        friendly = RC_MODE_FRIENDLY[self._current_encoder_key()]
        if value == friendly["quality"]:
            self.rc_quality_btn.setChecked(True)
        elif value == friendly["file_size"]:
            self.rc_filesize_btn.setChecked(True)
        elif value == friendly["advanced"]:
            self.rc_advanced_btn.setChecked(True)

    def _on_speed_slider_changed(self):
        self.speed_slider.setToolTip(
            "Left: faster encode.\n"
            "Right: slower, more size-efficient at the same quality.\n"
            f"(compression_level {self.speed_slider.value()}/{self.speed_slider.maximum()})"
        )
        # Fraction 0 = compression_level 1 = confirmed (real timing test:
        # 22.4s vs. 11.3s at level 7, smaller output too) the slowest and
        # most size-efficient end, not just the visually-leftmost one.
        # "Thorough"/"Balanced"/"Fast" are the original 3-tier set, kept as
        # anchors; the other 3 fill the gaps, same expansion Quality got.
        self.speed_tier_label.setText(formatting.tier_label(
            formatting.fraction_of(self.speed_slider),
            "Maximum effort -- best compression",
            "Thorough -- best efficiency",
            "Careful -- strong efficiency",
            "Balanced -- solid default",
            "Quick -- fast turnaround",
            "Fast -- quick previews",
        ))
        self._on_control_changed()

    def _on_speed_x265_slider_changed(self):
        preset = X265_PRESETS[self.speed_x265_slider.value()]
        # Same 6 captions as the VAAPI slider above (same axis, same
        # meaning, just a different underlying scale) -- but in the
        # opposite fraction order: X265_PRESETS is already sorted fastest
        # to slowest (ultrafast..placebo), so index 0 is the *fast* end
        # here, where compression_level 1 was the *slow* end there.
        caption = formatting.tier_label(
            formatting.fraction_of(self.speed_x265_slider),
            "Fast -- quick previews",
            "Quick -- fast turnaround",
            "Balanced -- solid default",
            "Careful -- strong efficiency",
            "Thorough -- best efficiency",
            "Maximum effort -- best compression",
        )
        # The raw x265 preset name used to sit in its own label next to
        # "Slower" ("Slower (medium)") -- discussed directly, dropped as
        # redundant now that the caption right underneath already
        # describes this same position ("Careful -- strong efficiency").
        # Folded in here instead of discarded outright, so the actual
        # preset name (useful for anyone cross-checking Effective
        # Command's "-preset medium") isn't lost, just shown once instead
        # of twice.
        self.speed_tier_label.setText(f"{caption} ({preset})")
        self._on_control_changed()

    # One caption per real AUDIO_BITRATES entry, not the 3-bucket
    # formatting.tier_label() helper Quality/Speed use above -- those two are smooth,
    # continuous ranges where fuzzy thirds make sense, but this is 5 fixed,
    # named stops (96k/128k/160k/192k/256k); bucketing 5 discrete values
    # into 3 fuzzy tiers would lump two stops together on one end and
    # leave the other end lopsided for no real reason.
    # Framed around video content, same as Quality's own captions above
    # ("Movies & TV", "Documentary / lighter footage") -- this track is a
    # movie/show's audio, not a standalone music file, so "casual
    # listening" / "transparent stereo" (a music-encoder's own vocabulary)
    # described the wrong thing.
    _AUDIO_BITRATE_DESCRIPTIONS = [
        "Dialogue / older TV -- smallest file",
        "Standard TV & streaming",
        "Movies & TV -- general-purpose target",
        "Action / music-heavy soundtrack",
        "Concert film / archival master",
    ]

    def _on_audio_bitrate_slider_changed(self):
        bitrate = AUDIO_BITRATES[self.audio_bitrate_slider.value()]
        self.audio_bitrate_label.setText(bitrate)
        self.audio_bitrate_tier_label.setText(
            self._AUDIO_BITRATE_DESCRIPTIONS[self.audio_bitrate_slider.value()]
        )
        self._on_control_changed()

    def _on_control_changed(self):
        """Fired by real user interaction with any settings control (not by
        _apply_settings_to_controls populating widgets from a preset or a
        queue selection -- _sync_settings_to_selected_queue_items guards
        against that itself)."""
        self._update_command_preview()
        self._sync_settings_to_selected_queue_items()

    def _sync_settings_to_selected_queue_items(self):
        # Selecting a queue item to inspect its settings (see
        # _on_queue_selection_changed) populates these same controls, which
        # would otherwise loop right back and stomp every other selected
        # item's settings with the first one's, just from clicking to select.
        if self._syncing_controls_from_selection or not self._queue_editable:
            return
        settings = self._current_settings()
        for item in self.queue_list.selectedItems():
            job = item.data(STATUS_COL, Qt.UserRole)
            job.update(settings)
            item.setData(STATUS_COL, Qt.UserRole, job)
            # Otherwise the tooltip goes stale the moment a selected row's
            # settings actually change -- still showing whatever was true
            # when the row was first added.
            tooltip = self._row_tooltip(job)
            for col in range(len(QUEUE_COLUMN_HEADERS)):
                item.setToolTip(col, tooltip)

    def _on_queue_selection_changed(self):
        selected = self.queue_list.selectedItems()
        if not selected:
            return
        # Multiple items selected with different settings: show the first
        # one's. Any control change from here applies to all of them --
        # this is the direct-manipulation replacement for the old "Apply
        # Settings to Selected" button.
        job = selected[0].data(STATUS_COL, Qt.UserRole)
        self._syncing_controls_from_selection = True
        try:
            self._apply_settings_to_controls(job)
        finally:
            self._syncing_controls_from_selection = False

    def _update_command_preview(self):
        if not hasattr(self, "command_preview"):
            return  # widgets still being constructed
        settings = self._current_settings()
        output_path = Path(f"output.{settings['container']}")
        try:
            if self.queue_list.topLevelItemCount() > 0:
                # A real file is queued -- probe its actual audio track and
                # duration (both cached, so dragging a slider doesn't shell
                # out to ffprobe repeatedly) instead of guessing, so the
                # preview matches what will really run. Whichever row is
                # actually selected (the one these settings apply to and
                # came from -- see _on_queue_selection_changed), not always
                # row 0 -- confirmed a real bug otherwise: selecting a
                # different file to edit its settings still showed the
                # *first* queued file's duration/audio in the preview.
                selected = self.queue_list.selectedItems()
                reference_item = selected[0] if selected else self.queue_list.topLevelItem(0)
                first_path = reference_item.data(STATUS_COL, Qt.UserRole)["path"]
                audio_codec = self._preview_audio_codec(first_path, settings["audio_track"])
                # Only probed when downmix is actually checked -- same
                # "don't pay for it unless it matters" reasoning as
                # build_args' own internal probe_audio_channels call, and
                # needed here so the preview correctly shows no -ac 2 for a
                # source that doesn't actually have more than 2 channels,
                # not just always assume it does.
                audio_channels = (
                    self._preview_audio_channels(first_path, settings["audio_track"])
                    if settings.get("audio_downmix_stereo") else None
                )
                args = worker.build_args(
                    settings, first_path, output_path,
                    probe_audio=False, audio_codec=audio_codec, audio_channels=audio_channels,
                    duration_seconds=self._preview_duration(first_path),
                )
            else:
                # No file queued yet -- there's no real audio track or
                # duration to reflect, so omit the audio codec decision
                # entirely (duration_seconds is left unset -- build_args
                # only probes it, against a placeholder path that can't
                # resolve to anything, if a bitrate-family mode needs it)
                # rather than assert values that would misrepresent what
                # actually happens.
                args = worker.build_args(
                    settings, Path("input.ext"), output_path,
                    probe_audio=False, audio_codec=None,
                )
            self._last_preview_args = args
            self.command_preview.setPlainText(self._format_preview_text(args))
        except Exception as exc:
            # build_args can hit real hardware (find_render_node) for the
            # VAAPI path -- on a machine with no Intel node this must degrade
            # to a message, not crash the control that triggered it.
            self._last_preview_args = []
            self.command_preview.setPlainText(f"(preview unavailable: {exc})")
        self._update_size_estimate_label(settings)
        self._update_preset_modified_indicator()

    # Args starting a new logical group: input, video encode, stream
    # mapping, container/finalization. Purely a display grouping -- the
    # actual argv passed to ffmpeg is unaffected.
    _PREVIEW_BREAK_BEFORE = {"-i", "-vf", "-map", "-sn"}

    def _format_preview_text(self, args: list[str]) -> str:
        lines, current = [], []
        for arg in args:
            if arg in self._PREVIEW_BREAK_BEFORE and current:
                lines.append(" ".join(current))
                current = []
            current.append(arg)
        if current:
            lines.append(" ".join(current))
        return "\n".join(lines)

    def _preview_audio_codec(self, path: Path, track_index: int) -> str | None:
        key = (path, track_index)
        if key not in self._preview_audio_cache:
            self._preview_audio_cache[key] = worker.probe_audio_codec(path, track_index)
        return self._preview_audio_cache[key]

    def _preview_audio_channels(self, path: Path, track_index: int) -> int | None:
        key = (path, track_index)
        if key not in self._preview_audio_channels_cache:
            self._preview_audio_channels_cache[key] = worker.probe_audio_channels(path, track_index)
        return self._preview_audio_channels_cache[key]

    def _preview_duration(self, path: Path) -> float:
        if path not in self._preview_duration_cache:
            self._preview_duration_cache[path] = worker.probe_duration(path)
        return self._preview_duration_cache[path]

    def _update_size_estimate_label(self, settings: dict):
        if not hasattr(self, "size_estimate_label") or settings["rc_mode"] not in BITRATE_RC_MODES:
            return
        if self.queue_list.topLevelItemCount() == 0:
            self.size_estimate_label.setText("Add a file to estimate the resulting bitrate")
            return
        first_path = self.queue_list.topLevelItem(0).data(STATUS_COL, Qt.UserRole)["path"]
        try:
            # Unguarded before -- confirmed a probe failure here (a stalled
            # network mount, ffprobe genuinely missing) would raise straight
            # out of _update_command_preview uncaught, unlike the sibling
            # build_args() call just above it in that method, which already
            # degrades to a message instead of propagating. Every keystroke
            # while a bitrate-family rc_mode is active with this file queued
            # would then hit the same uncaught exception again.
            duration = self._preview_duration(first_path)
            audio_codec = self._preview_audio_codec(first_path, settings["audio_track"])
        except Exception as exc:
            self.size_estimate_label.setText(f"(estimate unavailable: {exc})")
            return
        if duration <= 0:
            self.size_estimate_label.setText("Couldn't read this file's duration to estimate bitrate")
            return
        reserved_audio_kbps = worker.audio_bitrate_kbps(settings["audio_bitrate"]) if audio_codec is not None else 0
        video_kbps = worker.target_size_to_bitrate_kbps(settings["quality_value"], duration, reserved_audio_kbps)
        if video_kbps <= 0:
            # Same threshold build_args() itself now refuses to encode
            # against (raises rather than silently emitting "-b:v 0k",
            # which confirmed directly just makes libx265 fall back to its
            # own default CRF instead of erroring) -- this label should
            # say so before the user ever gets that far, not just describe
            # a number that Start would then refuse to act on anyway.
            self.size_estimate_label.setText(
                "Target size is too small for this file's length and audio settings"
            )
            return
        self.size_estimate_label.setText(f"≈ {video_kbps:,} kbps video for this file's length (estimate)")

    def _update_preset_modified_indicator(self):
        # A dynamic property + QSS[modified="true"] recoloring the combo's
        # own text, not a separate "(modified)" label -- that label's
        # appearing/disappearing changed the preset row's width and visibly
        # reflowed the window every time a control was touched, confirmed
        # by screenshot. Recoloring in place needs no space of its own.
        if not hasattr(self, "preset_combo"):
            return
        modified = (
            self._loaded_preset_settings is not None
            and formatting.settings_differ(self._current_settings(), self._loaded_preset_settings)
        )
        self.preset_combo.setProperty("modified", modified)
        self.preset_combo.style().unpolish(self.preset_combo)
        self.preset_combo.style().polish(self.preset_combo)

    # --- settings <-> controls ---
    def _current_settings(self) -> dict:
        encoder = self._current_encoder_id()
        rc_mode = self.rc_mode_combo.currentData()
        is_bitrate = rc_mode in BITRATE_RC_MODES
        res = RESOLUTIONS[self.res_combo.currentIndex()]
        return {
            "encoder": encoder,
            "gpu_vendor": self._current_gpu_vendor(),
            "rc_mode": rc_mode,
            "quality_value": self.size_spin.value() if is_bitrate else self.quality_slider.value(),
            "speed": X265_PRESETS[self.speed_x265_slider.value()] if encoder == "libx265" else str(self.speed_slider.value()),
            "bit_depth": self.bitdepth_combo.currentData(),
            "width": res["width"],
            "height": res["height"],
            "container": self.container_combo.currentText(),
            "tune": self.tune_combo.currentText(),
            "deinterlace": self.deinterlace_check.isChecked(),
            "audio_track": self.audio_combo.currentIndex(),
            "audio_copy_if_compatible": self.audio_copy_check.isChecked(),
            "audio_bitrate": AUDIO_BITRATES[self.audio_bitrate_slider.value()],
            "audio_downmix_stereo": self.audio_downmix_check.isChecked(),
        }

    def _apply_settings_to_controls(self, settings: dict):
        # .get("gpu_vendor", "intel"): presets/queue jobs saved before this
        # key existed only ever meant the Intel path (it was the only VAAPI
        # option then), so that's the correct default for anything missing it.
        wanted_vendor = settings.get("gpu_vendor", "intel") if settings["encoder"] == "hevc_vaapi" else None
        encoder_index = next(
            (i for i, (enc, vendor, _) in enumerate(ENCODERS)
             if enc == settings["encoder"] and vendor == wanted_vendor), 0
        )
        self.encoder_combo.setCurrentIndex(encoder_index)  # cascades rc_mode/speed/tune rebuild

        rc_index = next(
            (i for i in range(self.rc_mode_combo.count())
             if self.rc_mode_combo.itemData(i) == settings["rc_mode"]), 0
        )
        self.rc_mode_combo.setCurrentIndex(rc_index)  # cascades quality/bitrate widget swap

        if settings["rc_mode"] in BITRATE_RC_MODES:
            self.size_spin.setValue(settings["quality_value"])
        else:
            self.quality_slider.setValue(settings["quality_value"])

        if settings["encoder"] == "libx265":
            # Defensive fallback, same reasoning as audio_bitrate above --
            # X265_PRESETS is a fixed list in this app, but a hand-edited
            # presets.json could still carry a value that's not in it.
            speed = settings["speed"] if settings["speed"] in X265_PRESETS else "medium"
            self.speed_x265_slider.setValue(X265_PRESETS.index(speed))
        else:
            self.speed_slider.setValue(int(settings["speed"]))

        bitdepth_index = self.bitdepth_combo.findData(settings["bit_depth"])
        if bitdepth_index >= 0:
            self.bitdepth_combo.setCurrentIndex(bitdepth_index)

        res_index = next(
            (i for i, r in enumerate(RESOLUTIONS)
             if r["width"] == settings["width"] and r["height"] == settings["height"]), 0
        )
        self.res_combo.setCurrentIndex(res_index)
        self.container_combo.setCurrentText(settings.get("container", "mp4"))
        self.tune_combo.setCurrentText(settings.get("tune", "None"))
        self.deinterlace_check.setChecked(settings.get("deinterlace", False))
        self.audio_combo.setCurrentIndex(settings["audio_track"])
        self.audio_copy_check.setChecked(settings["audio_copy_if_compatible"])
        # Defensive fallback, same reasoning as bit_depth/resolution above --
        # a hand-edited presets.json could carry a bitrate string that's no
        # longer (or never was) one of the five real stops, and .index()
        # crashes on that where the old combo's setCurrentText() wouldn't have.
        audio_bitrate = settings["audio_bitrate"] if settings["audio_bitrate"] in AUDIO_BITRATES else "160k"
        self.audio_bitrate_slider.setValue(AUDIO_BITRATES.index(audio_bitrate))
        # .get, not a bare index -- predates every other new-field fallback
        # above it, an older saved preset (user or, briefly, a stale
        # built-in during dev) simply won't have this key at all.
        self.audio_downmix_check.setChecked(settings.get("audio_downmix_stereo", False))
        self._update_command_preview()

    # --- preset management ---
    def _all_presets(self) -> list[dict]:
        return self.presets

    def _refresh_preset_combo(self, select: str | None = None):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for p in self._all_presets():
            self.preset_combo.addItem(p["name"])
        self.preset_combo.blockSignals(False)
        if select is not None:
            idx = self.preset_combo.findText(select)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        if self.preset_combo.count() and select is None:
            self._on_preset_selected()

    def _on_preset_selected(self):
        name = self.preset_combo.currentText()
        self.preset_combo.setToolTip(name)
        settings = next((p for p in self._all_presets() if p["name"] == name), None)
        if settings:
            # Set before applying: _apply_settings_to_controls cascades through
            # several _update_command_preview() calls as it sets each control,
            # and each of those checks the modified indicator against this.
            self._loaded_preset_settings = {k: v for k, v in settings.items() if k != "name"}
            self._apply_settings_to_controls(settings)

    def _save_preset_as(self):
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        name = name.strip()
        if not ok or not name:
            return
        if name in BUILTIN_PRESET_NAMES:
            QMessageBox.warning(self, "Reserved name", "That name is a built-in preset. Choose a different name.")
            return
        existing = next((p for p in self.presets if p["name"] == name), None)
        if existing and QMessageBox.question(
            self, "Overwrite?", f'A preset named "{name}" already exists. Overwrite it?'
        ) != QMessageBox.Yes:
            return
        settings = self._current_settings()
        settings["name"] = name
        if existing:
            self.presets[self.presets.index(existing)] = settings
        else:
            # Appended, not inserted -- new presets land at the bottom of
            # presets.json, after the 9 built-ins, in save order.
            self.presets.append(settings)
        save_presets(self.presets)
        self._loaded_preset_settings = {k: v for k, v in settings.items() if k != "name"}
        self._refresh_preset_combo(select=name)
        self._update_preset_modified_indicator()

    def _delete_preset(self):
        name = self.preset_combo.currentText()
        if name in BUILTIN_PRESET_NAMES:
            QMessageBox.warning(self, "Can't delete", "Built-in presets can't be deleted.")
            return
        existing = next((p for p in self.presets if p["name"] == name), None)
        if not existing:
            return
        if QMessageBox.question(self, "Delete preset", f'Delete "{name}"?') != QMessageBox.Yes:
            return
        self.presets.remove(existing)
        save_presets(self.presets)
        self._refresh_preset_combo()


def main():
    app = QApplication(sys.argv)
    # Fusion is the style QSS was written against -- native styles (Breeze,
    # Windows) silently ignore some of the subcontrols the theme relies on,
    # e.g. the slider groove/handle and the combobox popup background.
    app.setStyle("Fusion")
    # Kept as an attribute on app, not a bare local -- installEventFilter
    # doesn't take Python-side ownership, and a filter with no surviving
    # Python reference is liable to get garbage-collected out from under
    # the C++ side and silently stop firing.
    app._combo_popup_filter = _ComboPopupBackgroundFilter()
    app.installEventFilter(app._combo_popup_filter)
    app._focus_visible_filter = _FocusVisibleFilter()
    app.installEventFilter(app._focus_visible_filter)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
