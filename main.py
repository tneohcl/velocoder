#!/usr/bin/env python3
"""TITAN-i Transcoder: minimal ffmpeg front-end replacing HandBrake QSV."""
import shlex
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSettings, QProcess, QEvent, QObject
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QSplitter, QGroupBox, QTreeWidget, QTreeWidgetItem,
    QPushButton, QComboBox, QLabel, QProgressBar, QPlainTextEdit, QFileDialog,
    QLineEdit, QSlider, QSpinBox, QCheckBox, QInputDialog, QMessageBox,
    QSizePolicy, QAbstractItemView, QButtonGroup,
)

import worker
import themes
from constants import (
    VIDEO_FILTER, AUDIO_TRACK_LABELS, ENCODERS, RC_MODES, RC_MODE_FRIENDLY,
    encoder_profile_key, QUALITY_RANGES, X265_PRESETS, RESOLUTIONS,
    AUDIO_BITRATES, CONTAINERS, X265_TUNES, BUILTIN_PRESETS, BUILTIN_PRESET_NAMES,
)
from presets import load_user_presets, save_user_presets
from worker import TranscodeQueue, BITRATE_RC_MODES

PANEL_MARGIN = 12
PANEL_SPACING = 10

THEME_CHOICES = [("dark", "Dark"), ("light", "Light"), ("system", "Match System")]

# Queue table columns -- source-file properties only (see DropTreeWidget);
# output settings live in the right-hand panel and apply live to whatever
# row is selected instead of being duplicated here. Resolution rides along
# in the Video cell ("H.264 1280x720") rather than getting its own column --
# the panel this table lives in isn't wide enough to give lots of columns
# room without squeezing File down to nothing (confirmed against this
# app's own real persisted window geometry, not just its fresh-install
# default).
FILE_COL, VIDEO_COL, DURATION_COL, AUDIO_COL, SIZE_COL, RESULT_COL = range(6)
# The run-status icon (play/done/failed) lives on the File cell itself --
# QTreeWidgetItem supports an icon and text on the same column
# simultaneously -- rather than a dedicated narrow column of its own. A
# separate status column started out at 24px, as unobtrusive as it could
# reasonably be, but an empty, unlabeled column with nothing in it (every
# row's icon is blank until a run actually starts) still read as a stray
# gap rather than a deliberate part of the design -- confirmed by
# feedback, not just a guess. STATUS_COL is kept as a name (rather than
# writing FILE_COL at every icon/tooltip call site below) purely so those
# call sites stay self-explanatory about *why* they're touching this
# column.
STATUS_COL = FILE_COL
QUEUE_COLUMN_HEADERS = ["File", "Video", "Duration", "Audio", "Size", "Result"]

_VIDEO_CODEC_LABELS = {
    "h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1",
    "mpeg2video": "MPEG-2", "mpeg4": "MPEG-4", "vc1": "VC-1", "prores": "ProRes",
}
_AUDIO_CODEC_LABELS = {
    "aac": "AAC", "ac3": "AC3", "eac3": "E-AC3", "dts": "DTS", "mp3": "MP3",
    "flac": "FLAC", "truehd": "TrueHD", "opus": "Opus", "vorbis": "Vorbis",
    "pcm_s16le": "PCM", "pcm_s24le": "PCM",
}
_AUDIO_CHANNEL_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}


