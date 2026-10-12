"""Renders the real main window offscreen for review: each theme x text size,
an empty queue (Video and Audio tabs) and a queue mid-run.

    /mnt/data/tools/venv/transcoder/bin/python scripts/screenshots.py OUT_DIR

Never touches the real user's data: HOME and the XDG config/data dirs point at
a throwaway directory, the session file is neither loaded nor saved, and the
demo videos are short generated clips. Intel and AMD are faked present (as the
test suite does) so the Processing row shows every segment on any machine.
Text sizes beyond 10 pt scale every font-size in style.qss, the way a larger
desktop font would."""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="velocoder-shots-"))
os.environ.update({
    "QT_QPA_PLATFORM": "offscreen",
    "HOME": str(SANDBOX),
    "XDG_CONFIG_HOME": str(SANDBOX / "config"),
    "XDG_DATA_HOME": str(SANDBOX / "data"),
    "LANG": "en_US.UTF-8",
})
sys.path.insert(0, str(REPO / "src"))

from unittest.mock import patch  # noqa: E402

from PySide6.QtWidgets import QApplication, QTabWidget  # noqa: E402

from velocoder.core import ffmpeg  # noqa: E402
from velocoder.ui import session  # noqa: E402
from velocoder.ui._vendor.odcs_ui import desktop as odcs_desktop  # noqa: E402
from velocoder.ui.icons import refresh_icons  # noqa: E402

DEMO_VIDEOS = ["harbour-timelapse.mp4", "family-dinner.mkv", "drone-coastline-4k.mp4", "old-camcorder-tape.avi"]


def _fake_render_node(vendor_id):
    if vendor_id == ffmpeg.GPU_VENDOR_IDS["intel"]:
        return "/dev/dri/renderD128"
    if vendor_id == ffmpeg.GPU_VENDOR_IDS["amd"]:
        return "/dev/dri/renderD129"
    raise RuntimeError("no render node")


def _make_demo_videos() -> list[Path]:
    folder = SANDBOX / "Videos"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for seconds, name in zip((3, 6, 4, 2), DEMO_VIDEOS):
        path = folder / name
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=10:duration={seconds}",
             "-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
        paths.append(path)
    return paths


def _settle(app, seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def _set_text_size(app, window, points: float):
    base = window._base_stylesheet
    if points != 10:
        factor = points / 10
        base = re.sub(r"font-size:\s*([\d.]+)pt", lambda m: f"font-size: {float(m.group(1)) * factor:g}pt", base)
    app.setStyleSheet(base)
    refresh_icons(window)
    window.queue_list.fit_columns()
    # A window manager never lets a window be narrower than its minimum;
    # offscreen Qt does, and would squeeze the layout instead.
    _settle(app)
    window.resize(max(window.width(), window.minimumSizeHint().width()), window.height())


def _grab(app, window, path: Path):
    _settle(app)
    window.grab().save(str(path))
    print(path, flush=True)


def _fill_queue(app, window, videos):
    window.add_files(videos)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and window._pending_analysis_items:
        _settle(app, 0.1)
    items = [window.queue_list.topLevelItem(i) for i in range(window.queue_list.topLevelItemCount())]
    window._running_items = items
    output = SANDBOX / "Videos" / "transcoded"
    output.mkdir(parents=True, exist_ok=True)
    done_output = output / DEMO_VIDEOS[0]
    done_output.write_bytes(b"\0" * (videos[0].stat().st_size // 3))
    window._apply_run_phase_visuals("converting")
    window._on_job_started(str(videos[0]), 1, 4)
    window._on_job_finished(str(videos[0]), str(done_output))
    window._on_job_started(str(videos[1]), 2, 4)
    window._on_job_failed(str(videos[1]), "Demo failure")
    window._on_job_started(str(videos[2]), 3, 4)
    window._on_job_progress(0.46)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--themes", nargs="+", default=["dark", "light"])
    parser.add_argument("--sizes", nargs="+", type=float, default=[10, 14, 22])
    parser.add_argument("--scale", default="2")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=800)
    args = parser.parse_args()
    os.environ["QT_SCALE_FACTOR"] = args.scale
    args.out.mkdir(parents=True, exist_ok=True)
    videos = _make_demo_videos()

    app = QApplication(sys.argv)
    with patch.object(session, "load_session", return_value=None), \
            patch.object(session, "save_session"), \
            patch.object(ffmpeg, "find_render_node", side_effect=_fake_render_node), \
            patch.object(ffmpeg, "validate_hevc_encode", return_value=None), \
            patch.object(odcs_desktop, "accent", return_value=None):
        from velocoder.ui import main_window
        for theme in args.themes:
            window = main_window.MainWindow()
            window._apply_theme(theme)
            window._base_stylesheet = app.styleSheet()
            window.resize(args.width, args.height)
            window.show()
            for points in args.sizes:
                tag = f"{theme}_{points:g}pt"
                _set_text_size(app, window, points)
                tabs = window.findChild(QTabWidget)
                tabs.setCurrentIndex(0)
                _grab(app, window, args.out / f"video_{tag}.png")
                tabs.setCurrentIndex(1)
                _grab(app, window, args.out / f"audio_{tag}.png")
                tabs.setCurrentIndex(0)
            _set_text_size(app, window, 10)
            _fill_queue(app, window, videos)
            for points in args.sizes:
                _set_text_size(app, window, points)
                _grab(app, window, args.out / f"queue_{theme}_{points:g}pt.png")
            window.close()


if __name__ == "__main__":
    main()
