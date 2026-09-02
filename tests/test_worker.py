"""Regression tests for worker.py (the ffmpeg command builder + queue engine).

Run with:  python3 -m unittest discover -s tests -v
(from the transcoder/ root, plain stdlib unittest -- no extra install needed)

Some tests actually invoke ffmpeg on tiny synthetic clips rather than mock
it -- the whole point of build_args() is producing a command line ffmpeg
accepts, and only real hardware/driver combinations can confirm the VAAPI
flags this app relies on (e.g. -rc_mode CQP needing -qp, not
-global_quality) are actually valid, not just plausible-looking.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import worker  # noqa: E402

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402

# TranscodeQueue tests drive a real QProcess, which needs a running Qt event
# loop -- QCoreApplication (no GUI needed here, unlike main.py's tests).
_app = QCoreApplication.instance() or QCoreApplication([])

HAS_VAAPI = Path("/dev/dri/by-path").exists()


def _has_vendor_render_node(vendor_id: str) -> bool:
    try:
        worker.find_render_node(vendor_id)
        return True
    except RuntimeError:
        return False


# Distinct from HAS_VAAPI (which only checks /dev/dri/by-path exists at all,
# true whenever *any* render node is present) -- this machine has both an
# Intel iGPU and a real discrete AMD GPU (confirmed via vainfo + real
# encodes, see constants.RC_MODES's comment), but a machine with only one
# or the other shouldn't spuriously run/skip the wrong vendor's tests.
HAS_AMD_VAAPI = _has_vendor_render_node(worker.AMD_VENDOR_ID)


def _run_queue_and_collect(queue: "worker.TranscodeQueue", jobs, output_dir, timeout_ms=15000):
    """Run a TranscodeQueue to completion, collecting every job_* signal
    emission as (event_name, args) tuples for direct assertion. Guards
    against a hung ffmpeg/ffprobe wedging the test suite forever."""
    events = []
    loop = QEventLoop()
    queue.job_started.connect(lambda *a: events.append(("job_started", a)))
    queue.job_finished.connect(lambda *a: events.append(("job_finished", a)))
    queue.job_failed.connect(lambda *a: events.append(("job_failed", a)))
    queue.all_finished.connect(loop.quit)

    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)

    queue.start(jobs, output_dir)
    loop.exec()
    return events

BASE_SETTINGS = {
    "width": 1280, "height": 720, "audio_track": 0,
    "audio_copy_if_compatible": True, "audio_bitrate": "160k",
}


def vaapi_settings(**overrides):
    settings = {**BASE_SETTINGS, "encoder": "hevc_vaapi", "rc_mode": "ICQ",
                "quality_value": 26, "speed": "1", "bit_depth": 10}
    settings.update(overrides)
    return settings


def x265_settings(**overrides):
    settings = {**BASE_SETTINGS, "encoder": "libx265", "rc_mode": "CRF",
                "quality_value": 23, "speed": "medium", "bit_depth": 10}
    settings.update(overrides)
    return settings


def _make_clip(path: Path, tracks=(("aac", 440),)):
    """1-second synthetic clip: 640x360 h264 video + the given audio tracks."""
    args = ["ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1"]
    for _, freq in tracks:
        args += ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration=1"]
    args += ["-map", "0:v"]
    for i in range(len(tracks)):
        args += ["-map", f"{i + 1}:a"]
    args += ["-c:v", "libx264"]
    for i, (codec, _) in enumerate(tracks):
        args += [f"-c:a:{i}", codec]
    args += ["-shortest", str(path)]
    subprocess.run(args, check=True, timeout=30)


class ClipTestCase(unittest.TestCase):
    """Base class providing a real tiny video file, built once per class."""

    tracks = (("aac", 440),)

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        cls.clip = cls.tmpdir / "clip.mkv"
        _make_clip(cls.clip, tracks=cls.tracks)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @property
    def out_path(self) -> Path:
        return self.tmpdir / "out.mp4"


class TestBuildArgsVaapi(ClipTestCase):
    def test_icq_uses_global_quality(self):
        args = worker.build_args(vaapi_settings(rc_mode="ICQ", quality_value=30), self.clip, self.out_path)
        self.assertIn("-global_quality", args)
        self.assertEqual(args[args.index("-global_quality") + 1], "30")
        self.assertNotIn("-qp", args)

    def test_cqp_uses_qp_not_global_quality(self):
        args = worker.build_args(vaapi_settings(rc_mode="CQP", quality_value=24), self.clip, self.out_path)
        self.assertIn("-qp", args)
        self.assertEqual(args[args.index("-qp") + 1], "24")
        self.assertNotIn("-global_quality", args)

    def test_vbr_uses_bitrate(self):
        # quality_value is a target output size in MB for a bitrate-family
        # rc_mode, converted to kbps using this file's duration -- pinned
        # explicitly here rather than relying on the real probed clip
        # length, so the expected number is exact: 100MB over 80s is
        # 8192*100/80 = 10240 kbps total, minus the 160k reserved for the
        # (real, aac) audio track it's copying = 10080.
        args = worker.build_args(
            vaapi_settings(rc_mode="VBR", quality_value=100),
            self.clip, self.out_path, duration_seconds=80,
        )
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "10080k")

    def test_10bit_uses_p010le_and_main10_profile(self):
        args = worker.build_args(vaapi_settings(bit_depth=10), self.clip, self.out_path)
        vf = args[args.index("-vf") + 1]
        self.assertIn("format=p010le", vf)
        self.assertEqual(args[args.index("-profile:v") + 1], "main10")

    def test_8bit_uses_nv12_and_main_profile(self):
        args = worker.build_args(vaapi_settings(bit_depth=8), self.clip, self.out_path)
        vf = args[args.index("-vf") + 1]
        self.assertIn("format=nv12", vf)
        self.assertEqual(args[args.index("-profile:v") + 1], "main")

    def test_never_sets_low_power(self):
        # HEVC on this hardware only exposes the non-low-power EncSlice
        # entrypoint (confirmed via vainfo) -- -low_power would just fail.
        for rc_mode in ("ICQ", "CQP", "VBR"):
            args = worker.build_args(vaapi_settings(rc_mode=rc_mode), self.clip, self.out_path)
            self.assertNotIn("-low_power", args)

    def test_uses_vaapi_device_and_hevc_vaapi_codec(self):
        args = worker.build_args(vaapi_settings(), self.clip, self.out_path)
        self.assertIn("-vaapi_device", args)
        self.assertEqual(args[args.index("-c:v") + 1], "hevc_vaapi")

    def test_sets_noautoscale(self):
        # A source with no container-level color metadata (confirmed on a
        # real ~2-hour file: ffprobe showed color_range/space/transfer/
        # primaries all "unknown") can signal a genuine mid-stream
        # parameter change as ffmpeg decodes it -- when that happens,
        # ffmpeg's default auto-inserted scale filter can't bridge a vaapi
        # hardware surface and crashes the whole job ("Impossible to
        # convert between the formats supported by ... 'auto_scale_1'").
        # Reproduced against that real file, at the exact same timestamp,
        # every time; -noautoscale eliminates the auto-insert and the same
        # reconfiguration then succeeds instead (confirmed against the
        # same file: the "Reconfiguring filter graph" log line still
        # appears, encoding just continues past it now).
        args = worker.build_args(vaapi_settings(), self.clip, self.out_path)
        self.assertIn("-noautoscale", args)


class TestBuildArgsX265(ClipTestCase):
    def test_crf_uses_crf_flag(self):
        args = worker.build_args(x265_settings(rc_mode="CRF", quality_value=20), self.clip, self.out_path)
        self.assertIn("-crf", args)
        self.assertEqual(args[args.index("-crf") + 1], "20")
        self.assertNotIn("-b:v", args)

    def test_bitrate_mode_uses_bv_not_crf(self):
        # Same size->kbps conversion as VBR above (see its comment): 100MB
        # over 80s minus 160k reserved audio = 10080k.
        args = worker.build_args(
            x265_settings(rc_mode="bitrate", quality_value=100),
            self.clip, self.out_path, duration_seconds=80,
        )
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "10080k")
        self.assertNotIn("-crf", args)

    def test_10bit_uses_yuv420p10le(self):
        args = worker.build_args(x265_settings(bit_depth=10), self.clip, self.out_path)
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p10le")

    def test_8bit_uses_yuv420p(self):
        args = worker.build_args(x265_settings(bit_depth=8), self.clip, self.out_path)
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")

    def test_speed_maps_to_preset_flag(self):
        args = worker.build_args(x265_settings(speed="veryslow"), self.clip, self.out_path)
        self.assertEqual(args[args.index("-preset") + 1], "veryslow")

    def test_no_vaapi_device_for_cpu_encoder(self):
        args = worker.build_args(x265_settings(), self.clip, self.out_path)
        self.assertNotIn("-vaapi_device", args)

    def test_no_noautoscale_for_cpu_encoder(self):
        # -noautoscale (see TestBuildArgsVaapi.test_sets_noautoscale) works
        # around a vaapi-hardware-surface-specific ffmpeg failure -- x265
        # has no such surface in its pipeline at all, so there's nothing
        # here for the flag to fix, and it shouldn't carry an untested
        # behavior change for a path that was never broken.
        args = worker.build_args(x265_settings(), self.clip, self.out_path)
        self.assertNotIn("-noautoscale", args)


class TestSizeToBitrate(unittest.TestCase):
    """Pure-function coverage for the size-target math build_args uses for
    VBR/bitrate rc_modes -- see TestBuildArgsVaapi.test_vbr_uses_bitrate and
    TestBuildArgsX265.test_bitrate_mode_uses_bv_not_crf for the same
    arithmetic exercised through the real build_args/ffmpeg path."""

    def test_basic_conversion(self):
        # 100MB over 80s = 8192*100/80 = 10240 total kbps, minus 160
        # reserved for audio = 10080.
        self.assertEqual(worker.target_size_to_bitrate_kbps(100, 80, 160), 10080)

    def test_zero_duration_returns_zero_not_a_crash(self):
        self.assertEqual(worker.target_size_to_bitrate_kbps(100, 0, 160), 0)

    def test_negative_duration_returns_zero(self):
        self.assertEqual(worker.target_size_to_bitrate_kbps(100, -5, 160), 0)

    def test_audio_alone_exceeding_target_clamps_to_zero_not_negative(self):
        # 1MB over 60s is only ~136 kbps total -- less than the 160
        # reserved for audio alone. A negative video bitrate would be
        # nonsensical (and likely reject at the ffmpeg level); 0 is at
        # least a legible "this target is too small" signal.
        self.assertEqual(worker.target_size_to_bitrate_kbps(1, 60, 160), 0)

    def test_no_audio_reserves_nothing(self):
        self.assertEqual(worker.target_size_to_bitrate_kbps(100, 80, 0), 10240)

    def test_audio_bitrate_kbps_parses_k_suffix(self):
        self.assertEqual(worker.audio_bitrate_kbps("160k"), 160)
        self.assertEqual(worker.audio_bitrate_kbps("96k"), 96)


class TestTargetSizeTooSmallRejected(unittest.TestCase):
    """target_size_to_bitrate_kbps returning 0 (see TestSizeToBitrate above)
    used to flow straight through into "-b:v 0k" -- confirmed directly
    against real ffmpeg/libx265 that this doesn't error, it makes x265
    silently fall back to its own default CRF (28.0), producing an
    arbitrary-quality encode with no actual relationship to the size the
    user asked for. build_args now raises instead of letting that happen;
    both real callers (TranscodeQueue._run_next, MainWindow's live preview)
    already catch exceptions from build_args and surface them as a
    message, so raising here reuses that rather than needing its own
    reporting path."""

    def test_raises_when_target_too_small_for_duration_and_audio(self):
        with self.assertRaises(ValueError):
            worker.build_args(
                x265_settings(rc_mode="bitrate", quality_value=0.001),
                Path("in.mkv"), Path("out.mp4"), duration_seconds=7200, probe_audio=False, audio_codec="aac",
            )

    def test_does_not_raise_for_a_reasonable_target(self):
        # Same shape as the too-small case above, just a target that's
        # actually big enough -- confirms this isn't rejecting every
        # bitrate-family request, only the ones that would derive to 0.
        args = worker.build_args(
            x265_settings(rc_mode="bitrate", quality_value=100),
            Path("in.mkv"), Path("out.mp4"), duration_seconds=80, probe_audio=False, audio_codec="aac",
        )
        self.assertIn("-b:v", args)


class TestBuildArgsCommon(ClipTestCase):
    def test_resolution_clamps_to_source_no_upscale(self):
        args = worker.build_args(vaapi_settings(width=99999, height=99999), self.clip, self.out_path)
        vf = args[args.index("-vf") + 1]
        self.assertIn("min(99999,iw)", vf)
        self.assertIn("force_original_aspect_ratio=decrease", vf)

    def test_drops_subtitle_and_data_streams(self):
        args = worker.build_args(vaapi_settings(), self.clip, self.out_path)
        self.assertIn("-sn", args)
        self.assertIn("-dn", args)

    def test_video_map_excludes_attached_pics(self):
        # Regression check: must be capital '0:V:0', not '0:v:0' -- lowercase
        # would also match embedded cover-art streams as "the video track".
        args = worker.build_args(vaapi_settings(), self.clip, self.out_path)
        self.assertIn("0:V:0", args)

    def test_uses_progress_pipe_for_gui_progress_bar(self):
        args = worker.build_args(vaapi_settings(), self.clip, self.out_path)
        self.assertIn("-progress", args)
        self.assertEqual(args[args.index("-progress") + 1], "pipe:1")

    def test_mp4_container_includes_faststart(self):
        args = worker.build_args(vaapi_settings(container="mp4"), self.clip, self.out_path)
        self.assertIn("-movflags", args)

    def test_mkv_container_omits_faststart(self):
        # movflags is a mov/mp4-muxer-private option -- ffmpeg silently
        # ignores it elsewhere, but it shouldn't appear in the mkv command
        # line at all (see worker.build_args' comment on this).
        args = worker.build_args(vaapi_settings(container="mkv"), self.clip, self.out_path)
        self.assertNotIn("-movflags", args)


class TestTune(ClipTestCase):
    def test_none_omits_tune_flag(self):
        args = worker.build_args(x265_settings(tune="None"), self.clip, self.out_path)
        self.assertNotIn("-tune", args)

    def test_value_adds_tune_flag(self):
        args = worker.build_args(x265_settings(tune="animation"), self.clip, self.out_path)
        self.assertIn("-tune", args)
        self.assertEqual(args[args.index("-tune") + 1], "animation")

    def test_ignored_for_vaapi_encoder(self):
        # -tune is an x265-only concept -- hevc_vaapi has no equivalent
        # option, so it must never appear even if the field is set.
        args = worker.build_args(vaapi_settings(tune="animation"), self.clip, self.out_path)
        self.assertNotIn("-tune", args)

    def test_film_is_deliberately_not_offered(self):
        # Confirmed against this exact libx265 build: "Error setting
        # preset/tune (null)/film." -- film is a real x265 tune name
        # elsewhere but invalid here, so constants.X265_TUNES must not
        # list it (regression check on the constants, not just build_args).
        from constants import X265_TUNES
        self.assertNotIn("film", X265_TUNES)


class TestAudioSelection(ClipTestCase):
    tracks = (("aac", 440), ("ac3", 880))

    def test_copies_compatible_codec(self):
        args = worker.build_args(
            vaapi_settings(audio_track=0, audio_copy_if_compatible=True), self.clip, self.out_path
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")

    def test_selects_requested_track_index(self):
        args = worker.build_args(
            vaapi_settings(audio_track=1, audio_copy_if_compatible=True), self.clip, self.out_path
        )
        self.assertIn("0:a:1", args)

    def test_forces_transcode_when_copy_disabled(self):
        # Track 1 is ac3, which IS normally copy-compatible -- confirm the
        # "copy if compatible" checkbox actually overrides it when off,
        # rather than being silently ignored.
        args = worker.build_args(
            vaapi_settings(audio_track=1, audio_copy_if_compatible=False), self.clip, self.out_path
        )
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertIn("-b:a", args)

    # A genuinely-incompatible source codec (e.g. mp3) needs a real ffprobe
    # read to detect, so that path is covered by the integration test below
    # rather than duplicated here with a fixture this class can't produce.


class TestAudioDownmix(unittest.TestCase):
    """audio_downmix_stereo -- probe_audio=False throughout except the one
    real-encode check at the bottom, same split as TestCommandPreview vs.
    the ClipTestCase-based classes above: these don't need a real file to
    confirm which flags build_args emits, only to confirm ffmpeg actually
    accepts -ac 2 and the resulting file really is 2-channel.

    audio_channels=6 throughout the "downmix should actually happen" cases
    below -- confirmed directly this control used to force -ac 2 (and an
    unnecessary transcode) regardless of the source's real channel count,
    so a plain "downmix requested" is deliberately not enough on its own
    here any more; the test_no_effect_on_* cases below cover the
    channels<=2 (and unknown) case these used to get wrong.
    """

    def test_adds_ac_2_when_transcoding(self):
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=True, audio_copy_if_compatible=False),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="mp3", audio_channels=6,
        )
        self.assertEqual(args[args.index("-ac") + 1], "2")

    def test_omits_ac_flag_when_downmix_is_off(self):
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=False, audio_copy_if_compatible=False),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="mp3", audio_channels=6,
        )
        self.assertNotIn("-ac", args)

    def test_forces_transcode_even_when_copy_would_otherwise_apply(self):
        # aac is normally copy-compatible with audio_copy_if_compatible=True
        # -- a stream copy can't remix channels, so requesting downmix on a
        # source that genuinely has more than 2 channels must override
        # that, the same way audio_copy_if_compatible=False does in
        # TestAudioSelection above. Silently keeping -c:a copy here would
        # mean checking "downmix to stereo" just quietly does nothing.
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=True, audio_copy_if_compatible=True),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="aac", audio_channels=6,
        )
        self.assertEqual(args[args.index("-c:a") + 1], "aac")
        self.assertEqual(args[args.index("-ac") + 1], "2")

    def test_default_is_off_and_does_not_affect_a_normal_copy(self):
        args = worker.build_args(
            vaapi_settings(audio_copy_if_compatible=True), Path("in.mkv"), Path("out.mp4"),
            probe_audio=False, audio_codec="aac", audio_channels=6,
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-ac", args)

    def test_no_effect_on_a_source_that_is_already_stereo(self):
        # The label says "if source has more channels" -- confirmed
        # directly this wasn't actually checked before: a stereo (or mono)
        # source with the box checked still forced an unnecessary
        # transcode, and a mono source would have been *upmixed* to two
        # channels, the opposite of what "downmix" means.
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=True, audio_copy_if_compatible=True),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="aac", audio_channels=2,
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-ac", args)

    def test_no_effect_on_a_mono_source(self):
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=True, audio_copy_if_compatible=True),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="aac", audio_channels=1,
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-ac", args)

    def test_unknown_channel_count_is_treated_as_no_effect_not_assumed_needed(self):
        # audio_channels=None (never probed, or genuinely unreported) must
        # not be read as "assume it needs downmixing" -- the safe default
        # is "don't force it" when it isn't actually known to be warranted.
        args = worker.build_args(
            vaapi_settings(audio_downmix_stereo=True, audio_copy_if_compatible=True),
            Path("in.mkv"), Path("out.mp4"), probe_audio=False, audio_codec="aac", audio_channels=None,
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")
        self.assertNotIn("-ac", args)

    def test_real_encode_actually_produces_two_channels(self):
        # The flag-presence checks above confirm build_args' own logic, not
        # that ffmpeg actually honors -ac 2 the way expected -- a genuine
        # 6-channel source, actually encoded, actually reprobed.
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip = tmpdir / "surround.mkv"
            subprocess.run([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                "-map", "0:v", "-map", "1:a",
                "-c:v", "libx264", "-c:a", "aac", "-ac", "6",
                "-shortest", str(clip),
            ], check=True, timeout=30)
            probe_in = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=channels", "-of", "csv=p=0", str(clip)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(probe_in.stdout.strip(), "6")  # confirm the fixture itself is really 6ch

            out = tmpdir / "downmixed.mp4"
            args = worker.build_args(x265_settings(audio_downmix_stereo=True), clip, out)
            result = subprocess.run(args, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            probe_out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=channels", "-of", "csv=p=0", str(out)],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(probe_out.stdout.strip(), "2")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestCommandPreview(unittest.TestCase):
    """build_args(probe_audio=False) -- the GUI's live command preview path.

    No real file needed: this mode exists specifically to avoid touching
    the filesystem, so these don't use ClipTestCase.
    """

    def test_skips_probe_and_uses_given_audio_codec(self):
        args = worker.build_args(
            vaapi_settings(), Path("input.ext"), Path("output.mp4"),
            probe_audio=False, audio_codec="aac",
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")  # aac is copy-compatible

    def test_none_audio_codec_omits_audio_flags(self):
        args = worker.build_args(
            vaapi_settings(), Path("input.ext"), Path("output.mp4"),
            probe_audio=False, audio_codec=None,
        )
        self.assertNotIn("-c:a", args)
        # Regression: -map 0:a:N used to be added unconditionally even when
        # audio_codec is None (the requested track doesn't exist), so ffmpeg
        # would fail on a stream map to nothing rather than just proceeding
        # without audio.
        self.assertNotIn("0:a:0", args)

    def test_never_touches_the_filesystem(self):
        # A path that can't possibly exist -- if this tried to probe it,
        # ffprobe would fail/hang on a subprocess call against a bogus path.
        args = worker.build_args(
            vaapi_settings(), Path("/nonexistent/does-not-exist.mkv"), Path("/nonexistent/out.mp4"),
            probe_audio=False, audio_codec="ac3",
        )
        self.assertEqual(args[args.index("-c:a") + 1], "copy")


class TestIdetHelpers(unittest.TestCase):
    """Pure-logic tests for the auto-detect building blocks -- the real
    end-to-end behavior (does detection actually flip the checkbox) is
    covered in test_main.py, since it's GUI-driven async wiring."""

    def test_build_idet_args_caps_sample_duration(self):
        args = worker.build_idet_args(Path("in.mkv"), sample_seconds=15)
        self.assertIn("-t", args)
        self.assertEqual(args[args.index("-t") + 1], "15")
        self.assertIn("idet", args[args.index("-vf") + 1])

    def test_parse_idet_output_all_interlaced(self):
        stderr = "[Parsed_idet_0] Multi frame detection: TFF:   100 BFF:     0 Progressive:     0 Undetermined:     0\n"
        self.assertEqual(worker.parse_idet_output(stderr), 1.0)

    def test_parse_idet_output_all_progressive(self):
        stderr = "[Parsed_idet_0] Multi frame detection: TFF:     0 BFF:     0 Progressive:   100 Undetermined:     0\n"
        self.assertEqual(worker.parse_idet_output(stderr), 0.0)

    def test_parse_idet_output_mixed(self):
        stderr = "[Parsed_idet_0] Multi frame detection: TFF:    30 BFF:    20 Progressive:    50 Undetermined:     0\n"
        self.assertEqual(worker.parse_idet_output(stderr), 0.5)

    def test_parse_idet_output_uses_last_line_not_first(self):
        # idet logs one line per filter instance in the graph; the final one
        # is the real per-stream summary (matches how the real ffmpeg -vf
        # idet output looks -- see the docstring on _detect_interlace_fraction
        # above for why this matters).
        stderr = (
            "Multi frame detection: TFF:     0 BFF:     0 Progressive:     0 Undetermined:     0\n"
            "Multi frame detection: TFF:   100 BFF:     0 Progressive:     0 Undetermined:     0\n"
        )
        self.assertEqual(worker.parse_idet_output(stderr), 1.0)

    def test_parse_idet_output_no_stats_returns_zero(self):
        self.assertEqual(worker.parse_idet_output("ffmpeg: command not found\n"), 0.0)


class TestSourceProbeHelpers(unittest.TestCase):
    """Pure-logic tests for the queue table's source-metadata probe -- the
    real end-to-end behavior (does a queued row's columns actually fill in)
    is covered in test_main.py, since it's GUI-driven async wiring. Same
    split as TestIdetHelpers above."""

    def test_build_probe_args_is_header_only(self):
        args = worker.build_probe_args(Path("in.mkv"))
        self.assertNotIn("-vf", args)  # no decoding -- contrast build_idet_args
        self.assertIn("-show_entries", args)
        self.assertIn("in.mkv", args)

    def test_parse_probe_output_full_fixture(self):
        # ffprobe's real -of json output reports numeric fields (duration,
        # here) as JSON strings, not numbers -- matched deliberately.
        stdout = json.dumps({
            "format": {"duration": "125.5"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 1920,
                 "height": 1080, "r_frame_rate": "24000/1001"},
                {"codec_type": "audio", "codec_name": "aac", "channels": 6},
                {"codec_type": "audio", "codec_name": "ac3", "channels": 2},
            ],
        })
        info = worker.parse_probe_output(stdout)
        self.assertEqual(info["duration"], 125.5)
        self.assertEqual(info["video_codec"], "hevc")
        self.assertEqual(info["width"], 1920)
        self.assertEqual(info["height"], 1080)
        self.assertAlmostEqual(info["frame_rate"], 23.976, places=2)
        self.assertEqual(info["audio_codec"], "aac")  # first audio stream, not the last
        self.assertEqual(info["audio_channels"], 6)
        self.assertEqual(info["audio_track_count"], 2)

    def test_parse_probe_output_no_video_stream(self):
        stdout = json.dumps({
            "format": {"duration": "10"},
            "streams": [{"codec_type": "audio", "codec_name": "mp3", "channels": 2}],
        })
        info = worker.parse_probe_output(stdout)
        self.assertNotIn("video_codec", info)
        self.assertEqual(info["audio_codec"], "mp3")

    def test_parse_probe_output_no_audio_stream(self):
        stdout = json.dumps({
            "format": {"duration": "10"},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": 640,
                         "height": 480, "r_frame_rate": "30/1"}],
        })
        info = worker.parse_probe_output(stdout)
        self.assertNotIn("audio_codec", info)
        self.assertEqual(info["video_codec"], "h264")

    def test_parse_probe_output_malformed_json_returns_empty(self):
        self.assertEqual(worker.parse_probe_output("not json"), {})

    def test_parse_probe_output_empty_string_returns_empty(self):
        self.assertEqual(worker.parse_probe_output(""), {})

    def test_frame_rate_zero_denominator_is_zero_not_a_crash(self):
        # A still-image "stream" some containers report alongside the real
        # video track -- r_frame_rate of "0/0" must not raise ZeroDivisionError.
        stdout = json.dumps({
            "format": {},
            "streams": [{"codec_type": "video", "codec_name": "mjpeg", "width": 100,
                         "height": 100, "r_frame_rate": "0/0"}],
        })
        info = worker.parse_probe_output(stdout)
        self.assertEqual(info["frame_rate"], 0.0)


