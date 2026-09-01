# TITAN-i Transcoder

Minimal ffmpeg front-end replacing HandBrake, whose QSV path is dead on this
box (see Root cause below). PySide6 GUI queue, one file at a time.

## Run

```
./launch.sh
```

## Root cause of the HandBrake QSV failure

Not a Flatpak sandboxing issue. Debian trixie dropped `libmfx1` (the legacy
Media SDK runtime needed by this Gen9.5 Comet Lake iGPU) and only packages
`libmfx-gen1.2` (oneVPL, Gen12+ only — wrong generation for this hardware
regardless). ffmpeg's own build confirms the same gap: `ffmpeg -version`
shows `--disable-libmfx --enable-libvpl`, so `hevc_qsv` is equally dead here,
in ffmpeg or HandBrake, native or Flatpak. The hardware and driver are fine —
`vainfo` against the Intel node shows the `iHD` driver loading with H264 and
HEVC (8-bit and Main10) `EncSlice` entrypoints.

**Note:** HEVC on this hardware only exposes `EncSlice` (non-low-power), not
`EncSliceLP` — H264 has both. Never add `-low_power 1` to the HEVC VAAPI
args; it isn't available for HEVC here and will fail. (`tests/test_worker.py`
has a regression test pinning this down.)

## Fix: ffmpeg's VAAPI encoders, direct

`hevc_vaapi` talks to the `iHD` VAAPI driver directly via the same
`EncSliceLP`-adjacent path QSV would have used, skipping Intel's MFX/oneVPL
dispatch layer entirely.

## GPU device selection gotcha

The two GPUs' PCI bus IDs do **not** match their render node numbers:

- Intel UHD 630: PCI `00:02.0` → `/dev/dri/renderD129`
- AMD RX 6600 XT: PCI `03:00.0` → `/dev/dri/renderD128`

`worker.find_render_node()` resolves this via `/dev/dri/by-path/pci-*-render`
symlinks + PCI vendor ID (`0x8086` = Intel), never a hardcoded node number.
If GPU selection code is touched again, keep using that approach.

## Project layout

```
constants.py   Static config: encoders, rate-control modes, resolutions,
               the two built-in seed presets. No I/O, no Qt.
presets.py     User preset persistence (load/save user_presets.json).
worker.py      The engine: turns a settings dict into an ffmpeg argv
               (build_args), and TranscodeQueue, which runs jobs one at a
               time via QProcess. No preset concept here — by design, this
               module never looks up a preset by name, it only ever sees
               a fully-resolved settings dict. Presets are a GUI-only
               convenience for naming/saving a settings snapshot.
main.py        PySide6 GUI: MainWindow (two-pane layout — settings tabs on
               the left, queue/progress/log on the right), DropListWidget
               (drag-and-drop queue).
tests/         unittest suite for constants.py/presets.py/worker.py.
```

The GUI is a `QSplitter`: left pane is the output-folder row plus a
`QTabWidget` (**Preset** / **Video** / **Audio** — grouped by what each
setting is, see Controls below), right pane is the queue, run controls,
progress bar, a live stats line, and the full log.

The settings dict that flows from the GUI into `build_args()` (and that a
saved preset *is*, plus a `name` key):

```python
{
    "encoder": "hevc_vaapi" | "libx265",
    "rc_mode": "ICQ" | "CQP" | "VBR" | "CRF" | "bitrate",
    "quality_value": int,   # quality units for ICQ/CQP/CRF, kbps for VBR/bitrate
    "speed": str,            # "1".."7" (vaapi compression_level) or an x265 preset name
    "bit_depth": 8 | 10,
    "width": int, "height": int,
    "container": "mp4" | "mkv",
    "tune": str,              # an x265 tune name, or "None" to omit -tune; ignored for hevc_vaapi
    "audio_track": int,      # 0-based
    "audio_copy_if_compatible": bool,
    "audio_bitrate": str,    # e.g. "160k", used only when transcoding audio
}
```

## Presets

Two built-in presets ship in `constants.BUILTIN_PRESETS`, mapped from the
user's actual HandBrake custom presets
(`~/.var/app/fr.handbrake.ghb/config/ghb/presets.json`). They're protected —
`Save As…` refuses to reuse their names, `Delete` refuses to remove them —
so there's always a known-good starting point.

1. **720p QSV Balanced (Hardware / VAAPI)** — was `qsv_h265_10bit`, ICQ 26,
   main10. `-compression_level 1` (the vaapi "speed" value) is an estimate
   for QSV's "quality" preset, not validated by A/B — see Known gaps.
2. **720p Stuff Tuned (CPU / x265)** — was already pure CPU x265, so this is
   a clean 1:1 mapping: `-preset medium -crf 23`, plus the original's
   `-x265-params "strong-intra-smoothing=0:aq-mode=3:psy-rdoq=1.0"` (fixed,
   not exposed as a control — nobody asked to tune it independently).

