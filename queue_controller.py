"""Queue management, file/output pickers, run control, and the
TranscodeQueue signal handlers -- split out of MainWindow into a mixin,
same pattern and same reasoning as ui_builder.py's _UiBuilderMixin.
Everything here still lands on `self` exactly as before the split."""
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox, QTreeWidgetItem

import worker
import formatting
from constants import VIDEO_FILTER
from queue_widget import (
    FILE_COL, VIDEO_COL, DURATION_COL, AUDIO_COL, SIZE_COL, RESULT_COL,
    STATUS_COL, QUEUE_COLUMN_HEADERS,
)


class _QueueControllerMixin:
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

    @staticmethod
    def _row_tooltip(job: dict) -> str:
        # File path first (useful when the File column itself is
        # truncated), then a friendly summary of the actual output
        # settings this row will encode with -- distinct from the File
        # column's own text (source properties only, see _make_queue_row
        # below) and from Effective Command's raw ffmpeg argv (main.py),
        # which is for a technical reader specifically.
        return f"{job['path']}\n\n{formatting.settings_summary(job)}"

    def _make_queue_row(self, job: dict) -> QTreeWidgetItem:
        # Deliberately source-properties-only (file/resolution/duration/
        # codecs/size) -- the chosen output settings (encoder, quality,
        # container, ...) already live in and edit live from the right-hand
        # panel for whichever row is selected, so repeating them here would
        # just be the same information twice.
        item = QTreeWidgetItem()
        item.setData(STATUS_COL, Qt.UserRole, job)
        item.setText(FILE_COL, job["path"].name)
        tooltip = self._row_tooltip(job)
        for col in range(len(QUEUE_COLUMN_HEADERS)):
            item.setToolTip(col, tooltip)
        try:
            item.setText(SIZE_COL, formatting.format_size(job["path"].stat().st_size))
        except OSError:
            pass
        return item

    # --- undo/redo (queue structure only -- add/remove/clear/reorder;
    # per-item settings edits and preset save/delete are deliberately out
    # of scope) ---
    def _queue_snapshot(self) -> list[dict]:
        return [
            dict(self.queue_list.topLevelItem(i).data(STATUS_COL, Qt.UserRole))
            for i in range(self.queue_list.topLevelItemCount())
        ]

    def _restore_queue_snapshot(self, snapshot: list[dict]):
        # Rebuilds fresh rows and restarts both probes for each, rather
        # than trying to preserve exactly what was on screen -- same
        # brief blank-columns-while-probing state a fresh Add Files
        # already shows, not a new degraded one. Only ever called while
        # idle (_undo/_redo below both refuse mid-run), so there's no
        # live run whose in-flight item references would go stale here.
        self.queue_list.clear()
        self._pending_mid_run_items.clear()
        for job in snapshot:
            item = self._make_queue_row(dict(job))
            self.queue_list.addTopLevelItem(item)
            self._start_interlace_detection(item, job["path"])
            self._start_source_probe(item, job["path"])
        self._update_command_preview()

    def _push_undo_snapshot(self):
        # Disabled entirely during a run, matching every other queue-
        # structure lock already in place (_set_queue_editable) --
        # rebuilding the queue mid-run via undo/redo would conflict with
        # the live run's own _running_items/TranscodeQueue._jobs
        # bookkeeping.
        if not self._queue_editable:
            return
        self._undo_stack.append(self._queue_snapshot())
        del self._undo_stack[:-50]  # cap -- keep only the most recent 50
        self._redo_stack.clear()  # any new action invalidates redo history

    def _undo(self):
        if not self._queue_editable or not self._undo_stack:
            return
        self._redo_stack.append(self._queue_snapshot())
        self._restore_queue_snapshot(self._undo_stack.pop())

    def _redo(self):
        if not self._queue_editable or not self._redo_stack:
            return
        self._undo_stack.append(self._queue_snapshot())
        self._restore_queue_snapshot(self._redo_stack.pop())

    # --- queue management ---
    def add_files(self, paths: list[Path]):
        real_paths = [p for p in paths if p.is_file()]
        if real_paths:
            self._push_undo_snapshot()
        for path in real_paths:
            job = {"path": path, **self._current_settings()}
            item = self._make_queue_row(job)
            self.queue_list.addTopLevelItem(item)
            if not self._queue_editable:
                # A run is already in progress -- don't hand this
                # straight to the live queue yet. Confirmed a real race
                # otherwise: if the currently-running job finishes before
                # this file's own ~20s interlace sample does, worker.py's
                # update_pending_job can't patch it (its own docstring:
                # only jobs still ahead of the queue's position get
                # patched) -- the file would start encoding with
                # whatever default deinterlace value it began with.
                # _maybe_submit_mid_run_job below submits it (with
                # by-then-final settings) only once both probes have
                # actually landed; if the run finishes first, it just
                # sits queued, unsubmitted, until Start is clicked again
                # -- no auto-resume plumbing needed, and worker.py stays
                # exactly as decoupled from detection state as it
                # already is by design.
                self._pending_mid_run_items[item] = 2
            self._start_interlace_detection(item, path)
            self._start_source_probe(item, path)
        self._update_command_preview()  # may now reflect a real queued file's audio

    def _maybe_submit_mid_run_job(self, item: QTreeWidgetItem):
        remaining = self._pending_mid_run_items.get(item)
        if remaining is None:
            return
        remaining -= 1
        if remaining > 0:
            self._pending_mid_run_items[item] = remaining
            return
        del self._pending_mid_run_items[item]
        try:
            job = item.data(STATUS_COL, Qt.UserRole)
        except RuntimeError:
            return  # item's C++ object was deleted (e.g. Clear Queue) before both probes landed
        if job is None:
            return
        self.queue.add_job(dict(job))
        self._running_items.append(item)

    def _on_probe_error(self, item: QTreeWidgetItem, proc: QProcess, error):
        # Mirrors worker.py's TranscodeQueue._on_process_error -- QProcess
        # .finished never fires when the binary itself fails to start
        # (confirmed directly), only errorOccurred(FailedToStart) does, so
        # without this a missing/broken ffmpeg or ffprobe install left proc
        # in _detection_processes forever, and _start() refuses to run
        # while that list is non-empty. Shared by both
        # _start_interlace_detection and _start_source_probe below -- either
        # way a probe failing to even start just means that file's
        # video/audio columns or interlace flag stay at whatever they
        # already were, same degraded-but-safe state as before any probe
        # completes. Every other error type still reaches finished too, so
        # only FailedToStart is handled here to avoid double-processing.
        if error != QProcess.ProcessError.FailedToStart:
            return
        self._detection_processes = [p for p in self._detection_processes if p is not proc]
        # Otherwise a mid-run add whose probe never even starts would leave
        # _pending_mid_run_items stuck at a nonzero count forever for this
        # item -- harmless functionally (Start still picks the row up from
        # the queue list next time regardless), but a needless permanent
        # bookkeeping leak this avoids for free.
        self._maybe_submit_mid_run_job(item)

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
        proc.errorOccurred.connect(lambda error: self._on_probe_error(item, proc, error))
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
            self._maybe_submit_mid_run_job(item)  # no-ops: same RuntimeError, caught there too
            return  # item's C++ object was deleted (e.g. Clear Queue) before detection finished
        if job is None:
            self._maybe_submit_mid_run_job(item)
            return
        if job.get("deinterlace_user_set"):
            # The user already explicitly set this file's deinterlace value
            # (see _on_deinterlace_checkbox_changed) -- their choice wins,
            # a same-file detection result landing after that shouldn't
            # silently replace it.
            self._maybe_submit_mid_run_job(item)
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
        self._maybe_submit_mid_run_job(item)

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
        proc.errorOccurred.connect(lambda error: self._on_probe_error(item, proc, error))
        self._detection_processes.append(proc)
        proc.start()

    def _on_source_probed(self, item: QTreeWidgetItem, stdout_text: str):
        self._detection_processes = [p for p in self._detection_processes if p.state() != QProcess.NotRunning]
        try:
            item.text(FILE_COL)  # touch the item; raises RuntimeError if its C++ object is gone
        except RuntimeError:
            self._maybe_submit_mid_run_job(item)  # no-ops: same RuntimeError, caught there too
            return  # item deleted (e.g. Clear Queue) before the probe landed
        info = worker.parse_probe_output(stdout_text)
        if info.get("duration"):
            item.setText(DURATION_COL, formatting.format_eta(info["duration"]))
        if info.get("video_codec"):
            label = formatting.video_codec_label(info["video_codec"])
            if info.get("width") and info.get("height"):
                label += f" {info['width']}x{info['height']}"
            item.setData(VIDEO_COL, Qt.UserRole, label)
            self._refresh_video_cell(item)
        if info.get("audio_codec"):
            extra = info.get("audio_track_count", 1) - 1
            suffix = f"  +{extra} more" if extra > 0 else ""
            channel_label = formatting.audio_channel_label(info.get("audio_channels"))
            item.setText(AUDIO_COL, f"{formatting.audio_codec_label(info['audio_codec'])} {channel_label}{suffix}")
        self._maybe_submit_mid_run_job(item)

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
            # .expanduser() -- a bare Path("~/...") does NOT expand the
            # tilde (confirmed directly), so a typed "~/Videos/out" silently
            # created a literal folder named "~" under the process's cwd
            # instead. .resolve() -- build_args appends the output path as
            # a bare final argv element with nothing preceding it, so a
            # relative path/folder name starting with "-" (e.g. "-render")
            # gets parsed by ffmpeg as a flag, not a filename (confirmed
            # against real ffmpeg: "Unrecognized option"). A resolved
            # absolute path can never start with "-", fixing both at once.
            # Reflected back into the field, matching what Browse
            # (_pick_output_dir above) already does after a selection.
            self.output_dir = Path(text).expanduser().resolve()
            self.output_edit.setText(str(self.output_dir))

    def _open_output_dir(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def _build_queue_context_menu(self, job: dict) -> QMenu:
        # Split out from _on_queue_context_menu below purely so the menu's
        # construction (which actions exist, which are enabled) can be
        # tested directly, without ever calling QMenu.exec() -- a real,
        # blocking, unmockable-from-Python C++ modal loop.
        menu = QMenu(self)
        reveal_source = menu.addAction("Reveal Source File")
        output_path = job.get("_completed_output_path")
        # Enabled only once a completed output actually exists -- a job
        # that hasn't run yet, is still running, or failed has nothing
        # real to reveal.
        reveal_output = menu.addAction("Reveal Output File")
        reveal_output.setEnabled(output_path is not None and Path(output_path).exists())
        return menu

    @staticmethod
    def _handle_queue_context_action(job: dict, action):
        # Also split out for the same testability reason as
        # _build_queue_context_menu above -- exercised directly against a
        # real QAction from a real (never-exec'd) menu, rather than
        # needing to fake what QMenu.exec() would have returned.
        if action is None:
            return
        # Opens the *containing folder*, matching _open_output_dir's own
        # behavior exactly above -- not a "select this file in the file
        # manager" action, which isn't portably available outside a
        # dolphin --select-style shell-out tied to one specific desktop.
        if action.text() == "Reveal Source File":
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(job["path"].parent)))
        elif action.text() == "Reveal Output File":
            output_path = job.get("_completed_output_path")
            if output_path is not None:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(output_path).parent)))

    def _on_queue_context_menu(self, pos):
        item = self.queue_list.itemAt(pos)
        if item is None:
            return
        item.setSelected(True)
        job = item.data(STATUS_COL, Qt.UserRole)
        if job is None:
            return
        menu = self._build_queue_context_menu(job)
        action = menu.exec(self.queue_list.viewport().mapToGlobal(pos))
        self._handle_queue_context_action(job, action)

    def _remove_selected(self):
        # Previously only reachable via the Remove button, which
        # _set_queue_editable already disables during a run -- the new
        # Delete-key shortcut (main.py) reaches this directly, bypassing
        # that. An explicit guard here protects the invariant at the
        # source regardless of how this gets called, current or future,
        # rather than depending on every caller remembering to check.
        if not self._queue_editable:
            return
        selected = self.queue_list.selectedItems()
        if not selected:
            return
        self._push_undo_snapshot()
        for item in selected:
            self.queue_list.takeTopLevelItem(self.queue_list.indexOfTopLevelItem(item))
        self._update_command_preview()

    def _clear_queue(self):
        # Same reasoning as _remove_selected's own guard above -- explicit
        # here too rather than relying solely on the Clear button being
        # disabled during a run.
        if not self._queue_editable:
            return
        count = self.queue_list.topLevelItemCount()
        if count == 0:
            return
        if QMessageBox.question(
            self, "Clear queue", f"Remove all {count} file(s) from the queue?"
        ) != QMessageBox.Yes:
            return
        self._push_undo_snapshot()
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
        # Otherwise a new run's very first job, if it fails preflight
        # before job_started ever reaches it, would get blamed on
        # whatever item _current_running_item was still pointing at from
        # the *previous* run -- confirmed a real gap, not just tidiness.
        self._current_running_item = None
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
        eta_str = formatting.format_eta(eta) if eta is not None else "--:--"
        self.stats_label.setText(f"{fps} fps  ·  {bitrate}  ·  {speed} speed  ·  ETA {eta_str}")

    def _on_job_log(self, line: str):
        self.log_view.appendPlainText(line)

    def _on_job_finished(self, path: str, output_path: str):
        self.log_view.appendPlainText(f"=== Done: {path} ===")
        if self._current_running_item is not None:
            self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_done"))
            self._append_result_size(self._current_running_item, Path(path), Path(output_path))
            # Retrievable later by the right-click "Reveal Output File"
            # action (see _on_queue_context_menu below) -- an extra
            # bookkeeping key alongside the real settings, same pattern
            # job["deinterlace_user_set"] already uses.
            job = self._current_running_item.data(STATUS_COL, Qt.UserRole)
            job["_completed_output_path"] = output_path
            self._current_running_item.setData(STATUS_COL, Qt.UserRole, job)

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
        item.setText(RESULT_COL, f"{formatting.format_size(out_size)} ({abs(change_pct):.0f}% {direction})")

    def _on_all_finished(self):
        self.status_label.setText("Idle")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_queue_editable(True)
        self.progress_bar.setValue(0)
        self.stats_label.setText("—")
