"""Regression tests for worker.py (the ffmpeg command builder + queue engine).

Run with:  python3 -m unittest discover -s tests -v
(from the transcoder/ root, plain stdlib unittest -- no extra install needed)

Some tests actually invoke ffmpeg on tiny synthetic clips rather than mock
it -- the whole point of build_args() is producing a command line ffmpeg
accepts, and only real hardware/driver combinations can confirm the VAAPI
flags this app relies on (e.g. -rc_mode CQP needing -qp, not
-global_quality) are actually valid, not just plausible-looking.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import worker  # noqa: E402

HAS_VAAPI = Path("/dev/dri/by-path").exists()

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
        args = worker.build_args(vaapi_settings(rc_mode="VBR", quality_value=3000), self.clip, self.out_path)
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "3000k")

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


class TestBuildArgsX265(ClipTestCase):
    def test_crf_uses_crf_flag(self):
        args = worker.build_args(x265_settings(rc_mode="CRF", quality_value=20), self.clip, self.out_path)
        self.assertIn("-crf", args)
        self.assertEqual(args[args.index("-crf") + 1], "20")
        self.assertNotIn("-b:v", args)

    def test_bitrate_mode_uses_bv_not_crf(self):
        args = worker.build_args(x265_settings(rc_mode="bitrate", quality_value=2500), self.clip, self.out_path)
        self.assertIn("-b:v", args)
        self.assertEqual(args[args.index("-b:v") + 1], "2500k")
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


class TestFindRenderNode(unittest.TestCase):
    @unittest.skipUnless(HAS_VAAPI, "no /dev/dri/by-path on this machine")
    def test_resolves_intel_node_when_present(self):
        node = worker.find_render_node(worker.INTEL_VENDOR_ID)
        self.assertTrue(Path(node).exists())

    @unittest.skipUnless(HAS_VAAPI, "no /dev/dri/by-path on this machine")
    def test_unknown_vendor_raises(self):
        with self.assertRaises(RuntimeError):
            worker.find_render_node("0xdead")


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
