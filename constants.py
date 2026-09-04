"""Static configuration: encoders, rate-control modes, resolutions, and the
built-in seed presets. The seed presets themselves live in
builtin_presets.json (see presets.py) -- BUILTIN_PRESETS/
BUILTIN_PRESET_NAMES below just load and expose them, they're not
user-editable at runtime; the app's real runtime preset list (built-ins
plus anything the user has saved) lives in presets.py / presets.json.
"""

from presets import load_builtin_presets

VIDEO_FILTER = "Video files (*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.wmv);;All files (*)"
AUDIO_TRACK_LABELS = ["Track 1", "Track 2", "Track 3", "Track 4"]

# (engine id, gpu_vendor or None, display label) -- which *engine*
# (software/CPU vs. which GPU) runs the encode. Two rows share the same
# engine id ("hevc_vaapi") because it's the same ffmpeg codec/profile/
# pixel-format logic on either GPU -- only which render node gets opened
# and which rc_modes are valid differ by vendor (see RC_MODES below;
# confirmed empirically, not assumed: this exact machine has both a real
# Intel iGPU and a real AMD discrete GPU, `vainfo` + real encodes run
# against each).
#
# The CPU row's own id ("libx265") is really just a placeholder/default,
# not necessarily what actually runs -- CODECS below is a second,
# independent axis (H.265 vs H.264) that only applies to the CPU engine;
# main.py's _current_encoder_id() resolves the two together into the real
# ffmpeg codec. Hardware stays HEVC-only here (no h264_vaapi wired up),
# which is exactly why this isn't a single flat "4 combined choices"
# list -- codec choice would be meaningless noise on the Intel/AMD rows.
ENCODERS = [
    ("libx265", None, "CPU"),
    ("hevc_vaapi", "intel", "Intel (iGPU)"),
    ("hevc_vaapi", "amd", "AMD (GPU)"),
]

# (ffmpeg codec id, display label) -- the CPU engine's own second axis,
# a separate Format-box control (main.py/ui_builder.py) rather than
# folded into ENCODERS above as more flat rows: confirmed direct testing
# against this ffmpeg build that libx264 shares almost the entire
# settings surface libx265 already exposes (CRF/bitrate rate control,
# 10-bit, tune, the same ultrafast..placebo preset names) -- worker.py's
# build_args treats it as a second software path alongside libx265, not
# a separate one. Meaningless for hardware (VAAPI is HEVC-only here), so
# this control is disabled -- not just hidden, since "H.265 (HEVC)" is
# still the real answer -- whenever a hardware engine is selected.
CODECS = [
    ("libx265", "H.265 (HEVC)"),
    ("libx264", "H.264 (AVC)"),
]


def encoder_profile_key(encoder: str, gpu_vendor: str | None) -> str:
    """RC_MODES/RC_MODE_FRIENDLY lookup key -- "hevc_vaapi" alone isn't
    specific enough once there are two GPU vendors with different valid
    rc_modes behind it."""
    return f"{encoder}_{gpu_vendor}" if gpu_vendor else encoder


RC_MODES = {
    "hevc_vaapi_intel": [
        ("ICQ", "ICQ (quality, hardware)"),
        ("CQP", "CQP (fixed quantizer)"),
        ("VBR", "VBR (target bitrate)"),
    ],
    # AMD's radeonsi VAAPI driver rejects ICQ outright ("Driver does not
    # support ICQ RC mode") -- confirmed by actually running it, not assumed
    # from a spec sheet. CQP and VBR both work (real encodes verified) and
    # are what Intel's Quality/File Size buttons already use, so those two
    # carry over untouched; QVBR/CBR exist on this driver too but aren't
    # exposed here -- QVBR needs both a quality *and* a bitrate value
    # simultaneously, which doesn't fit the single-slider model any of the
    # other rc_modes use, and CBR doesn't map to either Quality or File Size.
    "hevc_vaapi_amd": [
        ("CQP", "CQP (fixed quantizer)"),
        ("VBR", "VBR (target bitrate)"),
    ],
    "libx265": [
        ("CRF", "CRF (quality)"),
        ("bitrate", "Target bitrate"),
    ],
    # Same two rc_mode values as libx265 -- x264's CRF scale is nominally
    # 0-51 too, with the same default (23), so QUALITY_RANGES["CRF"]
    # below is shared rather than needing its own libx264-specific entry.
    "libx264": [
        ("CRF", "CRF (quality)"),
        ("bitrate", "Target bitrate"),
    ],
}

# The GUI's Rate Control control is two plain-English buttons -- Quality /
# File Size -- that mean the same thing regardless of which encoder is
# selected, mapped here to the real rc_mode underneath for that specific
# encoder+vendor. "advanced" is a third button for a mode that doesn't fit
# either bucket; None means that button is hidden entirely rather than
# shown-but-broken. Intel gets ICQ as Quality with CQP demoted to Advanced;
# AMD has no ICQ, so CQP *is* its Quality button and there's no third mode
# left worth surfacing as Advanced.
RC_MODE_FRIENDLY = {
    "hevc_vaapi_intel": {"quality": "ICQ", "file_size": "VBR", "advanced": "CQP"},
    "hevc_vaapi_amd": {"quality": "CQP", "file_size": "VBR", "advanced": None},
    "libx265": {"quality": "CRF", "file_size": "bitrate", "advanced": None},
    "libx264": {"quality": "CRF", "file_size": "bitrate", "advanced": None},
}

