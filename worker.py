"""ffmpeg-driven transcode queue. Runs one job at a time via QProcess.

Takes a plain settings dict per job — no preset catalog here. Presets are a
GUI-side convenience for naming/saving/loading a settings snapshot; the
engine only ever sees the resolved values.
"""
import re
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

INTEL_VENDOR_ID = "0x8086"
BITRATE_RC_MODES = {"VBR", "bitrate"}
_OUT_TIME_RE = re.compile(r"^out_time=(\d+):(\d+):(\d+)\.(\d+)$")

# Fraction of sampled frames idet must classify as interlaced (TFF+BFF) for
# auto-detect to enable deinterlacing. Both real-world cases seen so far
# (see README's Deinterlace section) came back unambiguous -- 100% either
# way, no messy middle ground -- so this doesn't need to be finely tuned.
INTERLACE_DETECT_THRESHOLD = 0.5
INTERLACE_DETECT_SAMPLE_SECONDS = 20

_render_node_cache: dict[str, str] = {}


def find_render_node(vendor_id: str) -> str:
    """Resolve a GPU's render node via /dev/dri/by-path + PCI vendor ID.

    PCI bus order does not predict render node numbering on this box (the
    AMD card enumerates as renderD128 despite being the later PCI device),
    so the node number must never be hardcoded.
    """
    if vendor_id in _render_node_cache:
        return _render_node_cache[vendor_id]
    for entry in sorted(Path("/dev/dri/by-path").glob("pci-*-render")):
        pci_addr = entry.name[len("pci-"):-len("-render")]
        vendor_file = Path(f"/sys/bus/pci/devices/{pci_addr}/vendor")
        try:
            vendor = vendor_file.read_text().strip()
        except OSError:
            continue
        if vendor.lower() == vendor_id.lower():
            resolved = str(entry.resolve())
            _render_node_cache[vendor_id] = resolved
            return resolved
    raise RuntimeError(f"No render node found for PCI vendor {vendor_id}")


def probe_duration(path: Path) -> float:
    """Duration in seconds via ffprobe, or 0.0 if it can't be determined."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def probe_audio_codec(path: Path, track_index: int = 0) -> str | None:
    """codec name of the given audio track index, or None if it doesn't exist."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{track_index}",
         "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    codec = result.stdout.strip()
    return codec or None


def audio_bitrate_kbps(audio_bitrate: str) -> int:
    """"160k" -> 160. AUDIO_BITRATES is always this exact "<int>k" shape."""
    return int(audio_bitrate.rstrip("k"))


def target_size_to_bitrate_kbps(size_mb: float, duration_seconds: float, audio_kbps: float) -> int:
    """Convert a target output size to a video bitrate for a file of the
    given duration, after reserving audio_kbps for the audio track.

    This is necessarily an estimate, not an exact target: audio_kbps is
    whatever the caller already knows to assume (the configured transcode
    bitrate, or the same figure used as a stand-in when copying, since the
    exact copied-track bitrate isn't known without an extra probe) rather
    than the copied track's real bitrate. Returns 0 (not negative) if
    duration is unknown or audio alone would already exceed the target.
    """
    if duration_seconds <= 0:
        return 0
    total_kbps = (size_mb * 8192) / duration_seconds  # 1 MB = 1024*8 kbit
    return max(int(total_kbps - audio_kbps), 0)


def build_idet_args(input_path: Path, sample_seconds: float = INTERLACE_DETECT_SAMPLE_SECONDS) -> list[str]:
    """ffmpeg argv for a decode-only interlace-detection sample. Container
    progressive/interlaced flags are frequently wrong (see README's
    Deinterlace section for a real example: tagged progressive, 100%
    TFF-interlaced by actual pixel content), so this checks real frames
    rather than trusting metadata."""
    return [
        "ffmpeg", "-hide_banner", "-t", str(sample_seconds),
        "-i", str(input_path), "-vf", "idet", "-an", "-f", "null", "-",
    ]


def parse_idet_output(stderr_text: str) -> float:
    """Fraction of sampled frames idet's final "Multi frame detection" line
    classifies as interlaced (TFF+BFF) rather than progressive. 0.0 if the
    text has no usable stats (e.g. the process was killed before finishing)."""
    lines = [ln for ln in stderr_text.splitlines() if "Multi frame detection" in ln]
    if not lines:
        return 0.0
    last = lines[-1]
    try:
        tff = int(last.split("TFF:")[1].split()[0])
        bff = int(last.split("BFF:")[1].split()[0])
        progressive = int(last.split("Progressive:")[1].split()[0])
    except (IndexError, ValueError):
        return 0.0
    total = tff + bff + progressive
    return (tff + bff) / total if total else 0.0


