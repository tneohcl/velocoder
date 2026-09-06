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

from PySide6.QtCore import QCoreApplication, QEventLoop, QProcess, QTimer  # noqa: E402

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


def x264_settings(**overrides):
    settings = {**BASE_SETTINGS, "encoder": "libx264", "rc_mode": "CRF",
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


class _MockedRenderNodeMixin:
    """For classes that only check the argv build_args() produces, not that
    any device it names is real -- these use vaapi_settings() as a generic
    "some encoder" fixture, not because the test is actually about VAAPI, so
    a real render node (this dev box's Intel iGPU, absent on a CI runner
    with no GPU at all) is not required. Mirrors the per-test
    patch.object(worker, "find_render_node") pattern TestGpuVendorSelection
    already uses below, just applied once per class instead of per test."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(worker, "find_render_node", return_value="/dev/dri/renderD128")
        self.addCleanup(patcher.stop)
        patcher.start()


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


class TestBuildArgsVaapi(_MockedRenderNodeMixin, ClipTestCase):
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


class TestBuildArgsAudioBitrateReservation(unittest.TestCase):
    """Real, confirmed bug: reserved_audio_kbps used to always assume the
    *configured* audio_bitrate for a bitrate-family rc_mode's target-size
    math, even when Automatic is actually going to *copy* the source
    audio through untouched. A copy-compatible source at a real bitrate
    higher than the configured audio_bitrate (a 256k AAC track with
    audio_bitrate set to 96k, say) meant the target-size math reserved
    96 but the real output audio consumed far more, letting the total
    output materially exceed the requested size. build_args now probes
    the source's real bitrate (probe_audio_bitrate_kbps) whenever it's
    actually going to be copied, falling back to the configured
    audio_bitrate only when that probe genuinely can't find one.

    MP4, not MKV -- confirmed directly that ffprobe reports stream=
    bit_rate as literal "N/A" for this repo's own MKV test fixture (see
    TestBuildArgsVaapi.test_vbr_uses_bitrate, which keeps its old
    assertion unchanged for exactly that reason: MKV falling back to the
    configured bitrate here is the *correct* new behavior, not an
    oversight), while MP4 reports a real one."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        cls.clip = cls.tmpdir / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-c:a", "aac", "-b:a", "256k", "-shortest", str(cls.clip)],
            check=True, timeout=30,
        )
        cls.real_audio_kbps = worker.probe_audio_bitrate_kbps(cls.clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_probe_reads_a_real_non_trivial_value(self):
        # Genuinely encoder-variable (real AAC VBR-ish output for a 1s
        # clip), not pinned to exactly 256 -- just confirms a real, sane
        # value came back, not None or something wildly wrong.
        self.assertIsNotNone(self.real_audio_kbps)
        self.assertGreater(self.real_audio_kbps, 20)
        self.assertLess(self.real_audio_kbps, 400)

    def test_probe_returns_none_for_a_track_that_does_not_exist(self):
        self.assertIsNone(worker.probe_audio_bitrate_kbps(self.clip, track_index=5))

    def test_copied_audio_reserves_the_real_source_bitrate_not_the_configured_one(self):
        args = worker.build_args(
            x265_settings(rc_mode="bitrate", quality_value=100, audio_bitrate="96k",
                          audio_copy_if_compatible=True),
            self.clip, self.tmpdir / "out.mp4", duration_seconds=80,
        )
        expected_video_kbps = worker.target_size_to_bitrate_kbps(100, 80, self.real_audio_kbps)
        self.assertEqual(args[args.index("-b:v") + 1], f"{expected_video_kbps}k")
        # Confirms this genuinely differs from the old (buggy) behavior,
        # not a coincidence where the two numbers happen to match.
        wrong_kbps_using_configured_bitrate = worker.target_size_to_bitrate_kbps(100, 80, 96)
        self.assertNotEqual(expected_video_kbps, wrong_kbps_using_configured_bitrate)

    def test_transcoded_audio_still_reserves_the_configured_bitrate(self):
        # audio_copy_if_compatible=False forces a transcode regardless of
        # source codec -- the configured audio_bitrate is the correct
        # (and only knowable ahead of time) number to reserve here, since
        # that's genuinely what the real output will use.
        args = worker.build_args(
            x265_settings(rc_mode="bitrate", quality_value=100, audio_bitrate="96k",
                          audio_copy_if_compatible=False),
            self.clip, self.tmpdir / "out.mp4", duration_seconds=80,
        )
        expected_video_kbps = worker.target_size_to_bitrate_kbps(100, 80, 96)
        self.assertEqual(args[args.index("-b:v") + 1], f"{expected_video_kbps}k")

    def test_forced_downmix_still_reserves_the_configured_bitrate(self):
        # A stream copy can't remix channels -- downmix forces a
        # transcode the same way audio_copy_if_compatible=False does, so
        # the configured bitrate is correct here too even though
        # audio_copy_if_compatible itself is still True. probe_audio=
        # False + explicit audio_codec/audio_channels here, not the real
        # clip -- its own sine-wave audio is genuinely mono, and
        # probe_audio=True would just re-probe and overwrite a caller-
        # supplied audio_channels with that real (non-6) value.
        args = worker.build_args(
            x265_settings(rc_mode="bitrate", quality_value=100, audio_bitrate="96k",
                          audio_copy_if_compatible=True, audio_downmix_stereo=True),
            self.clip, self.tmpdir / "out.mp4", duration_seconds=80,
            probe_audio=False, audio_codec="aac", audio_channels=6,
        )
        expected_video_kbps = worker.target_size_to_bitrate_kbps(100, 80, 96)
        self.assertEqual(args[args.index("-b:v") + 1], f"{expected_video_kbps}k")

    def test_caller_supplied_audio_source_bitrate_skips_the_probe(self):
        # Same pattern as duration_seconds -- a caller that already knows
        # the value (a cached preview, say) can skip a redundant ffprobe.
        with patch.object(worker, "probe_audio_bitrate_kbps") as mock_probe:
            args = worker.build_args(
                x265_settings(rc_mode="bitrate", quality_value=100, audio_bitrate="96k",
                              audio_copy_if_compatible=True),
                self.clip, self.tmpdir / "out.mp4", duration_seconds=80,
                audio_source_bitrate_kbps=640,
            )
            mock_probe.assert_not_called()
        expected_video_kbps = worker.target_size_to_bitrate_kbps(100, 80, 640)
        self.assertEqual(args[args.index("-b:v") + 1], f"{expected_video_kbps}k")


class TestQueueAudioBitrateReservation(unittest.TestCase):
    """Real, confirmed bug: the class above confirms build_args() itself
    reserves the real copied-audio source bitrate correctly given
    probe_audio=True (or a caller-supplied audio_source_bitrate_kbps) --
    but TranscodeQueue._run_next(), the actual encode path, called
    build_args() with probe_audio=False and no audio_source_bitrate_kbps
    at all (it already probes audio_codec/audio_channels itself, just
    not bitrate), silently falling back to the *configured* audio_
    bitrate for the real encode regardless of the source track's real
    bitrate. A direct build_args() test can't catch this -- the bug was
    in what _run_next() passes it, so this goes through a real
    TranscodeQueue instead, the same way TestMissingAudioTrackWarning
    above does to catch bugs specific to that call site."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        cls.clip = cls.tmpdir / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
             "-c:v", "libx264", "-c:a", "aac", "-b:a", "256k", "-shortest", str(cls.clip)],
            check=True, timeout=30,
        )
        cls.real_audio_kbps = worker.probe_audio_bitrate_kbps(cls.clip)
        cls.real_duration = worker.probe_duration(cls.clip)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_real_encode_reserves_the_actual_source_bitrate_not_the_configured_one(self):
        self.assertIsNotNone(self.real_audio_kbps, "MP4 should report a real per-stream bit_rate")
        out_dir = self.tmpdir / "out"
        out_dir.mkdir()
        job = {
            "path": self.clip,
            **x265_settings(rc_mode="bitrate", quality_value=100, audio_bitrate="96k",
                             audio_copy_if_compatible=True),
        }
        queue = worker.TranscodeQueue()
        log_lines = []
        loop = QEventLoop()
        queue.job_log.connect(log_lines.append)
        queue.all_finished.connect(loop.quit)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(15000)
        queue.start([job], out_dir)
        loop.exec()

        ffmpeg_line = next((line for line in log_lines if line.startswith("ffmpeg ")), "")
        expected_video_kbps = worker.target_size_to_bitrate_kbps(100, self.real_duration, self.real_audio_kbps)
        wrong_kbps_using_configured_bitrate = worker.target_size_to_bitrate_kbps(100, self.real_duration, 96)
        self.assertIn(f"-b:v {expected_video_kbps}k", ffmpeg_line, ffmpeg_line)
        self.assertNotEqual(expected_video_kbps, wrong_kbps_using_configured_bitrate)


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


class TestBuildArgsX264(ClipTestCase):
    """libx264 is a second software (non-VAAPI) path alongside libx265 --
    same rc_mode/pix_fmt/preset handling (confirmed structurally identical
    in build_args), but its own -c:v value and, critically, none of
    libx265's own -x265-params psycho-visual tuning (meaningless to
    libx264 -- would fail the job outright if it were ever sent there)."""

    def test_uses_libx264_not_libx265(self):
        args = worker.build_args(x264_settings(), self.clip, self.out_path)
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")

    def test_no_x265_params(self):
        # The one thing that's actually different from the x265 path
        # structurally, not just a different flag value -- see the
        # comment on this exact branch in worker.py's build_args.
        args = worker.build_args(x264_settings(), self.clip, self.out_path)
        self.assertNotIn("-x265-params", args)

    def test_crf_uses_crf_flag(self):
        args = worker.build_args(x264_settings(rc_mode="CRF", quality_value=20), self.clip, self.out_path)
        self.assertIn("-crf", args)
        self.assertEqual(args[args.index("-crf") + 1], "20")
        self.assertNotIn("-b:v", args)

    def test_bitrate_mode_uses_bv_not_crf(self):
        args = worker.build_args(
            x264_settings(rc_mode="bitrate", quality_value=100),
            self.clip, self.out_path, duration_seconds=80,
        )
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "10080k")
        self.assertNotIn("-crf", args)

    def test_10bit_uses_yuv420p10le(self):
        args = worker.build_args(x264_settings(bit_depth=10), self.clip, self.out_path)
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p10le")

    def test_8bit_uses_yuv420p(self):
        args = worker.build_args(x264_settings(bit_depth=8), self.clip, self.out_path)
        self.assertEqual(args[args.index("-pix_fmt") + 1], "yuv420p")

    def test_speed_maps_to_preset_flag(self):
        # Same ultrafast..placebo preset names as libx265 -- confirmed
        # against this exact ffmpeg build, not assumed.
        args = worker.build_args(x264_settings(speed="veryslow"), self.clip, self.out_path)
        self.assertEqual(args[args.index("-preset") + 1], "veryslow")

    def test_x264_only_tune_value_passes_through(self):
        # "film" is real for x264 but rejected outright by this exact
        # libx265 build (see TestTune.test_film_is_deliberately_not_offered)
        # -- build_args itself doesn't validate tune values against either
        # list, it just passes through whatever string it's given, so this
        # confirms the plumbing carries an x264-only value correctly.
        args = worker.build_args(x264_settings(tune="film"), self.clip, self.out_path)
        self.assertEqual(args[args.index("-tune") + 1], "film")

    def test_no_vaapi_device_for_cpu_encoder(self):
        args = worker.build_args(x264_settings(), self.clip, self.out_path)
        self.assertNotIn("-vaapi_device", args)

    def test_real_encode_produces_h264_output(self):
        # End-to-end with a real running ffmpeg, not just argv inspection
        # -- confirms libx264 is actually a valid -c:v value in this
        # build and the whole pipeline (scale/pix_fmt/preset/crf) is
        # genuinely accepted together, not just individually plausible.
        args = worker.build_args(x264_settings(), self.clip, self.out_path)
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(self.out_path)],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(probe.stdout.strip(), "h264")


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


class TestBuildArgsCommon(_MockedRenderNodeMixin, ClipTestCase):
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


class TestTune(_MockedRenderNodeMixin, ClipTestCase):
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


class TestAudioSelection(_MockedRenderNodeMixin, ClipTestCase):
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


class TestAudioDownmix(_MockedRenderNodeMixin, unittest.TestCase):
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


class TestCommandPreview(_MockedRenderNodeMixin, unittest.TestCase):
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


class TestEmitStats(unittest.TestCase):
    """_emit_stats reads plain instance state (_stats_buffer, _duration),
    not a live QProcess -- directly testable in isolation without a real
    encode running, unlike most of TranscodeQueue's own behavior."""

    def _emit(self, stats_buffer: dict, duration: float) -> dict:
        queue = worker.TranscodeQueue()
        queue._stats_buffer = stats_buffer
        queue._duration = duration
        received = []
        queue.job_stats.connect(received.append)
        queue._emit_stats()
        return received[0]

    def test_speed_multiplier_is_a_real_float_not_the_raw_string(self):
        stats = self._emit({"speed": "2.3x"}, duration=100.0)
        self.assertEqual(stats["speed_multiplier"], 2.3)
        self.assertEqual(stats["speed"], "2.3x")  # untouched, still available for display

    def test_missing_speed_gives_none_multiplier_not_a_crash(self):
        stats = self._emit({}, duration=100.0)
        self.assertIsNone(stats["speed_multiplier"])

    def test_zero_speed_gives_none_multiplier(self):
        # ffmpeg does briefly report "0x" right at a job's start -- must not
        # be treated as a usable rate (queue_controller.py's queue-wide ETA
        # divides by this).
        stats = self._emit({"speed": "0x"}, duration=100.0)
        self.assertIsNone(stats["speed_multiplier"])

    def test_unparseable_speed_gives_none_multiplier_not_a_crash(self):
        stats = self._emit({"speed": "N/A"}, duration=100.0)
        self.assertIsNone(stats["speed_multiplier"])


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