Anything saved via **Save As…** is appended to `user_presets.json`
(gitignored — it's the user's own data, not source) and shows up in the
same dropdown as the built-ins from then on.

Output is MP4 or MKV (see Container below), first video stream
(attached-pic/cover-art excluded) + one selectable audio track (no
subtitle/data passthrough yet, even on MKV — see Known gaps). Audio: copied
through as-is if the selected track is `aac`/`ac3`/`eac3` *and* "copy if
compatible" is checked, otherwise transcoded to AAC at the chosen bitrate
(default 160k, matching the real HandBrake preset's `av_aac` @ 160kbps).
Narrower than HandBrake's own copy mask (also allows
`dts`/`dtshd`/`truehd`/`flac`) because muxing those into MP4 via ffmpeg
isn't reliably playable.

## Controls

Grouped into three tabs by what kind of setting they are.

**Preset tab**
- **Preset** — load a saved settings snapshot into every control below.
  **Save As…** / **Delete** manage `user_presets.json`.

**Video tab**
- **Encoder** — VAAPI HEVC (hardware) or x265 (CPU).
- **Rate control** — options depend on encoder: VAAPI gets ICQ/CQP/VBR,
  x265 gets CRF/target-bitrate. Picking a bitrate-based mode swaps the
  Quality slider for a kbps spinbox.
- **Quality / Bitrate** — meaning and range follow the rate-control mode
  (e.g. ICQ 1–51 vs CRF 0–51 aren't the same scale, so this re-ranges
  itself on every encoder/rc_mode change).
- **Speed** — VAAPI's `-compression_level` (1–7, lower = slower/better) or
  x265's preset ladder (ultrafast…placebo), whichever applies.
- **Bit depth** — 8-bit or 10-bit (`main`/`nv12` vs `main10`/`p010le` for
  VAAPI; `yuv420p` vs `yuv420p10le` for x265).
- **Resolution** — `Source (no scale)` / `1080p` / `720p` / `480p`, fit
  within the box keeping aspect, never upscales.
- **Container** — MP4 or MKV. `-movflags +faststart` is only added for MP4
  (it's a mov/mp4-muxer-private option — ffmpeg silently ignores it on MKV,
  but there's no reason to carry a flag that means nothing there).
- **Tune (x265 only)** — hidden when Encoder is VAAPI (`hevc_vaapi` has no
  equivalent option). Options: `animation`, `grain`, `psnr`, `ssim`,
  `fastdecode`, `zerolatency`, or `None` to omit `-tune` entirely. **`film`
  is deliberately not offered** — it's a real x265 tune name in general, but
  this exact libx265 build rejects it outright (`Error setting preset/tune
  (null)/film.`, confirmed by actually running it, not assumed).

**Audio tab**
- **Audio track** — `Track 1`–`4`, by stream index (not probed per file —
  keeps the tool from having to pre-scan the whole queue just to populate a
  dropdown).
- **Copy audio if compatible** — uncheck to always transcode, even for a
  codec that would normally be copied through.
- **Audio bitrate** — used only when a track gets transcoded.

**Queue pane (right side)**
- **Apply Settings to Selected** — every control above is only the
  *default* baked into a file the moment it's added to the queue. To make
  one queued file different, select it, change the controls, click this.
  There's no per-row editable table — deliberately, to keep the main
  controls to one place.
- **Open** — opens the current output folder in the desktop file manager.
- **Live stats line** (under the progress bar) — fps / bitrate / speed /
  ETA for the job currently running, parsed from ffmpeg's `-progress`
  stream (`TranscodeQueue._emit_stats`). Separate from the full scrolling
  log further down, which stays raw ffmpeg stderr.

## Testing

```
python3 -m unittest discover -s tests -v
```

Plain stdlib `unittest`, no extra install. Most tests check the argv
`build_args()` produces; a few actually run ffmpeg against tiny
synthetic clips (real hardware encode included, skipped automatically if
`/dev/dri/by-path` doesn't exist) — the whole point of this module is
producing a command line ffmpeg accepts, and that's not something a test
that never calls ffmpeg can catch.

## Known gaps

- `-compression_level 1` not A/B'd against remembered QSV output quality —
  try the range (1–7, lower = slower/better) if output doesn't match
  expectations.
- No subtitle passthrough (explicitly `-sn`'d — originally to avoid an
  MP4-incompatible subtitle codec failing the mux; MKV output removes that
  specific risk but nothing maps subtitle streams on either container yet)
  and no foreign-audio-search/burn-in — the old "Stuff Tuned" HandBrake
  preset had both, neither is replicated.
- No batch folder-watch.
- No error-recovery beyond the log showing FAILED and the partial output
  file being deleted.