class TestNonMatchingAspectRatio(unittest.TestCase):
    """A source whose aspect ratio doesn't exactly match the target box must
    still produce even dimensions -- 4:2:0 formats reject odd ones outright."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        # 1920x800 (2.4:1 "scope" ratio) scaled to fit a 1280x720 box: the
        # binding dimension (width, ratio 0.6667) leaves height at
        # 800*0.6667 = 533.33 -- 533 is odd, 534 is the correct even round.
        cls.clip = cls.tmpdir / "scope.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=1920x800:rate=25:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-c:a", "aac", "-shortest", str(cls.clip)],
            check=True, timeout=30,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_x265_encode_succeeds_with_even_dimensions(self):
        out = self.tmpdir / "scope_x265.mp4"
        args = worker.build_args(x265_settings(width=1280, height=720), self.clip, out)
        self.assertIn("force_divisible_by=2", args[args.index("-vf") + 1])
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-select_streams", "v", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        width, height = (int(x) for x in probe.stdout.strip().split(","))
        self.assertEqual(height % 2, 0)
        self.assertEqual((width, height), (1280, 534))

    @unittest.skipUnless(HAS_VAAPI, "no VAAPI render node on this machine")
    def test_vaapi_encode_succeeds_with_even_dimensions(self):
        out = self.tmpdir / "scope_vaapi.mp4"
        args = worker.build_args(vaapi_settings(width=1280, height=720), self.clip, out)
        self.assertIn("force_divisible_by=2", args[args.index("-vf") + 1])
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-select_streams", "v", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        width, height = (int(x) for x in probe.stdout.strip().split(","))
        self.assertEqual(height % 2, 0)


def _detect_interlace_fraction(path: Path, sample_seconds: float = 9999) -> float:
    """Fraction of frames ffmpeg's idet filter classifies as interlaced
    (TFF+BFF) rather than progressive. The real ground-truth check used
    throughout this suite -- container-level progressive/interlaced flags
    are frequently wrong (this feature exists because of exactly that, on a
    real user file: tagged yuv420p(progressive), 100% TFF by idet).

    Reuses worker.parse_idet_output for the parsing itself -- this is a test
    helper for checking *outputs of an encode*, not the same job as
    worker.build_idet_args/parse_idet_output (which drive the GUI's
    pre-encode auto-detect), but the underlying idet-output parsing is
    identical and shouldn't be maintained in two places.
    """
    result = subprocess.run(
        worker.build_idet_args(path, sample_seconds=sample_seconds),
        capture_output=True, text=True, timeout=30,
    )
    return worker.parse_idet_output(result.stderr)


class TestDeinterlace(unittest.TestCase):
    """settings["deinterlace"] must actually remove interlacing, not just
    add a plausible-looking flag -- verified with idet, not assumed."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        # 60fps progressive source with real per-frame motion (needed for
        # fields to actually differ), field-interleaved down to a genuinely
        # interlaced 30fps source -- not a container flag, real combing.
        cls.clip = cls.tmpdir / "interlaced.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=60:duration=1",
             "-vf", "tinterlace=interleave_top", "-c:v", "libx264", str(cls.clip)],
            check=True, timeout=30,
        )
        # Confirm the fixture itself is actually interlaced before trusting
        # any test that relies on it -- if this ever fails, the fixture
        # generation broke, not the deinterlace feature.
        assert _detect_interlace_fraction(cls.clip) == 1.0, "test fixture isn't interlaced"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_off_by_default_and_leaves_filter_chain_unchanged(self):
        args = worker.build_args(x265_settings(deinterlace=False), self.clip, self.tmpdir / "out.mp4")
        self.assertNotIn("bwdif", args[args.index("-vf") + 1])

    def test_x265_deinterlace_uses_send_frame_not_the_frame_doubling_default(self):
        args = worker.build_args(x265_settings(deinterlace=True), self.clip, self.tmpdir / "out.mp4")
        vf = args[args.index("-vf") + 1]
        # bwdif's own default (send_field) doubles the frame rate -- one
        # output frame per field. That's not what this checkbox promises.
        self.assertIn("bwdif=mode=send_frame", vf)
        self.assertLess(vf.index("bwdif"), vf.index("scale"), "must deinterlace before scaling, not after")

    def test_x265_deinterlace_actually_fixes_a_real_interlaced_file(self):
        out = self.tmpdir / "x265_deint.mp4"
        args = worker.build_args(x265_settings(width=640, height=480, deinterlace=True), self.clip, out)
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        # Source is 100% interlaced (asserted in setUpClass); real deinterlacers
        # aren't perfect at clip boundaries, so allow a small residual rather
        # than demanding exactly 0% -- the fix under test cut this from 100%.
        self.assertLess(_detect_interlace_fraction(out), 0.15)

    def test_without_deinterlace_the_interlacing_survives_the_encode(self):
        # Negative control: without the flag, re-encoding alone does NOT
        # remove interlacing -- proves the improvement above comes from
        # bwdif, not incidentally from libx265's own encoding.
        out = self.tmpdir / "x265_no_deint.mp4"
        args = worker.build_args(x265_settings(width=640, height=480, deinterlace=False), self.clip, out)
        subprocess.run(args, check=True, capture_output=True, timeout=30)
        self.assertGreater(_detect_interlace_fraction(out), 0.8)

    @unittest.skipUnless(HAS_VAAPI, "no VAAPI render node on this machine")
    def test_vaapi_deinterlace_runs_after_hwupload_before_scale(self):
        args = worker.build_args(vaapi_settings(deinterlace=True), self.clip, self.tmpdir / "out.mp4")
        vf = args[args.index("-vf") + 1]
        self.assertIn("deinterlace_vaapi=rate=frame", vf)
        self.assertLess(vf.index("hwupload"), vf.index("deinterlace_vaapi"))
        self.assertLess(vf.index("deinterlace_vaapi"), vf.index("scale_vaapi"))

    @unittest.skipUnless(HAS_VAAPI, "no VAAPI render node on this machine")
    def test_vaapi_deinterlace_actually_fixes_a_real_interlaced_file(self):
        out = self.tmpdir / "vaapi_deint.mp4"
        args = worker.build_args(vaapi_settings(width=640, height=480, deinterlace=True), self.clip, out)
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertLess(_detect_interlace_fraction(out), 0.15)


