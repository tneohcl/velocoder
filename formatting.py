"""Pure display-formatting helpers, split out of MainWindow -- these were
already @staticmethod (no `self` dependency at all), so this is a
straightforward move: plain functions here, directly unit-testable without
building a QApplication/MainWindow, which they now are."""
from pathlib import Path

from PySide6.QtWidgets import QSlider

import worker
from constants import ENCODERS, RESOLUTIONS


def display_path(path: Path) -> str:
    """Collapse a path under the user's home directory to a "~/..." form
    for display -- friendlier than the fully resolved absolute path (e.g.
    Save to:) without changing what's actually used for file operations,
    which stays a plain resolved Path throughout."""
    home = Path.home()
    if path == home:
        return "~"
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)

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


def hardware_status_text() -> str:
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


def fraction_of(slider: QSlider) -> float:
    lo, hi = slider.minimum(), slider.maximum()
    return (slider.value() - lo) / (hi - lo) if hi > lo else 0.0


def tier_label(fraction: float, *labels: str) -> str:
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


def settings_differ(a: dict, b: dict) -> bool:
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


def video_codec_label(codec_name: str | None) -> str:
    if not codec_name:
        return "?"
    return _VIDEO_CODEC_LABELS.get(codec_name, codec_name.upper())


def audio_codec_label(codec_name: str | None) -> str:
    if not codec_name:
        return "?"
    return _AUDIO_CODEC_LABELS.get(codec_name, codec_name.upper())


def audio_channel_label(channels: int | None) -> str:
    if not channels:
        return "?"
    return _AUDIO_CHANNEL_LABELS.get(channels, f"{channels}ch")


def format_eta(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_eta_human(seconds: float) -> str:
    """"About 8 minutes remaining" -- the plain-language counterpart to
    format_eta's clock-style "8:12" above, for the prominent line in the
    run-control area (queue_controller.py's _on_job_stats); format_eta
    itself stays exactly as-is for the detailed technical stats line
    right below it, unchanged.
    Rounds to the nearest minute (or "less than a minute"/whole hours +
    minutes) rather than showing seconds -- a live countdown to the exact
    second reads as more precise than an ffmpeg-derived estimate actually
    is."""
    seconds = int(seconds)
    if seconds < 60:
        return "Less than a minute remaining"
    minutes = round(seconds / 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"About {hours} hr {minutes} min remaining"
    if hours:
        return f"About {hours} hr remaining"
    return f"About {minutes} min remaining"


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def format_run_summary(
    completed_count: int, failed_count: int, total_input_bytes: int, total_output_bytes: int
) -> tuple[str, str]:
    """Finished-run summary text for the run-control panel's two repurposed
    labels (queue_controller.py's _apply_run_phase_visuals, phase="finished")
    -- (count_line, size_line), e.g. ("4 videos converted", "12.4GB → 4.1GB ·
    67% smaller"), or ("3 videos converted · 1 failed", ...) when failed_count
    is nonzero. size_line is "" when total_input_bytes <= 0 -- every per-job
    size stat this run failed (_append_result_size returning None for each
    one) even though jobs still completed, so there's nothing real to compare
    rather than a bogus "100% smaller"/divide-by-zero. Which heading
    ("✓ Conversion Complete"/"Conversion Stopped"/"Completed with Issues")
    goes above these two lines is _on_all_finished's own decision, not this
    function's -- failed_count alone doesn't say whether the run was also
    cancelled, which matters more for picking the heading."""
    count_line = f"{completed_count} video{'s' if completed_count != 1 else ''} converted"
    if failed_count > 0:
        count_line += f" · {failed_count} failed"
    if total_input_bytes <= 0:
        return count_line, ""
    change_pct = 100 * (1 - total_output_bytes / total_input_bytes)
    direction = "smaller" if change_pct >= 0 else "larger"
    size_line = f"{format_size(total_input_bytes)} → {format_size(total_output_bytes)} · {abs(change_pct):.0f}% {direction}"
    return count_line, size_line


def settings_summary(job: dict) -> str:
    """Human-readable multi-line rendering of a queue row's chosen output
    settings, for the queue table's hover tooltip -- distinct from the
    Effective Command preview (main.py), which shows the raw ffmpeg argv
    for a technical reader; this is the friendly summary for everyone
    else."""
    encoder = job.get("encoder")
    gpu_vendor = job.get("gpu_vendor")
    if encoder == "hevc_vaapi":
        encoder_label = next(
            (label for enc, vendor, label in ENCODERS if enc == encoder and vendor == gpu_vendor),
            encoder or "?",
        )
    else:
        # CPU engine -- ENCODERS' own single CPU row is just a
        # placeholder id now (constants.py: "libx265", regardless of
        # which codec is actually chosen), it doesn't distinguish
        # libx264 from libx265 the way the old combined combo entries
        # did. Built directly from the real codec id instead -- display
        # only, not tied to an actual combo entry anymore now that
        # codec is main.py's own separate Format -- Codec control.
        encoder_label = "CPU (x264)" if encoder == "libx264" else "CPU (x265)"

    rc_mode = job.get("rc_mode")
    if rc_mode in worker.BITRATE_RC_MODES:
        rate_line = f"Target size: {job.get('quality_value')} MB"
    else:
        rate_line = f"{rc_mode} {job.get('quality_value')}"

    width, height = job.get("width"), job.get("height")
    res_label = next(
        (r["label"] for r in RESOLUTIONS if r["width"] == width and r["height"] == height),
        f"{width}x{height}" if width and height else "?",
    )

    lines = [
        f"{encoder_label} -- {rate_line}",
        f"{res_label}, {job.get('bit_depth')}-bit, {str(job.get('container', '?')).upper()}",
        f"Speed: {job.get('speed')}",
    ]
    tune = job.get("tune")
    if tune and tune != "None":
        lines.append(f"Tune: {tune}")
    lines.append(f"Deinterlace: {'on' if job.get('deinterlace') else 'off'}")

    audio_bits = [f"Track {job.get('audio_track', 0) + 1}"]
    if job.get("audio_copy_if_compatible"):
        audio_bits.append("copy if compatible")
    audio_bits.append(str(job.get("audio_bitrate", "?")))
    if job.get("audio_downmix_stereo"):
        audio_bits.append("downmix to stereo")
    lines.append("Audio: " + ", ".join(audio_bits))

    return "\n".join(lines)
