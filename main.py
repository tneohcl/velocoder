#!/usr/bin/env python3
"""VeloCoder: simplified, consumer-facing fork of TITAN-i Transcoder
(/mnt/data/tools/transcoder) -- same ffmpeg engine and settings model,
progressive-disclosure UI (Normal controls always visible, full technical
control set tucked behind an Expert section) instead of exposing everything
at once. See that sibling app's own docstring/README for the shared
backend's history; this file only diverges where the UI layer does."""
import shlex
import sys
import weakref
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QProcess, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QFormLayout, QMainWindow, QTreeWidgetItem, QLabel,
    QMessageBox, QVBoxLayout,
)

import worker
import formatting
from constants import (
    ENCODERS, RC_MODES, RC_MODE_FRIENDLY, QUALITY_TIERS,
    encoder_profile_key, QUALITY_RANGES, X265_PRESETS, X265_TUNES, X264_TUNES, RESOLUTIONS,
    AUDIO_BITRATES, AUDIO_TRACK_LABELS,
)
from worker import TranscodeQueue, BITRATE_RC_MODES
from queue_widget import (
    VIDEO_COL, DURATION_COL, SIZE_COL,
    RESULT_COL, STATUS_COL, AUDIO_TRACK_COUNT_ROLE, QUEUE_COLUMN_HEADERS,
)
from theming import (
    _current_theme_palette, _system_accent_tokens, _load_stylesheet,
    _ComboPopupBackgroundFilter, _FocusVisibleFilter, _ComboWheelBlockFilter,
    _validate_theme_choice, _resolve_theme, _fuzzy_text_color,
)
from ui_builder import _UiBuilderMixin
from queue_controller import _QueueControllerMixin

# The app's one fixed starting point now that there's no Presets UI to
# choose one from -- see __init__'s startup-default block. Was "720p CPU
# Balanced (Software / x265)" (a real built-in preset, chosen via the
# Presets dropdown) in the specialist build; ported here as a plain dict
# literal, not a lookup, since there's no longer a preset list to look it
# up in. Values match that preset exactly except width/height (overridden
# to "Keep Original" immediately below, same as the specialist build
# already did) and encoder/gpu_vendor (overridden to whatever
# worker.best_available_engine() picks on this machine, same as
# "Automatic" already did on click there).
DEFAULT_SETTINGS = {
    "encoder": "libx265",
    "rc_mode": "CRF",
    "quality_value": 23,
    "speed": "medium",
    "bit_depth": 10,
    "width": 1280,
    "height": 720,
    "container": "mp4",
    "tune": "None",
    "deinterlace": False,
    "audio_track": 0,
    "audio_copy_if_compatible": True,
    "audio_bitrate": "160k",
    "audio_downmix_stereo": False,
}