class TestOutputPathCollisionGuard(unittest.TestCase):
    def test_refuses_when_output_would_equal_input(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip = tmpdir / "same.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-c:v", "libx264", str(clip)],
                check=True, timeout=30,
            )
            job = {"path": clip, **x265_settings(container="mp4")}
            queue = worker.TranscodeQueue()
            events = _run_queue_and_collect(queue, [job], tmpdir)

            self.assertTrue(clip.exists(), "source file must survive")
            failed = [e for e in events if e[0] == "job_failed"]
            self.assertEqual(len(failed), 1)
            self.assertIn("same as the input", failed[0][1][1])
            self.assertEqual([e[0] for e in events if e[0] != "job_started"], ["job_failed"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_disambiguates_two_jobs_with_the_same_stem(self):
        # The actual reported scenario: folderA/shot01.mov and
        # folderB/shot01.mkv both want to become shot01.mp4 -- confirmed
        # directly this used to mean the second job's -y silently
        # overwrote the first job's completed output, with nothing to
        # indicate it had happened.
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            folder_a, folder_b = tmpdir / "folderA", tmpdir / "folderB"
            folder_a.mkdir()
            folder_b.mkdir()
            clip_a, clip_b = folder_a / "shot01.mov", folder_b / "shot01.mkv"
            for clip in (clip_a, clip_b):
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                     "-c:v", "libx264", str(clip)],
                    check=True, timeout=30,
                )
            out_dir = tmpdir / "out"
            out_dir.mkdir()
            jobs = [{"path": clip_a, **x265_settings(container="mp4")},
                    {"path": clip_b, **x265_settings(container="mp4")}]
            queue = worker.TranscodeQueue()
            events = _run_queue_and_collect(queue, jobs, out_dir)

            finished = sorted(e[1][1] for e in events if e[0] == "job_finished")
            self.assertEqual(len(finished), 2, events)
            self.assertNotEqual(finished[0], finished[1])
            self.assertTrue(all(Path(p).exists() for p in finished))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestAtomicOutputRename(unittest.TestCase):
    """ffmpeg now writes to a hidden temp name during the encode and this
    only ever gets renamed onto the real output name after a confirmed
    success -- confirmed directly (see TestOutputPathCollisionGuard's
    sibling class above and the class docstring reasoning) this used to
    mean an existing file at the final path (a previous run's completed
    output, or another job's -- see the disambiguation test above) was
    truncated by -y the instant a colliding job started, and destroyed
    outright if that job then failed or got stopped."""

    def test_no_temp_file_left_behind_on_success(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip = tmpdir / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-c:v", "libx264", str(clip)],
                check=True, timeout=30,
            )
            out_dir = tmpdir / "out"
            out_dir.mkdir()
            queue = worker.TranscodeQueue()
            _run_queue_and_collect(queue, [{"path": clip, **x265_settings(container="mp4")}], out_dir)

            names = [p.name for p in out_dir.iterdir()]
            self.assertEqual(names, ["clip.mp4"], "only the final name should remain, no .*.transcoding.* left over")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_a_failed_job_does_not_touch_a_pre_existing_file_at_the_final_path(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            # A source ffmpeg can't actually decode -- the job will fail,
            # but only after build_args/probe_duration succeed against it
            # (a real, if broken, file) and a real ffmpeg process starts
            # and then exits non-zero, exercising the same failure path
            # _on_finished's "else" branch takes for a genuine encode error.
            clip = tmpdir / "broken.mp4"
            clip.write_bytes(b"not actually a video file")
            out_dir = tmpdir / "out"
            out_dir.mkdir()
            preexisting = out_dir / "broken.mp4"
            preexisting.write_bytes(b"a completed output from a previous, unrelated run")

            queue = worker.TranscodeQueue()
            events = _run_queue_and_collect(queue, [{"path": clip, **x265_settings(container="mp4")}], out_dir)

            self.assertTrue(any(e[0] == "job_failed" for e in events), events)
            self.assertEqual(
                preexisting.read_bytes(), b"a completed output from a previous, unrelated run",
                "a failed job must never touch a file that was already at its output path",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestProcessFailedToStart(unittest.TestCase):
    """QProcess.finished never fires when the process fails to even start
    -- confirmed directly against a real nonexistent binary that only
    errorOccurred does. Before this was connected, a missing/broken ffmpeg
    install left the queue stuck on that job forever: no job_failed, no
    all_finished, nothing to click, nothing in the log."""

    def test_missing_ffmpeg_fails_the_job_and_continues_the_queue(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        fakebin = Path(tempfile.mkdtemp(prefix="transcoder_test_fakebin_"))
        try:
            # A real ffprobe (via a symlink), but no ffmpeg anywhere on
            # PATH -- isolates the QProcess-level failure specifically,
            # rather than an earlier probe_duration/probe_audio_codec
            # FileNotFoundError, which is a different, already-handled path.
            real_ffprobe = shutil.which("ffprobe")
            (fakebin / "ffprobe").symlink_to(real_ffprobe)

            clip = tmpdir / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-c:v", "libx264", str(clip)],
                check=True, timeout=30,
            )
            jobs = [{"path": clip, **x265_settings(container="mp4")},
                    {"path": clip, **x265_settings(container="mkv")}]
            out_dir = tmpdir / "out"
            out_dir.mkdir()

            # Not the shared _run_queue_and_collect helper -- it wires
            # all_finished straight to loop.quit() without also recording
            # it into events, which is exactly the one signal this test
            # actually needs to see fire (a stuck queue and a correctly-
            # finished one both look identical in that helper's own events
            # list; only the *timing* would differ, which this makes an
            # explicit, direct assertion on instead of an inferred one).
            old_path = os.environ.get("PATH", "")
            os.environ["PATH"] = str(fakebin)
            try:
                queue = worker.TranscodeQueue()
                events = []
                loop = QEventLoop()
                queue.job_failed.connect(lambda *a: events.append(("job_failed", a)))
                queue.all_finished.connect(lambda: events.append(("all_finished",)))
                queue.all_finished.connect(loop.quit)
                timer = QTimer()
                timer.setSingleShot(True)
                timer.timeout.connect(loop.quit)
                timer.start(10000)
                queue.start(jobs, out_dir)
                loop.exec()
            finally:
                os.environ["PATH"] = old_path

            failed = [e for e in events if e[0] == "job_failed"]
            self.assertEqual(len(failed), 2, events)
            self.assertTrue(all("failed to start" in e[1][1] for e in failed))
            self.assertTrue(
                any(e[0] == "all_finished" for e in events),
                "queue never reached all_finished -- it got stuck instead of "
                "progressing past the FailedToStart job(s)",
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
            shutil.rmtree(fakebin, ignore_errors=True)


class TestMissingAudioTrackWarning(unittest.TestCase):
    """Requesting an audio track that doesn't exist on the source (e.g.
    Track 4 on a file with only one) used to just silently produce
    video-only output -- build_args itself already handled this correctly
    (skips mapping a stream that isn't there rather than failing the whole
    job), but nothing told the user their output would have no audio at
    all until they noticed on playback."""

    def test_logs_a_note_when_the_requested_track_does_not_exist(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip = tmpdir / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-c:a", "aac",
                 "-shortest", str(clip)],
                check=True, timeout=30,
            )
            out_dir = tmpdir / "out"
            out_dir.mkdir()
            job = {"path": clip, **x265_settings(audio_track=3, container="mp4")}

            # Not the shared _run_queue_and_collect helper -- it doesn't
            # connect job_log at all (most callers never need per-line
            # ffmpeg output), and this test specifically needs to see it.
            queue = worker.TranscodeQueue()
            log_lines = []
            finished = []
            loop = QEventLoop()
            queue.job_log.connect(log_lines.append)
            queue.job_finished.connect(lambda *a: finished.append(a))
            queue.all_finished.connect(loop.quit)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(15000)
            queue.start([job], out_dir)
            loop.exec()

            self.assertTrue(
                any("audio track 3" in line and "not found" in line for line in log_lines),
                log_lines,
            )
            self.assertTrue(finished, "job should still succeed, just without audio")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestStopRaceFix(unittest.TestCase):
    """_on_finished must not delete a job that actually completed
    successfully, even if stop() was also called (finished-signal delivery
    races the user's click).

    _on_finished's own signature changed shape (temp_output_path,
    final_output_path, not a single output_path) when the atomic-rename
    fix landed -- see TestAtomicOutputRename -- these two now exercise
    that same real temp-file/rename mechanics directly instead of a
    single pre-existing output file, matching what _run_next actually
    hands it.
    """

    def test_successful_exit_wins_over_stopped_flag(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            temp_output = tmpdir / ".done.transcoding.mp4"
            final_output = tmpdir / "done.mp4"
            temp_output.write_bytes(b"pretend this is a completed encode")
            queue = worker.TranscodeQueue()
            queue._jobs = []  # nothing queued after this one
            queue._stopped = True  # simulate: Stop was clicked
            events = []
            queue.job_finished.connect(lambda *a: events.append(("finished", a)))
            queue.job_failed.connect(lambda *a: events.append(("failed", a)))

            # exit_code=0: genuinely succeeded
            queue._on_finished(Path("in.mkv"), temp_output, final_output, 0, None)

            self.assertTrue(final_output.exists(), "a successfully completed file must not be deleted")
            self.assertFalse(temp_output.exists(), "must be renamed onto the final name, not left behind")
            self.assertEqual([e[0] for e in events], ["finished"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_genuine_stop_of_an_incomplete_job_still_cleans_up(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            temp_output = tmpdir / ".partial.transcoding.mp4"
            final_output = tmpdir / "partial.mp4"
            temp_output.write_bytes(b"partial data from a killed ffmpeg")
            queue = worker.TranscodeQueue()
            queue._jobs = []
            queue._stopped = True
            events = []
            queue.job_failed.connect(lambda *a: events.append(("failed", a)))

            # nonzero: actually interrupted
            queue._on_finished(Path("in.mkv"), temp_output, final_output, 1, None)

            self.assertFalse(temp_output.exists(), "a genuinely-stopped job's partial temp file must be removed")
            self.assertFalse(final_output.exists(), "must never have been created at all")
            self.assertEqual([e[0] for e in events], ["failed"])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestProbeDurationGuarded(unittest.TestCase):
    def test_probe_duration_failure_emits_job_failed_not_crash(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip = tmpdir / "clip.mkv"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
                 "-c:v", "libx264", str(clip)],
                check=True, timeout=30,
            )
            job = {"path": clip, **x265_settings()}
            queue = worker.TranscodeQueue()
            with patch("worker.probe_duration", side_effect=FileNotFoundError("ffprobe not found")):
                events = _run_queue_and_collect(queue, [job], tmpdir)
            failed = [e for e in events if e[0] == "job_failed"]
            self.assertEqual(len(failed), 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestFindRenderNode(unittest.TestCase):
    @unittest.skipUnless(HAS_VAAPI, "no /dev/dri/by-path on this machine")
    def test_resolves_intel_node_when_present(self):
        node = worker.find_render_node(worker.INTEL_VENDOR_ID)
        self.assertTrue(Path(node).exists())

    @unittest.skipUnless(HAS_VAAPI, "no /dev/dri/by-path on this machine")
    def test_unknown_vendor_raises(self):
        with self.assertRaises(RuntimeError):
            worker.find_render_node("0xdead")

    @unittest.skipUnless(HAS_AMD_VAAPI, "no AMD render node on this machine")
    def test_resolves_amd_node_when_present(self):
        node = worker.find_render_node(worker.AMD_VENDOR_ID)
        self.assertTrue(Path(node).exists())

    @unittest.skipUnless(HAS_VAAPI and HAS_AMD_VAAPI, "needs both vendors present")
    def test_intel_and_amd_resolve_to_different_nodes(self):
        self.assertNotEqual(
            worker.find_render_node(worker.INTEL_VENDOR_ID),
            worker.find_render_node(worker.AMD_VENDOR_ID),
        )


class TestGpuVendorSelection(ClipTestCase):
    """gpu_vendor picks which GPU's render node build_args opens -- separate
    from HAS_AMD_VAAPI-gated tests below since these only check which flag
    value gets used, not that the chosen device actually works."""

    def test_defaults_to_intel_when_absent(self):
        # Presets/queue jobs saved before gpu_vendor existed have no such
        # key -- must still resolve to the same device Intel-only builds
        # of this app always used, not raise a KeyError.
        settings = vaapi_settings()
        self.assertNotIn("gpu_vendor", settings)
        with patch.object(worker, "find_render_node") as mock_find:
            mock_find.return_value = "/dev/dri/renderD999"
            worker.build_args(settings, self.clip, self.out_path)
        mock_find.assert_called_once_with(worker.INTEL_VENDOR_ID)

    def test_amd_vendor_opens_the_amd_device(self):
        settings = vaapi_settings(gpu_vendor="amd", rc_mode="CQP")
        with patch.object(worker, "find_render_node") as mock_find:
            mock_find.return_value = "/dev/dri/renderD999"
            worker.build_args(settings, self.clip, self.out_path)
        mock_find.assert_called_once_with(worker.AMD_VENDOR_ID)


class TestIntegrationRealEncode(unittest.TestCase):
    """Actually run ffmpeg end to end -- the part a pure arg-list check can't catch."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        cls.clip = cls.tmpdir / "clip.mkv"
        _make_clip(cls.clip, tracks=(("mp3", 440),))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    @unittest.skipUnless(HAS_VAAPI, "no VAAPI render node on this machine")
    def test_vaapi_encode_runs_and_produces_correct_output(self):
        # Source clip is 640x360 (16:9). A 320x320 box is wider than tall,
        # so width is the binding constraint -- expect exactly 320x180,
        # not an upscale and not a naive same-aspect assumption.
        out = self.tmpdir / "vaapi_out.mp4"
        args = worker.build_args(vaapi_settings(width=320, height=320), self.clip, out)
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        self.assertIn("hevc", probe.stdout)
        self.assertIn("320,180", probe.stdout.replace("\n", ","))

    @unittest.skipUnless(HAS_AMD_VAAPI, "no AMD render node on this machine")
    def test_amd_vaapi_encode_runs_and_produces_correct_output(self):
        # CQP, not ICQ -- confirmed by actually running it that this AMD
        # driver rejects ICQ outright ("Driver does not support ICQ RC
        # mode"), unlike Intel's iHD driver which is what the rest of this
        # suite's VAAPI tests exercise.
        out = self.tmpdir / "amd_vaapi_out.mp4"
        args = worker.build_args(
            vaapi_settings(gpu_vendor="amd", rc_mode="CQP", width=320, height=320),
            self.clip, out,
        )
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        self.assertIn("hevc", probe.stdout)
        self.assertIn("320,180", probe.stdout.replace("\n", ","))

    def test_x265_encode_runs_and_transcodes_incompatible_audio(self):
        out = self.tmpdir / "x265_out.mp4"
        args = worker.build_args(x265_settings(width=320, height=240), self.clip, out)
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", str(out)],
            capture_output=True, text=True,
        )
        # Source track is mp3, which is never copy-compatible -- must have
        # been transcoded to aac, not passed through untouched.
        self.assertEqual(probe.stdout.strip(), "aac")


if __name__ == "__main__":
    unittest.main()