class DropTreeWidget(QTreeWidget):
    """Flat (no hierarchy) QTreeWidget -- gives the queue a real multi-column
    grid with a header bar while keeping the same "one item per row, holding
    its job dict via Qt.UserRole" shape QListWidgetItem had, so drag-drop and
    row reordering carry over unchanged. Accepts files dragged in from a file
    manager, and also supports dragging its own rows to reorder the queue."""

    PLACEHOLDER_TEXT = "Drag video files here,\nor click “Add Files…”"

    def __init__(self, on_files_dropped, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self._on_files_dropped = on_files_dropped
        # Dropping a file in mid-run is fine (add_files pushes it straight
        # into the run in progress) and was never gated by this -- but
        # reordering *existing* rows mid-run is different: the running
        # queue captured its own execution-order snapshot at Start
        # (MainWindow._running_items / TranscodeQueue._jobs), which a
        # drag here doesn't touch. Confirmed this was a real gap: nothing
        # stopped a mid-run drag before, so the visible order could show
        # something other than what was actually executing, and status
        # icons (looked up by position) could land on the wrong row.
        # Blocking only the internal-move branch of dropEvent, not
        # dropEvent entirely, keeps external file drops working during a
        # run exactly as before.
        self.reorder_locked = False

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)  # internal row-reorder drag

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            self._on_files_dropped(paths)
        elif not self.reorder_locked:
            self._reorder_rows(event)
        else:
            event.ignore()

    def _reorder_rows(self, event):
        # QTreeWidget's own InternalMove drop handling is built for real
        # trees: dropping squarely on top of a row (rather than near its
        # top/bottom edge) reparents the dragged row as a CHILD of the
        # target instead of reordering siblings. This list is flat by
        # design (setRootIsDecorated(False), items never expanded), so
        # that child silently stops being drawn -- it reads as the
        # dragged file "disappearing". Reimplemented as a manual
        # top-level-only move so every drop, anywhere on a row, is a
        # sibling reorder and nothing ever becomes a child.
        selected = sorted(self.selectedItems(), key=self.indexOfTopLevelItem)
        if not selected:
            event.ignore()
            return

        pos = event.position().toPoint()
        target_item = self.itemAt(pos)
        if target_item is None or target_item in selected:
            insert_at = self.topLevelItemCount()
        else:
            insert_at = self.indexOfTopLevelItem(target_item)
            row_rect = self.visualItemRect(target_item)
            if pos.y() >= row_rect.center().y():
                insert_at += 1  # dropped on the lower half: insert after

        removed_before_target = sum(
            1 for it in selected if self.indexOfTopLevelItem(it) < insert_at
        )
        for it in selected:
            self.takeTopLevelItem(self.indexOfTopLevelItem(it))
        insert_at = max(0, min(insert_at - removed_before_target, self.topLevelItemCount()))
        for offset, it in enumerate(selected):
            self.insertTopLevelItem(insert_at + offset, it)
            it.setSelected(True)

        event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(self.palette().color(QPalette.PlaceholderText))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, self.PLACEHOLDER_TEXT)
            painter.end()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TITAN-i Transcoder")
        self.resize(1240, 820)

        self.user_presets: list[dict] = load_user_presets()
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
        # Explicit select=, not just "whatever's first" -- BUILTIN_PRESETS'
        # own order is CPU/Intel/AMD (matches the Encoder dropdown's display
        # order), but this app exists to get real hardware encoding working
        # again, so every launch should still land on Intel by default
        # regardless of where it sits in that list. Preset selection isn't
        # otherwise persisted across launches at all (unlike window
        # geometry/theme/etc.), so this runs on every single startup, not
        # just a first install.
        self._refresh_preset_combo(select="720p Intel Balanced (Hardware / VAAPI)")
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

    # --- UI construction ---
    def _build_ui(self):
        self._splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(self._splitter)
        self._splitter.addWidget(self._build_left_panel())
        self._splitter.addWidget(self._build_right_panel())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._build_status_bar()

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

    def _build_status_bar(self):
        # A qBittorrent-style footer strip: ambient, persistent, out of the
        # way of the actual controls. Theme picker lives here rather than a
        # menu bar -- a whole menu bar for one three-item setting was more
        # chrome than the setting warranted.
        #
        # QStatusBar has two genuinely different widget areas, not just a
        # single row: addWidget puts something on the left (also where a
        # showMessage() temporary message would appear -- it temporarily
        # hides addWidget widgets specifically, though nothing here calls
        # showMessage today) and addPermanentWidget puts something on the
        # right, immune to that. Theme goes left/addWidget, hardware status
        # stays right/addPermanentWidget -- deliberately different APIs, not
        # just visual left/right positioning of the same call.
        #
        # setContentsMargins, not a QSS padding rule -- QStatusBar manages
        # its own internal layout for addWidget/addPermanentWidget content,
        # which a stylesheet padding rule turned out not to reach at all
        # (tried it, confirmed by screenshot: zero visible difference).
        # Contents margins are a plain widget property, not something QSS
        # has to cooperate with, so they reliably do give the whole footer
        # strip some vertical breathing room instead of sitting flush
        # against its own top/bottom edge.
        self.statusBar().setContentsMargins(8, 4, 8, 4)
        theme_label = QLabel("Theme:")
        theme_label.setStyleSheet("font-size: 9pt;")
        self.statusBar().addWidget(theme_label)
        self.theme_combo = QComboBox()
        for value, label in THEME_CHOICES:
            self.theme_combo.addItem(label, userData=value)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self._theme_choice))
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_combo.currentData())
        )
        self.statusBar().addWidget(self.theme_combo)

        self.hw_status_label = QLabel(self._hardware_status_text())
        self.hw_status_label.setStyleSheet("font-size: 9pt;")
        self.statusBar().addPermanentWidget(self.hw_status_label)

    def _build_preset_row(self) -> QHBoxLayout:
        # Preset is the main lever -- it sets every other control at once --
        # so it sits above the tabs, not buried as one of them. Previously a
        # QToolBar, but that spans the full window and draws a separator
        # below it; a plain row scoped to the left column reads as part of
        # the settings panel instead of a distinct chrome region.
        row = QHBoxLayout()
        label = QLabel("Preset:")
        bold = QFont()
        bold.setBold(True)
        label.setFont(bold)
        row.addWidget(label)

        self.preset_combo = QComboBox()
        # Long preset names (both built-ins, and anything a user later saves)
        # were hard-clipping mid-character against the row's other widgets
        # with no ellipsis -- this floor is measured to comfortably fit the
        # longer built-in name; setToolTip in _on_preset_selected below is
        # the safety net for anything still longer than that.
        self.preset_combo.setMinimumWidth(300)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        row.addWidget(self.preset_combo, 1)

        # Custom icons, not style().standardIcon(...) -- Fusion's standard
        # icons are colored from the app's QPalette, which this app never
        # sets of its own (only this stylesheet), so they stayed locked to
        # whatever Fusion's default happens to be regardless of the chosen
        # theme -- confirmed by screenshot: SP_TrashIcon in particular was
        # all but invisible against a light-theme button. The queue row's
        # status icons (▶/✓/⚠, _on_job_started/_finished/_failed) used to
        # be standardIcon() too, mixing two icon styles in one app -- an
        # Apple-design-language pass's "one icon family throughout" moved
        # those onto the same custom-SVG family this file already used for
        # Save/Delete, not the other way around: reverting Save/Delete back
        # to standardIcon() would have reintroduced the confirmed contrast
        # bug above just to make the family "native" instead of consistent.
        # _refresh_themed_icons re-applies these on every theme change,
        # same reason the SVGs style.qss references have separate dark/
        # light files.
        self.save_btn = QPushButton(self._themed_icon("save"), "Save As…")
        self.save_btn.clicked.connect(self._save_preset_as)
        self.delete_btn = QPushButton(self._themed_icon("delete"), "Delete")
        self.delete_btn.clicked.connect(self._delete_preset)
        row.addWidget(self.save_btn)
        row.addWidget(self.delete_btn)
        return row

    def _apply_fuzzy_caption_style(self, label: QLabel):
        # Matches "Drag video files here..." (DropTreeWidget.paintEvent)
        # exactly, pulled from the same real QPalette role rather than a
        # guessed/hardcoded gray -- confirmed via real screenshot that the
        # fuzzy captions were rendering full-strength $TEXT_PRIMARY (QSS's
        # blanket QWidget{color:...} rule) while the placeholder, painted
        # directly with QPainter and never touched by QSS, was visibly
        # muted. Applying PlaceholderText here keeps both looking the same
        # kind of secondary/explanatory text, and staying correct across
        # every theme including "Match System" since it's read fresh each
        # call rather than baked in once.
        color = self.palette().color(QPalette.PlaceholderText).name()
        label.setStyleSheet(f"font-size: 9pt; color: {color};")

    def _refresh_fuzzy_caption_style(self):
        for label in (self.quality_tier_label, self.speed_tier_label, self.audio_bitrate_tier_label):
            self._apply_fuzzy_caption_style(label)

    def _themed_icon(self, name: str) -> QIcon:
        theme = _resolve_theme(self._theme_choice)
        return QIcon(str(Path(__file__).parent / "assets" / f"{name}_{theme}.svg"))

    def _refresh_themed_icons(self):
        self.save_btn.setIcon(self._themed_icon("save"))
        self.delete_btn.setIcon(self._themed_icon("delete"))

    def _build_left_panel(self) -> QWidget:
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        layout.addLayout(self._build_preset_row())

        tabs = QTabWidget()
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
        # The two tabs have very different row counts, and a QSplitter pane
        # is always forced to the full window height regardless of content
        # -- capping the tab widget to its natural size (instead of letting
        # it stretch into that forced height) keeps the sparser tab from
        # looking broken. The command preview below puts the leftover space
        # to use instead of leaving it blank.
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(tabs)

        self._command_group = self._build_command_preview()
        layout.addWidget(self._command_group)

        layout.addStretch(1)
        return left

    @staticmethod
    def _hardware_status_text() -> str:
        found = []
        for vendor, label in (("intel", "Intel iGPU"), ("amd", "AMD GPU")):
            try:
                node = worker.find_render_node(worker.GPU_VENDOR_IDS[vendor])
                found.append(f"{label} ({node})")
            except RuntimeError:
                pass
        if found:
            return "Hardware encode available: " + ", ".join(found)
        return "No VAAPI render node detected — hardware encoding unavailable"

    @staticmethod
    def _make_collapsible_group(title: str, content: QWidget, *, expanded: bool) -> QGroupBox:
        # A real title string, via the exact same native QGroupBox::title
        # subcontrol every other section (Encoding, Format, Audio Settings)
        # uses -- same font, color, border-overlapping position, no
        # separate mechanism to keep visually in sync with those. The only
        # difference is the checkbox indicator's rendered size is zeroed
        # out in style.qss (QGroupBox#collapsibleGroup::indicator), with a
        # trailing arrow baked into the title text instead of a checkbox
        # glyph -- confirmed empirically that hiding the indicator doesn't
        # shrink the *clickable* area down to where the glyph would have
        # been: Qt/Fusion already treats a checkable QGroupBox's whole
        # title bar as one hit region, glyph size notwithstanding, so this
        # is a skin change, not a rebuild of how clicking it works. (An
        # earlier version of this used a separate flat QPushButton sitting
        # below the border instead -- functioned fine, but visually broke
        # from every other section's title, which sits overlapping the
        # border -- reverted for exactly that inconsistency.)
        group = QGroupBox(title)
        group.setObjectName("collapsibleGroup")
        group.setCheckable(True)
        group.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(group)
        layout.addWidget(content)

        # A stretch factor in the parent layout (the Log group has one, to
        # share space with the queue list) still applies to the *group*
        # even with its content hidden, so collapsing needs a fixed size
        # policy too or the group keeps claiming its full stretch share --
        # confirmed by screenshot, a large empty box where the log used to be.
        expanded_policy = group.sizePolicy()
        collapsed_policy = QSizePolicy(expanded_policy.horizontalPolicy(), QSizePolicy.Fixed)

        def _toggle(checked):
            group.setTitle(f"{title}  {'▾' if checked else '▸'}")
            content.setVisible(checked)
            group.setSizePolicy(expanded_policy if checked else collapsed_policy)
            group.updateGeometry()

        group.setChecked(expanded)
        _toggle(expanded)
        group.toggled.connect(_toggle)
        return group

    def _build_command_preview(self) -> QGroupBox:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)

        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(110)
        self.command_preview.setStyleSheet("font-family: monospace; font-size: 9pt;")
        # _format_preview_text below already breaks the command into one
        # logical group per line (input / filter / mapping / etc.) -- with
        # WidgetWidth wrap, a single long -vf value still doesn't fit one
        # line and gets re-wrapped a second time at an arbitrary character
        # (confirmed by screenshot: "force_divisible_b" / "y=2" mid-token).
        # NoWrap preserves the intended one-line-per-group layout and lets
        # only that one line scroll horizontally instead.
        self.command_preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.command_preview)

        # Below the text, not above it, and visibly smaller -- this is a
        # power-user convenience for a section that's already collapsed by
        # default, not an action worth the same visual weight as Start or
        # the preset buttons.
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_btn = QPushButton("Copy")
        copy_btn.setToolTip("Copy the full command to the clipboard")
        copy_btn.setStyleSheet("padding: 2px 10px; font-size: 8pt;")
        copy_btn.clicked.connect(self._copy_command_to_clipboard)
        copy_row.addWidget(copy_btn)
        layout.addLayout(copy_row)

        # Collapsed by default: this is the one control in the whole left
        # panel aimed at a technical reader double-checking the exact ffmpeg
        # invocation, not something the simplified default view needs open.
        return self._make_collapsible_group("Effective Command", content, expanded=False)

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

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setSpacing(PANEL_SPACING)

        encoding_group = QGroupBox("Encoding")
        self.video_form = form = QFormLayout(encoding_group)
        # Default Fusion spacing reads as cramped once every row has a small
        # secondary line under it (quality/speed tiers, the bit-depth combo's
        # own description) -- confirmed by screenshot, this is the fix.
        form.setVerticalSpacing(14)

        self.encoder_combo = QComboBox()
        for _, _, label in ENCODERS:
            self.encoder_combo.addItem(label)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        form.addRow("Encoder:", self.encoder_combo)

        # rc_mode_combo stays the source of truth (everything downstream --
        # _on_rc_mode_changed, _current_settings, presets -- reads it) but
        # is never shown: the visible control is the two/three buttons
        # below, which just drive this combo's index. Two ways to reach the
        # same state would risk them drifting apart; one hidden model plus
        # a friendlier view over it can't.
        self.rc_mode_combo = QComboBox(encoding_group)
        self.rc_mode_combo.hide()
        self.rc_mode_combo.currentIndexChanged.connect(self._on_rc_mode_changed)
        self.rc_mode_combo.currentIndexChanged.connect(self._sync_rc_buttons_to_combo)

        rc_row = QHBoxLayout()
        rc_row.setSpacing(0)
        self.rc_button_group = QButtonGroup(self)
        self.rc_quality_btn = QPushButton("Quality")
        self.rc_quality_btn.setObjectName("segLeft")
        self.rc_quality_btn.setToolTip("Aim for a consistent perceptual quality; file size follows.")
        self.rc_filesize_btn = QPushButton("File Size")
        self.rc_filesize_btn.setObjectName("segMid")
        self.rc_filesize_btn.setToolTip("Aim for a target output size; quality follows.")
        self.rc_advanced_btn = QPushButton("Advanced")
        self.rc_advanced_btn.setObjectName("segRight")
        self.rc_advanced_btn.setToolTip(
            "Fixed quantizer (CQP): the same compression level on every\n"
            "frame, regardless of content complexity. Rarely needed --\n"
            "Quality (ICQ) adapts per-frame and usually looks better for\n"
            "the same average bitrate."
        )
        for btn in (self.rc_quality_btn, self.rc_filesize_btn, self.rc_advanced_btn):
            btn.setCheckable(True)
            self.rc_button_group.addButton(btn)
            rc_row.addWidget(btn, 1)
        self.rc_quality_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["quality"])
        )
        self.rc_filesize_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["file_size"])
        )
        self.rc_advanced_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["advanced"])
        )
        form.addRow("Rate control:", rc_row)

        quality_row = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Horizontal)
        # Left = worse quality/smaller, right = better quality/larger --
        # the intuitive direction for a horizontal slider. The underlying
        # ICQ/CQP/CRF value this drives is the opposite (lower number is
        # better quality), so invertedAppearance/-Controls flips the visual
        # and interaction direction while .value() keeps returning the real
        # number untouched -- Qt handles the remapping, nothing downstream
        # (settings, presets, build_args) needs to know this happened.
        self.quality_slider.setInvertedAppearance(True)
        self.quality_slider.setInvertedControls(True)
        self.quality_slider.setToolTip(
            "Left: more compression, smaller file.\n"
            "Right: higher quality, larger file."
        )
        self.quality_slider.valueChanged.connect(self._on_quality_changed)
        self.quality_label = QLabel()
        self.quality_label.setStyleSheet("font-size: 9pt;")
        self.size_spin = QSpinBox()
        self.size_spin.setRange(10, 20000)
        self.size_spin.setSingleStep(50)
        self.size_spin.setSuffix(" MB")
        self.size_spin.setValue(1000)
        self.size_spin.setToolTip("Target output size -- the actual bitrate is computed from this file's length.")
        self.size_spin.valueChanged.connect(self._on_control_changed)
        quality_row.addWidget(self.quality_slider, 1)
        quality_row.addWidget(self.quality_label)
        quality_row.addWidget(self.size_spin, 1)

        # Only one of these two is ever visible at a time (is_bitrate in
        # _on_rc_mode_changed) -- same one-row-two-widgets pattern as
        # quality_slider/size_spin just above, rather than two separate rows
        # where one is always an empty gap.
        # stretch=1 on both (only one is ever visible at a time) so each
        # claims the row's full width and its own AlignCenter has something
        # to center within -- otherwise a shrink-wrapped label sits flush
        # left with nothing to visually tie it to the slider above it.
        quality_detail_row = QHBoxLayout()
        self.quality_tier_label = QLabel()
        self.quality_tier_label.setAlignment(Qt.AlignCenter)
        self._apply_fuzzy_caption_style(self.quality_tier_label)
        self.size_estimate_label = QLabel()
        self.size_estimate_label.setAlignment(Qt.AlignCenter)
        self.size_estimate_label.setStyleSheet("font-size: 9pt;")
        quality_detail_row.addWidget(self.quality_tier_label, 1)
        quality_detail_row.addWidget(self.size_estimate_label, 1)

        # The slider and its fuzzy caption underneath share one outlined
        # box (objectName carries the QSS rule -- see style.qss's
        # #fuzzyGroup, shared with Speed's identical box below) instead of
        # being two independent-looking form rows -- the caption explains
        # *that specific slider*, so it reads better visually grouped with
        # it rather than just sitting in the row underneath.
        quality_group = QWidget()
        quality_group.setObjectName("fuzzyGroup")
        quality_group_layout = QVBoxLayout(quality_group)
        quality_group_layout.setContentsMargins(8, 6, 8, 6)
        # Default QVBoxLayout spacing (Fusion's ~11px) read as the caption
        # floating unrelated to the slider above it rather than explaining
        # it -- confirmed by screenshot.
        quality_group_layout.setSpacing(2)
        quality_group_layout.addLayout(quality_row)
        quality_group_layout.addLayout(quality_detail_row)
        form.addRow("Quality:", quality_group)

        speed_row = QHBoxLayout()
        self.speed_faster_label = QLabel("Faster")
        speed_row.addWidget(self.speed_faster_label)
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(1, 7)
        # Same reasoning as the Quality slider's inversion above: left=fast,
        # right=thorough is the intuitive direction, and it's now also the
        # *correct* one -- confirmed by timing real encodes at
        # compression_level 1/4/7 (22.4s/18.8s/11.3s, smallest to largest
        # output in that order too), a lower value is genuinely slower, not
        # just assumed. Before this, "Faster" sat on the end that was
        # actually the slowest.
        self.speed_slider.setInvertedAppearance(True)
        self.speed_slider.setInvertedControls(True)
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        speed_row.addWidget(self.speed_slider, 1)

        self.speed_x265_slider = QSlider(Qt.Horizontal)
        # Index into X265_PRESETS, not a value with real arithmetic meaning
        # of its own -- same reasoning as Audio Bitrate's slider. No
        # invertedAppearance/-Controls needed here unlike the VAAPI slider
        # above: X265_PRESETS is already ordered fastest-to-slowest
        # (ultrafast..placebo), so index 0 landing on the visual left and
        # the last index on the right is already the correct "Faster ...
        # More Thorough" direction without flipping anything.
        self.speed_x265_slider.setRange(0, len(X265_PRESETS) - 1)
        self.speed_x265_slider.setToolTip(
            "Left: faster encode.\n"
            "Right: slower, more size-efficient at the same quality.\n"
            "(x265 preset)"
        )
        self.speed_x265_slider.valueChanged.connect(self._on_speed_x265_slider_changed)
        speed_row.addWidget(self.speed_x265_slider, 1)

        self.speed_thorough_label = QLabel("More Thorough")
        speed_row.addWidget(self.speed_thorough_label)
        self.speed_x265_label = QLabel()
        self.speed_x265_label.setStyleSheet("font-size: 9pt;")
        speed_row.addWidget(self.speed_x265_label)

        self.speed_tier_label = QLabel()
        self.speed_tier_label.setAlignment(Qt.AlignCenter)
        self.speed_tier_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._apply_fuzzy_caption_style(self.speed_tier_label)

        # Same outlined-box grouping as Quality above, same #fuzzyGroup rule.
        speed_group = QWidget()
        speed_group.setObjectName("fuzzyGroup")
        speed_group_layout = QVBoxLayout(speed_group)
        speed_group_layout.setContentsMargins(8, 6, 8, 6)
        speed_group_layout.setSpacing(2)  # see Quality's identical fix above
        speed_group_layout.addLayout(speed_row)
        speed_group_layout.addWidget(self.speed_tier_label)
        form.addRow("Speed:", speed_group)

        # Bit depth's tradeoff used to live in a separate caption row below
        # the combo -- folded directly into the item text instead (one less
        # row fighting Quality/Speed for space, and the tradeoff is right
        # there the moment the dropdown opens rather than a beat later).
        self.bitdepth_combo = QComboBox()
        self.bitdepth_combo.addItem("10-bit -- smoother gradients, larger file", userData=10)
        self.bitdepth_combo.addItem("8-bit -- smaller, maximum compatibility", userData=8)
        self.bitdepth_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Bit depth:", self.bitdepth_combo)

        self.tune_combo = QComboBox()
        self.tune_combo.addItems(X265_TUNES)
        self.tune_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Tune (x265 only):", self.tune_combo)

        self.deinterlace_check = QCheckBox("Deinterlace (interlaced or telecined source)")
        self.deinterlace_check.setToolTip(
            "Container-level progressive/interlaced flags are frequently wrong,\n"
            "especially on camcorder-sourced footage. New files are sampled\n"
            "and this is set automatically, but detection only checks the\n"
            "first ~20s -- override it here if the output still shows\n"
            "combing/interlacing artifacts."
        )
        self.deinterlace_check.stateChanged.connect(self._on_deinterlace_checkbox_changed)
        form.addRow("", self.deinterlace_check)

        outer.addWidget(encoding_group)

        output_group = QGroupBox("Format")
        out_form = QFormLayout(output_group)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        self.res_combo.setCurrentIndex(2)  # 720p
        self.res_combo.currentIndexChanged.connect(self._on_control_changed)
        out_form.addRow("Resolution:", self.res_combo)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)
        self.container_combo.currentIndexChanged.connect(self._on_control_changed)
        out_form.addRow("Container:", self.container_combo)

        outer.addWidget(output_group)
        return tab

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)

        group = QGroupBox("Audio Settings")
        form = QFormLayout(group)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(AUDIO_TRACK_LABELS)
        self.audio_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Audio track:", self.audio_combo)

        self.audio_copy_check = QCheckBox("Copy audio if compatible (aac/ac3/eac3)")
        self.audio_copy_check.setChecked(True)
        self.audio_copy_check.stateChanged.connect(self._on_control_changed)
        form.addRow("", self.audio_copy_check)

        audio_bitrate_row = QHBoxLayout()
        self.audio_bitrate_slider = QSlider(Qt.Horizontal)
        # Index into AUDIO_BITRATES, not the kbps number itself -- the real
        # values (96/128/160/192/256) aren't evenly spaced (some steps are
        # 32, the last is 64), which a linear QSlider can't represent
        # directly without irregular, confusing tick spacing. An index is
        # exact and trivial to map back to the real string everywhere this
        # setting is read (_current_settings, _apply_settings_to_controls).
        self.audio_bitrate_slider.setRange(0, len(AUDIO_BITRATES) - 1)
        self.audio_bitrate_slider.setToolTip(
            "Left: more compression, smaller file.\n"
            "Right: higher quality, larger file.\n"
            "Only applies when the source audio is actually being "
            "transcoded -- see \"Copy audio if compatible\" above."
        )
        self.audio_bitrate_slider.valueChanged.connect(self._on_audio_bitrate_slider_changed)
        audio_bitrate_row.addWidget(self.audio_bitrate_slider, 1)
        self.audio_bitrate_label = QLabel()
        self.audio_bitrate_label.setStyleSheet("font-size: 9pt;")
        audio_bitrate_row.addWidget(self.audio_bitrate_label)

        self.audio_bitrate_tier_label = QLabel()
        self.audio_bitrate_tier_label.setAlignment(Qt.AlignCenter)
        self._apply_fuzzy_caption_style(self.audio_bitrate_tier_label)

        # Same slider-plus-fuzzy-caption outlined box as Quality/Speed on
        # the Video tab (#fuzzyGroup in style.qss) -- same reasoning: the
        # caption explains *this* slider, so it reads better grouped with
        # it than as a separate form row underneath.
        audio_bitrate_group = QWidget()
        audio_bitrate_group.setObjectName("fuzzyGroup")
        audio_bitrate_group_layout = QVBoxLayout(audio_bitrate_group)
        audio_bitrate_group_layout.setContentsMargins(8, 6, 8, 6)
        audio_bitrate_group_layout.setSpacing(2)  # see Quality's identical fix above
        audio_bitrate_group_layout.addLayout(audio_bitrate_row)
        audio_bitrate_group_layout.addWidget(self.audio_bitrate_tier_label)
        form.addRow("Audio bitrate (if transcoded):", audio_bitrate_group)

        self.audio_downmix_check = QCheckBox("Downmix to stereo (if source has more channels)")
        self.audio_downmix_check.setToolTip(
            "Mixes 5.1/7.1/etc. sources down to plain stereo -- for\n"
            "playback on a phone, laptop, or anything without a surround\n"
            "setup. Has no effect on a source that's already stereo or\n"
            "mono. A stream copy can't remix channels, so on a source\n"
            "that does have more channels, checking this transcodes the\n"
            "audio track even if it would otherwise have been copied\n"
            "through untouched."
        )
        self.audio_downmix_check.stateChanged.connect(self._on_control_changed)
        form.addRow("", self.audio_downmix_check)

        outer.addWidget(group)
        return tab

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        layout.addWidget(QLabel(
            "Queue (drag files here, or use Add Files — select a row to edit its settings live):"
        ))
        self.queue_list = DropTreeWidget(self.add_files)
        self.queue_list.setColumnCount(len(QUEUE_COLUMN_HEADERS))
        self.queue_list.setHeaderLabels(QUEUE_COLUMN_HEADERS)
        # File is Interactive/user-resizable, not Stretch (which auto-
        # claims leftover space but also makes Qt refuse to let it be
        # dragged at all, silently, with no visible resize handle) --
        # explicit initial widths below instead of Qt's generic default,
        # sized to each column's actual content ("H.264 1280x720",
        # "392.2KB", ...). Result, the last column, is the one exception:
        # setStretchLastSection(True) makes *it* claim whatever's left
        # over on the right rather than leaving a bare gap between it and
        # the panel's edge -- losing manual-resize on Result specifically
        # is an easy trade, unlike File, since its content ("612.3MB (71%
        # smaller)") doesn't vary anywhere near as much as a filename does.
        self.queue_list.header().setStretchLastSection(True)
        for col, width in (
            (FILE_COL, 150), (VIDEO_COL, 130), (DURATION_COL, 70),
            (AUDIO_COL, 60), (SIZE_COL, 52), (RESULT_COL, 85),
        ):
            self.queue_list.setColumnWidth(col, width)
        self.queue_list.itemSelectionChanged.connect(self._on_queue_selection_changed)
        layout.addWidget(self.queue_list, 1)

        q_btns = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Files…")
        self.add_files_btn.clicked.connect(self._pick_files)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear Queue")
        self.clear_btn.clicked.connect(self._clear_queue)
        q_btns.addWidget(self.add_files_btn)
        q_btns.addWidget(self.remove_btn)
        q_btns.addWidget(self.clear_btn)
        q_btns.addStretch()
        layout.addLayout(q_btns)

        # Output folder is a per-run detail, not the first decision anyone
        # makes -- it lives right next to Start, where it's actually used.
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_dir))
        # Typing a path directly, not just Change...'s browse dialog -- the
        # dialog only ever hands back a real, already-existing directory,
        # so unlike there, a typed path isn't checked to exist here either;
        # it's created on demand (mkdir(parents=True, exist_ok=True)) the
        # same way a browsed-to path already was, right before it's
        # actually used (Start, Open).
        self.output_edit.editingFinished.connect(self._on_output_edit_changed)
        browse_btn = QPushButton("Change…")
        browse_btn.clicked.connect(self._pick_output_dir)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_output_dir)
        out_row.addWidget(QLabel("Output folder:"))
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_btn)
        out_row.addWidget(open_btn)
        layout.addLayout(out_row)

        run_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(36)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        run_row.addWidget(self.start_btn, 1)
        run_row.addWidget(self.stop_btn, 1)
        layout.addLayout(run_row)

        self.status_label = QLabel("Idle")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel("—")
        # Same fix as hw_status_label above: palette(mid) reads as
        # near-invisible on a dark theme. Default text color, smaller size.
        self.stats_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.stats_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("ffmpeg output will appear here once a job starts…")
        # Collapsed by default, same reasoning and the same disclosure
        # pattern as Effective Command on the left: raw ffmpeg stderr is a
        # debugging aid, not something the simplified default view needs
        # open, and the queue list above happily reclaims the freed space
        # (already the only other stretch=1 widget in this layout).
        self._log_group = self._make_collapsible_group("Log", self.log_view, expanded=False)
        layout.addWidget(self._log_group, 2)
        return right

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
        self.rc_advanced_btn.setVisible(RC_MODE_FRIENDLY[encoder_key]["advanced"] is not None)

        # speed_faster_label/speed_thorough_label/speed_tier_label are
        # shared by both sliders below (same "Faster .. More Thorough" axis,
        # same 3-tier fuzzy caption vocabulary either way) -- always visible
        # now that x265 has its own real slider too, not just VAAPI.
        self.speed_slider.setVisible(is_vaapi)
        self.speed_x265_slider.setVisible(not is_vaapi)
        self.speed_x265_label.setVisible(not is_vaapi)
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

    @staticmethod
    def _fraction_of(slider: QSlider) -> float:
        lo, hi = slider.minimum(), slider.maximum()
        return (slider.value() - lo) / (hi - lo) if hi > lo else 0.0

    @staticmethod
    def _tier_label(fraction: float, *labels: str) -> str:
        # Variadic rather than a fixed low/mid/high trio -- both Quality and
        # Speed pass 6 of these now (a 3-way split felt too coarse: Quality
        # dragging across a ~50-value ICQ/CQP/CRF range, Speed just because
        # 3 buckets was reported as too coarse directly). Speed's own range
        # is only 1-7 (VAAPI) / 10 presets (x265), so 6 buckets lands closer
        # to one caption per real slider stop than a fuzzy zone -- fine,
        # same reasoning Quality already accepted for its own case. Same
        # bucketing math either way, just over however many labels got
        # passed.
        bucket = min(int(fraction * len(labels)), len(labels) - 1)
        return labels[bucket]

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
        self.quality_tier_label.setText(self._tier_label(
            self._fraction_of(self.quality_slider),
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
        self.speed_tier_label.setText(self._tier_label(
            self._fraction_of(self.speed_slider),
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
        self.speed_x265_label.setText(preset)
        # Same 6 captions as the VAAPI slider above (same axis, same
        # meaning, just a different underlying scale) -- but in the
        # opposite fraction order: X265_PRESETS is already sorted fastest
        # to slowest (ultrafast..placebo), so index 0 is the *fast* end
        # here, where compression_level 1 was the *slow* end there.
        self.speed_tier_label.setText(self._tier_label(
            self._fraction_of(self.speed_x265_slider),
            "Fast -- quick previews",
            "Quick -- fast turnaround",
            "Balanced -- solid default",
            "Careful -- strong efficiency",
            "Thorough -- best efficiency",
            "Maximum effort -- best compression",
        ))
        self._on_control_changed()

    # One caption per real AUDIO_BITRATES entry, not the 3-bucket
    # _tier_label helper Quality/Speed use above -- those two are smooth,
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
                # preview matches what will really run.
                first_path = self.queue_list.topLevelItem(0).data(STATUS_COL, Qt.UserRole)["path"]
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
            and self._settings_differ(self._current_settings(), self._loaded_preset_settings)
        )
        self.preset_combo.setProperty("modified", modified)
        self.preset_combo.style().unpolish(self.preset_combo)
        self.preset_combo.style().polish(self.preset_combo)

    @staticmethod
    def _settings_differ(a: dict, b: dict) -> bool:
        # Plain != would treat a dict missing "gpu_vendor" entirely (every
        # built-in CPU preset, which never had a reason to define it) as
        # different from one where it's explicitly None (_current_settings()
        # always includes it, via _current_gpu_vendor() returning None for a
        # non-VAAPI encoder) -- confirmed directly: selecting any of the
        # three CPU presets showed "modified" immediately, nothing actually
        # changed. Comparing key-by-key with .get() on both sides treats
        # "key absent" and "key explicitly None" as equivalent, which is
        # what they're actually meant to mean here -- and stays correct for
        # any future settings key with the same absent-in-older-dicts shape
        # (tune/deinterlace/container already have it, handled the same
        # .get()-with-a-default way elsewhere in this file), not just this
        # one field.
        keys = a.keys() | b.keys()
        return any(a.get(k) != b.get(k) for k in keys)

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

    @staticmethod
    def _video_codec_label(codec_name: str | None) -> str:
        if not codec_name:
            return "?"
        return _VIDEO_CODEC_LABELS.get(codec_name, codec_name.upper())

    @staticmethod
    def _audio_codec_label(codec_name: str | None) -> str:
        if not codec_name:
            return "?"
        return _AUDIO_CODEC_LABELS.get(codec_name, codec_name.upper())

    @staticmethod
    def _audio_channel_label(channels: int | None) -> str:
        if not channels:
            return "?"
        return _AUDIO_CHANNEL_LABELS.get(channels, f"{channels}ch")

    def _refresh_video_cell(self, item: QTreeWidgetItem):
        # Video-column text depends on two independent async results (the
        # source probe's codec+resolution label, stashed on this column's
        # own UserRole slot, and the interlace detector's job["deinterlace"]
        # flag) that can land in either order -- recomputing from both each
        # time either one arrives, rather than concatenating piecemeal,
        # keeps the result correct regardless of which finishes first.
        base_label = item.data(VIDEO_COL, Qt.UserRole)
        if not base_label:
            return
        job = item.data(STATUS_COL, Qt.UserRole)
        suffix = " (interlaced)" if job and job.get("deinterlace") else ""
        item.setText(VIDEO_COL, f"{base_label}{suffix}")

    def _make_queue_row(self, job: dict) -> QTreeWidgetItem:
        # Deliberately source-properties-only (file/resolution/duration/
        # codecs/size) -- the chosen output settings (encoder, quality,
        # container, ...) already live in and edit live from the right-hand
        # panel for whichever row is selected, so repeating them here would
        # just be the same information twice.
        item = QTreeWidgetItem()
        item.setData(STATUS_COL, Qt.UserRole, job)
        item.setText(FILE_COL, job["path"].name)
        for col in range(len(QUEUE_COLUMN_HEADERS)):
            item.setToolTip(col, str(job["path"]))
        try:
            item.setText(SIZE_COL, self._format_size(job["path"].stat().st_size))
        except OSError:
            pass
        return item

    # --- preset management ---
    def _all_presets(self) -> list[dict]:
        return BUILTIN_PRESETS + self.user_presets

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
        existing = next((p for p in self.user_presets if p["name"] == name), None)
        if existing and QMessageBox.question(
            self, "Overwrite?", f'A preset named "{name}" already exists. Overwrite it?'
        ) != QMessageBox.Yes:
            return
        settings = self._current_settings()
        settings["name"] = name
        if existing:
            self.user_presets[self.user_presets.index(existing)] = settings
        else:
            self.user_presets.append(settings)
        save_user_presets(self.user_presets)
        self._loaded_preset_settings = {k: v for k, v in settings.items() if k != "name"}
        self._refresh_preset_combo(select=name)
        self._update_preset_modified_indicator()

    def _delete_preset(self):
        name = self.preset_combo.currentText()
        if name in BUILTIN_PRESET_NAMES:
            QMessageBox.warning(self, "Can't delete", "Built-in presets can't be deleted.")
            return
        existing = next((p for p in self.user_presets if p["name"] == name), None)
        if not existing:
            return
        if QMessageBox.question(self, "Delete preset", f'Delete "{name}"?') != QMessageBox.Yes:
            return
        self.user_presets.remove(existing)
        save_user_presets(self.user_presets)
        self._refresh_preset_combo()

    # --- queue management ---
    def add_files(self, paths: list[Path]):
        for path in paths:
            if path.is_file():
                job = {"path": path, **self._current_settings()}
                item = self._make_queue_row(job)
                self.queue_list.addTopLevelItem(item)
                self._start_interlace_detection(item, path)
                self._start_source_probe(item, path)
                if not self._queue_editable:
                    # A run is already in progress -- keep it going instead
                    # of silently adding a row that would otherwise just sit
                    # there until the user noticed and clicked Start again.
                    # add_job takes a plain snapshot dict, not a live
                    # reference to this item's data (QTreeWidgetItem.setData/
                    # .data() round-trips a copy, confirmed empirically, so
                    # a shared reference wouldn't see the interlace-detection
                    # update below anyway) -- self._running_items grows in
                    # lockstep so _on_job_started's index lookup stays valid.
                    self.queue.add_job(dict(job))
                    self._running_items.append(item)
        self._update_command_preview()  # may now reflect a real queued file's audio

    def _start_interlace_detection(self, item: QTreeWidgetItem, path: Path):
        # Runs async (real files can take tens of seconds to sample) --
        # never blocks adding files, the item just updates once this lands.
        args = worker.build_idet_args(path)
        proc = QProcess(self)
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        stderr_chunks: list[str] = []
        proc.readyReadStandardError.connect(
            lambda: stderr_chunks.append(bytes(proc.readAllStandardError()).decode(errors="replace"))
        )
        proc.finished.connect(lambda code, status: self._on_interlace_detected(item, "".join(stderr_chunks)))
        self._detection_processes.append(proc)
        proc.start()

    def _on_deinterlace_checkbox_changed(self):
        # A dedicated handler, not just the generic _on_control_changed
        # every other control uses -- needed to record which items the
        # user has actually, manually decided this for, so a same-file
        # auto-detect result landing later (_on_interlace_detected) can
        # respect that instead of silently overwriting it. Confirmed this
        # was a real race before: toggling this checkbox for a file right
        # after adding it could get quietly reverted a few seconds later
        # when the ~20s detector finished, with nothing to indicate it had
        # happened. _syncing_controls_from_selection guards against this
        # firing from a *programmatic* setChecked() (selecting a different
        # queue item, or the detector's own UI sync below) -- only a
        # genuine user click should count as an override.
        if not self._syncing_controls_from_selection:
            for item in self.queue_list.selectedItems():
                job = item.data(STATUS_COL, Qt.UserRole)
                if job is not None:
                    job["deinterlace_user_set"] = True
                    item.setData(STATUS_COL, Qt.UserRole, job)
        self._on_control_changed()

    def _on_interlace_detected(self, item: QTreeWidgetItem, stderr_text: str):
        self._detection_processes = [p for p in self._detection_processes if p.state() != QProcess.NotRunning]
        try:
            job = item.data(STATUS_COL, Qt.UserRole)
        except RuntimeError:
            return  # item's C++ object was deleted (e.g. Clear Queue) before detection finished
        if job is None:
            return
        if job.get("deinterlace_user_set"):
            # The user already explicitly set this file's deinterlace value
            # (see _on_deinterlace_checkbox_changed) -- their choice wins,
            # a same-file detection result landing after that shouldn't
            # silently replace it.
            return
        fraction = worker.parse_idet_output(stderr_text)
        job["deinterlace"] = fraction > worker.INTERLACE_DETECT_THRESHOLD
        item.setData(STATUS_COL, Qt.UserRole, job)
        self._refresh_video_cell(item)
        # If this file was added mid-run (see add_files), the running queue
        # got its own snapshot copy of job at add time, made before this
        # detection result was known -- patch that copy too, or a file
        # added while encoding was in progress would always encode with
        # deinterlace off regardless of what detection actually found.
        self.queue.update_pending_job(job["path"], {"deinterlace": job["deinterlace"]})
        # Reflect it in the checkbox if this item happens to be selected, but
        # guarded: without this, updating just this one item's checkbox would
        # cascade into _sync_settings_to_selected_queue_items and stamp this
        # single file's detected value onto every OTHER currently-selected
        # item too, if more than one happens to be selected at that moment.
        selected = self.queue_list.selectedItems()
        if selected == [item]:
            self._syncing_controls_from_selection = True
            try:
                self.deinterlace_check.setChecked(job["deinterlace"])
            finally:
                self._syncing_controls_from_selection = False

    def _start_source_probe(self, item: QTreeWidgetItem, path: Path):
        # Async for the same reason as _start_interlace_detection above --
        # ffprobe's header-only read is fast, but "fast" still isn't free
        # for a large dropped batch or a file on slow/network storage, and
        # this must never block adding files either.
        args = worker.build_probe_args(path)
        proc = QProcess(self)
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        stdout_chunks: list[str] = []
        proc.readyReadStandardOutput.connect(
            lambda: stdout_chunks.append(bytes(proc.readAllStandardOutput()).decode(errors="replace"))
        )
        proc.finished.connect(lambda code, status: self._on_source_probed(item, "".join(stdout_chunks)))
        self._detection_processes.append(proc)
        proc.start()

    def _on_source_probed(self, item: QTreeWidgetItem, stdout_text: str):
        self._detection_processes = [p for p in self._detection_processes if p.state() != QProcess.NotRunning]
        try:
            item.text(FILE_COL)  # touch the item; raises RuntimeError if its C++ object is gone
        except RuntimeError:
            return  # item deleted (e.g. Clear Queue) before the probe landed
        info = worker.parse_probe_output(stdout_text)
        if info.get("duration"):
            item.setText(DURATION_COL, self._format_eta(info["duration"]))
        if info.get("video_codec"):
            label = self._video_codec_label(info["video_codec"])
            if info.get("width") and info.get("height"):
                label += f" {info['width']}x{info['height']}"
            item.setData(VIDEO_COL, Qt.UserRole, label)
            self._refresh_video_cell(item)
        if info.get("audio_codec"):
            extra = info.get("audio_track_count", 1) - 1
            suffix = f"  +{extra} more" if extra > 0 else ""
            channel_label = self._audio_channel_label(info.get("audio_channels"))
            item.setText(AUDIO_COL, f"{self._audio_codec_label(info['audio_codec'])} {channel_label}{suffix}")

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add video files", str(Path.home()), VIDEO_FILTER
        )
        self.add_files([Path(f) for f in files])

    def _pick_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", str(self.output_dir))
        if d:
            self.output_dir = Path(d)
            self.output_edit.setText(d)

    def _on_output_edit_changed(self):
        text = self.output_edit.text().strip()
        if text:
            self.output_dir = Path(text)

    def _open_output_dir(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def _remove_selected(self):
        for item in self.queue_list.selectedItems():
            self.queue_list.takeTopLevelItem(self.queue_list.indexOfTopLevelItem(item))
        self._update_command_preview()

    def _clear_queue(self):
        count = self.queue_list.topLevelItemCount()
        if count == 0:
            return
        if QMessageBox.question(
            self, "Clear queue", f"Remove all {count} file(s) from the queue?"
        ) != QMessageBox.Yes:
            return
        self.queue_list.clear()
        self._update_command_preview()

    # --- run control ---
    def _start(self):
        jobs = [self.queue_list.topLevelItem(i).data(STATUS_COL, Qt.UserRole)
                 for i in range(self.queue_list.topLevelItemCount())]
        if not jobs:
            self.status_label.setText("Queue is empty")
            return
        if self._detection_processes:
            # jobs above is a snapshot of each row's *current* job dict --
            # a file added moments ago whose ~20s interlace sample hasn't
            # landed yet still has whatever deinterlace value it started
            # with (see add_files/_current_settings), not what detection
            # would actually find. Confirmed this was a real race: adding
            # an interlaced file and clicking Start immediately could
            # start that job with deinterlace off. Refusing to start while
            # any sample is still running closes it outright rather than
            # letting it happen silently.
            self.status_label.setText(
                f"Still analyzing {len(self._detection_processes)} file(s) for "
                f"interlacing -- try Start again in a few seconds"
            )
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_queue_editable(False)
        self.log_view.clear()
        # Jobs run strictly one at a time in this same order, and the queue
        # is locked for the run's duration (_set_queue_editable(False)), so
        # this position-based snapshot stays valid throughout -- job_started's
        # 1-based index is enough to look up which row is now running.
        self._running_items = [self.queue_list.topLevelItem(i) for i in range(self.queue_list.topLevelItemCount())]
        for item in self._running_items:
            item.setIcon(STATUS_COL, QIcon())  # clear any status icon left from a previous run
            item.setText(RESULT_COL, "")  # clear a previous run's result too
        self.queue.start(jobs, self.output_dir)

    def _stop(self):
        self.queue.stop()
        self.stop_btn.setEnabled(False)

    def _set_queue_editable(self, editable: bool):
        # Add Files is deliberately NOT gated by this -- add_files() pushes
        # a file dropped in mid-run straight into the run in progress
        # (queue.add_job/update_pending_job). Remove/Clear/reordering stay
        # locked during a run, though: none of them can affect a job
        # already running or already finished, so any of them would just
        # make the list lie about what's actually executing -- confirmed
        # dragging to reorder specifically wasn't actually blocked before
        # this, despite that same reasoning already applying to it just as
        # much as Remove/Clear. This also gates live selection-editing
        # (_sync_settings_to_selected_queue_items), so selecting an
        # already-finished row to check its tooltip during a run can't
        # accidentally overwrite its (now purely historical) settings.
        self._queue_editable = editable
        self.remove_btn.setEnabled(editable)
        self.clear_btn.setEnabled(editable)
        self.queue_list.reorder_locked = not editable

    # --- queue signal handlers ---
    def _on_job_started(self, path: str, index: int, total: int):
        self.status_label.setText(f"[{index}/{total}] Encoding {Path(path).name}")
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")
        self.log_view.appendPlainText(f"\n=== Starting {path} ===")
        self._current_running_item = self._running_items[index - 1]
        self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_play"))

    def _on_job_progress(self, fraction: float):
        self.progress_bar.setValue(int(fraction * 1000))

    def _on_job_stats(self, stats: dict):
        fps = stats.get("fps", "?")
        bitrate = stats.get("bitrate", "?")
        speed = stats.get("speed", "?")
        eta = stats.get("eta_seconds")
        eta_str = self._format_eta(eta) if eta is not None else "--:--"
        self.stats_label.setText(f"{fps} fps  ·  {bitrate}  ·  {speed} speed  ·  ETA {eta_str}")

    @staticmethod
    def _format_eta(seconds: float) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _on_job_log(self, line: str):
        self.log_view.appendPlainText(line)

    def _on_job_finished(self, path: str, output_path: str):
        self.log_view.appendPlainText(f"=== Done: {path} ===")
        if self._current_running_item is not None:
            self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_done"))
            self._append_result_size(self._current_running_item, Path(path), Path(output_path))

    def _on_job_failed(self, path: str, reason: str):
        self.log_view.appendPlainText(f"=== FAILED: {path}: {reason} ===")
        if self._current_running_item is not None:
            self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_warning"))
            self._current_running_item.setToolTip(STATUS_COL, reason)

    @staticmethod
    def _append_result_size(item: QTreeWidgetItem, input_path: Path, output_path: Path):
        try:
            in_size = input_path.stat().st_size
            out_size = output_path.stat().st_size
        except OSError:
            return
        if in_size <= 0:
            return
        change_pct = 100 * (1 - out_size / in_size)
        direction = "smaller" if change_pct >= 0 else "larger"
        item.setText(RESULT_COL, f"{MainWindow._format_size(out_size)} ({abs(change_pct):.0f}% {direction})")

    @staticmethod
    def _format_size(num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}TB"

    def _on_all_finished(self):
        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_queue_editable(True)
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")


# Populated by _load_stylesheet on every load/theme switch; read back by
# _ComboPopupBackgroundFilter so a popup styled after a theme change gets
# the theme it was opened under, not whatever was loaded at startup.
_current_theme_palette: dict = {}


def _system_accent_tokens(app) -> dict:
    """Derives ACCENT/ACCENT_HOVER/ACCENT_PRESSED/TEXT_ON_ACCENT from the
    desktop's own accent color instead of a color this app picks on its
    own -- QPalette.Accent (Qt 6.6+; QPalette.Highlight on older Qt, where
    Accent doesn't exist yet) is what a properly-integrated Qt app is
    supposed to derive its accent from, and on a real KDE session it
    already resolves to that session's actual configured accent (confirmed
    on this machine: #308cc6, a real chosen color, not some generic
    Fusion-style default blue). Hover/pressed are lighter/darker variants
    of that same color; TEXT_ON_ACCENT is picked from the accent's own
    perceived luminance rather than assumed, since a user's chosen system
    accent could be any hue or lightness, not just the blue this app
    previously shipped with fixed per theme. Falls back to that previous
    fixed blue only if the palette role comes back invalid or pure black --
    a real desktop session's accent is never actually black, so that's a
    reliable signal nothing resolved rather than a legitimate choice.
    BG_ACCENT_DISABLED/TEXT_ACCENT_DISABLED (Start button, disabled) are
    deliberately left theme-fixed, not derived here -- a muted echo of an
    arbitrary accent hue is a harder thing to get right by formula than
    the four tokens above, and isn't what "follow the system accent"
    was actually asking for.

    CHECK_ICON rides along too, and needs to -- the checkmark drawn inside
    a checked QCheckBox sits directly on the $ACCENT fill, exactly like
    button text does, so it needs the same contrast-partner color
    TEXT_ON_ACCENT already picks, not whichever of check_dark.svg/
    check_light.svg happened to match the *old*, theme-fixed accent.
    Confirmed this was a real, live bug, not a hypothetical: Dark's
    accent used to be light enough that a near-black checkmark
    (check_dark.svg -- named for the theme it shipped with, not the
    stroke color) made sense; once the accent became this session's
    real KDE accent (#308cc6) instead, TEXT_ON_ACCENT correctly switched
    to white for *both* themes (the accent doesn't vary by theme
    anymore), but the checkmark file selection was still keyed off
    theme name, not off that same decision -- Dark's checkbox ended up
    with a near-black check on the same blue fill Start's white text
    sits on. Screenshotted both themes to confirm the mismatch before
    fixing it this way.
    """
    role = getattr(QPalette, "Accent", QPalette.Highlight)
    accent = app.palette().color(role)
    if not accent.isValid() or accent == QColor(0, 0, 0):
        accent = QColor("#4fa8e0")
    luminance = 0.299 * accent.redF() + 0.587 * accent.greenF() + 0.114 * accent.blueF()
    on_accent_is_dark = luminance > 0.5
    return {
        "ACCENT": accent.name(),
        "ACCENT_HOVER": accent.lighter(118).name(),
        "ACCENT_PRESSED": accent.darker(115).name(),
        "TEXT_ON_ACCENT": "#0d1117" if on_accent_is_dark else "#ffffff",
        "CHECK_ICON": "check_dark.svg" if on_accent_is_dark else "check_light.svg",
    }


def _load_stylesheet(app, theme_name: str = "dark", style_path: Path = Path(__file__).parent / "style.qss"):
    try:
        text = style_path.read_text()
        # QSS url() is resolved relative to the process's working directory,
        # not the .qss file's location -- not safe to hardcode given launch.sh
        # cd's first but a direct `python3 main.py` from elsewhere wouldn't.
        # Substituting an absolute path in for each *_ICON token below (e.g.
        # $CHECK_ICON) keeps style.qss itself portable.
        assets_dir = style_path.parent / "assets"
        # A copy, not the THEMES dict itself -- mutating that shared dict
        # in place would leak this call's system-accent override into every
        # later read of themes.DARK/LIGHT, theme switches included.
        palette = {**themes.THEMES.get(theme_name, themes.THEMES["dark"]), **_system_accent_tokens(app)}
        # Longest token first: "$BG_CONTROL" is a literal prefix of
        # "$BG_CONTROL_HOVER" and "$BG_CONTROL_PRESSED" (same for
        # $ACCENT/$ACCENT_HOVER/$ACCENT_PRESSED, $BORDER/$BORDER_STRONG/
        # $BORDER_HOVER) -- replacing the short one first would consume
        # the start of the longer token's name too, corrupting it before
        # its own turn came up. Sorting longest-first is what makes plain
        # str.replace() safe here regardless of which tokens exist.
        for token in sorted(palette, key=len, reverse=True):
            value = palette[token]
            if token.endswith("_ICON"):
                value = str(assets_dir / value)
            text = text.replace(f"${token}", value)
        app.setStyleSheet(text)
        _current_theme_palette.clear()
        _current_theme_palette.update(palette)
    except OSError as exc:
        # Missing/unreadable style.qss shouldn't take the whole app down --
        # fall back to plain Fusion rather than crash at startup over theming.
        print(f"Warning: couldn't load {style_path} ({exc}); using unstyled Fusion.")


class _ComboPopupBackgroundFilter(QObject):
    """Fixes two real, confirmed-via-real-screen-capture combo-popup bugs
    that QWidget.grab() had wrongly suggested were already fixed --
    grab() renders a widget's own paint buffer, not real compositor
    output, and missed both.

    Bug 1, solid black top/bottom bars on every popup: the popup list's
    own top-level QFrame (Qt's internal QComboBoxPrivateContainer) has
    autoFillBackground False and frameShape NoFrame, so nothing paints
    its background by ordinary QWidget means -- it's meant to rely
    entirely on the QSS engine, which for some reason doesn't reach it
    via the app-wide cascade the way it does the QComboBoxListView nested
    inside it (that one's background applies correctly, via the existing
    "QComboBox QAbstractItemView" rule). Setting a stylesheet directly on
    the frame instance at Show time -- confirmed via real capture --
    paints it correctly where the cascade alone didn't.

    Matched by metaObject().className(), not isinstance/type(obj).__name__:
    PySide6 has no Python binding for this private class, so its Python
    type reports as the nearest exposed base (QFrame), indistinguishable
    that way from every *other* QFrame in the app. metaObject().className()
    reads Qt's real C++ class name regardless of Python bindings.

    Bug 2, QComboBox[modified="true"]'s italic/colored styling bleeding
    into its own popup's list items (every preset name shown italic and
    accent-colored, not just the closed combo's own text): not a cascade
    problem at all -- confirmed by resetting font/color directly on the
    frame and the QListView inside it, immediately, deferred by one event
    loop tick, every combination, with zero effect on the popup's
    rendering. What did work: temporarily clearing the "modified" property
    on the combo box *itself* while its popup is open. That means the
    popup's item delegate paints using the owning combo's own currently-
    matched QSS state directly, not anything inherited or copied onto the
    view/frame -- so the only way to keep the popup's rendering plain is
    to make the combo's own matched state plain for as long as the popup
    is on screen, then restore it on Hide so the closed combo still shows
    its modified indicator afterward.
    """

    def eventFilter(self, obj, event):
        if obj.metaObject().className() != "QComboBoxPrivateContainer":
            return False
        if event.type() == QEvent.Type.Show:
            bg = _current_theme_palette.get("BG_PANEL", "#21252c")
            obj.setStyleSheet(f"background-color: {bg};")
            for child in obj.children():
                if isinstance(child, QWidget):
                    child.setStyleSheet(f"background-color: {bg};")
            combo = obj.parent()
            if isinstance(combo, QComboBox) and combo.property("modified"):
                combo.setProperty("modified", False)
                combo.setProperty("_popupSuppressedModified", True)
                combo.style().unpolish(combo)
                combo.style().polish(combo)
        elif event.type() == QEvent.Type.Hide:
            combo = obj.parent()
            if isinstance(combo, QComboBox) and combo.property("_popupSuppressedModified"):
                combo.setProperty("modified", True)
                combo.setProperty("_popupSuppressedModified", False)
                combo.style().unpolish(combo)
                combo.style().polish(combo)
        return False


class _FocusVisibleFilter(QObject):
    """QSS has no :focus-visible equivalent -- plain :focus matches a
    mouse click exactly the same as Tab, so a checkbox clicked with the
    mouse picked up the same accent-colored ring Tab-ing to it does
    (reported directly, confirmed by screenshot) -- wrong the same way it
    would be in a browser without :focus-visible: a pointer click doesn't
    need a keyboard-navigation aid pointing at where it already is.

    QFocusEvent.reason() is exactly the signal a browser's own
    :focus-visible heuristic is standing in for -- TabFocusReason/
    BacktabFocusReason for real keyboard navigation, MouseFocusReason for
    a click, plus a handful of others (ActiveWindowFocusReason,
    PopupFocusReason, ShortcutFocusReason, ...) that aren't keyboard
    navigation either. This filter watches FocusIn/FocusOut app-wide and
    mirrors that distinction onto a "focusVisible" dynamic property,
    which style.qss matches instead of :focus for every control this
    applies to. Applied universally rather than scoped to specific widget
    types: a property no QSS rule references is a harmless no-op, so
    there's nothing to lose covering every focusable widget the same way
    instead of maintaining a matching type list here.
    """

    def eventFilter(self, obj, event):
        # FocusIn/FocusOut also reach plain QWindow objects (a top-level
        # window gaining/losing OS-level focus, not any widget inside it)
        # -- confirmed by a real crash, QWindow has no .style(). Only
        # QWidgets carry the QSS-matched property this filter sets.
        if not isinstance(obj, QWidget):
            return False
        if event.type() == QEvent.Type.FocusIn:
            visible = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
            obj.setProperty("focusVisible", visible)
            obj.style().unpolish(obj)
            obj.style().polish(obj)
        elif event.type() == QEvent.Type.FocusOut:
            obj.setProperty("focusVisible", False)
            obj.style().unpolish(obj)
            obj.style().polish(obj)
        return False


def _validate_theme_choice(value) -> str:
    """QSettings hands back whatever was last stored there, which could be
    anything -- a hand-edited config file, a future/foreign version of this
    app, or simply nothing yet on first launch. Anything other than one of
    the three real choices falls back to dark rather than propagating into
    _resolve_theme (which only knows what to do with those three)."""
    return value if value in ("dark", "light", "system") else "dark"


def _resolve_theme(choice: str) -> str:
    """"dark"/"light" pass straight through; "system" resolves against the
    desktop's actual live color-scheme preference (confirmed this reports
    correctly on this machine's real desktop, not just assumed available
    because the Qt version is new enough) -- Unknown (a platform that
    doesn't expose one) falls back to dark, this app's original default."""
    if choice != "system":
        return choice
    scheme = QApplication.instance().styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"


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
