"""Everything VeloCoder knows about ffmpeg, with no Qt: hardware detection,
probing, interlace detection, building the encode arguments, and reading a
failure's reason. ui/transcode_queue.py runs the actual encodes with QProcess.

Takes a plain settings dict per job — no preset catalog here. Presets are a
GUI-side convenience for naming/saving/loading a settings snapshot; the
engine only ever sees the resolved values.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

INTEL_VENDOR_ID = "0x8086"
AMD_VENDOR_ID = "0x1002"
GPU_VENDOR_IDS = {"intel": INTEL_VENDOR_ID, "amd": AMD_VENDOR_ID}
BITRATE_RC_MODES = {"VBR", "bitrate"}
# ffmpeg stderr lines that state why an encode failed. "Conversion
# failed!" matches too but says nothing, so summarize_ffmpeg_failure skips it.
_FFMPEG_ERROR_RE = re.compile(
    r"error|failed|invalid|cannot|could not|unable|not supported|no such|denied|no usable",
    re.IGNORECASE,
)

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


@dataclass(frozen=True)
class ProcessingBackend:
    """One real, verified-usable Processing choice. For a GPU, "verified"
    means both a DRM render node exists for that PCI vendor and a tiny
    real hevc_vaapi encode on it succeeded (validate_hevc_encode) -- a
    render node alone only proves the kernel sees the card, not that a
    VAAPI driver capable of encoding is installed for it."""
    id: str
    display_name: str


@dataclass(frozen=True)
class UnusableGpu:
    """A GPU whose render node exists but whose validation encode failed
    -- kept separate from ProcessingBackend so nothing that builds
    Processing choices from the backend list can offer it by mistake.
    reason is ffmpeg's own error line, for the debug log only."""
    id: str
    display_name: str
    reason: str


# Each vendor's validation encode uses the rate-control mode its Quality
# default actually runs with (constants.RC_MODE_FRIENDLY's "quality"), so
# a driver that can encode HEVC but rejects that mode still gets caught
# here rather than on the user's first real job.
_VALIDATION_RC_ARGS = {
    "intel": ["-rc_mode", "ICQ", "-global_quality", "26"],
    "amd": ["-rc_mode", "CQP", "-qp", "26"],
}
_VALIDATION_TIMEOUT_SECONDS = 10


def summarize_ffmpeg_failure(stderr_lines: list[str], exit_code: int) -> str:
    """The job-failed reason for a non-zero ffmpeg exit: its last real error
    line (minus the " @ 0x..." pointer noise), so the queue row can say why
    instead of just "ffmpeg exited 1"."""
    for line in reversed(stderr_lines):
        line = line.strip()
        if line and line != "Conversion failed!" and _FFMPEG_ERROR_RE.search(line):
            return f"{re.sub(r' @ 0x[0-9a-f]+', '', line)} (ffmpeg exited {exit_code})"
    return f"ffmpeg exited {exit_code}"


def validate_hevc_encode(render_node: str, vendor: str) -> str | None:
    """Run a one-frame hevc_vaapi encode on render_node. None means it
    worked; otherwise ffmpeg's first error line (hex addresses stripped).

    Confirmed necessary on real hardware: Fedora's own intel-media-driver
    build (free kernels only) loads fine on a UHD 630 but has no encode
    entrypoints at all ("Compatible profile VAProfileHEVCMain (17) is not
    supported by driver"), and with no VAAPI driver installed the render
    nodes still exist -- both cases a render-node-only check reported as
    available. ~0.15s per GPU."""
    args = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-vaapi_device", render_node,
        "-f", "lavfi", "-i", "testsrc2=s=256x144:d=1", "-frames:v", "1",
        "-vf", "format=nv12,hwupload", "-c:v", "hevc_vaapi",
        *_VALIDATION_RC_ARGS[vendor],
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=_VALIDATION_TIMEOUT_SECONDS,
        )
    except OSError as e:
        return f"could not run ffmpeg: {e}"
    except subprocess.TimeoutExpired:
        return f"validation encode timed out after {_VALIDATION_TIMEOUT_SECONDS}s"
    if result.returncode == 0:
        return None
    lines = [ln.strip() for ln in result.stderr.splitlines() if ln.strip()]
    first = lines[0] if lines else f"ffmpeg exited with code {result.returncode}"
    return re.sub(r" @ 0x[0-9a-f]+", "", first)