class TestJobStartedFiresBeforeAnyFailureForThatJob(unittest.TestCase):
    """Real regression: job_started used to only fire after preflight
    (duration/audio probing, build_args) succeeded, so a preflight
    failure emitted job_failed for a job the GUI's _current_running_item
    (queue_controller.py) had never actually been pointed at yet --
    whatever job *previously* had job_started fire (from this run, or a
    stale reference from an earlier one) got blamed instead. Verified
    directly against the real signal sequence here, not the GUI layer
    (see tests/test_main.py for that side)."""

    def test_second_jobs_preflight_failure_gets_its_own_job_started_first(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            # In its own subdirectory, distinct from output_dir=tmpdir
            # below -- so *only* bad_clip collides with its own natural
            # output path, not both.
            source_dir = tmpdir / "source"
            source_dir.mkdir()
            good_clip = source_dir / "good.mp4"
            _make_clip(good_clip)
            # Sits directly at what would be its own natural output path
            # (same stem/container/directory as output_dir=tmpdir below) --
            # deterministically triggers the same-as-input preflight
            # refusal, same technique as TestOutputPathCollisionGuard above.
            bad_clip = tmpdir / "bad.mp4"
            _make_clip(bad_clip)

            jobs = [
                {"path": good_clip, **x265_settings(container="mp4")},
                {"path": bad_clip, **x265_settings(container="mp4")},
            ]
            queue = worker.TranscodeQueue()
            events = _run_queue_and_collect(queue, jobs, tmpdir)

            self.assertEqual(
                [e[0] for e in events],
                ["job_started", "job_finished", "job_started", "job_failed"],
                events,
            )
            self.assertEqual(events[0][1], (str(good_clip), 1, 2))
            self.assertEqual(events[2][1], (str(bad_clip), 2, 2))
            self.assertEqual(events[3][1][0], str(bad_clip))
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


class TestPauseResume(unittest.TestCase):
    """request_pause() lets the *current* job run to completion untouched
    (unlike stop(), nothing is terminated) and halts the run right after
    -- _on_finished/_on_process_error route through _advance_or_pause()
    instead of a plain _run_next() for exactly this reason. Exercised
    directly against _on_finished with synthetic temp files, same
    approach TestStopRaceFix above already uses -- no real subprocess
    needed to test this mechanics."""

    def _finish_a_job(self, queue, tmpdir, name="done"):
        temp_output = tmpdir / f".{name}.transcoding.mp4"
        final_output = tmpdir / f"{name}.mp4"
        temp_output.write_bytes(b"pretend this is a completed encode")
        queue._on_finished(Path(f"{name}.mkv"), temp_output, final_output, 0, None)

    def test_pause_request_halts_after_the_current_job_finishes(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            queue = worker.TranscodeQueue()
            # _index = 0, not 1 -- _run_next() dispatches self._jobs[self._index]
            # and only increments _index *after*, so "next.mkv would run next if
            # not paused" means it's sitting at _jobs[_index] itself, still
            # un-dispatched (an _index of 1 against a single-element list would
            # mean this job had already been dispatched, the opposite of pending
            # -- caught by _advance_or_pause's own added len(_jobs) check below
            # this test, which (correctly) no longer treats that state as "more
            # jobs exist").
            queue._jobs = [{"path": Path("next.mkv")}]  # would run next if not paused
            queue._index = 0
            queue.request_pause()
            paused_events = []
            started_events = []
            queue.paused.connect(lambda: paused_events.append(True))
            queue.job_started.connect(lambda *a: started_events.append(a))

            self._finish_a_job(queue, tmpdir)

            self.assertEqual(paused_events, [True])
            self.assertEqual(started_events, [], "the next job must not have started")
            self.assertTrue(queue._paused)
            self.assertFalse(queue._pause_requested, "one-shot -- consumed once it takes effect")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_pause_requested_on_the_last_job_finishes_instead_of_pausing(self):
        # Real, reported-live bug: "Stop After Current Video" checked
        # during the *final* job in the queue paused anyway -- "Paused --
        # 0 file(s) remaining" with a Resume button that has nothing left
        # to resume, because _advance_or_pause only checked
        # _pause_requested, not whether a next job actually existed. The
        # run is genuinely finished at that point; it must behave exactly
        # like the no-pause-requested case below (all_finished, not
        # paused) once there's nothing left in the queue, regardless of
        # whether a pause happened to be armed.
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            queue = worker.TranscodeQueue()
            queue._jobs = []  # nothing left -- this was the last job
            queue.request_pause()
            paused_events = []
            all_finished_events = []
            queue.paused.connect(lambda: paused_events.append(True))
            queue.all_finished.connect(lambda: all_finished_events.append(True))

            self._finish_a_job(queue, tmpdir)

            self.assertEqual(paused_events, [], "nothing left to resume -- must not pause")
            self.assertEqual(all_finished_events, [True])
            self.assertFalse(queue._paused)
            self.assertFalse(
                queue._pause_requested, "one-shot -- consumed even when it didn't take effect"
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_pause_requested_continues_normally(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            queue = worker.TranscodeQueue()
            queue._jobs = []  # nothing left -- confirms the normal all_finished path still fires
            all_finished_events = []
            queue.all_finished.connect(lambda: all_finished_events.append(True))

            self._finish_a_job(queue, tmpdir)

            self.assertEqual(all_finished_events, [True])
            self.assertFalse(queue._paused)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_cancel_pause_request_before_it_takes_effect(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            queue = worker.TranscodeQueue()
            queue._jobs = []
            queue.request_pause()
            queue.cancel_pause_request()  # e.g. user unchecked the box before this job finished
            paused_events = []
            all_finished_events = []
            queue.paused.connect(lambda: paused_events.append(True))
            queue.all_finished.connect(lambda: all_finished_events.append(True))

            self._finish_a_job(queue, tmpdir)

            self.assertEqual(paused_events, [])
            self.assertEqual(all_finished_events, [True])
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_resume_continues_from_the_same_index_not_a_fresh_run(self):
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            queue = worker.TranscodeQueue()
            queue._jobs = [{"path": Path("a.mkv")}, {"path": Path("b.mkv")}, {"path": Path("c.mkv")}]
            queue._index = 2  # a.mkv and b.mkv already ran
            queue._paused = True
            queue._output_dir = tmpdir
            failed_events = []
            queue.job_failed.connect(lambda *a: failed_events.append(a))

            queue.resume()

            # c.mkv doesn't exist on disk, so probing it fails -- confirms
            # resume() actually picked up index 2 (c.mkv), not a fresh
            # run restarting from index 0 (which would have tried a.mkv
            # first instead).
            self.assertEqual(len(failed_events), 1)
            self.assertIn("c.mkv", failed_events[0][0])
            self.assertFalse(queue._paused)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_stop_while_paused_emits_all_finished_immediately(self):
        # No live _process to terminate while genuinely paused between
        # jobs -- nothing would otherwise ever call _run_next() again to
        # notice _stopped and emit all_finished on its own (that only
        # happens from _advance_or_pause, already returned out of for
        # good once a pause takes effect), so stop() has to do it directly.
        queue = worker.TranscodeQueue()
        queue._jobs = [{"path": Path("a.mkv")}]
        queue._index = 1
        queue._paused = True
        all_finished_events = []
        queue.all_finished.connect(lambda: all_finished_events.append(True))

        queue.stop()

        self.assertEqual(all_finished_events, [True])
        self.assertFalse(queue._paused)
        self.assertTrue(queue._stopped)

    def test_stop_while_genuinely_running_does_not_emit_all_finished_synchronously(self):
        # Contrast case -- a live process still needs its own async
        # finished/errorOccurred signal to arrive before all_finished is
        # appropriate; stop() must not short-circuit that by emitting it
        # immediately just because _paused happens to be False here too.
        queue = worker.TranscodeQueue()
        queue._process = QProcess()
        queue._process.setProgram("sleep")
        queue._process.setArguments(["5"])
        queue._process.start()
        self.assertTrue(queue._process.waitForStarted(3000))
        all_finished_events = []
        queue.all_finished.connect(lambda: all_finished_events.append(True))

        queue.stop()

        self.assertEqual(all_finished_events, [])
        queue._process.waitForFinished(3000)

    def test_real_two_job_run_pauses_between_them_and_resumes(self):
        # End-to-end with two real, genuinely-running encodes -- the
        # tests above cover the mechanics directly against synthetic
        # _on_finished calls; this confirms the same behavior actually
        # holds with a real ffmpeg process in between.
        tmpdir = Path(tempfile.mkdtemp(prefix="transcoder_test_"))
        try:
            clip_a = tmpdir / "a.mkv"
            clip_b = tmpdir / "b.mkv"
            _make_clip(clip_a)
            _make_clip(clip_b)
            jobs = [{"path": clip_a, **x265_settings()}, {"path": clip_b, **x265_settings()}]

            queue = worker.TranscodeQueue()
            events = []
            loop = QEventLoop()
            queue.job_finished.connect(lambda *a: events.append(("job_finished", a)))

            # request_pause() only after the run has genuinely started --
            # matches real usage (the GUI checkbox stays disabled until
            # then) and, separately, start() itself resets
            # _pause_requested to False, so requesting it any earlier
            # would just get wiped out before _run_next ever saw it. A
            # named slot, not a lambda, so it can be disconnected before
            # phase 2 below -- otherwise it would also arm a pause for
            # the second (resumed) job, which isn't what this is testing.
            def request_pause_on_start(*a):
                events.append(("job_started", a))
                queue.request_pause()

            queue.job_started.connect(request_pause_on_start)
            queue.paused.connect(lambda: events.append(("paused", ())))
            queue.paused.connect(loop.quit)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(loop.quit)
            timer.start(15000)

            queue.start(jobs, tmpdir)
            loop.exec()
            queue.job_started.disconnect(request_pause_on_start)

            self.assertEqual([e[0] for e in events], ["job_started", "job_finished", "paused"])
            self.assertTrue((tmpdir / "a.mp4").exists())
            self.assertFalse((tmpdir / "b.mp4").exists(), "b.mkv must not have started yet")

            # Resume -- b.mkv should now actually run to completion.
            events.clear()
            queue.job_started.connect(lambda *a: events.append(("job_started", a)))
            loop2 = QEventLoop()
            queue.all_finished.connect(loop2.quit)
            timer2 = QTimer()
            timer2.setSingleShot(True)
            timer2.timeout.connect(loop2.quit)
            timer2.start(15000)

            queue.resume()
            loop2.exec()

            self.assertEqual([e[0] for e in events], ["job_started", "job_finished"])
            self.assertTrue((tmpdir / "b.mp4").exists())
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
