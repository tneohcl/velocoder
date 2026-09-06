"""ffmpeg-driven transcode queue. Runs one job at a time via QProcess.

Takes a plain settings dict per job — no preset catalog here. Presets are a
GUI-side convenience for naming/saving/loading a settings snapshot; the
engine only ever sees the resolved values.
"""
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

INTEL_VENDOR_ID = "0x8086"
AMD_VENDOR_ID = "0x1002"
GPU_VENDOR_IDS = {"intel": INTEL_VENDOR_ID, "amd": AMD_VENDOR_ID}
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


@dataclass(frozen=True)
class ProcessingBackend:
    """One real, verified-present Processing choice. "Verified" here means
    only what find_render_node already means -- a DRM render node exists
    for that PCI vendor -- not a real validation encode; see this
    fork's hardware-detection proposal for the (currently deferred) real-
    encode validation stage."""
    id: str
    display_name: str


def detect_available_backends() -> list[ProcessingBackend]:
    """Every Processing choice actually usable on this machine, CPU first
    then whichever GPU vendors resolve a real render node -- CPU/Intel/AMD
    order matches the segmented row's own longstanding left-to-right
    layout (ui_builder.py), not a hardware-preference ranking. CPU is
    unconditional: the software encoder needs no device at all. The one
    place anything picks hardware *for* the user (best_available_engine,
    below) reuses this instead of re-probing on its own, so "what Automatic
    silently resolves to" and "what Processing even offers to pick
    manually" can never disagree."""
    backends = [ProcessingBackend("cpu", "CPU")]
    for vendor, display_name in (("intel", "Intel"), ("amd", "AMD")):
        try:
            find_render_node(GPU_VENDOR_IDS[vendor])
            backends.append(ProcessingBackend(vendor, display_name))
        except RuntimeError:
            continue
    return backends


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
        that mid-run today (main.py's add_files, gated on _queue_editable)."""
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
        try:
            self._duration = probe_duration(input_path)
            audio_codec = probe_audio_codec(input_path, job["audio_track"])
            audio_channels = None
            if audio_codec is not None and job.get("audio_downmix_stereo"):
                audio_channels = probe_audio_channels(input_path, job["audio_track"])
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
                audio_source_bitrate_kbps = probe_audio_bitrate_kbps(input_path, job["audio_track"])
            args = build_args(
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
            self.job_failed.emit(str(input_path), f"ffmpeg exited {exit_code}")
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
