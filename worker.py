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


def build_args(settings: dict, input_path: Path, output_path: Path) -> list[str]:
    """Build the full ffmpeg argv for one job from a resolved settings dict.

    settings keys: encoder ("hevc_vaapi"|"libx265"), rc_mode, quality_value
    (quality units, or kbps when rc_mode is a bitrate mode), speed
    (compression_level 1-7 as str, or an x265 preset name), bit_depth (8|10),
    width, height, container ("mp4"|"mkv", default mp4), tune (an x265 tune
    name or "None", ignored for hevc_vaapi), audio_track,
    audio_copy_if_compatible, audio_bitrate.
    """
    encoder = settings["encoder"]
    is_vaapi = encoder == "hevc_vaapi"
    rc_mode = settings["rc_mode"]
    quality_value = settings["quality_value"]
    width, height = settings["width"], settings["height"]
    bit_depth = settings["bit_depth"]
    container = settings.get("container", "mp4")

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]

    if is_vaapi:
        args += ["-vaapi_device", find_render_node(INTEL_VENDOR_ID)]
    args += ["-i", str(input_path)]

    if is_vaapi:
        upload_fmt = "p010le" if bit_depth == 10 else "nv12"
        vf = (
            f"format={upload_fmt},hwupload,"
            f"scale_vaapi=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease"
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
            args += ["-rc_mode", "VBR", "-b:v", f"{quality_value}k"]
        args += ["-compression_level", str(settings["speed"])]
    else:
        vf = f"scale=w='min({width},iw)':h='min({height},ih)':force_original_aspect_ratio=decrease"
        pix_fmt = "yuv420p10le" if bit_depth == 10 else "yuv420p"
        args += ["-vf", vf, "-pix_fmt", pix_fmt, "-c:v", "libx265", "-preset", settings["speed"]]
        if rc_mode == "CRF":
            args += ["-crf", str(quality_value)]
        elif rc_mode == "bitrate":
            args += ["-b:v", f"{quality_value}k"]
        tune = settings.get("tune", "None")
        if tune and tune != "None":
            args += ["-tune", tune]
        args += ["-x265-params", "strong-intra-smoothing=0:aq-mode=3:psy-rdoq=1.0"]

    audio_track = settings["audio_track"]
    # Capital V excludes attached-pic/cover-art streams from the video map,
    # matching ffmpeg's own default auto-selection more closely than 'v'.
    args += ["-map", "0:V:0", "-map", f"0:a:{audio_track}"]
    audio_codec = probe_audio_codec(input_path, audio_track)
    if audio_codec is not None:
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
    job_finished = Signal(str)           # path
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
        self._duration = probe_duration(input_path)
        self._stats_buffer = {}

        try:
            args = build_args(job, input_path, output_path)
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
        if self._stopped:
            self._cleanup_partial(output_path)
            self.job_failed.emit(str(input_path), "stopped by user")
        elif exit_code == 0 and output_path.exists():
            self.job_progress.emit(1.0)
            self.job_finished.emit(str(input_path))
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