def detect_hardware() -> tuple[list[ProcessingBackend], list[UnusableGpu]]:
    """Every Processing choice actually usable on this machine, plus any
    GPU that has a render node but failed its validation encode.

    Backends are CPU first, then whichever GPU vendors both resolve a
    render node and pass validate_hevc_encode -- CPU/Intel/AMD order
    matches the segmented row's own longstanding left-to-right layout
    (ui_builder.py), not a hardware-preference ranking. CPU is
    unconditional: the software encoder needs no device at all. The one
    place anything picks hardware *for* the user (best_available_engine,
    below) reuses this instead of re-probing on its own, so "what Automatic
    silently resolves to" and "what Processing even offers to pick
    manually" can never disagree.

    A GPU with no render node at all is simply absent from both lists --
    only a present-but-broken one is worth telling the user about."""
    backends = [ProcessingBackend("cpu", "CPU")]
    unusable = []
    for vendor, display_name in (("intel", "Intel"), ("amd", "AMD")):
        try:
            node = find_render_node(GPU_VENDOR_IDS[vendor])
        except RuntimeError:
            continue
        error = validate_hevc_encode(node, vendor)
        if error is None:
            backends.append(ProcessingBackend(vendor, display_name))
        else:
            unusable.append(UnusableGpu(vendor, display_name, error))
    return backends, unusable


def detect_available_backends() -> list[ProcessingBackend]:
    """Just detect_hardware()'s usable backends, for callers that don't
    report unusable GPUs."""
    return detect_hardware()[0]


def best_available_engine(backends: list[ProcessingBackend] | None = None) -> tuple[str, str | None]:
    """Normal-mode's "Processing: Automatic" resolves to this -- prefer
    Intel iGPU, then AMD GPU, then fall back to CPU. Returns (encoder id,
    gpu_vendor), matching ENCODERS' own row shape in constants.py, so a
    caller can feed this straight into the same code path a manual
    Encoder-dropdown pick already goes through.

    backends: pass this session's own cached detect_available_backends()
    snapshot (MainWindow._available_backends) rather than leaving this to
    re-probe on its own -- hardware is fixed for the life of a run of this
    app (no hot-plug monitoring), and every caller re-detecting
    independently is how "what Automatic resolves to" and "what Processing
    offers to pick manually" could end up disagreeing the moment detection
    stops being a cheap sysfs read (a real validation encode, say).
    Defaults to a fresh probe for standalone/test use where no such
    snapshot exists.

    Order matches formatting.hardware_status_text()'s own vendor-probe
    order.
    """
    if backends is None:
        backends = detect_available_backends()
    available = {backend.id for backend in backends}
    for vendor in ("intel", "amd"):
        if vendor in available:
            return "hevc_vaapi", vendor
    return "libx265", None


