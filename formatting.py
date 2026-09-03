"""Pure display-formatting helpers, split out of MainWindow -- these were
already @staticmethod (no `self` dependency at all), so this is a
straightforward move: plain functions here, directly unit-testable without
building a QApplication/MainWindow, which they now are."""
from PySide6.QtWidgets import QSlider

import worker

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


def format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"
