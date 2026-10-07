"""Runs the transcode queue: one ffmpeg job at a time via QProcess, with
progress, stats, pause/stop and per-job results as Qt signals. Everything
ffmpeg-specific (probing, arguments, failure reasons) comes from core/ffmpeg.py;
it's called through the module (ffmpeg.probe_duration, ...) so tests can
patch it there.
"""
import re
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

from velocoder.core import ffmpeg

_OUT_TIME_RE = re.compile(r"^out_time=(\d+):(\d+):(\d+)\.(\d+)$")


class TranscodeQueue(QObject):
    job_started = Signal(str, int, int)  # path, index (1-based), total
    job_progress = Signal(float)         # 0.0-1.0
    job_stats = Signal(dict)             # {"fps", "bitrate", "speed", "eta_seconds", "speed_multiplier"} -- see _emit_stats
    job_log = Signal(str)                # one log line
    job_finished = Signal(str, str)      # input_path, output_path
    job_failed = Signal(str, str)        # path, reason
    all_finished = Signal()
    paused = Signal()                    # a requested pause actually took effect -- see request_pause

    def __init__(self):
        super().__init__()
        self._process: QProcess | None = None
        self._jobs: list[dict] = []
        self._index = 0
        self._output_dir: Path | None = None
        self._duration = 0.0
        self._stopped = False
        # request_pause() only arms this -- the run doesn't actually halt
        # until the *current* job finishes (deliberately, per the user:
        # "let this one finish, then stop", not an immediate interrupt
        # like stop() is). _paused is the separate, already-in-effect
        # state _advance_or_pause sets once that happens -- resume()/
        # stop() both need to tell "armed but still running" apart from
        # "actually halted between jobs" (stop() while genuinely paused
        # has no live _process to terminate, so it has to notice _paused
        # instead and emit all_finished itself -- see stop() below).
        self._pause_requested = False
        self._paused = False
        self._stats_buffer: dict = {}
        # The current job's last few stderr lines, for summarize_ffmpeg_failure.
        self._recent_log: list[str] = []
        # Every final output path handed out this run, so two jobs that
        # would otherwise both want e.g. shot01.mp4 (different source
        # folders, same stem) get disambiguated instead of the second one
        # silently overwriting the first -- confirmed this was possible
        # before, not just theoretical. Reset per start(), grows as add_job
        # appends mid-run jobs too, since _resolve_output_path is what
        # populates it, not start() itself.
        self._used_output_paths: set[Path] = set()

    def start(self, jobs: list[dict], output_dir: Path):
        """jobs: list of settings dicts (see build_args) plus a "path" key."""
        self._jobs = list(jobs)
        self._output_dir = output_dir
        self._index = 0
        self._stopped = False
        self._pause_requested = False
        self._paused = False
        self._used_output_paths = set()
        self._run_next()

    def _resolve_output_path(self, input_path: Path, container: str) -> Path:
        """One final output path per input file, disambiguated against
        every other path already handed out this run *and* against
        whatever's already sitting on disk (a previous run's completed
        output, or unrelated content that happens to share the name) --
        confirmed both were real ways for one job to silently clobber
        another's finished file before this existed. "name (2).ext",
        "name (3).ext", ... matching how most file managers already
        resolve the same kind of collision."""
        stem = input_path.stem
        n = 1
        while True:
            name = f"{stem}.{container}" if n == 1 else f"{stem} ({n}).{container}"
            candidate = self._output_dir / name
            if candidate not in self._used_output_paths and not candidate.exists():
                self._used_output_paths.add(candidate)
                return candidate
            n += 1

    def add_job(self, job: dict):
        """Append a job to the run already in progress -- _run_next picks it
        up automatically the next time it looks for one (it just checks
        self._index against len(self._jobs), both of which keep working
        correctly as this list grows). No separate "resume" call needed;
        harmless if called with nothing running, though callers only do
        that mid-run today (main_window.py's add_files, gated on _queue_editable)."""
        self._jobs.append(job)

    def update_pending_job(self, path: Path, updates: dict):
        """Patch fields on any not-yet-started job(s) matching path.

        Needed because a job handed to add_job is a plain snapshot dict, not
        a live reference to whatever the GUI's queue row holds -- confirmed
        empirically that QTreeWidgetItem.setData/.data() round-trips a copy,
        not the original object, so mutating the GUI-side dict later (e.g.
        auto-detected deinterlace landing after this job was already queued)
        would silently never reach this one without an explicit patch like
        this. Jobs at or before self._index are already running or finished
        and are deliberately left alone.
        """
        for pending in self._jobs[self._index:]:
            if pending["path"] == path:
                pending.update(updates)

    def stop(self):
        self._stopped = True
        if self._process is not None:
            self._process.terminate()
        elif self._paused:
            # Genuinely paused between jobs -- no live process to
            # terminate, and nothing will ever call _run_next() again on
            # its own to notice _stopped and emit all_finished (that only
            # happens from _advance_or_pause, which a real pause has
            # already returned out of for good). Emit it directly instead
            # so the GUI still unlocks the queue the same way a Stop
            # during an actual run does.
            self._paused = False
            self.all_finished.emit()

    def request_pause(self):
        """Arms a one-shot pause: the *current* job (if any) still runs to
        completion untouched -- unlike stop(), nothing is terminated --
        and the run halts right after, instead of advancing to the next
        job. Harmless if called with nothing running; the request just
        sits armed until start() resets it, since _advance_or_pause is
        never called between jobs."""
        self._pause_requested = True

    def cancel_pause_request(self):
        """Un-arms a pause requested but not yet in effect -- e.g. the
        user unchecked "Pause after this file" before the current job
        actually finished. No-op once the pause has already taken effect
        (_paused, not _pause_requested, is set by then) -- resume() is
        what reverses that."""
        self._pause_requested = False

    def resume(self):
        """Continues a paused run from exactly where it left off (self._index
        is untouched by a pause) -- not start(), which would rebuild
        _jobs/_index from scratch as a fresh run instead."""
        self._paused = False
        self._run_next()

    def _advance_or_pause(self):
        # self._index < len(self._jobs) too, not just _pause_requested --
        # reported live as "Stop After Current Video" on the *last* video
        # pausing with 0 files remaining and a Resume button that has
        # nothing left to resume. The run is genuinely finished at that
        # point; _run_next() below already knows this exact same
        # condition means "done, not paused" (it's what decides whether
        # to emit all_finished), _pause_requested just wasn't checking it
        # too before honoring the pause.
        if self._pause_requested and self._index < len(self._jobs):
            self._pause_requested = False
            self._paused = True
            self.paused.emit()
            return
        self._pause_requested = False
        self._run_next()

    def _run_next(self):
        if self._stopped or self._index >= len(self._jobs):
            self.all_finished.emit()
            return

        job = self._jobs[self._index]
        self._index += 1
        input_path: Path = job["path"]
        container = job.get("container", "mp4")

        # Emitted here, before any of the preflight checks below that can
        # themselves fail (same-as-input, duration/audio probing,
        # build_args) -- not only once everything succeeds. Confirmed a
        # real bug otherwise: job_failed can fire for *this* job before
        # job_started ever does, but the GUI's _current_running_item is
        # only updated inside its job_started handler, so a preflight
        # failure got blamed on whichever job *previously* had
        # job_started fire (the last one that actually started encoding,
        # possibly from an earlier run entirely) instead of the job that
        # actually failed.
        self.job_started.emit(str(input_path), self._index, len(self._jobs))

        # Checked against the *un*-disambiguated name specifically, before
        # _resolve_output_path ever runs -- confirmed directly that doing
        # this check after resolving would let this exact case slip
        # through silently: since the input file itself already exists at
        # that path, _resolve_output_path's own disambiguation loop would
        # just treat it as "taken" and move on to " (2)" instead of the
        # explicit refusal this is actually supposed to be. This is a
        # correctness guard (don't let ffmpeg -y truncate the file it's
        # also reading from), not a naming-collision one -- the two need
        # to stay separate, not merge into the same "just pick another
        # name" logic.
        natural_output_path = self._output_dir / f"{input_path.stem}.{container}"
        if natural_output_path.resolve() == input_path.resolve():
            self.job_failed.emit(
                str(input_path), "output path is the same as the input file — skipped"
            )
            self._run_next()
            return

        final_output_path = self._resolve_output_path(input_path, container)

        # Written here during the encode, renamed to final_output_path only
        # on confirmed success (_on_finished) -- ffmpeg's -y used to write
        # straight to the final name, so an existing file there (a previous
        # run's completed output, another job's -- see _resolve_output_path)
        # was truncated the instant this job started, and a failed/stopped
        # job's cleanup then deleted whatever was left, destroying it either
        # way. The real extension stays at the very end (ffmpeg has no -f
        # here -- it infers the muxer from this filename), so the temp
        # marker goes in the middle, not appended after it.
        temp_output_path = final_output_path.with_name(
            f".{final_output_path.stem}.transcoding{final_output_path.suffix}"
        )

        self._stats_buffer = {}
        self._recent_log = []
        try:
            self._duration = ffmpeg.probe_duration(input_path)
            audio_codec = ffmpeg.probe_audio_codec(input_path, job["audio_track"])
            audio_channels = None
            if audio_codec is not None and job.get("audio_downmix_stereo"):
                audio_channels = ffmpeg.probe_audio_channels(input_path, job["audio_track"])
            if audio_codec is None:
                self.job_log.emit(
                    f"Note: audio track {job['audio_track']} not found on this "
                    f"file -- output will have no audio"
                )
            # Real, confirmed bug: probe_audio=False here (audio_codec/
            # audio_channels are already resolved above, so build_args
            # doesn't need to re-probe those) also skipped the *bitrate*
            # probe target-size math needs when Automatic ends up copying
            # the source through untouched -- the preview
            # (_update_size_estimate_label) already probes this and
            # reserves the real figure, but the actual encode fell back
            # to the configured audio_bitrate regardless, silently
            # letting the real output exceed the requested Target Size
            # whenever the copied track's real bitrate was higher.
            audio_source_bitrate_kbps = None
            if audio_codec is not None:
                audio_source_bitrate_kbps = ffmpeg.probe_audio_bitrate_kbps(input_path, job["audio_track"])
            args = ffmpeg.build_args(
                job, input_path, temp_output_path, duration_seconds=self._duration,
                probe_audio=False, audio_codec=audio_codec, audio_channels=audio_channels,
                audio_source_bitrate_kbps=audio_source_bitrate_kbps,
            )
        except Exception as exc:
            self.job_failed.emit(str(input_path), str(exc))
            self._run_next()
            return

        self.job_log.emit("ffmpeg " + " ".join(args[1:]))

        proc = QProcess()
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        proc.readyReadStandardOutput.connect(lambda: self._read_progress(proc))
        proc.readyReadStandardError.connect(lambda: self._read_log(proc))
        # QProcess.finished never fires when the process fails to even start
        # (confirmed directly against a nonexistent binary: only errorOccurred
        # does) -- without this, a missing/broken ffmpeg install left this
        # job, and the whole queue behind it, stuck forever: no failure ever
        # reported, nothing to click, nothing in the log. Every *other* error
        # kind (Crashed, Timedout, ...) still reaches finished too (a crash is
        # a way of finishing), so only FailedToStart is handled here -- the
        # rest stay on the existing finished-based path.
        proc.errorOccurred.connect(
            lambda error: self._on_process_error(input_path, temp_output_path, error)
        )
        proc.finished.connect(
            lambda code, status: self._on_finished(input_path, temp_output_path, final_output_path, code, status)
        )
        self._process = proc
        proc.start()

    def _read_progress(self, proc: QProcess):
        data = bytes(proc.readAllStandardOutput()).decode(errors="replace")
        for line in data.splitlines():
            key, sep, value = line.partition("=")
            if not sep:
                continue
            if key == "out_time":
                match = _OUT_TIME_RE.match(line)
                if match and self._duration > 0:
                    h, m, s, frac = match.groups()
                    seconds = int(h) * 3600 + int(m) * 60 + int(s) + float(f"0.{frac}")
                    self._stats_buffer["out_time_seconds"] = seconds
                    self.job_progress.emit(min(seconds / self._duration, 1.0))
            elif key in ("fps", "bitrate", "speed"):
                self._stats_buffer[key] = value
            elif key == "progress":
                # One full -progress update block ends here (continue/end) --
                # emit what's accumulated and start the next block fresh.
                self._emit_stats()
                self._stats_buffer = {}

    def _emit_stats(self):
        stats = dict(self._stats_buffer)
        eta_seconds = None
        speed_multiplier = None
        try:
            speed = float(stats.get("speed", "").rstrip("x"))
            if speed > 0:
                speed_multiplier = speed
            out_time = stats.get("out_time_seconds")
            if speed > 0 and out_time is not None and self._duration > 0:
                eta_seconds = max(self._duration - out_time, 0) / speed
        except ValueError:
            pass
        stats["eta_seconds"] = eta_seconds
        # The same parsed value "speed" (the raw "2.3x" string) already
        # carries, just as a real float -- queue_controller.py's
        # queue-wide ETA estimate needs this too (applied to each
        # not-yet-started job's own probed duration), and re-parsing the
        # same string a second time in the UI layer would just be this
        # exact parsing logic duplicated for no reason.
        stats["speed_multiplier"] = speed_multiplier
        self.job_stats.emit(stats)

    def _read_log(self, proc: QProcess):
        data = bytes(proc.readAllStandardError()).decode(errors="replace")
        for line in data.splitlines():
            if line.strip():
                self.job_log.emit(line)
                self._recent_log = self._recent_log[-19:] + [line]

    def _on_finished(self, input_path: Path, temp_output_path: Path, final_output_path: Path, exit_code: int, exit_status):
        self._process = None
        # A successful exit always wins, even if stop() was also called --
        # QProcess.finished delivery is async, so a job can genuinely finish
        # (exit 0, file written) in the same window the user clicks Stop.
        # Checking _stopped first would then delete a completed output and
        # report it as cancelled instead of keeping it.
        if exit_code == 0 and temp_output_path.exists():
            # Atomic rename onto the real name only now that success is
            # confirmed -- ffmpeg itself never touches final_output_path,
            # so a failed/stopped job (below) can never take an existing
            # file there down with it the way it used to.
            temp_output_path.rename(final_output_path)
            self.job_progress.emit(1.0)
            self.job_finished.emit(str(input_path), str(final_output_path))
        elif self._stopped:
            self._cleanup_partial(temp_output_path)
            self.job_failed.emit(str(input_path), "stopped by user")
        else:
            self._cleanup_partial(temp_output_path)
            self.job_failed.emit(str(input_path), ffmpeg.summarize_ffmpeg_failure(self._recent_log, exit_code))
        # Not a plain _run_next() -- this is a real job actually finishing
        # (success or failure both count), exactly the point "pause after
        # this file" means. The two _run_next() calls inside _run_next()
        # itself (a preflight skip/failure before any process ever
        # started) are deliberately left alone -- nothing "finished" there
        # in the sense the pause checkbox means, and the checkbox is only
        # enabled while a job is actually running in the first place.
        self._advance_or_pause()

    def _on_process_error(self, input_path: Path, temp_output_path: Path, error):
        if error != QProcess.ProcessError.FailedToStart:
            return  # every other error kind still reaches _on_finished via `finished`
        self._process = None
        self._cleanup_partial(temp_output_path)
        self.job_failed.emit(str(input_path), "ffmpeg failed to start -- is it installed and on PATH?")
        self._advance_or_pause()

    @staticmethod
    def _cleanup_partial(temp_output_path: Path):
        try:
            temp_output_path.unlink()
        except OSError:
            pass
