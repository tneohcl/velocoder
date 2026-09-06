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
    VIDEO_COL, DURATION_COL, SIZE_COL, RESULT_COL,
    STATUS_COL, VIDEO_SUBTITLE_ROLE, AUDIO_TRACK_COUNT_ROLE, QUEUE_COLUMN_HEADERS,
)

# Raw pieces _refresh_video_cell composes into the delegate-facing
# VIDEO_SUBTITLE_ROLE below -- kept separate from that final, already-
# combined text (not re-parsed back out of it) so whichever of video
# probe / audio probe / interlace detection lands *last* can recompose
# the whole subtitle fresh from all three, regardless of arrival order,
# without needing to know what the other two already contributed.
_RAW_VIDEO_LABEL_ROLE = Qt.UserRole + 2
_RAW_AUDIO_LABEL_ROLE = Qt.UserRole + 3


class _QueueControllerMixin:
    def _refresh_video_cell(self, item: QTreeWidgetItem):
        # Video-column *subtitle* (the delegate's second line -- see
        # queue_widget._VideoCellDelegate) depends on three independent
        # async results -- the source probe's own video codec/resolution
        # label and audio codec/channel label (each stashed on its own
        # raw role by _on_source_probed below), and the interlace
        # detector's job["deinterlace"] flag -- that can land in any
        # order. Recomposing fresh from all three raw pieces each time
        # any one of them arrives (rather than accumulating onto the
        # previous result) keeps this correct regardless of arrival
        # order, and safe to call more than once for the same reason.
        #
        # Written to VIDEO_SUBTITLE_ROLE, not item.setText() -- the
        # column's *text* is the filename (set once, in _make_queue_row,
        # title line of the delegate's two-line card), not this summary
        # (the delegate's muted subtitle line). VIDEO_SUBTITLE_ROLE, not
        # Qt.UserRole: VIDEO_COL and STATUS_COL are now the same column
        # index, and Qt.UserRole on it is already the job dict's own slot
        # -- see that role constant's own comment in queue_widget.py.
        video_label = item.data(VIDEO_COL, _RAW_VIDEO_LABEL_ROLE)
        if not video_label:
            return
        job = item.data(STATUS_COL, Qt.UserRole)
        suffix = " (interlaced)" if job and job.get("deinterlace") else ""
        audio_label = item.data(VIDEO_COL, _RAW_AUDIO_LABEL_ROLE)
        parts = [f"{video_label}{suffix}"]
        if audio_label:
            parts.append(audio_label)
        item.setData(VIDEO_COL, VIDEO_SUBTITLE_ROLE, "  ·  ".join(parts))

    @staticmethod
    def _row_tooltip(job: dict) -> str:
        # File path first (useful when the Video column's own filename
        # title is truncated), then a friendly summary of the actual
        # output settings this row will encode with -- distinct from the
        # Video column's own text (source properties only, see
        # _make_queue_row below) and from Effective Command's raw ffmpeg
        # argv (main.py), which is for a technical reader specifically.
        return f"{job['path']}\n\n{formatting.settings_summary(job)}"

    def _make_queue_row(self, job: dict) -> QTreeWidgetItem:
        # Deliberately source-properties-only (file/resolution/duration/
        # codecs/size) -- the chosen output settings (encoder, quality,
        # container, ...) already live in and edit live from the right-hand
        # panel for whichever row is selected, so repeating them here would
        # just be the same information twice.
        item = QTreeWidgetItem()
        item.setData(STATUS_COL, Qt.UserRole, job)
        item.setText(VIDEO_COL, job["path"].name)
        item.setText(RESULT_COL, "Ready")
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
        # Otherwise a detached row's still-outstanding analysis count (its
        # QTreeWidgetItem no longer even in the queue) lingered forever --
        # confirmed real: rapid Add -> Convert -> Undo/Redo could leave
        # bookkeeping for both the old, now-gone rows and the freshly
        # restored ones, reporting "analyzing 4 videos" for 2 visible
        # ones. Every restored row gets its own fresh entry right below,
        # same as a real Add Files would.
        self._pending_analysis_items.clear()
        for job in snapshot:
            item = self._make_queue_row(dict(job))
            self.queue_list.addTopLevelItem(item)
            self._pending_analysis_items[item] = 2
            self._start_interlace_detection(item, job["path"])
            self._start_source_probe(item, job["path"])
        self._update_command_preview()
        self._refresh_idle_controls()
        self._reconcile_pending_start_after_mutation()

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
            self._pending_analysis_items[item] = 2
            self._start_interlace_detection(item, path)
            self._start_source_probe(item, path)
        self._update_command_preview()  # may now reflect a real queued file's audio
        self._refresh_idle_controls()
        # A video added while a deferred Convert was already "Preparing…"
        # correctly gets waited on (the _pending_analysis_items[item] = 2
        # line above already covers that) -- but without this, the status
        # text itself stayed stuck at the old count ("analyzing 1 video…"
        # after a second video was added mid-preparation), a real state/UI
        # consistency gap even though the eventual conversion was already
        # correct. Every queue-membership change during a deferred Convert
        # goes through this same reconciliation now -- Add included, not
        # just Remove/Clear/Undo/Redo.
        self._reconcile_pending_start_after_mutation()

    def _note_probe_finished(self, item: QTreeWidgetItem):
        # Purely for the "Preparing -- analyzing N video(s)…" count (see
        # _pending_analysis_items' own comment in main.py) -- same
        # count-down-from-2 shape as _maybe_submit_mid_run_job just below,
        # deliberately not merged with it: that one gates a real action
        # (submitting a mid-run add to the live queue) and only ever
        # applies during a run, this one is display-only bookkeeping that
        # applies to every added file regardless of run state.
        remaining = self._pending_analysis_items.get(item)
        if remaining is None:
            return
        remaining -= 1
        if remaining > 0:
            self._pending_analysis_items[item] = remaining
        else:
            del self._pending_analysis_items[item]

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
        self._note_probe_finished(item)
        self._maybe_begin_ready_conversion()

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
        # finally, not a plain call after this try block -- confirmed a
        # real race, not just a theoretical one: called unconditionally
        # right here (ahead of the job-dict update below) meant that if
        # this was the last outstanding probe, _begin_conversion() could
        # snapshot every row's job dict (queue_controller.py's
        # topLevelItem(i).data(...) loop) *before* this file's own
        # job["deinterlace"] assignment below ever ran -- the exact
        # stale-data race this whole deferred-start mechanism exists to
        # prevent, just reintroduced one step later. finally guarantees
        # this runs only after every path below has already applied (or
        # deliberately skipped, e.g. deinterlace_user_set) its update.
        try:
            try:
                job = item.data(STATUS_COL, Qt.UserRole)
            except RuntimeError:
                self._maybe_submit_mid_run_job(item)  # no-ops: same RuntimeError, caught there too
                return  # item's C++ object was deleted (e.g. Clear Queue) before detection finished
            if job is None:
                self._maybe_submit_mid_run_job(item)
                return
            if job.get("deinterlace_user_set"):
                # The user already explicitly set this file's deinterlace
                # value (see _on_deinterlace_checkbox_changed) -- their
                # choice wins, a same-file detection result landing after
                # that shouldn't silently replace it.
                self._maybe_submit_mid_run_job(item)
                return
            fraction = worker.parse_idet_output(stderr_text)
            job["deinterlace"] = fraction > worker.INTERLACE_DETECT_THRESHOLD
            item.setData(STATUS_COL, Qt.UserRole, job)
            self._refresh_video_cell(item)
            # If this file was added mid-run (see add_files), the running
            # queue got its own snapshot copy of job at add time, made
            # before this detection result was known -- patch that copy
            # too, or a file added while encoding was in progress would
            # always encode with deinterlace off regardless of what
            # detection actually found.
            self.queue.update_pending_job(job["path"], {"deinterlace": job["deinterlace"]})
            # Reflect it in the checkbox if this item happens to be
            # selected, but guarded: without this, updating just this one
            # item's checkbox would cascade into
            # _sync_settings_to_selected_queue_items and stamp this single
            # file's detected value onto every OTHER currently-selected
            # item too, if more than one happens to be selected right now.
            selected = self.queue_list.selectedItems()
            if selected == [item]:
                self._syncing_controls_from_selection = True
                try:
                    self.deinterlace_check.setChecked(job["deinterlace"])
                finally:
                    self._syncing_controls_from_selection = False
            self._maybe_submit_mid_run_job(item)
        finally:
            self._note_probe_finished(item)
            self._maybe_begin_ready_conversion()

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
        # finally, not a plain call ahead of the block below -- same real
        # race already fixed in _on_interlace_detected's identical
        # restructuring (see that method's own comment). This probe's own
        # writes land on display-only columns (worker.py re-probes
        # duration/audio fresh at actual encode time, it doesn't read
        # these), so there's no *settings* correctness bug here the way
        # there was for deinterlace -- kept symmetric with the interlace
        # side anyway, on the same "apply everything this callback is
        # going to apply before letting a deferred run begin" principle.
        try:
            try:
                item.text(VIDEO_COL)  # touch the item; raises RuntimeError if its C++ object is gone
            except RuntimeError:
                self._maybe_submit_mid_run_job(item)  # no-ops: same RuntimeError, caught there too
                return  # item deleted (e.g. Clear Queue) before the probe landed
            info = worker.parse_probe_output(stdout_text)
            if info.get("duration"):
                item.setText(DURATION_COL, formatting.format_eta(info["duration"]))
                # Raw seconds, alongside the formatted display text --
                # needed by _queue_eta_seconds below to estimate each
                # not-yet-started row's own encode time; re-parsing
                # "H:MM:SS" back into a number would just be format_eta's
                # own logic run in reverse.
                item.setData(DURATION_COL, Qt.UserRole, info["duration"])
            if info.get("video_codec"):
                label = formatting.video_codec_label(info["video_codec"])
                if info.get("width") and info.get("height"):
                    label += f" {info['width']}x{info['height']}"
                item.setData(VIDEO_COL, _RAW_VIDEO_LABEL_ROLE, label)
                self._refresh_video_cell(item)
            if info.get("audio_codec"):
                # No longer a visible column of its own (see
                # QUEUE_COLUMN_HEADERS in queue_widget.py) -- folded into
                # the Video cell's own subtitle line instead, by
                # _refresh_video_cell, same as the video codec/resolution
                # label just above.
                extra = info.get("audio_track_count", 1) - 1
                suffix = f"  +{extra} more" if extra > 0 else ""
                channel_label = formatting.audio_channel_label(info.get("audio_channels"))
                audio_label = f"{formatting.audio_codec_label(info['audio_codec'])} {channel_label}{suffix}"
                item.setData(VIDEO_COL, _RAW_AUDIO_LABEL_ROLE, audio_label)
                self._refresh_video_cell(item)
            if info.get("audio_track_count"):
                item.setData(VIDEO_COL, AUDIO_TRACK_COUNT_ROLE, info["audio_track_count"])
                # Real, confirmed bug: _refresh_audio_track_choices below
                # only narrows the *visible* combo for a currently-
                # selected row, via blockSignals -- deliberately, so it
                # doesn't fire the normal control-changed write-back while
                # just re-populating the list. A job added while a higher
                # Track index was selected (queue empty, pick Track 4,
                # then add a 1-track file) keeps that out-of-range index
                # in its own stored settings regardless of whether this
                # row is ever selected again -- worker.py degrades that
                # gracefully at encode time (no crash, just silently no
                # audio), but the job itself should never be allowed to
                # stay invalid once the real track count is known.
                job = item.data(STATUS_COL, Qt.UserRole)
                if job is not None and job["audio_track"] >= info["audio_track_count"]:
                    job["audio_track"] = info["audio_track_count"] - 1
                    item.setData(STATUS_COL, Qt.UserRole, job)
                # Only worth recomputing Track's own choices if this probe
                # actually affects what's currently shown -- a background/
                # mid-run probe for a row the user isn't looking at
                # shouldn't yank the combo out from under an unrelated
                # in-progress edit.
                if item in self.queue_list.selectedItems():
                    self._refresh_audio_track_choices()
            self._maybe_submit_mid_run_job(item)
        finally:
            self._note_probe_finished(item)
            self._maybe_begin_ready_conversion()

    def _pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Add video files", str(Path.home()), VIDEO_FILTER
        )
        self.add_files([Path(f) for f in files])

    def _pick_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", str(self.output_dir))
        if d:
            self.output_dir = Path(d)
            self.output_edit.setText(formatting.display_path(self.output_dir))

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
            self.output_edit.setText(formatting.display_path(self.output_dir))

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
            # Otherwise a video removed while its analysis was still
            # outstanding kept counting against a deferred Convert
            # (_start_when_ready) that no longer has any reason to wait
            # for it -- confirmed real: Convert while analyzing -> Remove
            # could leave TITAN "preparing" forever for a video that was
            # never going to run.
            self._pending_analysis_items.pop(item, None)
        self._update_command_preview()
        self._refresh_idle_controls()
        self._reconcile_pending_start_after_mutation()

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
        # Same reasoning as _remove_selected's own pop() just above, for
        # every row at once -- otherwise Convert while analyzing -> Clear
        # Queue left _start_when_ready armed against an now-empty queue,
        # and once those irrelevant probes eventually finished landing,
        # _maybe_begin_ready_conversion would call _begin_conversion()
        # anyway (TranscodeQueue.start([]) degrades harmlessly, but the
        # user explicitly cleared the queue -- honoring a stale Convert
        # request afterward is simply the wrong state to end up in).
        self._pending_analysis_items.clear()
        self._update_command_preview()
        self._refresh_idle_controls()
        self._reconcile_pending_start_after_mutation()

    def _update_start_button_label(self):
        # "Convert" states the outcome, not the mechanism -- "Start"
        # describes clicking a button, "Convert 4 Videos" describes what
        # actually happens next. Skipped whenever preparing/converting/
        # paused already owns start_btn's text ("Preparing…"/"Converting…"/
        # "Resume", all set by _apply_run_phase_visuals below) -- otherwise
        # add_files() calling this unconditionally (it deliberately isn't
        # gated by _queue_editable, see its own comment -- adding a file
        # mid-run is a supported workflow) would silently flip a disabled
        # "Converting…" button's text back to "Convert 4 Videos" the
        # instant a new row landed. Confirmed as a real gap this pass would
        # otherwise have introduced, not a pre-existing bug: start_btn's
        # text was never phase-owned before, so nothing could stomp it.
        if self._start_when_ready or not self._queue_editable:
            return
        count = self.queue_list.topLevelItemCount()
        self.start_btn.setText("Convert" if count == 0 else f"Convert {count} Video{'s' if count != 1 else ''}")

    def _refresh_idle_controls(self):
        """What add_files()/_remove_selected()/_clear_queue()/
        _restore_queue_snapshot() call after a queue-content mutation --
        refreshes the idle "Convert N Videos" label and dismisses a
        showing finished-run summary, but only when idle/finished actually
        owns the run-control area right now. A no-op during preparing/
        converting/paused (_update_start_button_label's own guard above
        already covers add_files() specifically; this extra check is
        because _apply_run_phase_visuals("idle") does more than relabel
        the button -- it also hides progress_bar/eta_label/stats_label/
        stop_btn, which would be actively wrong to do mid-run)."""
        if self._start_when_ready or not self._queue_editable:
            return
        # open_folder_btn.isVisible() is the real, current ground truth for
        # "was a finished summary actually showing" -- _run_completed_count
        # alone can't answer that (it stays nonzero until the next
        # _begin_conversion, well past this one dismissal). Only reset
        # status_label when it genuinely was showing one: confirmed a real
        # bug otherwise -- _apply_run_phase_visuals("idle") below already
        # hides the summary's own two labels (eta_label/stats_label) and
        # resets start_btn, but never touched status_label itself, so
        # "✓ Conversion Complete" could keep showing above a queue that had
        # already moved on. Unconditionally resetting to "Idle" instead
        # would have its own bug: this same method also runs on an
        # ordinary idle-to-idle mutation (e.g. adding the first file to a
        # never-yet-run queue), where status_label may legitimately be
        # holding some other status set independently of the queue's own
        # empty/non-empty state -- that must not get clobbered just
        # because a file was added.
        was_showing_finished_summary = self.open_folder_btn.isVisible()
        self._apply_run_phase_visuals("idle")
        if was_showing_finished_summary:
            self._set_status("Idle")

    def _apply_run_phase_visuals(self, phase: str):
        """One-shot: makes start_btn/stop_btn/open_folder_btn/progress_bar/
        eta_label/stats_label/pause_after_check reflect `phase` right now.
        phase is one of "idle"/"preparing"/"converting"/"paused"/"finished".
        Called explicitly at every transition point below rather than
        tracked as its own persistent state -- the existing _queue_paused/
        _start_when_ready/_queue_editable flags already are the real state;
        this only renders it. Never touches status_label's text -- that
        stays _set_status's own concern, called separately by each caller."""
        is_run_phase = phase in ("converting", "paused")
        self.stop_btn.setVisible(is_run_phase)
        self.open_folder_btn.setVisible(phase == "finished")
        # Only meaningful while actively converting -- there's nothing to
        # arm a pause against otherwise, and staying enabled through pause/
        # finished would let it be re-checked with no live job left to stop
        # after. Unchecking here too, not just disabling, matters for
        # _on_paused specifically -- see its own comment.
        self.pause_after_check.setEnabled(phase == "converting")
        if phase != "converting":
            self.pause_after_check.setChecked(False)

        if phase == "idle":
            self.start_btn.setEnabled(True)
            self._update_start_button_label()
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            self.eta_label.setVisible(False)
            self.stats_label.setVisible(False)
        elif phase == "preparing":
            # No stop_btn here either (is_run_phase above is False) -- there
            # is no cancel path for in-flight analysis today (a pre-existing
            # gap, not something this pass adds); a dead, disabled Cancel
            # would be more confusing than none. Building a real abort-
            # analysis path is a separate, more invasive piece of work.
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Preparing…")
            self.progress_bar.setRange(0, 0)  # indeterminate -- no real fraction yet
            self.progress_bar.setVisible(True)
            self.eta_label.setVisible(False)
            self.stats_label.setVisible(False)
        elif phase == "converting":
            self.start_btn.setEnabled(False)
            self.start_btn.setText("Converting…")
            self.stop_btn.setEnabled(True)
            self.stop_btn.setText("Cancel")
            # Un-sticks the marquee left over from "preparing" -- _on_job_
            # started's own progress_bar.setValue(0) never resets the range
            # by itself, so without this the bar would stay spinning
            # indeterminately through the whole first job.
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(True)
            self.eta_label.setVisible(True)
            # Consumer build: no live technical line during conversion --
            # stats_label stays hidden here (unlike the specialist build)
            # since _on_job_stats no longer populates it in this phase;
            # it's still used, and shown, once phase == "finished" below.
            self.stats_label.setVisible(False)
        elif phase == "paused":
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Resume")
            self.stop_btn.setEnabled(True)
            self.stop_btn.setText("Cancel")
            # progress_bar/eta_label/stats_label deliberately untouched --
            # still showing the last job's numbers, same as pre-this-pass
            # _on_paused already did.
        elif phase == "finished":
            self.start_btn.setEnabled(True)
            self._update_start_button_label()
            self.open_folder_btn.setEnabled(True)
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(0)
            self.progress_bar.setVisible(False)
            count_line, size_line = formatting.format_run_summary(
                self._run_completed_count, self._run_failed_count,
                self._run_total_input_bytes, self._run_total_output_bytes
            )
            self.eta_label.setText(count_line)
            self.eta_label.setVisible(True)
            self.stats_label.setText(size_line)
            self.stats_label.setVisible(bool(size_line))

    # --- run control ---
    def _start(self):
        if self._queue_paused:
            # Same button, same click handler -- Start becomes "Resume"
            # while paused (see _on_paused) rather than a second widget,
            # since the two are mutually exclusive states anyway.
            # queue.resume() continues from self._index as it was left,
            # not a fresh run() (which would rebuild the job list from
            # the current queue rows from scratch).
            self._queue_paused = False
            self._apply_run_phase_visuals("converting")
            self.queue.resume()
            return
        if self.queue_list.topLevelItemCount() == 0:
            self._set_status("Queue is empty")
            return
        if self._pending_analysis_items:
            # A file added moments ago whose ~20s interlace sample (or
            # duration/audio probe) hasn't landed yet still has whatever
            # settings it started with (see add_files/_current_settings),
            # not what analysis would actually find. Confirmed this was a
            # real race: adding an interlaced file and clicking Convert
            # immediately could start that job with deinterlace off.
            #
            # The user already stated their intent by clicking Convert --
            # asking them to click it again once analysis happens to
            # finish is exactly the kind of implementation detail leaking
            # into the workflow this app is trying to hide. Instead of
            # refusing outright, this arms _start_when_ready and disables
            # the button; _maybe_begin_ready_conversion (called from every
            # place _pending_analysis_items gets decremented:
            # _on_probe_error, _on_interlace_detected, _on_source_probed)
            # begins the real run automatically once the last probe's own
            # callback has actually finished applying its result.
            #
            # Gated on _pending_analysis_items, not "self._detection_
            # processes is non-empty" -- confirmed a real, deeper race
            # than the ordering bug above: that list is pruned by
            # re-checking every process's *current* QProcess.state(), not
            # by "the process this specific callback owns finished", so
            # two processes completing close together could have the
            # first callback's own pruning line remove *both* (since the
            # second was also already NotRunning by then) and trigger
            # readiness before the second callback -- possibly the one
            # that actually applies job["deinterlace"] -- has run at all.
            # _pending_analysis_items only decrements once per callback
            # that has truly finished (see _note_probe_finished), so it
            # can't be fooled by another process's coincidental timing.
            self._start_when_ready = True
            self._apply_run_phase_visuals("preparing")
            count = len(self._pending_analysis_items)
            self._set_status(
                f"Preparing -- analyzing {count} video{'s' if count != 1 else ''}…"
            )
            return
        self._begin_conversion()

    def _maybe_begin_ready_conversion(self):
        # _pending_analysis_items, not _detection_processes -- see
        # _start()'s own comment on this same gate for why.
        if self._start_when_ready and not self._pending_analysis_items:
            self._start_when_ready = False
            self._begin_conversion()

    def _reconcile_pending_start_after_mutation(self):
        # Called after every queue-structure mutation (Remove/Clear/Undo/
        # Redo) that could have changed what a deferred Convert
        # (_start_when_ready) is actually waiting on -- _queue_editable
        # stays True during "Preparing…" (only _begin_conversion() flips
        # it), so all of those are genuinely reachable mid-preparation,
        # not just mid-idle. A no-op whenever nothing is actually pending.
        if not self._start_when_ready:
            return
        if self.queue_list.topLevelItemCount() == 0:
            # The user cleared/removed everything out from under a
            # pending Convert -- honoring that stale request once its
            # now-irrelevant probes eventually land would be the wrong
            # state to end up in, even though TranscodeQueue.start([])
            # itself degrades harmlessly.
            self._start_when_ready = False
            self._apply_run_phase_visuals("idle")
            self._set_status("Queue is empty")
            return
        # Still has files: begins right away if the removed/undone rows
        # were the only thing left outstanding, otherwise just refreshes
        # the visible count to match what's actually still being waited on.
        self._maybe_begin_ready_conversion()
        if self._start_when_ready:
            count = len(self._pending_analysis_items)
            self._set_status(
                f"Preparing -- analyzing {count} video{'s' if count != 1 else ''}…"
            )

    def _begin_conversion(self):
        jobs = [self.queue_list.topLevelItem(i).data(STATUS_COL, Qt.UserRole)
                 for i in range(self.queue_list.topLevelItemCount())]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._apply_run_phase_visuals("converting")
        self._set_queue_editable(False)
        self.log_view.clear()
        # Jobs run strictly one at a time in this same order, and the queue
        # is locked for the run's duration (_set_queue_editable(False)), so
        # this position-based snapshot stays valid throughout -- job_started's
        # 1-based index is enough to look up which row is now running.
        self._running_items = [self.queue_list.topLevelItem(i) for i in range(self.queue_list.topLevelItemCount())]
        for item in self._running_items:
            item.setIcon(STATUS_COL, QIcon())  # clear any status icon left from a previous run
            item.setText(RESULT_COL, "Ready")  # clear a previous run's result too
        # Otherwise a new run's very first job, if it fails preflight
        # before job_started ever reaches it, would get blamed on
        # whatever item _current_running_item was still pointing at from
        # the *previous* run -- confirmed a real gap, not just tidiness.
        self._current_running_item = None
        self._run_total_input_bytes = 0
        self._run_total_output_bytes = 0
        self._run_completed_count = 0
        self._run_failed_count = 0
        self._run_cancelled = False
        self.queue.start(jobs, self.output_dir)

    def _stop(self):
        # _run_cancelled set *before* queue.stop(), not after -- reported
        # live as sometimes showing "Conversion Complete" on a genuine
        # cancel. Root cause: TranscodeQueue.stop() (worker.py) isn't
        # always async -- if the queue is paused between jobs (no live
        # process to terminate), it emits all_finished synchronously,
        # right there inside the stop() call, via a plain (same-thread,
        # direct) connection to _on_all_finished below. With the old
        # ordering, that handler ran and read _run_cancelled while it was
        # still False, before this method's next line ever set it to True.
        self._run_cancelled = True
        self.queue.stop()
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("Stop")

    def _on_pause_after_toggled(self):
        # Reads the action's own current state rather than the toggled
        # signal's passed value -- same reasoning as
        # _on_deinterlace_checkbox_changed above, avoids depending on
        # exactly what type/value that signal happens to hand back.
        if self.pause_after_check.isChecked():
            self.queue.request_pause()
        else:
            self.queue.cancel_pause_request()
        # Re-applies (or clears) the "will stop after this video" suffix
        # -- see _set_status -- against whatever the status already says,
        # without needing to know what that text actually is.
        self._set_status(self._base_status_text)

    def _set_status(self, text: str):
        # Every status_label update goes through here (not
        # status_label.setText directly) so toggling "Stop After Current
        # Video" (self.pause_after_check, now in the overflow menu, not a
        # permanent checkbox next to Convert/Cancel the way it used to
        # be) can visibly say so regardless of which of the several call
        # sites below last set the status -- a single suffix rule applied
        # in one place, rather than every call site needing to remember
        # to check this itself.
        self._base_status_text = text
        if self.pause_after_check.isChecked():
            # Genuinely informative even at rest (a pending one-shot
            # intent the user just set) -- shown in full despite the
            # "Idle" hiding rule right below, which is only about the
            # bare word carrying no information on its own.
            text = f"{text}  —  will stop after this video"
            self.status_label.setVisible(True)
        elif text == "Idle":
            # Reported live: "Idle" reads as internal state-machine
            # language, not product language, and permanently occupying
            # this line gives real status (Preparing/Converting N of M/
            # Conversion Complete/...) less visual weight than it should
            # have. Hidden rather than left showing empty text -- this
            # whole run-status area already collapses to nothing at rest
            # (progress_bar/eta_label/stats_label, see _apply_run_phase_
            # visuals's "idle" branch), status_label was the one holdout
            # still always occupying space here.
            self.status_label.setVisible(False)
        else:
            self.status_label.setVisible(True)
        self.status_label.setText(text)

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
        # "Converting N of M — name", not "[N/M] Encoding name" -- states
        # the outcome in plain language rather than a technical bracket
        # notation, matching the same "Convert"/"Preparing…" language
        # already established elsewhere in this run-control flow.
        self._set_status(f"Converting {index} of {total} — {Path(path).name}")
        self.progress_bar.setValue(0)
        self.eta_label.setText("")  # no ETA yet -- the first real stats tick hasn't landed
        self.stats_label.setText("—")
        self.log_view.appendPlainText(f"\n=== Starting {path} ===")
        self._current_running_item = self._running_items[index - 1]
        self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_play"))
        self._current_running_item.setText(RESULT_COL, "Converting… 0%")

    def _on_job_progress(self, fraction: float):
        self.progress_bar.setValue(int(fraction * 1000))
        if self._current_running_item is not None:
            self._current_running_item.setText(RESULT_COL, f"Converting… {int(fraction * 100)}%")

    def _queue_eta_seconds(self, current_job_eta: float | None, speed_multiplier: float | None) -> float | None:
        """Rolls the current job's own live ETA (already computed in
        worker.py from its real ffmpeg progress) up into a whole-queue
        estimate, by applying that same observed speed multiplier to
        each not-yet-started job's own probed duration -- the best
        available estimate for a file that hasn't started encoding yet
        and so has no live speed data of its own. An estimate, not a
        guarantee: real per-file variance (resolution, content
        complexity) can make any one job faster or slower than this,
        same honest caveat every "time remaining" indicator in any
        transcoder carries.

        None whenever current_job_eta/speed_multiplier themselves
        aren't available yet (mirrors _on_job_stats' own "--:--"
        placeholder for the per-job ETA at the very start of a run,
        before the first real progress tick has landed).
        """
        if current_job_eta is None or not speed_multiplier:
            return None
        if self._current_running_item not in self._running_items:
            return current_job_eta
        remaining_items = self._running_items[self._running_items.index(self._current_running_item) + 1:]
        total = current_job_eta
        for item in remaining_items:
            try:
                duration = item.data(DURATION_COL, Qt.UserRole)
            except RuntimeError:
                continue  # item's C++ object was deleted before this tick landed
            if duration:
                total += duration / speed_multiplier
        return total

    def _on_job_stats(self, stats: dict):
        # Consumer build: no live fps/bitrate/speed/clock-format-ETA line
        # (stats_label used to show one here) -- cut per CONSUMER_FORK_
        # PLAN.md's v1 scope table. stats_label itself stays -- it's also
        # the finished-run size/savings summary ("1.74 GB · 68% smaller",
        # _apply_run_phase_visuals' "finished" branch), which is genuinely
        # useful, non-technical information worth keeping; only its use
        # *here*, during an active conversion, was the specialist-flavored
        # part. eta_label (the plain-language "About 7 minutes remaining"
        # line) is unaffected -- still the queue-wide estimate, not just
        # this one file's own ETA, since "when will everything actually be
        # done" is the more useful number for a multi-file queue.
        eta = stats.get("eta_seconds")
        queue_eta = self._queue_eta_seconds(eta, stats.get("speed_multiplier"))
        self.eta_label.setText(formatting.format_eta_human(queue_eta) if queue_eta is not None else "")

    def _on_job_log(self, line: str):
        self.log_view.appendPlainText(line)

    def _on_job_finished(self, path: str, output_path: str):
        self.log_view.appendPlainText(f"=== Done: {path} ===")
        if self._current_running_item is not None:
            self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_done"))
            sizes = self._append_result_size(self._current_running_item, Path(path), Path(output_path))
            # Counted unconditionally -- this handler only ever fires on a
            # real success (a failure goes through _on_job_failed instead),
            # so "did a video finish" and "did its size stat succeed" are
            # separate questions. A transient stat() failure on one output
            # file shouldn't silently uncount a video that genuinely
            # finished converting.
            self._run_completed_count += 1
            if sizes is not None:
                in_size, out_size = sizes
                self._run_total_input_bytes += in_size
                self._run_total_output_bytes += out_size
            # Retrievable later by the right-click "Reveal Output File"
            # action (see _on_queue_context_menu below) -- an extra
            # bookkeeping key alongside the real settings, same pattern
            # job["deinterlace_user_set"] already uses.
            job = self._current_running_item.data(STATUS_COL, Qt.UserRole)
            job["_completed_output_path"] = output_path
            self._current_running_item.setData(STATUS_COL, Qt.UserRole, job)

    def _on_job_failed(self, path: str, reason: str):
        self.log_view.appendPlainText(f"=== FAILED: {path}: {reason} ===")
        self._run_failed_count += 1
        if self._current_running_item is not None:
            self._current_running_item.setIcon(STATUS_COL, self._themed_icon("status_warning"))
            self._current_running_item.setToolTip(STATUS_COL, reason)
            # Short in the visible column -- the real reason can be long
            # (a raw ffmpeg error line) and already lives in the tooltip
            # just set above, and in the log.
            self._current_running_item.setText(RESULT_COL, "Failed")

    @staticmethod
    def _append_result_size(item: QTreeWidgetItem, input_path: Path, output_path: Path) -> tuple[int, int] | None:
        # Returns the raw byte counts (not just writing the row's own
        # display text) so _on_job_finished can accumulate them into a
        # whole-run total -- nothing else in the app tracks aggregate size
        # anywhere, this was the only place either number briefly existed.
        try:
            in_size = input_path.stat().st_size
            out_size = output_path.stat().st_size
        except OSError:
            return None
        if in_size <= 0:
            return None
        change_pct = 100 * (1 - out_size / in_size)
        direction = "smaller" if change_pct >= 0 else "larger"
        item.setText(RESULT_COL, f"{formatting.format_size(out_size)} ({abs(change_pct):.0f}% {direction})")
        return in_size, out_size

    def _on_all_finished(self):
        self._queue_paused = False
        self._set_queue_editable(True)
        # Whenever at least one video actually finished -- whether the run
        # completed in full, or was cancelled/failed partway through after
        # some had already succeeded ("2 videos converted" after 2-of-4
        # then Cancel is still a real, worth-showing result) -- show the
        # finished summary instead of collapsing straight back to Idle. A
        # run where nothing ever completed (cancelled before the first job
        # finished, or every job failed) has nothing to summarize, so it
        # falls straight back to idle instead of "0 videos converted".
        if self._run_completed_count > 0:
            self._apply_run_phase_visuals("finished")
            # "Conversion Complete" implies the whole run succeeded --
            # confirmed a real, misleading gap otherwise: a 2-of-4 run the
            # user cancelled, or one where some jobs simply failed, both
            # still said "✓ Conversion Complete" before this distinction
            # existed. Cancelled takes priority over failed if somehow
            # both happened in the same run (a job failed, then the user
            # cancelled before the rest finished) -- "the user stopped it"
            # is the more relevant fact to lead with either way.
            if self._run_cancelled:
                self._set_status("Conversion Stopped")
            elif self._run_failed_count > 0:
                self._set_status("Completed with Issues")
            else:
                self._set_status("✓ Conversion Complete")
        else:
            # Zero-success runs used to say "Idle" unconditionally --
            # accurate for "nothing was ever queued", misleading for "3
            # videos just failed" or "cancelled before the first one
            # finished". The rows themselves already show Failed, so this
            # isn't a correctness gap, just a status line that stopped
            # matching how complete the rest of this lifecycle's reporting
            # is. No finished-summary controls either way -- there's
            # nothing to summarize with 0 successes, same reasoning as
            # before, just a truthful status instead of a blank one.
            self._apply_run_phase_visuals("idle")
            if self._run_cancelled:
                self._set_status("Conversion Stopped")
            elif self._run_failed_count > 0:
                self._set_status("Conversion Failed")
            else:
                self._set_status("Idle")

    def _on_paused(self):
        # The run halted between jobs (request_pause armed, the job that
        # was running finished on its own -- see worker.py's
        # _advance_or_pause) rather than everything actually finishing.
        # Queue stays locked (_set_queue_editable untouched) -- a pause
        # mid-run isn't "done", the same reasoning _start() already
        # applies to Remove/Clear/reordering during a real run applies
        # here too.
        remaining = len(self._running_items) - self._running_items.index(self._current_running_item) - 1
        # _apply_run_phase_visuals runs BEFORE _set_status, not after --
        # it's what unchecks pause_after_check, and _set_status reads that
        # checkbox to decide whether to append its own "will stop after
        # this video" suffix. Confirmed a real, pre-existing bug the old
        # order had: _on_paused only ever fires because the box WAS
        # checked, so every real pause produced "Paused -- 2 file(s)
        # remaining  —  will stop after this video" -- a nonsensical
        # suffix on a status that's already paused.
        self._apply_run_phase_visuals("paused")
        self._set_status(f"Paused -- {remaining} file(s) remaining")
        self._queue_paused = True