def build_args(
    settings: dict,
    input_path: Path,
    output_path: Path,
    *,
    probe_audio: bool = True,
    audio_codec: str | None = None,
    duration_seconds: float | None = None,
) -> list[str]:
    """Build the full ffmpeg argv for one job from a resolved settings dict.

    settings keys: encoder ("hevc_vaapi"|"libx265"), rc_mode, quality_value
    (quality units for a quality-family rc_mode; target output size in MB
    for a bitrate-family one -- VBR/bitrate mean "hit roughly this file
    size", not "encode at exactly this bitrate", so the number the user
    sets is size, and the bitrate ffmpeg actually gets is derived from it
    plus this specific file's duration, below), speed (compression_level
    1-7 as str, or an x265 preset name), bit_depth (8|10), width, height,
    container ("mp4"|"mkv", default mp4), tune (an x265 tune name or
    "None", ignored for hevc_vaapi), deinterlace (bool, default False --
    container-level progressive/interlaced flags are frequently wrong,
    especially on camcorder-sourced footage; this is a manual override, not
    auto-detected), audio_track, audio_copy_if_compatible, audio_bitrate.

    probe_audio=False skips the real ffprobe call and uses audio_codec as
    given instead -- for building a representative command line to *show*
    the user (e.g. a live preview) against a file that may not exist yet,
    without shelling out on every keystroke. Real jobs always probe.

    duration_seconds, likewise, lets a caller that already has it (real
    jobs always probe duration anyway, for progress tracking) skip a
    redundant ffprobe call. Only used for a bitrate-family rc_mode -- a
    quality-family one never touches it, so it's never probed for the
    common case. None means "probe it if a bitrate-family mode needs it".
    """
    encoder = settings["encoder"]
    is_vaapi = encoder == "hevc_vaapi"
    rc_mode = settings["rc_mode"]
    quality_value = settings["quality_value"]
    width, height = settings["width"], settings["height"]
    bit_depth = settings["bit_depth"]
    container = settings.get("container", "mp4")
    deinterlace = settings.get("deinterlace", False)
    audio_track = settings["audio_track"]
    if probe_audio:
        audio_codec = probe_audio_codec(input_path, audio_track)

    video_kbps = None
    if rc_mode in BITRATE_RC_MODES:
        if duration_seconds is None:
            duration_seconds = probe_duration(input_path)
        reserved_audio_kbps = audio_bitrate_kbps(settings["audio_bitrate"]) if audio_codec is not None else 0
        video_kbps = target_size_to_bitrate_kbps(quality_value, duration_seconds, reserved_audio_kbps)

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]

    if is_vaapi:
        args += ["-vaapi_device", find_render_node(INTEL_VENDOR_ID)]
    args += ["-i", str(input_path)]

    if is_vaapi:
        upload_fmt = "p010le" if bit_depth == 10 else "nv12"
        # deinterlace_vaapi operates on hardware surfaces, so it has to run
        # after hwupload; before scale_vaapi so it works on full-resolution
        # fields rather than already-downscaled ones. rate=frame keeps
        # single-rate output (one deinterlaced frame per field pair) rather
        # than the frame-doubling "bob" behavior.
        deinterlace_stage = "deinterlace_vaapi=rate=frame," if deinterlace else ""
        vf = (
            f"format={upload_fmt},hwupload,{deinterlace_stage}"
            f"scale_vaapi=w='min({width},iw)':h='min({height},ih)':"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        args += ["-vf", vf, "-c:v", "hevc_vaapi"]
        args += ["-profile:v", "main10" if bit_depth == 10 else "main"]
        # HEVC on this hardware only exposes the non-low-power EncSlice
        # entrypoint (confirmed via vainfo) — never add -low_power here.
        if rc_mode == "ICQ":
            args += ["-rc_mode", "ICQ", "-global_quality", str(quality_value)]
        elif rc_mode == "CQP":
            args += ["-rc_mode", "CQP", "-qp", str(quality_value)]
        elif rc_mode == "VBR":
            args += ["-rc_mode", "VBR", "-b:v", f"{video_kbps}k"]
        args += ["-compression_level", str(settings["speed"])]
    else:
        # bwdif's own default (mode=send_field) doubles the frame rate -- one
        # output frame per FIELD -- which isn't what a "fix the interlacing,
        # leave everything else the same" toggle should do. send_frame keeps
        # single-rate output, matching deinterlace_vaapi's rate=frame above.
        deinterlace_stage = "bwdif=mode=send_frame," if deinterlace else ""
        vf = (
            f"{deinterlace_stage}scale=w='min({width},iw)':h='min({height},ih)':"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2"
        )
        pix_fmt = "yuv420p10le" if bit_depth == 10 else "yuv420p"
        args += ["-vf", vf, "-pix_fmt", pix_fmt, "-c:v", "libx265", "-preset", settings["speed"]]
        if rc_mode == "CRF":
            args += ["-crf", str(quality_value)]
        elif rc_mode == "bitrate":
            args += ["-b:v", f"{video_kbps}k"]
        tune = settings.get("tune", "None")
        if tune and tune != "None":
            args += ["-tune", tune]
        args += ["-x265-params", "strong-intra-smoothing=0:aq-mode=3:psy-rdoq=1.0"]

    # Capital V excludes attached-pic/cover-art streams from the video map,
    # matching ffmpeg's own default auto-selection more closely than 'v'.
    args += ["-map", "0:V:0"]
    if audio_codec is not None:
        # Only map the audio track when it's known to exist -- probe_audio_codec
        # (or the caller, for a preview) returning None means audio_track doesn't
        # exist on this file, and mapping it anyway would fail the whole job on
        # a stream ffmpeg can't find, instead of just proceeding without audio.
        args += ["-map", f"0:a:{audio_track}"]
        if settings["audio_copy_if_compatible"] and audio_codec in ("aac", "ac3", "eac3"):
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", settings["audio_bitrate"]]

    # Subtitle/data passthrough isn't implemented — drop both explicitly so an
    # MP4-incompatible subtitle codec (e.g. PGS) can't fail the mux. (MKV
    # output would tolerate them, but nothing currently maps them in either
    # case -- see README's Known gaps.)
    args += ["-sn", "-dn"]
    args += ["-map_metadata", "0"]
    if container == "mp4":
        # movflags is a mov/mp4-muxer-private option; ffmpeg silently
        # ignores it for other muxers, but omit it for mkv anyway so the
        # command line doesn't carry a flag that means nothing there.
        args += ["-movflags", "+faststart"]
    args += ["-progress", "pipe:1", "-nostats"]
    args += [str(output_path)]
    return args


class TranscodeQueue(QObject):
    job_started = Signal(str, int, int)  # path, index (1-based), total
    job_progress = Signal(float)         # 0.0-1.0
    job_stats = Signal(dict)             # {"fps", "bitrate", "speed", "eta_seconds"} -- see _emit_stats
    job_log = Signal(str)                # one log line
    job_finished = Signal(str, str)      # input_path, output_path
    job_failed = Signal(str, str)        # path, reason
    all_finished = Signal()

    def __init__(self):
        super().__init__()
        self._process: QProcess | None = None
        self._jobs: list[dict] = []
        self._index = 0
        self._output_dir: Path | None = None
        self._duration = 0.0
        self._stopped = False
        self._stats_buffer: dict = {}

    def start(self, jobs: list[dict], output_dir: Path):
        """jobs: list of settings dicts (see build_args) plus a "path" key."""
        self._jobs = list(jobs)
        self._output_dir = output_dir
        self._index = 0
        self._stopped = False
        self._run_next()

    def stop(self):
        self._stopped = True
        if self._process is not None:
            self._process.terminate()

    def _run_next(self):
        if self._stopped or self._index >= len(self._jobs):
            self.all_finished.emit()
            return

        job = self._jobs[self._index]
        self._index += 1
        input_path: Path = job["path"]
        container = job.get("container", "mp4")
        output_path = self._output_dir / (input_path.stem + f".{container}")

        if output_path.resolve() == input_path.resolve():
            # ffmpeg's -y would truncate this file for writing while still
            # reading from it as input -- refuse rather than destroy the source.
            self.job_failed.emit(
                str(input_path), "output path is the same as the input file — skipped"
            )
            self._run_next()
            return

        self._stats_buffer = {}
        try:
            self._duration = probe_duration(input_path)
            args = build_args(job, input_path, output_path, duration_seconds=self._duration)
        except Exception as exc:
            self.job_failed.emit(str(input_path), str(exc))
            self._run_next()
            return

        self.job_started.emit(str(input_path), self._index, len(self._jobs))
        self.job_log.emit("ffmpeg " + " ".join(args[1:]))

        proc = QProcess()
        proc.setProgram(args[0])
        proc.setArguments(args[1:])
        proc.readyReadStandardOutput.connect(lambda: self._read_progress(proc))
        proc.readyReadStandardError.connect(lambda: self._read_log(proc))
        proc.finished.connect(
            lambda code, status: self._on_finished(input_path, output_path, code, status)
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
        try:
            speed = float(stats.get("speed", "").rstrip("x"))
            out_time = stats.get("out_time_seconds")
            if speed > 0 and out_time is not None and self._duration > 0:
                eta_seconds = max(self._duration - out_time, 0) / speed
        except ValueError:
            pass
        stats["eta_seconds"] = eta_seconds
        self.job_stats.emit(stats)

    def _read_log(self, proc: QProcess):
        data = bytes(proc.readAllStandardError()).decode(errors="replace")
        for line in data.splitlines():
            if line.strip():
                self.job_log.emit(line)

    def _on_finished(self, input_path: Path, output_path: Path, exit_code: int, exit_status):
        self._process = None
        # A successful exit always wins, even if stop() was also called --
        # QProcess.finished delivery is async, so a job can genuinely finish
        # (exit 0, file written) in the same window the user clicks Stop.
        # Checking _stopped first would then delete a completed output and
        # report it as cancelled instead of keeping it.
        if exit_code == 0 and output_path.exists():
            self.job_progress.emit(1.0)
            self.job_finished.emit(str(input_path), str(output_path))
        elif self._stopped:
            self._cleanup_partial(output_path)
            self.job_failed.emit(str(input_path), "stopped by user")
        else:
            self._cleanup_partial(output_path)
            self.job_failed.emit(str(input_path), f"ffmpeg exited {exit_code}")
        self._run_next()

    @staticmethod
    def _cleanup_partial(output_path: Path):
        try:
            output_path.unlink()
        except OSError:
            pass
