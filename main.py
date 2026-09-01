#!/usr/bin/env python3
"""TITAN-i Transcoder: minimal ffmpeg front-end replacing HandBrake QSV."""
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSettings
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QSplitter, QGroupBox, QListWidget, QListWidgetItem,
    QPushButton, QComboBox, QLabel, QProgressBar, QPlainTextEdit, QFileDialog,
    QLineEdit, QSlider, QSpinBox, QCheckBox, QInputDialog, QMessageBox,
    QSizePolicy, QStyle, QAbstractItemView,
)

import worker
from constants import (
    VIDEO_FILTER, AUDIO_TRACK_LABELS, ENCODERS, RC_MODES, QUALITY_RANGES,
    X265_PRESETS, RESOLUTIONS, AUDIO_BITRATES, CONTAINERS, X265_TUNES,
    BUILTIN_PRESETS, BUILTIN_PRESET_NAMES,
)
from presets import load_user_presets, save_user_presets
from worker import TranscodeQueue, BITRATE_RC_MODES

PANEL_MARGIN = 12
PANEL_SPACING = 10


class DropListWidget(QListWidget):
    """QListWidget that accepts files dragged in from a file manager, and
    also supports dragging its own rows to reorder the queue."""

    PLACEHOLDER_TEXT = "Drag video files here,\nor click “Add Files…”"

    def __init__(self, on_files_dropped, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self._on_files_dropped = on_files_dropped

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
        else:
            super().dropEvent(event)  # internal row-reorder drop

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
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
        self._loaded_preset_settings: dict | None = None
        self._running_items: list[QListWidgetItem] = []
        self._current_running_item: QListWidgetItem | None = None
        self.output_dir = Path.home() / "Videos" / "transcoded"
        self._qsettings = QSettings("TITAN-i", "Transcoder")

        self.queue = TranscodeQueue()
        self.queue.job_started.connect(self._on_job_started)
        self.queue.job_progress.connect(self._on_job_progress)
        self.queue.job_stats.connect(self._on_job_stats)
        self.queue.job_log.connect(self._on_job_log)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.job_failed.connect(self._on_job_failed)
        self.queue.all_finished.connect(self._on_all_finished)

        self._build_ui()
        # Must run before _refresh_preset_combo(): it's the only thing that
        # populates rc_mode_combo, and applying a preset while that combo is
        # still empty leaves rc_mode reading back as None.
        self._on_encoder_changed()
        self._refresh_preset_combo()
        self._restore_window_state()

    def closeEvent(self, event):
        self._qsettings.setValue("window_geometry", self.saveGeometry())
        self._qsettings.setValue("splitter_state", self._splitter.saveState())
        super().closeEvent(event)

    def _restore_window_state(self):
        geometry = self._qsettings.value("window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
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
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        row.addWidget(self.preset_combo, 1)

        self.preset_modified_label = QLabel("(modified)")
        self.preset_modified_label.setStyleSheet("font-style: italic; font-size: 9pt;")
        self.preset_modified_label.setVisible(False)
        row.addWidget(self.preset_modified_label)

        style = self.style()
        save_btn = QPushButton(style.standardIcon(QStyle.SP_DialogSaveButton), "Save As…")
        save_btn.clicked.connect(self._save_preset_as)
        delete_btn = QPushButton(style.standardIcon(QStyle.SP_TrashIcon), "Delete")
        delete_btn.clicked.connect(self._delete_preset)
        row.addWidget(save_btn)
        row.addWidget(delete_btn)
        return row

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

        layout.addWidget(self._build_command_preview())

        self.hw_status_label = QLabel(self._hardware_status_text())
        # palette(mid) is meant for borders/shadows, not body text -- against
        # a dark theme it's nearly unreadable. Rely on the default (theme-
        # correct in both light and dark) text color; a smaller size is
        # enough to read as "secondary detail" without losing contrast.
        self.hw_status_label.setStyleSheet("font-size: 10pt;")
        self.hw_status_label.setWordWrap(True)
        layout.addWidget(self.hw_status_label)

        layout.addStretch(1)
        return left

    @staticmethod
    def _hardware_status_text() -> str:
        try:
            node = worker.find_render_node(worker.INTEL_VENDOR_ID)
            return f"Hardware encode available via {node} (Intel iGPU)"
        except RuntimeError:
            return "No Intel VAAPI render node detected — hardware encoding unavailable"

    def _build_command_preview(self) -> QGroupBox:
        group = QGroupBox("Effective Command")
        layout = QVBoxLayout(group)
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(110)
        self.command_preview.setStyleSheet("font-family: monospace; font-size: 9pt;")
        self.command_preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        layout.addWidget(self.command_preview)
        return group

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setSpacing(PANEL_SPACING)

        encoding_group = QGroupBox("Encoding")
        self.video_form = form = QFormLayout(encoding_group)

        self.encoder_combo = QComboBox()
        for _, label in ENCODERS:
            self.encoder_combo.addItem(label)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        form.addRow("Encoder:", self.encoder_combo)

        self.rc_mode_combo = QComboBox()
        self.rc_mode_combo.currentIndexChanged.connect(self._on_rc_mode_changed)
        form.addRow("Rate control:", self.rc_mode_combo)

        quality_row = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Horizontal)
        self.quality_slider.valueChanged.connect(self._on_quality_changed)
        self.quality_label = QLabel()
        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(200, 50000)
        self.bitrate_spin.setSingleStep(100)
        self.bitrate_spin.setSuffix(" kbps")
        self.bitrate_spin.valueChanged.connect(self._update_command_preview)
        quality_row.addWidget(self.quality_slider, 1)
        quality_row.addWidget(self.quality_label)
        quality_row.addWidget(self.bitrate_spin, 1)
        form.addRow("Quality / Bitrate:", quality_row)

        speed_row = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(1, 7)
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        self.speed_label = QLabel()
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(X265_PRESETS)
        self.speed_combo.setCurrentText("medium")
        self.speed_combo.currentIndexChanged.connect(self._update_command_preview)
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_label)
        speed_row.addWidget(self.speed_combo, 1)
        form.addRow("Speed:", speed_row)

        self.bitdepth_combo = QComboBox()
        self.bitdepth_combo.addItems(["8-bit", "10-bit"])
        self.bitdepth_combo.setCurrentText("10-bit")
        self.bitdepth_combo.currentIndexChanged.connect(self._update_command_preview)
        form.addRow("Bit depth:", self.bitdepth_combo)

        self.tune_combo = QComboBox()
        self.tune_combo.addItems(X265_TUNES)
        self.tune_combo.currentIndexChanged.connect(self._update_command_preview)
        form.addRow("Tune (x265 only):", self.tune_combo)

        self.deinterlace_check = QCheckBox("Deinterlace (interlaced or telecined source)")
        self.deinterlace_check.setToolTip(
            "Container-level progressive/interlaced flags are frequently wrong,\n"
            "especially on camcorder-sourced footage -- this isn't auto-detected,\n"
            "turn it on if the output shows combing/interlacing artifacts."
        )
        self.deinterlace_check.stateChanged.connect(self._update_command_preview)
        form.addRow("", self.deinterlace_check)

        outer.addWidget(encoding_group)

        output_group = QGroupBox("Output Shape")
        out_form = QFormLayout(output_group)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        self.res_combo.setCurrentIndex(2)  # 720p
        self.res_combo.currentIndexChanged.connect(self._update_command_preview)
        out_form.addRow("Resolution:", self.res_combo)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)
        self.container_combo.currentIndexChanged.connect(self._update_command_preview)
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
        self.audio_combo.currentIndexChanged.connect(self._update_command_preview)
        form.addRow("Audio track:", self.audio_combo)

        self.audio_copy_check = QCheckBox("Copy audio if compatible (aac/ac3/eac3)")
        self.audio_copy_check.setChecked(True)
        self.audio_copy_check.stateChanged.connect(self._update_command_preview)
        form.addRow("", self.audio_copy_check)

        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(AUDIO_BITRATES)
        self.audio_bitrate_combo.setCurrentText("160k")
        self.audio_bitrate_combo.currentIndexChanged.connect(self._update_command_preview)
        form.addRow("Audio bitrate (if transcoded):", self.audio_bitrate_combo)

        outer.addWidget(group)
        return tab

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        layout.addWidget(QLabel("Queue (drag files here, or use Add Files):"))
        self.queue_list = DropListWidget(self.add_files)
        layout.addWidget(self.queue_list, 1)

        q_btns = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Files…")
        self.add_files_btn.clicked.connect(self._pick_files)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear Queue")
        self.clear_btn.clicked.connect(self._clear_queue)
        self.apply_btn = QPushButton("Apply Settings to Selected")
        self.apply_btn.clicked.connect(self._apply_to_selected)
        q_btns.addWidget(self.add_files_btn)
        q_btns.addWidget(self.remove_btn)
        q_btns.addWidget(self.clear_btn)
        q_btns.addWidget(self.apply_btn)
        q_btns.addStretch()
        layout.addLayout(q_btns)

        # Output folder is a per-run detail, not the first decision anyone
        # makes -- it lives right next to Start, where it's actually used.
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_dir))
        self.output_edit.setReadOnly(True)
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

        layout.addWidget(QLabel("Log:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("ffmpeg output will appear here once a job starts…")
        layout.addWidget(self.log_view, 2)
        return right

    # --- cascading settings behavior ---
    def _current_encoder_id(self) -> str:
        return ENCODERS[self.encoder_combo.currentIndex()][0]

    def _on_encoder_changed(self):
        encoder = self._current_encoder_id()
        is_vaapi = encoder == "hevc_vaapi"

        self.rc_mode_combo.blockSignals(True)
        self.rc_mode_combo.clear()
        for value, label in RC_MODES[encoder]:
            self.rc_mode_combo.addItem(label, userData=value)
        self.rc_mode_combo.blockSignals(False)

        self.speed_slider.setVisible(is_vaapi)
        self.speed_label.setVisible(is_vaapi)
        self.speed_combo.setVisible(not is_vaapi)
        self._on_speed_slider_changed()
        self.video_form.setRowVisible(self.tune_combo, not is_vaapi)

        self._on_rc_mode_changed()

    def _on_rc_mode_changed(self):
        # rc_mode_combo is always populated by this point -- __init__ calls
        # _on_encoder_changed() (the only thing that populates it) before
        # anything that could apply a preset and reach this method.
        rc_mode = self.rc_mode_combo.currentData()
        is_bitrate = rc_mode in BITRATE_RC_MODES
        self.quality_slider.setVisible(not is_bitrate)
        self.quality_label.setVisible(not is_bitrate)
        self.bitrate_spin.setVisible(is_bitrate)
        if not is_bitrate:
            lo, hi, default = QUALITY_RANGES[rc_mode]
            self.quality_slider.blockSignals(True)
            self.quality_slider.setRange(lo, hi)
            self.quality_slider.setValue(default)
            self.quality_slider.blockSignals(False)
            self._on_quality_changed()
        else:
            self._update_command_preview()

    def _on_quality_changed(self):
        rc_mode = self.rc_mode_combo.currentData()
        self.quality_label.setText(f"{self.quality_slider.value()} ({rc_mode})")
        self._update_command_preview()

    def _on_speed_slider_changed(self):
        self.speed_label.setText(f"{self.speed_slider.value()} (compression_level)")
        self._update_command_preview()

    def _update_command_preview(self):
        if not hasattr(self, "command_preview"):
            return  # widgets still being constructed
        settings = self._current_settings()
        output_path = Path(f"output.{settings['container']}")
        try:
            if self.queue_list.count() > 0:
                # A real file is queued -- probe its actual audio track (cached,
                # so dragging a slider doesn't shell out to ffprobe repeatedly)
                # instead of guessing, so the preview matches what will really run.
                first_path = self.queue_list.item(0).data(Qt.UserRole)["path"]
                audio_codec = self._preview_audio_codec(first_path, settings["audio_track"])
                args = worker.build_args(
                    settings, first_path, output_path,
                    probe_audio=False, audio_codec=audio_codec,
                )
            else:
                # No file queued yet -- there's no real audio track to reflect,
                # so omit the audio codec decision entirely rather than assert
                # a codec that would misrepresent what actually happens.
                args = worker.build_args(
                    settings, Path("input.ext"), output_path,
                    probe_audio=False, audio_codec=None,
                )
            self.command_preview.setPlainText(self._format_preview_text(args))
        except Exception as exc:
            # build_args can hit real hardware (find_render_node) for the
            # VAAPI path -- on a machine with no Intel node this must degrade
            # to a message, not crash the control that triggered it.
            self.command_preview.setPlainText(f"(preview unavailable: {exc})")
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

    def _update_preset_modified_indicator(self):
        if not hasattr(self, "preset_modified_label"):
            return
        if self._loaded_preset_settings is None:
            self.preset_modified_label.setVisible(False)
            return
        self.preset_modified_label.setVisible(self._current_settings() != self._loaded_preset_settings)

    # --- settings <-> controls ---
    def _current_settings(self) -> dict:
        encoder = self._current_encoder_id()
        rc_mode = self.rc_mode_combo.currentData()
        is_bitrate = rc_mode in BITRATE_RC_MODES
        res = RESOLUTIONS[self.res_combo.currentIndex()]
        return {
            "encoder": encoder,
            "rc_mode": rc_mode,
            "quality_value": self.bitrate_spin.value() if is_bitrate else self.quality_slider.value(),
            "speed": self.speed_combo.currentText() if encoder == "libx265" else str(self.speed_slider.value()),
            "bit_depth": 10 if self.bitdepth_combo.currentText() == "10-bit" else 8,
            "width": res["width"],
            "height": res["height"],
            "container": self.container_combo.currentText(),
            "tune": self.tune_combo.currentText(),
            "deinterlace": self.deinterlace_check.isChecked(),
            "audio_track": self.audio_combo.currentIndex(),
            "audio_copy_if_compatible": self.audio_copy_check.isChecked(),
            "audio_bitrate": self.audio_bitrate_combo.currentText(),
        }

    def _apply_settings_to_controls(self, settings: dict):
        encoder_index = 0 if settings["encoder"] == "hevc_vaapi" else 1
        self.encoder_combo.setCurrentIndex(encoder_index)  # cascades rc_mode/speed/tune rebuild

        rc_index = next(
            (i for i in range(self.rc_mode_combo.count())
             if self.rc_mode_combo.itemData(i) == settings["rc_mode"]), 0
        )
        self.rc_mode_combo.setCurrentIndex(rc_index)  # cascades quality/bitrate widget swap

        if settings["rc_mode"] in BITRATE_RC_MODES:
            self.bitrate_spin.setValue(settings["quality_value"])
        else:
            self.quality_slider.setValue(settings["quality_value"])

        if settings["encoder"] == "libx265":
            self.speed_combo.setCurrentText(settings["speed"])
        else:
            self.speed_slider.setValue(int(settings["speed"]))

        self.bitdepth_combo.setCurrentText("10-bit" if settings["bit_depth"] == 10 else "8-bit")

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
        self.audio_bitrate_combo.setCurrentText(settings["audio_bitrate"])
        self._update_command_preview()

    def _format_item_text(self, job: dict) -> str:
        enc_tag = "VAAPI" if job["encoder"] == "hevc_vaapi" else "x265"
        rc = job["rc_mode"]
        qv = job["quality_value"]
        q_str = f"{qv}kbps" if rc in BITRATE_RC_MODES else f"{rc}{qv}"
        res_label = self._res_label.get((job["width"], job["height"]), f"{job['width']}x{job['height']}")
        deinterlace_tag = " · Deinterlace" if job.get("deinterlace") else ""
        return (
            f"{job['path'].name}   "
            f"[{enc_tag} {job['bit_depth']}b · {q_str} · {res_label} · "
            f"{AUDIO_TRACK_LABELS[job['audio_track']]} · {job.get('container', 'mp4')}{deinterlace_tag}]"
        )

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
                item = QListWidgetItem(self._format_item_text(job))
                item.setData(Qt.UserRole, job)
                self.queue_list.addItem(item)
        self._update_command_preview()  # may now reflect a real queued file's audio

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

    def _open_output_dir(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def _remove_selected(self):
        for item in self.queue_list.selectedItems():
            self.queue_list.takeItem(self.queue_list.row(item))
        self._update_command_preview()

    def _clear_queue(self):
        count = self.queue_list.count()
        if count == 0:
            return
        if QMessageBox.question(
            self, "Clear queue", f"Remove all {count} file(s) from the queue?"
        ) != QMessageBox.Yes:
            return
        self.queue_list.clear()
        self._update_command_preview()

    def _apply_to_selected(self):
        settings = self._current_settings()
        for item in self.queue_list.selectedItems():
            job = item.data(Qt.UserRole)
            job.update(settings)
            item.setData(Qt.UserRole, job)
            item.setText(self._format_item_text(job))

    # --- run control ---
    def _start(self):
        jobs = [self.queue_list.item(i).data(Qt.UserRole) for i in range(self.queue_list.count())]
        if not jobs:
            self.status_label.setText("Queue is empty")
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
        self._running_items = [self.queue_list.item(i) for i in range(self.queue_list.count())]
        for item in self._running_items:
            item.setIcon(QIcon())  # clear any status icon left from a previous run
        self.queue.start(jobs, self.output_dir)

    def _stop(self):
        self.queue.stop()
        self.stop_btn.setEnabled(False)

    def _set_queue_editable(self, editable: bool):
        # TranscodeQueue.start() snapshots the job list once; editing the
        # visible queue after that point can't affect jobs already running
        # or already skipped, so it just makes the list lie about what's
        # actually executing. Lock it for the duration of a run.
        self.add_files_btn.setEnabled(editable)
        self.remove_btn.setEnabled(editable)
        self.clear_btn.setEnabled(editable)
        self.apply_btn.setEnabled(editable)

    # --- queue signal handlers ---
    def _on_job_started(self, path: str, index: int, total: int):
        self.status_label.setText(f"[{index}/{total}] Encoding {Path(path).name}")
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")
        self.log_view.appendPlainText(f"\n=== Starting {path} ===")
        self._current_running_item = self._running_items[index - 1]
        self._current_running_item.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))

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
            self._current_running_item.setIcon(self.style().standardIcon(QStyle.SP_DialogApplyButton))
            self._append_result_size(self._current_running_item, Path(path), Path(output_path))

    def _on_job_failed(self, path: str, reason: str):
        self.log_view.appendPlainText(f"=== FAILED: {path}: {reason} ===")
        if self._current_running_item is not None:
            self._current_running_item.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxWarning))
            self._current_running_item.setToolTip(reason)

    @staticmethod
    def _append_result_size(item: QListWidgetItem, input_path: Path, output_path: Path):
        try:
            in_size = input_path.stat().st_size
            out_size = output_path.stat().st_size
        except OSError:
            return
        if in_size <= 0:
            return
        change_pct = 100 * (1 - out_size / in_size)
        direction = "smaller" if change_pct >= 0 else "larger"
        item.setText(f"{item.text()}  →  {MainWindow._format_size(out_size)} ({abs(change_pct):.0f}% {direction})")

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


def _load_stylesheet(app, style_path: Path = Path(__file__).parent / "style.qss"):
    try:
        app.setStyleSheet(style_path.read_text())
    except OSError as exc:
        # Missing/unreadable style.qss shouldn't take the whole app down --
        # fall back to plain Fusion rather than crash at startup over theming.
        print(f"Warning: couldn't load {style_path} ({exc}); using unstyled Fusion.")


def main():
    app = QApplication(sys.argv)
    # Fusion is the style QSS was written against -- native styles (Breeze,
    # Windows) silently ignore some of the subcontrols the theme relies on,
    # e.g. the slider groove/handle and the combobox popup background.
    app.setStyle("Fusion")
    _load_stylesheet(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
