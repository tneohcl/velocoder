#!/usr/bin/env python3
"""TITAN-i Transcoder: minimal ffmpeg front-end replacing HandBrake QSV."""
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QTabWidget, QSplitter, QListWidget, QListWidgetItem, QPushButton, QComboBox,
    QLabel, QProgressBar, QPlainTextEdit, QFileDialog, QLineEdit, QSlider,
    QSpinBox, QCheckBox, QInputDialog, QMessageBox, QSizePolicy,
)

from constants import (
    VIDEO_FILTER, AUDIO_TRACK_LABELS, ENCODERS, RC_MODES, QUALITY_RANGES,
    X265_PRESETS, RESOLUTIONS, AUDIO_BITRATES, CONTAINERS, X265_TUNES,
    BUILTIN_PRESETS, BUILTIN_PRESET_NAMES,
)
from presets import load_user_presets, save_user_presets
from worker import TranscodeQueue, BITRATE_RC_MODES


class DropListWidget(QListWidget):
    """QListWidget that accepts files dragged in from a file manager."""

    def __init__(self, on_files_dropped, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self._on_files_dropped = on_files_dropped

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        self._on_files_dropped(paths)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("TITAN-i Transcoder")
        self.resize(1200, 800)

        self.user_presets: list[dict] = load_user_presets()
        self._res_label = {(r["width"], r["height"]): r["label"] for r in RESOLUTIONS}
        self.output_dir = Path.home() / "Videos" / "transcoded"

        self.queue = TranscodeQueue()
        self.queue.job_started.connect(self._on_job_started)
        self.queue.job_progress.connect(self._on_job_progress)
        self.queue.job_stats.connect(self._on_job_stats)
        self.queue.job_log.connect(self._on_job_log)
        self.queue.job_finished.connect(self._on_job_finished)
        self.queue.job_failed.connect(self._on_job_failed)
        self.queue.all_finished.connect(self._on_all_finished)

        self._build_ui()
        self._refresh_preset_combo()
        self._on_encoder_changed()

    # --- UI construction ---
    def _build_ui(self):
        splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(splitter)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

    def _build_left_panel(self) -> QWidget:
        left = QWidget()
        layout = QVBoxLayout(left)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_dir))
        self.output_edit.setReadOnly(True)
        browse_btn = QPushButton("Output Folder…")
        browse_btn.clicked.connect(self._pick_output_dir)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_output_dir)
        out_row.addWidget(QLabel("Output:"))
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_btn)
        out_row.addWidget(open_btn)
        layout.addLayout(out_row)

        tabs = QTabWidget()
        tabs.addTab(self._build_preset_tab(), "Preset")
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
        # Tab pages have very different row counts (Preset: 2, Video: 8) --
        # without this, the tab widget stretches to fill the splitter pane
        # and the sparser tabs look broken. Cap it to its content's natural
        # height instead and let the trailing stretch take the rest.
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(tabs)
        layout.addStretch(1)
        return left

    def _build_preset_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        save_preset_btn = QPushButton("Save As…")
        save_preset_btn.clicked.connect(self._save_preset_as)
        delete_preset_btn = QPushButton("Delete")
        delete_preset_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(self.preset_combo, 1)
        form.addRow("Preset:", preset_row)

        btn_row = QHBoxLayout()
        btn_row.addWidget(save_preset_btn)
        btn_row.addWidget(delete_preset_btn)
        btn_row.addStretch()
        form.addRow("", btn_row)
        return tab

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        self.video_form = form = QFormLayout(tab)

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
        speed_row.addWidget(self.speed_slider, 1)
        speed_row.addWidget(self.speed_label)
        speed_row.addWidget(self.speed_combo, 1)
        form.addRow("Speed:", speed_row)

        self.bitdepth_combo = QComboBox()
        self.bitdepth_combo.addItems(["8-bit", "10-bit"])
        self.bitdepth_combo.setCurrentText("10-bit")
        form.addRow("Bit depth:", self.bitdepth_combo)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        self.res_combo.setCurrentIndex(2)  # 720p
        form.addRow("Resolution:", self.res_combo)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)
        form.addRow("Container:", self.container_combo)

        self.tune_combo = QComboBox()
        self.tune_combo.addItems(X265_TUNES)
        form.addRow("Tune (x265 only):", self.tune_combo)

        return tab

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        form = QFormLayout(tab)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(AUDIO_TRACK_LABELS)
        form.addRow("Audio track:", self.audio_combo)

        self.audio_copy_check = QCheckBox("Copy audio if compatible (aac/ac3/eac3)")
        self.audio_copy_check.setChecked(True)
        form.addRow("", self.audio_copy_check)

        self.audio_bitrate_combo = QComboBox()
        self.audio_bitrate_combo.addItems(AUDIO_BITRATES)
        self.audio_bitrate_combo.setCurrentText("160k")
        form.addRow("Audio bitrate (if transcoded):", self.audio_bitrate_combo)
        return tab

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)

        layout.addWidget(QLabel("Queue (drag files here, or use Add Files):"))
        self.queue_list = DropListWidget(self.add_files)
        layout.addWidget(self.queue_list, 1)

        q_btns = QHBoxLayout()
        add_btn = QPushButton("Add Files…")
        add_btn.clicked.connect(self._pick_files)
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn = QPushButton("Clear Queue")
        clear_btn.clicked.connect(self._clear_queue)
        apply_btn = QPushButton("Apply Settings to Selected")
        apply_btn.clicked.connect(self._apply_to_selected)
        q_btns.addWidget(add_btn)
        q_btns.addWidget(remove_btn)
        q_btns.addWidget(clear_btn)
        q_btns.addWidget(apply_btn)
        q_btns.addStretch()
        layout.addLayout(q_btns)

        run_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        run_row.addWidget(self.start_btn)
        run_row.addWidget(self.stop_btn)
        layout.addLayout(run_row)

        self.status_label = QLabel("Idle")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.stats_label)

        layout.addWidget(QLabel("Log:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
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
        rc_mode = self.rc_mode_combo.currentData()
        if rc_mode is None:
            return
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

    def _on_quality_changed(self):
        rc_mode = self.rc_mode_combo.currentData()
        self.quality_label.setText(f"{self.quality_slider.value()} ({rc_mode})")

    def _on_speed_slider_changed(self):
        self.speed_label.setText(f"{self.speed_slider.value()} (compression_level)")

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
        self.audio_combo.setCurrentIndex(settings["audio_track"])
        self.audio_copy_check.setChecked(settings["audio_copy_if_compatible"])
        self.audio_bitrate_combo.setCurrentText(settings["audio_bitrate"])

    def _format_item_text(self, job: dict) -> str:
        enc_tag = "VAAPI" if job["encoder"] == "hevc_vaapi" else "x265"
        rc = job["rc_mode"]
        qv = job["quality_value"]
        q_str = f"{qv}kbps" if rc in BITRATE_RC_MODES else f"{rc}{qv}"
        res_label = self._res_label.get((job["width"], job["height"]), f"{job['width']}x{job['height']}")
        return (
            f"{job['path'].name}   "
            f"[{enc_tag} {job['bit_depth']}b · {q_str} · {res_label} · "
            f"{AUDIO_TRACK_LABELS[job['audio_track']]} · {job.get('container', 'mp4')}]"
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
        self._refresh_preset_combo(select=name)

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

    def _clear_queue(self):
        self.queue_list.clear()

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
        self.log_view.clear()
        self.queue.start(jobs, self.output_dir)

    def _stop(self):
        self.queue.stop()
        self.stop_btn.setEnabled(False)

    # --- queue signal handlers ---
    def _on_job_started(self, path: str, index: int, total: int):
        self.status_label.setText(f"[{index}/{total}] Encoding {Path(path).name}")
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")
        self.log_view.appendPlainText(f"\n=== Starting {path} ===")

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

    def _on_job_finished(self, path: str):
        self.log_view.appendPlainText(f"=== Done: {path} ===")

    def _on_job_failed(self, path: str, reason: str):
        self.log_view.appendPlainText(f"=== FAILED: {path}: {reason} ===")

    def _on_all_finished(self):
        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