def ffmpeg_version() -> str | None:
    """Just the version token from ffmpeg's own first output line (e.g.
    "6.1.1" out of "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) ..."),
    for System Information (about_dialogs.py) -- None if ffmpeg isn't on
    PATH, or its output doesn't look like the format above, rather than
    ever raising into a dialog that's meant to degrade gracefully."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = result.stdout.splitlines()[0] if result.stdout else ""
    match = re.match(r"ffmpeg version (\S+)", first_line)
    return match.group(1) if match else None


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


def probe_audio_channels(path: Path, track_index: int = 0) -> int | None:
    """Channel count of the given audio track index, or None if it doesn't
    exist / isn't reported. A separate probe from probe_audio_codec (not a
    combined query) and only called where actually needed (build_args, only
    when a downmix is actually being considered) -- most jobs never need
    channel count at all, so there's no reason to pay for it by default."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{track_index}",
         "-show_entries", "stream=channels",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def probe_audio_bitrate_kbps(path: Path, track_index: int = 0) -> int | None:
    """Real bitrate (kbps) of the given audio track, or None if it isn't
    reported. Only called when a bitrate-family rc_mode (File Size) is
    actually going to *copy* the audio track through untouched -- that's
    the one case where the configured AAC bitrate (audio_bitrate_kbps)
    is the wrong number to reserve: a copy-compatible source (AC-3/EAC-3/
    AAC) can genuinely be encoded at any bitrate, and Automatic will copy
    it exactly as-is regardless of what audio_bitrate happens to be set
    to. Reserving the configured AAC figure for a copied 640kbps AC-3
    track under-reserves by 480kbps, letting the actual output
    materially exceed the requested target size -- confirmed as a real
    correctness gap, not a hypothetical one. Some containers/codecs
    don't report a per-stream bit_rate at all (this ffprobe field is
    genuinely absent, not just zero, for some MKV sources) -- None here
    means exactly that, and the caller falls back to the configured AAC
    bitrate as the best remaining estimate, same as before this fix
    existed."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", f"a:{track_index}",
         "-show_entries", "stream=bit_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        bits_per_second = int(result.stdout.strip())
    except ValueError:
        return None
    return bits_per_second // 1000 if bits_per_second > 0 else None


def build_probe_args(input_path: Path) -> list[str]:
    """ffprobe argv for a single-shot source-metadata query: container
    duration plus every stream's key fields. Header-only (no decoding, unlike
    build_idet_args), so this is fast even for large files -- run async via
    QProcess anyway (see MainWindow._start_source_probe) since "fast" still
    isn't instant on a slow disk or network share, and add_files() may be
    probing a whole dropped batch at once."""
    return [
        "ffprobe", "-v", "error", "-of", "json",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,channels",
        str(input_path),
    ]


def _parse_frame_rate(raw: str | None) -> float:
    """ffprobe reports r_frame_rate as "num/den" (e.g. "24000/1001"); 0.0 if
    missing or the denominator is 0 (a still-image "stream" some containers
    report alongside the real video track)."""
    if not raw or "/" not in raw:
        return 0.0
    num, _, den = raw.partition("/")
    try:
        num, den = float(num), float(den)
    except ValueError:
        return 0.0
    return num / den if den else 0.0


def parse_probe_output(stdout_text: str) -> dict:
    """Pulls the fields the queue table's source-property columns need out
    of build_probe_args's JSON. Missing or unparseable pieces are just
    absent from the result rather than raising -- a probe hiccup shouldn't
    ever block a file from being queued, only leave that column blank."""
    try:
        data = json.loads(stdout_text)
    except (json.JSONDecodeError, TypeError):
        return {}
    result = {}
    try:
        result["duration"] = float(data.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        pass
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video:
        result["video_codec"] = video.get("codec_name")
        result["width"] = video.get("width")
        result["height"] = video.get("height")
        result["frame_rate"] = _parse_frame_rate(video.get("r_frame_rate"))
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    if audio_streams:
        result["audio_codec"] = audio_streams[0].get("codec_name")
        result["audio_channels"] = audio_streams[0].get("channels")
        result["audio_track_count"] = len(audio_streams)
    return result


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
    audio_channels: int | None = None,
    duration_seconds: float | None = None,
    audio_source_bitrate_kbps: int | None = None,
) -> list[str]:
    """Build the full ffmpeg argv for one job from a resolved settings dict.

    settings keys: encoder ("hevc_vaapi"|"libx265"|"libx264"), gpu_vendor ("intel"|"amd",
    only meaningful when encoder is hevc_vaapi -- picks which GPU's render
    node opens; defaults to "intel" if absent, for presets saved before this
    key existed), rc_mode, quality_value (quality units for a quality-family
    rc_mode; target output size in MB for a bitrate-family one -- VBR/bitrate
    mean "hit roughly this file size", not "encode at exactly this bitrate",
    so the number the user sets is size, and the bitrate ffmpeg actually
    gets is derived from it plus this specific file's duration, below),
    speed (compression_level 1-7 as str, or an x264/x265 preset name --
    lower compression_level is slower but more size-efficient at the same
    quality target, confirmed by timing real encodes at levels 1/4/7, not
    assumed; libx264 and libx265 share the exact same ultrafast..placebo
    preset names, confirmed against this build, not assumed),
    bit_depth (8|10), width, height, container ("mp4"|"mkv", default mp4),
    tune (an x264/x265 tune name or "None", ignored for hevc_vaapi --
    libx264 and libx265 don't accept identical tune lists, see
    constants.X264_TUNES/X265_TUNES), deinterlace
    (bool, default False -- container-level progressive/interlaced flags
    are frequently wrong, especially on camcorder-sourced footage; this is
    a manual override, not auto-detected), audio_track,
    audio_copy_if_compatible, audio_bitrate, audio_downmix_stereo (bool,
    default False -- forces a stereo mixdown of the audio track, but only
    when the source genuinely has more than 2 channels; a stereo or mono
    source is left alone regardless of this flag, matching the control's
    own label ("if source has more channels"). Requesting it on a source
    that does have more channels also forces a transcode even when
    audio_copy_if_compatible would otherwise apply, the same way
    audio_copy_if_compatible=False does -- a stream copy can't remix.

    probe_audio=False skips the real ffprobe calls and uses audio_codec/
    audio_channels as given instead -- for building a representative
    command line to *show* the user (e.g. a live preview) against a file
    that may not exist yet, without shelling out on every keystroke. Real
    jobs always probe. audio_channels is only ever probed (or needed) when
    audio_downmix_stereo is actually set -- most jobs never touch it.

    duration_seconds, likewise, lets a caller that already has it (real
    jobs always probe duration anyway, for progress tracking) skip a
    redundant ffprobe call. Only used for a bitrate-family rc_mode -- a
    quality-family one never touches it, so it's never probed for the
    common case. None means "probe it if a bitrate-family mode needs it".

    audio_source_bitrate_kbps, same shape as duration_seconds -- a
    caller-supplied value skips the probe. Only used for a bitrate-family
    rc_mode whose audio will actually be *copied* (audio_copy_if_
    compatible, not force-downmixed, and already aac/ac3/eac3): that's
    the one case where the configured audio_bitrate is the wrong number
    to reserve for the target-size calculation below, since a copied
    track can genuinely be any bitrate regardless of what audio_bitrate
    is set to (a copy-compatible 640kbps AC-3 source under-reserved at a
    160kbps AAC assumption used to let the real output materially exceed
    the requested size -- a real, confirmed correctness gap, not
    hypothetical). None means "probe it if this specific case needs it";
    if the probe itself can't find a bitrate (some containers/codecs
    genuinely don't report one), this falls back to the configured
    audio_bitrate, same as before this parameter existed.

    Raises ValueError if a bitrate-family rc_mode's derived video bitrate
    would be zero or negative (the requested target size can't fit this
    file's length plus its reserved audio allocation) -- confirmed
    directly that silently passing that through as "-b:v 0k" doesn't
    error, it makes libx265 silently fall back to its own default CRF
    (28.0), producing an arbitrary-quality encode with no size relationship
    to what was actually requested at all. Callers (the real queue, the
    GUI preview) already catch exceptions from this function and surface
    them as a message -- raising here reuses that instead of needing a
    second reporting path.
    """
    encoder = settings["encoder"]
    is_vaapi = encoder == "hevc_vaapi"
    rc_mode = settings["rc_mode"]
    quality_value = settings["quality_value"]
    width, height = settings["width"], settings["height"]
    bit_depth = settings["bit_depth"]
    container = settings.get("container", "mp4")
    deinterlace = settings.get("deinterlace", False)
    audio_downmix_stereo = settings.get("audio_downmix_stereo", False)
    audio_track = settings["audio_track"]
    if probe_audio:
        audio_codec = probe_audio_codec(input_path, audio_track)
        if audio_codec is not None and audio_downmix_stereo:
            audio_channels = probe_audio_channels(input_path, audio_track)

    # Resolved once, up front, and reused both for the target-size
    # reservation below and the real -c:a decision further down -- must
    # stay the exact same condition in both places, or the reservation
    # and the actual encode could silently disagree about whether audio
    # is being copied or transcoded.
    #
    # Downmix only actually means something when the source has more
    # channels than the stereo it's being asked to become -- confirmed
    # this wasn't checked before: a stereo/mono source with the box
    # checked forced an unnecessary transcode (mono even got upmixed to
    # two channels, the opposite of what "downmix" means). audio_channels
    # is None whenever it was never probed (downmix not requested, so
    # never needed) or genuinely unknown -- either way, "unknown" must
    # not be treated as "assume it needs downmixing". A stream copy can't
    # remix channels either way -- an actual downmix forces a transcode
    # here too, the same way audio_copy_if_compatible=False does, so
    # checking "downmix to stereo" on a source that genuinely needs it
    # always actually produces stereo output instead of silently no-
    # op'ing whenever the source happens to already be copy-compatible.
    force_downmix = audio_downmix_stereo and audio_channels is not None and audio_channels > 2
    will_copy_audio = (
        audio_codec is not None
        and settings["audio_copy_if_compatible"]
        and not force_downmix
        and audio_codec in ("aac", "ac3", "eac3")
    )

    video_kbps = None
    if rc_mode in BITRATE_RC_MODES:
        if duration_seconds is None:
            duration_seconds = probe_duration(input_path)
        if audio_codec is None:
            reserved_audio_kbps = 0
        elif will_copy_audio:
            if probe_audio and audio_source_bitrate_kbps is None:
                audio_source_bitrate_kbps = probe_audio_bitrate_kbps(input_path, audio_track)
            reserved_audio_kbps = (
                audio_source_bitrate_kbps if audio_source_bitrate_kbps is not None
                else audio_bitrate_kbps(settings["audio_bitrate"])
            )
        else:
            reserved_audio_kbps = audio_bitrate_kbps(settings["audio_bitrate"])
        video_kbps = target_size_to_bitrate_kbps(quality_value, duration_seconds, reserved_audio_kbps)
        if video_kbps <= 0:
            raise ValueError(
                f"Target size ({quality_value} MB) is too small for this file's length "
                f"and audio settings -- the derived video bitrate would be zero or "
                f"negative. Increase the target size, lower the audio bitrate, or "
                f"switch to a Quality-based rate control mode instead."
            )

    args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"]

    if is_vaapi:
        gpu_vendor = settings.get("gpu_vendor", "intel")
        args += ["-vaapi_device", find_render_node(GPU_VENDOR_IDS[gpu_vendor])]
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
        # encoder itself ("libx265"/"libx264") IS the correct -c:v value --
        # confirmed directly, this app's own internal encoder id string is
        # already ffmpeg's own codec name for both, not just x265's.
        args += ["-vf", vf, "-pix_fmt", pix_fmt, "-c:v", encoder, "-preset", settings["speed"]]
        if rc_mode == "CRF":
            args += ["-crf", str(quality_value)]
        elif rc_mode == "bitrate":
            args += ["-b:v", f"{video_kbps}k"]
        tune = settings.get("tune", "None")
        if tune and tune != "None":
            args += ["-tune", tune]
        if encoder == "libx265":
            # x265-specific psycho-visual tuning (this app's own choice,
            # not a default ffmpeg/libx265 ships with) -- -x265-params is
            # meaningless to libx264 and would fail the whole job with an
            # "Unrecognized option" error if passed to it. No equivalent
            # added for libx264: unlike x265's historically conservative
            # defaults, libx264's own upstream defaults are already
            # well-regarded, and inventing a params string here without
            # the same kind of real justification x265's own has would
            # just be unjustified complexity.
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
        # force_downmix/will_copy_audio already resolved above (needed
        # early, for the target-size reservation) -- reused here as the
        # real -c:a decision itself, not recomputed, so the two can never
        # disagree about whether audio is being copied or transcoded.
        if will_copy_audio:
            args += ["-c:a", "copy"]
        else:
            args += ["-c:a", "aac", "-b:a", settings["audio_bitrate"]]
            if force_downmix:
                # Plain -ac 2 (libswresample's own remix), not an explicit
                # pan filter with hand-picked ITU-R BS.775 coefficients --
                # a fixed 5.1-shaped pan formula would mis-handle anything
                # that isn't exactly that layout (7.1, quad, whatever else
                # a real source might carry), where -ac 2 remixes correctly
                # from any input layout automatically.
                args += ["-ac", "2"]

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
    if is_vaapi:
        # A source with inconsistent color-range/matrix signaling across
        # its own GOPs (no container-level color metadata around to
        # override it, so ffmpeg has to infer this from the bitstream as
        # it decodes -- confirmed on a real ~2-hour file: ffprobe showed
        # color_range/space/transfer/primaries all "unknown" at the
        # container level) can trigger a mid-stream filter-graph
        # reconfiguration. When that happens, ffmpeg's default behavior
        # tries to auto-insert a software scale filter to bridge an
        # apparent format mismatch -- which can never actually work
        # against a vaapi hardware-surface pipeline, and crashes the whole
        # job instead ("Impossible to convert between the formats
        # supported by ... 'auto_scale_1'"). Reproduced consistently
        # against that real file at the exact same timestamp every time;
        # -noautoscale eliminates the auto-inserted filter, and the same
        # mid-stream reconfiguration then succeeds cleanly instead
        # (confirmed against the same file/timestamp: the "Reconfiguring
        # filter graph" log line still appears, encoding just continues
        # past it now). Harmless when the trigger never happens, the
        # normal case -- this only disables an auto-insert this app's own
        # explicit scale_vaapi already makes unnecessary anyway. x265 has
        # no vaapi surface in its pipeline at all, so it doesn't get this
        # flag -- nothing here to fix for it, and an untested behavior
        # change for a path that was never broken isn't worth the risk.
        args += ["-noautoscale"]
    args += ["-progress", "pipe:1", "-nostats"]
    args += [str(output_path)]
    return args