# rc_mode -> (min, max, default) for the quality control (ignored for bitrate modes)
QUALITY_RANGES = {
    "ICQ": (1, 51, 26),
    "CQP": (0, 52, 26),
    "CRF": (0, 51, 23),
}

X265_PRESETS = [
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow", "placebo",
]

RESOLUTIONS = [
    # "Keep Original" here, not the sibling TITAN-i Transcoder app's
    # "Source (no scale)" -- same width/height sentinel (build_args reads
    # this pair, never the label), plain language only.
    {"label": "Keep Original", "width": 99999, "height": 99999},
    {"label": "1080p", "width": 1920, "height": 1080},
    {"label": "720p", "width": 1280, "height": 720},
    {"label": "480p", "width": 854, "height": 480},
]

AUDIO_BITRATES = ["96k", "128k", "160k", "192k", "256k"]

CONTAINERS = ["mp4", "mkv"]

# x265's own tune list, verified against this exact build -- "film" is a
# commonly-listed x265 tune elsewhere but this libx265 rejects it outright
# ("Error setting preset/tune (null)/film"), so it's deliberately excluded.
# "None" means omit -tune entirely, x265's own default. VAAPI ignores this.
X265_TUNES = ["None", "animation", "grain", "psnr", "ssim", "fastdecode", "zerolatency"]

# x264's own tune list -- a real superset of X265_TUNES above, not just
# assumed to match: every X265_TUNES value plus "film" and "stillimage",
# both confirmed to actually work against this exact libx264 build (real
# encodes run with each, none rejected) despite "film" specifically being
# the one value x265 here refuses outright. Kept as its own separate list
# rather than adding film/stillimage to X265_TUNES -- that would silently
# offer them for x265 too, right back into the bug X265_TUNES's own
# comment above already documents fixing.
X264_TUNES = ["None", "film", "animation", "grain", "stillimage", "psnr", "ssim", "fastdecode", "zerolatency"]

# Normal-mode's Quality button row (Smaller File / Balanced / Better
# Quality) -- three plain-English buttons that resolve, per current
# encoder+vendor, to the exact same numeric quality_value the matching
# Low/Balanced/High built-in preset already ships (see BUILTIN_PRESETS'
# own comment below for where 18/23/28 and 16/26/36 come from). Always
# paired with RC_MODE_FRIENDLY[key]["quality"] as the rc_mode -- every
# High/Balanced/Low built-in uses the quality-family rc_mode (CRF/ICQ/CQP),
# never bitrate, so Quality tiers don't need their own rc_mode axis.
QUALITY_TIERS = {
    "libx265": {"smaller": 28, "balanced": 23, "better": 18},
    "libx264": {"smaller": 28, "balanced": 23, "better": 18},
    "hevc_vaapi_intel": {"smaller": 36, "balanced": 26, "better": 16},
    "hevc_vaapi_amd": {"smaller": 36, "balanced": 26, "better": 16},
}

# Mapped from the user's real HandBrake custom presets — see README's Presets
# section. Protected: Save As refuses these names, Delete refuses these entries.
#
# Each engine gets a High/Balanced/Low trio, not just the one Balanced point
# each used to be alone -- same speed as that engine's own Balanced (the
# quality/size tradeoff is the axis these tiers move along, not effort-vs-
# time, which Balanced already picked for engine-specific reasons of its
# own -- see AMD Balanced's own note below), quality_value moved to a
# meaningfully different point on the same ~50-value ICQ/CQP/CRF scale the
# Quality slider's own 6-tier fuzzy captions (main.py) divide up. CPU's CRF
# 18/28 are real, widely-used x265 community reference points ("visually
# lossless" / "noticeably smaller, still watchable"); ICQ/CQP's 16/36 are
# this app's own estimate by rough analogy to those, not independently
# A/B'd against remembered output quality any more than Intel's Balanced
# compression_level was -- see Known gaps.
#
# The actual 9 presets live in builtin_presets.json, not here -- plain,
# readable/inspectable JSON, rather than a literal Python list baked into
# this file. Loaded once at import time since nothing in the app ever
# writes to it (unlike presets.json, which save_presets/_save_preset_as/
# _delete_preset do write -- see presets.py).
#
# AMD Balanced's speed="4": no HandBrake/QSV legacy to map from (unlike the
# Intel preset) -- "4" is a genuine middle-of-the-ladder default (1..7),
# matching what "Balanced" actually means in this app's own speed
# semantics, rather than inheriting Intel's "1" (its slowest, highest-
# effort setting, chosen there for unrelated historical reasons -- see
# that preset's own history). CQP 26 mirrors the Intel preset's ICQ 26 --
# AMD's driver has no ICQ (see RC_MODES), so CQP is the closest quality-
# family equivalent. High/Low mirror the Intel trio's own ICQ 16/36 the
# same way, for the same reason.
BUILTIN_PRESETS = load_builtin_presets()
BUILTIN_PRESET_NAMES = {p["name"] for p in BUILTIN_PRESETS}
