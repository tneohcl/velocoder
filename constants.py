"""Static configuration: encoders, rate-control modes, resolutions, and the
built-in seed presets. Nothing here is user-editable at runtime — user-saved
presets live separately, in presets.py / user_presets.json.
"""

VIDEO_FILTER = "Video files (*.mkv *.mp4 *.avi *.mov *.m4v *.ts *.wmv);;All files (*)"
AUDIO_TRACK_LABELS = ["Track 1", "Track 2", "Track 3", "Track 4"]

ENCODERS = [("hevc_vaapi", "VAAPI HEVC (Hardware)"), ("libx265", "x265 (CPU)")]

RC_MODES = {
    "hevc_vaapi": [
        ("ICQ", "ICQ (quality, hardware)"),
        ("CQP", "CQP (fixed quantizer)"),
        ("VBR", "VBR (target bitrate)"),
    ],
    "libx265": [
        ("CRF", "CRF (quality)"),
        ("bitrate", "Target bitrate"),
    ],
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
    {"label": "Source (no scale)", "width": 99999, "height": 99999},
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

# Mapped from the user's real HandBrake custom presets — see README's Presets
# section. Protected: Save As refuses these names, Delete refuses these entries.
BUILTIN_PRESETS = [
    {
        "name": "720p QSV Balanced (Hardware / VAAPI)",
        "encoder": "hevc_vaapi", "rc_mode": "ICQ", "quality_value": 26, "speed": "1",
        "bit_depth": 10, "width": 1280, "height": 720, "container": "mp4", "tune": "None",
        "audio_track": 0, "audio_copy_if_compatible": True, "audio_bitrate": "160k",
    },
    {
        "name": "720p Stuff Tuned (CPU / x265)",
        "encoder": "libx265", "rc_mode": "CRF", "quality_value": 23, "speed": "medium",
        "bit_depth": 10, "width": 1280, "height": 720, "container": "mp4", "tune": "None",
        "audio_track": 0, "audio_copy_if_compatible": True, "audio_bitrate": "160k",
    },
]
BUILTIN_PRESET_NAMES = {p["name"] for p in BUILTIN_PRESETS}