class MainWindow(QMainWindow, _UiBuilderMixin, _QueueControllerMixin):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VeloCoder")
        self.resize(1240, 820)
        # Left panel is a fixed 470px inspector now (ui_builder.py's
        # _build_ui), not a resizable pane -- narrowing the window has
        # nowhere left to take width from except Videos, and a saved
        # window_geometry from before this change (or just a user dragging
        # the window edge) could otherwise crush it well past usable.
        # 960 = 470 (settings) + ~490 (enough for the queue's Video/
        # Duration/Size/Status columns plus Save-to/Change/Open without
        # feeling cramped) -- the normal 1240 default still has plenty of
        # room above this floor.
        self.setMinimumWidth(960)

        self._res_label = {(r["width"], r["height"]): r["label"] for r in RESOLUTIONS}
        self._preview_audio_cache: dict[tuple, str | None] = {}
        self._preview_audio_channels_cache: dict[tuple, int | None] = {}
        self._preview_audio_source_bitrate_cache: dict[tuple, int | None] = {}
        self._preview_duration_cache: dict[Path, float] = {}
        self._last_preview_args: list[str] = []
        self._running_items: list[QTreeWidgetItem] = []
        self._current_running_item: QTreeWidgetItem | None = None
        # Whole-run totals, reset in _begin_conversion -- nothing tracked
        # these in aggregate before this pass; _append_result_size only
        # ever computed per-job byte counts and discarded them. Read by
        # _on_all_finished (via formatting.format_run_summary) to decide
        # whether a finished-run summary has anything real to show.
        self._run_total_input_bytes = 0
        self._run_total_output_bytes = 0
        self._run_completed_count = 0
        # Also reset in _begin_conversion, also read by _on_all_finished --
        # distinguishes "everything succeeded" from "some failed" from "the
        # user cancelled partway through", all of which _run_completed_count
        # alone can't tell apart (it's just a count of real successes).
        # "Conversion Complete" implying a fully successful run when it was
        # actually cancelled after 2 of 4 -- confirmed a real, misleading
        # gap this pass otherwise left behind.
        self._run_failed_count = 0
        self._run_cancelled = False
        self._syncing_controls_from_selection = False
        self._queue_editable = True
        # True between "Pause after this file" taking effect (_on_paused)
        # and either Resume or Stop -- lets start_btn's one click handler
        # (_start) tell "fresh run" and "continue a paused one" apart
        # without needing a second button.
        self._queue_paused = False
        # The status_label text before _set_status (queue_controller.py)
        # applies "Stop After Current Video"'s own suffix, if that's
        # currently checked -- see _set_status's own comment.
        self._base_status_text = "Idle"
        # True from the moment Convert is clicked while files are still
        # being analyzed until conversion actually begins -- lets
        # _maybe_begin_ready_conversion (queue_controller.py) start the
        # real run automatically the instant the last outstanding probe
        # lands, instead of making the user click Convert a second time.
        # See _start()'s own comment for why this matters.
        self._start_when_ready = False
        # Counts remaining outstanding probes (interlace detection + source
        # probe, starts at 2) for a file added while a run is already in
        # progress -- see add_files/_maybe_submit_mid_run_job in
        # queue_controller.py. Only ever populated for a genuinely mid-run
        # add; an item added while idle is never in here.
        self._pending_mid_run_items: dict[QTreeWidgetItem, int] = {}
        # Same shape and same "starts at 2, counts down as each probe's
        # own callback finishes applying its result" mechanics as
        # _pending_mid_run_items above, but populated for every added
        # file regardless of run state -- purely for the "Preparing --
        # analyzing N video(s)…" message (_start/_maybe_begin_ready_
        # conversion in queue_controller.py) to count actual videos, not
        # raw QProcess objects. len(_detection_processes) counts two
        # processes per video (interlace + source probe), so it reported
        # "analyzing 2 file(s)" for a single added video -- confirmed a
        # real, reported bug, not just imprecise wording.
        self._pending_analysis_items: dict[QTreeWidgetItem, int] = {}
        # Queue-structure undo/redo (add/remove/clear/reorder) -- see
        # _push_undo_snapshot/_undo/_redo in queue_controller.py. Each
        # entry is a full snapshot (list of job dicts), not a diff.
        self._undo_stack: list[list[dict]] = []
        self._redo_stack: list[list[dict]] = []
        self._detection_processes: list[QProcess] = []  # keep references alive; Qt won't
        self.output_dir = Path.home() / "Videos" / "transcoded"
        # Deliberately a different (org, app) pair than the sibling
        # TITAN-i Transcoder app ("TITAN-i", "Transcoder") -- QSettings
        # resolves its backing file purely from this pair, so sharing it
        # would mean the two apps clobbered each other's window geometry,
        # theme, and expanded-section state every time either one closed.
        self._qsettings = QSettings("VeloCoder", "VeloCoder")

        # "system" (not "dark") -- this fork's whole premise is following
        # OS/Mac-style conventions by default rather than an app-specific
        # choice; a first-time user should see whatever their desktop's
        # own light/dark preference already is, not this app's opinion.
        self._theme_choice = _validate_theme_choice(self._qsettings.value("theme_choice", "system"))
        _load_stylesheet(QApplication.instance(), _resolve_theme(self._theme_choice))
        # "System" needs to react live, not just at launch -- confirmed this
        # signal actually exists and fires on this Qt/PySide6 version before
        # relying on it (see the git history for the real check). Connected
        # via a weak reference, not self._on_system_theme_changed directly --
        # QStyleHints is owned by QApplication and outlives every MainWindow,
        # so a direct bound-method connection would keep each MainWindow (and
        # its entire widget tree) alive for the rest of the process even
        # after the window closes: nothing else ever disconnects it, and nothing
        # about closing a window makes Qt/PySide walk a persistent object's
        # connection list looking for dead receivers. Invisible in real use
        # (one MainWindow for the process's whole lifetime), but confirmed
        # directly as a real leak -- a plain loop constructing and dropping
        # MainWindow() with the direct connection showed construction time
        # and RSS both growing without bound (20s and 225MB by the 28th
        # instance, each previous instance still fully alive) purely from
        # _load_stylesheet's app.setStyleSheet() having to re-polish every
        # accumulated leaked widget on every subsequent call.
        weak_on_theme_changed = weakref.WeakMethod(self._on_system_theme_changed)

        def _relay_theme_changed(scheme, _ref=weak_on_theme_changed):
            method = _ref()
            if method is not None:
                method(scheme)

        QApplication.instance().styleHints().colorSchemeChanged.connect(_relay_theme_changed)

        self.queue = TranscodeQueue()
        self.queue.job_started.connect(self._on_job_started)
        self.queue.job_progress.connect(self._on_job_progress)
        self.queue.job_stats.connect(self._on_job_stats)
        self.queue.job_log.connect(self._on_job_log)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.job_failed.connect(self._on_job_failed)
        self.queue.all_finished.connect(self._on_all_finished)
        self.queue.paused.connect(self._on_paused)

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
        # Must run before _apply_settings_to_controls() below: it's the
        # only thing that populates rc_mode_combo, and applying settings
        # while that combo is still empty leaves rc_mode reading back as
        # None.
        self._on_encoder_changed()
        # No Presets UI to choose a starting point from anymore -- applies
        # this app's one fixed default (DEFAULT_SETTINGS above) directly.
        self._apply_settings_to_controls(DEFAULT_SETTINGS)
        # Overrides just the two fields DEFAULT_SETTINGS fixes to a value
        # that's a poor default for this fork specifically: Resolution
        # (already "Keep Original" in ui_builder.py, but a belt-and-
        # suspenders override here too) and encoder/gpu_vendor, which
        # DEFAULT_SETTINGS hardcodes to CPU/libx265 -- this picks whatever
        # worker.best_available_engine() finds on the machine actually
        # running the app instead, same as clicking Processing's own
        # Automatic button does, just run once at startup instead of
        # waiting for one.
        self.res_combo.setCurrentIndex(0)  # Keep Original
        self._on_processing_choice("automatic")
        # Before _restore_window_state(): Expert is still guaranteed
        # collapsed here (construction-time default), which is exactly
        # the height _pin_left_panel_min_height needs to read. Connecting
        # _on_expert_toggled before that restore call is deliberate too --
        # if a previous session left Expert expanded, restoring that
        # state below re-checks the box and should trigger the same
        # grow-to-fit behavior a live click does, not a silent exception.
        self._pin_left_panel_min_height()
        # None means "nothing to undo" -- either Expert has never been
        # expanded yet, or it was expanded without needing a resize (the
        # window was already tall enough), so a later collapse must
        # leave the window's height alone either way.
        self._expert_pre_expand_height = None
        self.video_expert_group.toggled.connect(self._on_expert_toggled)
        self._restore_window_state()
        # Establishes correct starting visibility (progress_bar/eta_label/
        # stats_label/stop_btn/open_folder_btn hidden while idle) -- one
        # call through the same function every later phase transition uses,
        # rather than hand-setting setVisible(False) per-widget in
        # ui_builder.py at construction time.
        self._apply_run_phase_visuals("idle")
        self._maybe_note_no_hardware()
        self._update_settings_scope_label()
        # Only ever read while something is selected (_sync_settings_to_
        # selected_queue_items) -- this initial value is never actually
        # consulted before _on_queue_selection_changed sets a real one,
        # but every control already has its real starting value by this
        # point in __init__, so there's no reason to leave it unset.
        self._last_synced_settings = self._current_settings()

    def _maybe_note_no_hardware(self):
        # Silent when hardware acceleration is available -- Automatic
        # Processing already just works, nobody needs ambient reassurance
        # a render node exists (the old persistent footer said so
        # regardless, reported as the most generic-utility-feeling part
        # of the window). Only speaks up in the one case that actually
        # matters to the user: no hardware found at all, so Processing
        # will always resolve to CPU regardless of which engine button is
        # picked -- worth knowing once, not worth a permanent status line.
        engine, _vendor = worker.best_available_engine()
        if engine != "hevc_vaapi":
            self._set_status("No hardware acceleration detected — using CPU")

    def closeEvent(self, event):
        self._qsettings.setValue("window_geometry", self.saveGeometry())
        self._qsettings.setValue("video_expert_expanded", self.video_expert_group.isChecked())
        super().closeEvent(event)

    def _pin_left_panel_min_height(self):
        # Reported live: the floor is always Expert's *collapsed* height,
        # not whichever height the panel currently needs -- expanding
        # Expert should never make the window impossible to shrink back
        # down again afterward (falls back to the scrolling that already
        # existed if the user does shrink it while Expert is still
        # expanded). Called once, right after _build_ui() and before
        # _restore_window_state() -- Expert is still guaranteed collapsed
        # at that point (its own construction-time default), so this
        # reads the right value regardless of what gets restored right
        # after. Setting the scroll area's own minimum height is enough --
        # central's QHBoxLayout won't let a fixed-width sibling be
        # squeezed shorter than its minimum, so this propagates up to the
        # window's own effective minimum automatically, no separate
        # self.setMinimumHeight() needed.
        self._left_panel_scroll.setMinimumHeight(self._left_panel_content.sizeHint().height())

    def _on_expert_toggled(self, checked):
        # Deferred a full event-loop turn (QTimer.singleShot, delay 0):
        # sizeHint() read synchronously inside this handler still
        # reflects the pre-toggle layout every time, confirmed directly
        # -- content.setVisible() inside _make_collapsible_group's own
        # _toggle marks the layout dirty, but Qt only recomputes it
        # lazily once the event loop actually runs, not synchronously
        # within the same call stack as the toggled signal that
        # triggered it.
        QTimer.singleShot(0, lambda: self._fit_window_to_left_panel(checked))

    def _fit_window_to_left_panel(self, expanded):
        # Never touches a maximized/full-screen window -- resizing one
        # of those doesn't mean what it means for a normal window (Qt
        # either ignores it or silently un-maximizes first), and neither
        # state has any real gap or overflow to fix in the first place.
        if self.isMaximized() or self.isFullScreen():
            return
        if expanded:
            needed = self._left_panel_content.sizeHint().height()
            shortfall = needed - self._left_panel_scroll.height()
            # Reported live: only ever reverse a resize VeloCoder made on
            # Expert's own behalf, never a size the user chose -- growing
            # is remembered (pre-expand height, and the exact height
            # grown *to*) only when a real shortfall actually forced a
            # resize here; already having enough room (the user had
            # already made the window tall, or a restored geometry
            # already fit) leaves nothing to undo later, so collapse
            # below must never touch the window in that case.
            if shortfall > 0:
                self._expert_pre_expand_height = self.height()
                self.resize(self.width(), self.height() + shortfall)
                self._expert_auto_grown_height = self.height()
            else:
                self._expert_pre_expand_height = None
            return
        # Collapsing: restore the exact pre-expand height, but only if
        # the window is still at the exact height this class grew it to
        # -- if the user resized it at all in the meantime (even while
        # Expert was still open), that's their own deliberate choice now,
        # not leftover auto-grow to clean up, so it must be left alone.
        if (
            self._expert_pre_expand_height is not None
            and self.height() == self._expert_auto_grown_height
        ):
            self.resize(self.width(), self._expert_pre_expand_height)
            self._expert_pre_expand_height = None

    def _restore_window_state(self):
        geometry = self._qsettings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        # QSettings round-trips bool through its backing store as the string
        # "true"/"false" on some platforms -- str(...) != "false" rather than
        # a bare truthiness check, so a stored False doesn't come back truthy.
        video_expert_expanded = self._qsettings.value("video_expert_expanded")
        if video_expert_expanded is not None:
            self.video_expert_group.setChecked(str(video_expert_expanded) != "false")
        # No command_expanded/log_expanded/audio_expert_expanded/
        # preset_group_expanded here -- Effective Command, Log, Audio
        # Expert, and Presets don't exist as visible/collapsible sections
        # in this build at all (see ui_builder.py's own comments), so
        # there's no expanded/collapsed state left to persist for any of
        # them.

    def _apply_theme(self, choice: str):
        self._theme_choice = choice
        self._qsettings.setValue("theme_choice", choice)
        _load_stylesheet(QApplication.instance(), _resolve_theme(choice))
        self._refresh_fuzzy_caption_style()

    def _open_settings_dialog(self):
        # Lazily built once, reused on every subsequent open -- same
        # pattern as _show_log_window below. This used to build a fresh,
        # uncached QDialog on every call and reparent self.theme_combo
        # (ui_builder.py's _build_status_bar -- still that method name, no
        # longer builds an actual status bar) into it each time. That let
        # a live crash through: "RuntimeError: libshiboken: Internal C++
        # object (QComboBox) already deleted" on the addRow call, because
        # theme_combo is built with no parent at construction time
        # (ui_builder.py) and only ever gets a real C++ parent from
        # whichever dialog most recently reparented it in -- a fresh,
        # uncached dialog on every open meant that parent's own lifetime
        # was never tracked, and repeated reproduction attempts couldn't
        # pin down the exact GC timing that deleted it, so this removes
        # the pattern outright rather than chasing the trigger further.
        # Caching the dialog means theme_combo is reparented exactly once.
        if not hasattr(self, "_settings_dialog") or self._settings_dialog is None:
            self._settings_dialog = QDialog(self)
            self._settings_dialog.setWindowTitle("Settings")
            form = QFormLayout(self._settings_dialog)
            form.addRow("Appearance:", self.theme_combo)
        self._settings_dialog.exec()

    def _show_log_window(self):
        # Lazily built once, reused on every subsequent open (unlike
        # Settings above) -- this hosts self.log_view itself, reparented
        # in on first use, so the running conversion's own log output
        # (already streaming into log_view regardless of whether this
        # window has ever been opened) shows up immediately rather than
        # this dialog needing its own separate copy kept in sync.
        if not hasattr(self, "_log_window") or self._log_window is None:
            self._log_window = QDialog(self)
            self._log_window.setWindowTitle("Conversion Log")
            self._log_window.resize(700, 400)
            # Non-modal (not exec()) -- this stays open and updating
            # while the user keeps working in the main window, closer to
            # a diagnostics panel than a blocking dialog.
            layout = QVBoxLayout(self._log_window)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.log_view)
        self._log_window.show()
        self._log_window.raise_()
        self._log_window.activateWindow()

    def _on_system_theme_changed(self, _scheme):
        if self._theme_choice == "system":
            _load_stylesheet(QApplication.instance(), _resolve_theme("system"))
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
        for label in (
            self.quality_tier_label, self.speed_tier_label, self.audio_bitrate_tier_label,
            self.video_scope_label, self.audio_scope_label,
        ):
            self._apply_fuzzy_caption_style(label)

    def _update_settings_scope_label(self, selected=None):
        # Reported live: nothing distinguished "these controls are about
        # to become defaults for a newly-added video" from "these controls
        # are editing whatever's selected right now" -- both are real,
        # everyday states with identical-looking controls either way.
        # Takes the already-known selection when the caller has it
        # (_on_queue_selection_changed) rather than re-querying, since Qt
        # already handed it over there; falls back to a fresh query for
        # callers that don't (queue add/remove/clear -- selection itself
        # didn't necessarily change, but which videos exist to describe
        # might have).
        if selected is None:
            selected = self.queue_list.selectedItems()
        if not selected:
            text = "Settings for new videos"
        elif len(selected) == 1:
            job = selected[0].data(STATUS_COL, Qt.UserRole)
            text = f'Settings for "{job["path"].name}"'
        else:
            text = f"Settings for {len(selected)} selected videos"
        self.video_scope_label.setText(text)
        self.audio_scope_label.setText(text)

    def _themed_icon(self, name: str) -> QIcon:
        # Restored -- removed along with save_btn/delete_btn (its only
        # *direct* callers in this file) without checking queue_controller.py
        # first, where it's still genuinely needed for the queue row status
        # icons (▶/✓/⚠, _on_job_started/_finished/_failed). Confirmed via a
        # real test failure (AttributeError), not caught by grepping this
        # file alone.
        theme = _resolve_theme(self._theme_choice)
        return QIcon(str(Path(__file__).parent / "assets" / f"{name}_{theme}.svg"))

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
        # Resolves ENCODERS' engine choice and CODECS' codec choice into
        # the one real ffmpeg encoder id the rest of the app (RC_MODES,
        # build_args, ...) actually keys off of. Hardware ignores
        # codec_combo entirely -- there's no h264_vaapi wired up, VAAPI
        # is HEVC-only here (codec_combo is disabled + forced to H.265
        # whenever a hardware engine is selected, see _on_encoder_changed,
        # but this reads the engine directly rather than trusting that
        # the combo's disabled state was actually respected).
        engine = ENCODERS[self.encoder_combo.currentIndex()][0]
        if engine == "hevc_vaapi":
            return engine
        return self.codec_combo.currentData()

    def _current_gpu_vendor(self) -> str | None:
        return ENCODERS[self.encoder_combo.currentIndex()][1]

    def _current_encoder_key(self) -> str:
        """RC_MODES/RC_MODE_FRIENDLY lookup key -- encoder id alone isn't
        specific enough once two GPU vendors share "hevc_vaapi" but support
        different rc_modes (AMD's driver rejects ICQ outright)."""
        return encoder_profile_key(self._current_encoder_id(), self._current_gpu_vendor())

    def _on_encoder_changed(self):
        # Also called from _on_codec_changed below (not wired to codec_
        # combo.currentIndexChanged directly anymore) -- the resolved
        # encoder id depends on both encoder_combo and codec_combo (see
        # _current_encoder_id()), so a codec change needs this same full
        # cascade too, just wrapped with quality-tier preservation first.
        encoder = self._current_encoder_id()
        encoder_key = self._current_encoder_key()
        is_vaapi = encoder == "hevc_vaapi"

        # Codec (H.265/H.264) only means anything for the CPU engine --
        # no h264_vaapi wired up, hardware is HEVC-only here. Forced to
        # H.265 and disabled (not hidden -- "H.265 (HEVC)" is still the
        # real, correct answer, just not a choice anymore) whenever a
        # hardware engine is selected, matching what hevc_vaapi actually
        # runs. blockSignals: setCurrentIndex would otherwise recurse
        # back into this same method for a change it made itself, not a
        # real user action.
        self.codec_combo.setEnabled(not is_vaapi)
        if is_vaapi:
            self.codec_combo.blockSignals(True)
            self.codec_combo.setCurrentIndex(0)  # H.265 (HEVC)
            self.codec_combo.blockSignals(False)

        # rc_mode_combo is a real, directly visible Expert dropdown now
        # (ui_builder.py) -- RC_MODES[encoder_key] already only lists the
        # modes valid for this specific encoder (e.g. AMD has no ICQ),
        # so repopulating it is the whole story; there's no separate
        # Advanced-button visibility or segEnd-rounding quirk to manage
        # anymore now that Rate Control isn't a segmented row.
        self.rc_mode_combo.blockSignals(True)
        self.rc_mode_combo.clear()
        for value, label in RC_MODES[encoder_key]:
            self.rc_mode_combo.addItem(label, userData=value)
        self.rc_mode_combo.blockSignals(False)

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

        # x264 and x265 don't accept identical -tune values (x264 also
        # takes "film"/"stillimage", both confirmed rejected outright by
        # this build's libx265 -- see X264_TUNES/X265_TUNES in
        # constants.py) -- repopulated per encoder, same
        # blockSignals/clear/re-add pattern rc_mode_combo above uses, for
        # the same reason (clearing+adding one at a time would otherwise
        # fire currentIndexChanged repeatedly on a half-built list). Only
        # for a real software encoder -- skipped for VAAPI (build_args
        # ignores this value there anyway, and the row's hidden the line
        # above) rather than repopulating with a list that wouldn't mean
        # anything for whichever encoder is actually selected.
        #
        # Preserves the current selection by text when the new list still
        # has it (e.g. switching x265 -> x264 keeps "grain" selected);
        # falls back to "None" -- never silently to some other tune
        # picked by whatever index happened to land there -- when it
        # doesn't (e.g. x264 -> x265 with "film" selected, which x265
        # doesn't offer at all).
        if not is_vaapi:
            current_tune = self.tune_combo.currentText()
            new_tunes = X265_TUNES if encoder == "libx265" else X264_TUNES
            self.tune_combo.blockSignals(True)
            self.tune_combo.clear()
            self.tune_combo.addItems(new_tunes)
            self.tune_combo.setCurrentText(current_tune if current_tune in new_tunes else "None")
            self.tune_combo.blockSignals(False)

        # Repopulating above ran with signals blocked (clearing/adding items
        # one at a time would otherwise fire currentIndexChanged repeatedly
        # on a half-built list), so neither of its normal listeners ran --
        # both are called explicitly here instead. Order matters: the
        # buttons read rc_mode_combo's now-settled state, they don't drive it.
        self._on_rc_mode_changed()
        self._sync_mode_buttons_to_combo()

    def _on_codec_changed(self):
        # Real, reported bug: codec_combo used to wire straight to
        # _on_encoder_changed, which rebuilds rc_mode_combo from scratch
        # (clear() + re-add) -- that resets currentIndex to 0 regardless
        # of what was selected before, silently abandoning the user's
        # Mode/Quality-tier/Target-Size choice on every H.265<->H.264
        # switch (e.g. File Size 800 MB reverting to Quality Balanced).
        # Same capture-before/restore-after shape as _on_processing_choice
        # -- carry the *tier* across when the old value matched one
        # (libx265/libx264 share identical QUALITY_TIERS/RC_MODE_FRIENDLY
        # values, so this is normally a same-value round-trip, but the
        # combo rebuild still needs it re-applied); bitrate-family
        # selections (File Size's MB target) need no translation at all,
        # just re-application, since libx265/libx264 share the same
        # "bitrate" rc_mode and a plain MB number has no per-encoder scale.
        settings = self._current_settings()
        old_key = self._current_encoder_key()
        self._on_encoder_changed()
        new_key = self._current_encoder_key()
        if old_key != new_key and settings["rc_mode"] == RC_MODE_FRIENDLY[old_key]["quality"]:
            old_tier = next(
                (tier for tier, value in QUALITY_TIERS.get(old_key, {}).items()
                 if value == settings["quality_value"]), "balanced"
            )
            settings["rc_mode"] = RC_MODE_FRIENDLY[new_key]["quality"]
            settings["quality_value"] = QUALITY_TIERS[new_key][old_tier]
        self._apply_settings_to_controls(settings)

    def _on_rc_mode_changed(self):
        # rc_mode_combo is always populated by this point -- __init__ calls
        # _on_encoder_changed() (the only thing that populates it) before
        # anything that could apply a preset and reach this method.
        rc_mode = self.rc_mode_combo.currentData()
        is_bitrate = rc_mode in BITRATE_RC_MODES
        self.quality_slider.setVisible(not is_bitrate)
        self.quality_label.setVisible(not is_bitrate)
        self.quality_tier_label.setVisible(not is_bitrate)
        # Quality tier buttons vs. Target Size -- Normal-mode rows, not
        # individual widget visibility, since each is its own full
        # QFormLayout row in the Quality group now (ui_builder.py's
        # _build_quality_group) rather than two widgets sharing one row
        # the way quality_slider/size_spin used to in Expert.
        self.quality_form.setRowVisible(self._quality_tier_field, not is_bitrate)
        self.quality_form.setRowVisible(self._target_size_field, is_bitrate)
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

    def _sync_mode_buttons_to_combo(self):
        # Keeps Normal's Mode toggle correct no matter what actually
        # changed rc_mode_combo underneath -- a Mode click, an Expert
        # rc_mode_combo pick, an encoder switch repopulating it, or a
        # queue-selection load -- rather than scattering a sync call
        # across every one of those call sites. Was _sync_rc_buttons_to_
        # combo, syncing Expert's own Quality/File Size/Advanced buttons
        # too -- those are gone (ui_builder.py's Rate Control row now
        # shows rc_mode_combo directly instead of duplicating this same
        # Quality/File Size choice, per the final control-hierarchy
        # decision), so this is Mode-only now.
        value = self.rc_mode_combo.currentData()
        friendly = RC_MODE_FRIENDLY[self._current_encoder_key()]
        # Advanced (CQP) has no Normal-mode equivalent, so neither button
        # reads as selected then -- same "no exact match" handling as the
        # Quality tier buttons below.
        if value == friendly["quality"]:
            self.mode_quality_btn.setChecked(True)
        elif value == friendly["file_size"]:
            self.mode_filesize_btn.setChecked(True)
        else:
            self.mode_button_group.setExclusive(False)
            self.mode_quality_btn.setChecked(False)
            self.mode_filesize_btn.setChecked(False)
            self.mode_button_group.setExclusive(True)

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
    # Plain size/quality labels, not scenario prescriptions ("Dialogue /
    # older TV", "Concert film / archival master") -- reviewed directly:
    # this app's target user already understands bitrate, so telling them
    # what genre a number is "for" reads as presumptuous rather than
    # helpful, and "archival master" specifically overstates what 256kbps
    # AAC actually is. Same plain size <-> quality axis every other
    # slider caption in this app already uses (Quality's own "Smaller
    # File"/"Better Quality", Speed's "Faster"/"Slower").
    _AUDIO_BITRATE_DESCRIPTIONS = [
        "Smallest file",
        "Compact",
        "Balanced",
        "High quality",
        "Highest quality",
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
        self._sync_normal_video_controls()

    def _sync_normal_video_controls(self):
        """Keeps the Normal-mode Processing/Quality buttons correct no
        matter what actually changed the underlying state -- an Expert
        control, a queue-selection load, or one of this same row's own
        buttons -- same reasoning as _sync_mode_buttons_to_combo above,
        which this runs alongside (both reached via _on_control_changed,
        itself reachable from every path that can change encoder/
        rc_mode/quality_value). Compatibility button sync stays cut --
        Compatibility itself is gone (Codec is Normal-visible directly
        now), so there's nothing left to sync for it."""
        engine, vendor = self._current_encoder_id(), self._current_gpu_vendor()
        target_btn = None
        if engine == "hevc_vaapi" and vendor == "intel":
            target_btn = self.processing_intel_btn
        elif engine == "hevc_vaapi" and vendor == "amd":
            target_btn = self.processing_amd_btn
        else:
            target_btn = self.processing_cpu_btn
        if target_btn is not None:
            target_btn.setChecked(True)
        else:
            # A settings dict naming a vendor this machine's Processing
            # row never offered a button for at all (e.g. a preset saved
            # on a different machine) -- same "no exact match" shape as
            # the quality-tier handling just below, and the same fix:
            # leave the row showing nothing checked rather than falsely
            # implying CPU when the real settings say otherwise.
            self.processing_button_group.setExclusive(False)
            for btn in (self.processing_cpu_btn, self.processing_intel_btn, self.processing_amd_btn):
                if btn is not None:
                    btn.setChecked(False)
            self.processing_button_group.setExclusive(True)

        key = self._current_encoder_key()
        rc_mode = self.rc_mode_combo.currentData()
        matched_tier = None
        if rc_mode == RC_MODE_FRIENDLY[key]["quality"]:
            value = self.quality_slider.value()
            matched_tier = next(
                (tier for tier, tier_value in QUALITY_TIERS[key].items() if tier_value == value), None
            )
        tier_buttons = {
            "smaller": self.quality_smaller_btn,
            "balanced": self.quality_balanced_btn,
            "better": self.quality_better_btn,
        }
        if matched_tier is not None:
            tier_buttons[matched_tier].setChecked(True)
        else:
            # No exact match (e.g. the Expert quality slider was dragged
            # to some in-between value) -- none of the three should read
            # as selected. QButtonGroup's own exclusivity won't let a
            # single setChecked(False) leave the group with nothing
            # checked, so exclusivity is dropped just long enough to
            # clear all three.
            self.quality_tier_button_group.setExclusive(False)
            for btn in tier_buttons.values():
                btn.setChecked(False)
            self.quality_tier_button_group.setExclusive(True)

    def _on_processing_choice(self, choice: str):
        if choice == "automatic":
            engine, vendor = worker.best_available_engine()
        elif choice == "cpu":
            # Whatever Codec (H.265/H.264) already holds -- switching
            # engine back to CPU shouldn't silently change codec too.
            engine, vendor = self.codec_combo.currentData(), None
        elif choice == "intel":
            engine, vendor = "hevc_vaapi", "intel"
        else:
            engine, vendor = "hevc_vaapi", "amd"
        settings = self._current_settings()
        old_key = self._current_encoder_key()
        was_vaapi = settings["encoder"] == "hevc_vaapi"
        now_vaapi = engine == "hevc_vaapi"
        settings["encoder"] = engine
        settings["gpu_vendor"] = vendor
        if now_vaapi != was_vaapi:
            # Crossing the software/hardware boundary changes what
            # "speed" even means -- VAAPI stores it as a compression_level
            # string ("1".."7"), x264/x265 as a preset name ("medium" etc).
            # Same real crash already fixed once for _current_settings()/
            # _apply_settings_to_controls() themselves (mismatched speed
            # representation between the two families); hit again here
            # because this delta can cross that boundary in a single step,
            # which neither of those two ever does on its own. Reset to
            # each family's own middle-of-the-road default (AMD Balanced's
            # own "4" reasoning in constants.py) rather than attempting
            # some numeric translation between two unrelated scales.
            settings["speed"] = "4" if now_vaapi else "medium"

        new_key = encoder_profile_key(engine, vendor)
        if old_key != new_key and settings["rc_mode"] == RC_MODE_FRIENDLY[old_key]["quality"]:
            # Same problem one level down: a quality-family value only
            # means something within one engine's own scale (CRF 23 and
            # ICQ 23 aren't remotely the same quality) -- carrying the raw
            # number across a Processing switch left it matching none of
            # QUALITY_TIERS' three named tiers (confirmed live: switching
            # CPU's default CRF 23 to Intel left Quality showing no
            # selection at all). Carries the *tier* across instead, when
            # the old value matched one; falls back to "balanced" when it
            # didn't (e.g. after an Expert-mode slider drag). Bitrate-
            # family selections (File Size) are left untouched above --
            # a target output size doesn't need this kind of translation.
            old_tier = next(
                (tier for tier, value in QUALITY_TIERS.get(old_key, {}).items()
                 if value == settings["quality_value"]), "balanced"
            )
            settings["rc_mode"] = RC_MODE_FRIENDLY[new_key]["quality"]
            settings["quality_value"] = QUALITY_TIERS[new_key][old_tier]
        self._apply_settings_to_controls(settings)

    def _on_quality_tier_clicked(self, tier: str):
        key = self._current_encoder_key()
        settings = self._current_settings()
        settings["rc_mode"] = RC_MODE_FRIENDLY[key]["quality"]
        settings["quality_value"] = QUALITY_TIERS[key][tier]
        self._apply_settings_to_controls(settings)

    def _on_audio_handling_clicked(self, choice: str):
        settings = self._current_settings()
        settings["audio_copy_if_compatible"] = choice == "automatic"
        self._apply_settings_to_controls(settings)

    def _on_audio_channels_clicked(self, choice: str):
        settings = self._current_settings()
        settings["audio_downmix_stereo"] = choice == "stereo"
        self._apply_settings_to_controls(settings)

    def _sync_settings_to_selected_queue_items(self):
        # Selecting a queue item to inspect its settings (see
        # _on_queue_selection_changed) populates these same controls, which
        # would otherwise loop right back and stomp every other selected
        # item's settings with the first one's, just from clicking to select.
        if self._syncing_controls_from_selection or not self._queue_editable:
            return
        settings = self._current_settings()
        selected = self.queue_list.selectedItems()
        if not selected:
            return
        # Real, reported bug: this used to push the *entire* settings dict
        # onto every selected item on every single control change. Two
        # files selected together with genuinely different Codec/
        # Resolution/etc. (the panel only ever shows the first one's, see
        # _on_queue_selection_changed) -- nudging just the AAC Bitrate
        # slider silently overwrote the *other* file's Codec/Resolution
        # too, not only the one control actually touched. Only the keys
        # that actually changed since the panel last settled (either this
        # selection's own starting point, or the last time this ran) get
        # pushed now -- everything else about each selected item's own
        # settings is left exactly as it was. A handler that legitimately
        # changes several keys together as one decision (e.g. Quality
        # tier's rc_mode + quality_value pair) still carries all of them,
        # since all of them show up as changed here too.
        changed_keys = {
            key for key, value in settings.items()
            if self._last_synced_settings.get(key) != value
        }
        for item in selected:
            job = item.data(STATUS_COL, Qt.UserRole)
            for key in changed_keys:
                job[key] = settings[key]
            item.setData(STATUS_COL, Qt.UserRole, job)
            # Otherwise the tooltip goes stale the moment a selected row's
            # settings actually change -- still showing whatever was true
            # when the row was first added.
            tooltip = self._row_tooltip(job)
            for col in range(len(QUEUE_COLUMN_HEADERS)):
                item.setToolTip(col, tooltip)
        self._last_synced_settings = settings

    def _on_queue_selection_changed(self):
        selected = self.queue_list.selectedItems()
        self._update_settings_scope_label(selected)
        if not selected:
            return
        # Before _apply_settings_to_controls below -- Track's own choices
        # must already reflect the newly-selected file(s) by the time it
        # sets audio_combo's index, or a stored audio_track pointing past
        # a narrower list's end would silently fail to select anything.
        self._refresh_audio_track_choices()
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
        # Baseline for _sync_settings_to_selected_queue_items' own diff --
        # a control change from here on is only ever compared against
        # what the panel showed *for this selection*, not some earlier
        # selection's settings (which could easily differ in ways that
        # have nothing to do with anything the user actually touched).
        self._last_synced_settings = self._current_settings()

    def _refresh_audio_track_choices(self):
        """Limits Track's choices to what was actually source-probed for
        the selected queue row(s) -- picking a track index a file doesn't
        have used to silently produce audio-less output instead of an
        error (worker.py deliberately skips mapping a nonexistent audio
        track rather than failing the whole job). That gap mattered less
        while Track was Expert-only; now that it's a primary Normal
        control, offering an index that can't possibly work isn't
        acceptable. No selection, or nothing probed yet for any selected
        row, falls back to the full, unconstrained AUDIO_TRACK_LABELS --
        the same default this control has always started on. Multiple
        selected rows: only offers track indexes valid for *every*
        selected file (the safe intersection, via min()), not just the
        first one -- applying a shared Track choice to several files at
        once (same reasoning as _sync_settings_to_selected_queue_items)
        must never pick an index that fails on any of them."""
        selected = self.queue_list.selectedItems()
        known_counts = [
            count for item in selected
            if (count := item.data(VIDEO_COL, AUDIO_TRACK_COUNT_ROLE))
        ]
        max_tracks = min(known_counts) if known_counts else len(AUDIO_TRACK_LABELS)
        max_tracks = max(1, min(max_tracks, len(AUDIO_TRACK_LABELS)))
        if self.audio_combo.count() == max_tracks:
            return  # already showing the right list -- don't clobber the current selection for nothing
        current_index = self.audio_combo.currentIndex()
        self.audio_combo.blockSignals(True)
        self.audio_combo.clear()
        self.audio_combo.addItems(AUDIO_TRACK_LABELS[:max_tracks])
        # Clamp rather than reset to 0 -- switching selection between two
        # files that both have at least as many tracks as the current
        # pick shouldn't silently jump back to Track 1.
        self.audio_combo.setCurrentIndex(max(0, min(current_index, max_tracks - 1)))
        self.audio_combo.blockSignals(False)

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
        text = "\n".join(lines)
        # command_preview now wraps in place (ui_builder.py) instead of
        # scrolling horizontally. The -vf filter chain is one long
        # comma/colon-separated value with no spaces at all, so Qt's own
        # word-wrap has nowhere to break but mid-word without help. A
        # zero-width space is an invisible, legal wrap point -- inserted
        # after every comma, which is already how this value's own logical
        # stages (format=..., hwupload, scale_vaapi=...) are separated.
        return text.replace(",", ",​")

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

    def _preview_audio_source_bitrate(self, path: Path, track_index: int) -> int | None:
        key = (path, track_index)
        if key not in self._preview_audio_source_bitrate_cache:
            self._preview_audio_source_bitrate_cache[key] = worker.probe_audio_bitrate_kbps(path, track_index)
        return self._preview_audio_source_bitrate_cache[key]

    def _preview_duration(self, path: Path) -> float:
        if path not in self._preview_duration_cache:
            self._preview_duration_cache[path] = worker.probe_duration(path)
        return self._preview_duration_cache[path]

    def _update_size_estimate_label(self, settings: dict):
        if not hasattr(self, "size_estimate_label") or settings["rc_mode"] not in BITRATE_RC_MODES:
            return
        if self.queue_list.topLevelItemCount() == 0:
            self.size_estimate_label.setText("Add a video to estimate bitrate")
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
        except Exception:
            self.size_estimate_label.setText("(estimate unavailable)")
            return
        if duration <= 0:
            self.size_estimate_label.setText("Couldn't read this video's duration")
            return
        # Same will_copy_audio reasoning as worker.build_args (which this
        # label doesn't call directly, so the condition has to be
        # reproduced here) -- Automatic can copy an already-compatible
        # source through untouched, and that copied track's real bitrate
        # is the number to reserve, not the configured audio_bitrate;
        # reserving the configured figure for e.g. a copied 640kbps AC-3
        # track under-reserved by hundreds of kbps, letting this estimate
        # (and the real encode -- see build_args' own fix) understate the
        # actual output size. Only probed when it might actually matter
        # (audio exists, copy is even being considered) -- most jobs
        # never need this any more than build_args' own version does.
        preview_channels = (
            self._preview_audio_channels(first_path, settings["audio_track"])
            if settings.get("audio_downmix_stereo") else None
        )
        force_downmix = preview_channels is not None and preview_channels > 2
        will_copy_audio = (
            audio_codec is not None
            and settings["audio_copy_if_compatible"]
            and not force_downmix
            and audio_codec in ("aac", "ac3", "eac3")
        )
        if audio_codec is None:
            reserved_audio_kbps = 0
        elif will_copy_audio:
            source_kbps = self._preview_audio_source_bitrate(first_path, settings["audio_track"])
            reserved_audio_kbps = source_kbps if source_kbps is not None else worker.audio_bitrate_kbps(settings["audio_bitrate"])
        else:
            reserved_audio_kbps = worker.audio_bitrate_kbps(settings["audio_bitrate"])
        video_kbps = worker.target_size_to_bitrate_kbps(settings["quality_value"], duration, reserved_audio_kbps)
        if video_kbps <= 0:
            # Same threshold build_args() itself now refuses to encode
            # against (raises rather than silently emitting "-b:v 0k",
            # which confirmed directly just makes libx265 fall back to its
            # own default CRF instead of erroring) -- this label should
            # say so before the user ever gets that far, not just describe
            # a number that Start would then refuse to act on anyway.
            self.size_estimate_label.setText("Target size too small for this video")
            return
        self.size_estimate_label.setText(f"≈ {video_kbps:,} kbps video (estimate)")

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
            # != "hevc_vaapi", not == "libx265" -- libx264 also drives
            # speed_x265_slider (same ultrafast..placebo preset names,
            # confirmed shared in worker.py's build_args docstring),
            # not the VAAPI compression_level slider. Reading the wrong
            # one for libx264 was a real bug: it fell through to
            # speed_slider (str(int), an out-of-range compression_level
            # value the encoder never uses) instead.
            "speed": X265_PRESETS[self.speed_x265_slider.value()] if encoder != "hevc_vaapi" else str(self.speed_slider.value()),
            "bit_depth": self.bitdepth_combo.currentData(),
            "width": res["width"],
            "height": res["height"],
            "container": self.container_combo.currentText(),
            "tune": self.tune_combo.currentText(),
            "deinterlace": self.deinterlace_check.isChecked(),
            "audio_track": self.audio_combo.currentIndex(),
            "audio_copy_if_compatible": self.audio_handling_automatic_btn.isChecked(),
            "audio_bitrate": AUDIO_BITRATES[self.audio_bitrate_slider.value()],
            "audio_downmix_stereo": self.audio_channels_stereo_btn.isChecked(),
        }

    def _apply_settings_to_controls(self, settings: dict):
        # .get("gpu_vendor", "intel"): queue jobs saved before this key
        # existed only ever meant the Intel path (it was the only VAAPI
        # option then), so that's the correct default for anything missing it.
        is_vaapi_settings = settings["encoder"] == "hevc_vaapi"
        wanted_vendor = settings.get("gpu_vendor", "intel") if is_vaapi_settings else None
        # Matched by vendor alone, not enc == settings["encoder"] -- the
        # CPU row's own id in ENCODERS is just a placeholder now (see its
        # own comment in constants.py), not necessarily what
        # settings["encoder"] actually is (could be "libx264"), so an
        # exact-string match would never find the CPU row for an x264
        # preset/job. vendor is None only for the single CPU row, so
        # that's what actually identifies "the CPU engine" now.
        engine_index = next(
            (i for i, (_, vendor, _label) in enumerate(ENCODERS)
             if vendor == wanted_vendor), 0
        )
        self.encoder_combo.setCurrentIndex(engine_index)  # cascades rc_mode/speed/tune rebuild
        if not is_vaapi_settings:
            # codec_combo's own change also cascades the same rebuild
            # (ui_builder.py wires it to the same _on_encoder_changed) --
            # findData, not assuming an exact index, since CODECS' own
            # order isn't guaranteed to match settings["encoder"]'s value
            # positionally.
            codec_index = self.codec_combo.findData(settings["encoder"])
            if codec_index >= 0:
                self.codec_combo.setCurrentIndex(codec_index)

        rc_index = next(
            (i for i in range(self.rc_mode_combo.count())
             if self.rc_mode_combo.itemData(i) == settings["rc_mode"]), 0
        )
        self.rc_mode_combo.setCurrentIndex(rc_index)  # cascades quality/bitrate widget swap

        if settings["rc_mode"] in BITRATE_RC_MODES:
            self.size_spin.setValue(settings["quality_value"])
        else:
            self.quality_slider.setValue(settings["quality_value"])

        if settings["encoder"] != "hevc_vaapi":
            # != "hevc_vaapi", not == "libx265" -- same real bug, same fix
            # as _current_settings() above: libx264 also uses
            # speed_x265_slider, not the VAAPI one. Applying a libx264
            # preset used to fall through to int(settings["speed"]) here,
            # which crashes outright ("medium" isn't an int).
            #
            # Defensive fallback, same reasoning as audio_bitrate above --
            # X265_PRESETS is a fixed list in this app, but a queue job's
            # settings dict (line ~789's _apply_settings_to_controls(job)
            # call) can outlive a change to that list, carrying a value
            # that's no longer in it.
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
        # Clamped, not a bare index -- audio_combo's own item count can be
        # narrower than 4 now (_refresh_audio_track_choices), and a job's
        # stored audio_track could in principle point past that (a queue
        # selection change elsewhere already calls _refresh_audio_track_
        # choices first to avoid this in the common path, but this stays
        # defensive rather than assuming every call site does).
        self.audio_combo.setCurrentIndex(min(settings["audio_track"], self.audio_combo.count() - 1))
        if settings["audio_copy_if_compatible"]:
            self.audio_handling_automatic_btn.setChecked(True)
        else:
            self.audio_handling_convert_btn.setChecked(True)
        # Defensive fallback, same reasoning as bit_depth/resolution above --
        # a queue job's settings dict can carry a bitrate string that's no
        # longer (or never was) one of the five real stops, and .index()
        # crashes on that where the old combo's setCurrentText() wouldn't have.
        audio_bitrate = settings["audio_bitrate"] if settings["audio_bitrate"] in AUDIO_BITRATES else "160k"
        self.audio_bitrate_slider.setValue(AUDIO_BITRATES.index(audio_bitrate))
        # .get, not a bare index -- predates every other new-field fallback
        # above it, an older saved preset (user or, briefly, a stale
        # built-in during dev) simply won't have this key at all.
        if settings.get("audio_downmix_stereo", False):
            self.audio_channels_stereo_btn.setChecked(True)
        else:
            self.audio_channels_keep_btn.setChecked(True)
        self._update_command_preview()


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
    app._combo_wheel_block_filter = _ComboWheelBlockFilter()
    app.installEventFilter(app._combo_wheel_block_filter)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
