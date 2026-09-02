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
main.py        PySide6 GUI: MainWindow (preset toolbar + two-pane layout —
               settings tabs and a live command preview on the left,
               queue/progress/log on the right), DropListWidget
               (drag-and-drop queue).
style.qss      Dark theme, loaded by _load_stylesheet() in main.py — see
               the gotcha about its $ASSETS token in Known gaps.
assets/        SVG glyphs style.qss paints on top of Fusion's native
               checkbox/spinbox subcontrols (see Known gaps for why).
tests/         unittest suite for constants.py/presets.py/worker.py.
```

The GUI is a `QSplitter`. Left pane: a **Preset** row (load/Save As/Delete)
above a `QTabWidget` (**Video** / **Audio**, grouped by what each setting
is — see Controls below), then a live **Effective Command** preview. Right
pane: the queue, output folder, run controls, progress bar, a live stats
line, and the full log. Window geometry and the splitter position are
remembered across launches via `QSettings("TITAN-i", "Transcoder")` (on
Linux: `~/.config/TITAN-i/Transcoder.conf`) — separate from
`user_presets.json`, since this is per-viewer window state, not app data.

The settings dict that flows from the GUI into `build_args()` (and that a
saved preset *is*, plus a `name` key):

```python
{
    "encoder": "hevc_vaapi" | "libx265",
    "rc_mode": "ICQ" | "CQP" | "VBR" | "CRF" | "bitrate",
    "quality_value": int,   # quality units for ICQ/CQP/CRF, target size in MB for VBR/bitrate
    "speed": str,            # "1".."7" (vaapi compression_level) or an x265 preset name
    "bit_depth": 8 | 10,
    "width": int, "height": int,
    "container": "mp4" | "mkv",
    "tune": str,              # an x265 tune name, or "None" to omit -tune; ignored for hevc_vaapi
    "deinterlace": bool,     # bwdif (x265) or deinterlace_vaapi (VAAPI), see Deinterlace below
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

**Preset row** (top of the left pane, above the tabs)
- **Preset** — load a saved settings snapshot into every control below.
  This is the main lever — it sets everything else at once — so it isn't a
  tab alongside its own dependents, it sits above them instead.
  **Save As…** / **Delete** manage `user_presets.json`.
- **`(modified)`** — appears next to the dropdown the moment any control
  drifts from the loaded preset's saved values, and disappears again if you
  change it back. Without this, nothing indicated that the preset name
  shown no longer matches what's actually configured.

The rest is grouped into two tabs, by what kind of setting they are.

**Video tab** (grouped into "Encoding" and "Format")

The controls here deliberately lead with plain English, not ffmpeg's own
names for things — a "Apple-style" simplification pass over what used to be
five separate rate-control modes and a raw `-compression_level` readout. The
underlying settings dict and `worker.build_args` are completely unaffected;
this is presentation only. See `constants.RC_MODE_FRIENDLY` for the mapping.

- **Encoder** — "Hardware (iGPU)" (`hevc_vaapi`, this machine's only real GPU)
  or "CPU" (`libx265`). No AMD/NVIDIA option — this workstation doesn't have
  that hardware, and there's no NVENC/AMF code in `worker.py` to back one.
- **Rate control** — a three-button row, not a dropdown: **Quality** / **File
  Size** / **Advanced**. Quality and File Size mean the same thing regardless
  of encoder (mapped to ICQ/VBR for VAAPI, CRF/bitrate for x265); Advanced is
  CQP (fixed quantizer), VAAPI-only since x265 has no equivalent here, so its
  button hides entirely rather than doing nothing when picked. The three are
  really just three positions of `rc_mode_combo`, still the actual source of
  truth for everything downstream — the combo itself stays alive but hidden
  (`main.py`'s `_set_rc_mode` / `_sync_rc_buttons_to_combo`) rather than
  being replaced, so there's exactly one place rc_mode can drift out of sync
  with what the UI shows.
- **Quality** (the slider, when Rate control is Quality or Advanced) — range
  follows the specific mode (e.g. ICQ 1–51 vs CRF 0–51 aren't the same
  scale, so this re-ranges itself on every encoder/rc_mode change). The raw
  number and mode name (e.g. "26 (ICQ)") show as small secondary text next
  to the slider, not the primary label.
- **Quality** (the size field, when Rate control is File Size) — a target
  **output size in MB**, not a literal bitrate. `quality_value` in the
  settings dict carries this same meaning for VBR/bitrate rc_modes now
  (previously literal kbps) — the actual `-b:v` value ffmpeg gets is
  computed from this number and the specific file's real duration inside
  `build_args` itself (`worker.target_size_to_bitrate_kbps`), using whatever
  audio bitrate is configured as an estimate of the audio track's share.
  That's necessarily an estimate: a copied (not transcoded) audio track's
  real bitrate isn't known without an extra probe this doesn't do, so the
  output lands close to the target size, not exactly on it. A small caption
  under the field shows the resulting kbps for whatever file is first in
  the queue, so the number being computed is never a total black box — "Add
  a file to estimate the resulting bitrate" if the queue's empty.
  Setting the *same* target size across a multi-selected batch of
  differently-long files (see "Selecting a row edits it live" below) is a
  feature, not a bug: each file independently aims for that size using its
  own duration, which is what you'd actually want encoding a season of
  episodes with mixed runtimes to a consistent output size.
- **Speed** — a slider from "Faster" to "More Thorough" for VAAPI
  (`-compression_level` 1–7 underneath, exact value on the slider's
  tooltip), or x265's own preset ladder (ultrafast…placebo) in a dropdown,
  whichever encoder applies. Deliberately kept as its own control rather
  than fused with Quality into a single dial — they're different axes (what
  quality/size to target, vs. how much effort to spend getting there), and
  fusing them would mean two controls fighting over the same stored value
  the moment both were shown at once. If you want one-click "good bundle
  for this scenario" behavior, that's what Presets are for.
- **Bit depth** — 8-bit or 10-bit (`main`/`nv12` vs `main10`/`p010le` for
  VAAPI; `yuv420p` vs `yuv420p10le` for x265), with a one-line caption under
  it on the actual tradeoff (smoother gradients/larger vs. smaller/most
  compatible) rather than expecting that to be obvious from the label alone.
- **Resolution** — `Source (no scale)` / `1080p` / `720p` / `480p`, fit
  within the box keeping aspect, never upscales. Both scale filters carry
  `force_divisible_by=2` — without it, a source whose aspect ratio doesn't
  exactly match the target box rounds one dimension to odd, which every
  pixel format this app uses (4:2:0) rejects outright. Verified against a
  real 2.4:1 "scope"-ratio source, not just reasoned about.
- **Container** — MP4 or MKV. `-movflags +faststart` is only added for MP4
  (it's a mov/mp4-muxer-private option — ffmpeg silently ignores it on MKV,
  but there's no reason to carry a flag that means nothing there).
- **Tune (x265 only)** — hidden when Encoder is VAAPI (`hevc_vaapi` has no
  equivalent option). Options: `animation`, `grain`, `psnr`, `ssim`,
  `fastdecode`, `zerolatency`, or `None` to omit `-tune` entirely. **`film`
  is deliberately not offered** — it's a real x265 tune name in general, but
  this exact libx265 build rejects it outright (`Error setting preset/tune
  (null)/film.`, confirmed by actually running it, not assumed).
- **Deinterlace** — auto-detected the moment a file is added (see
  "The queue itself" below), and still a manual checkbox on top of that.
  This exists because a container's progressive/interlaced flag is
  frequently just wrong: a real user file was tagged
  `yuv420p(progressive)` in its own metadata, played back with visible
  combing, and `ffmpeg -vf idet` on the actual pixel data showed 100% of
  sampled frames as TFF-interlaced — camcorder/broadcast-sourced footage
  (the `Mainconcept`-encoded file here is exactly that lineage) does this
  often enough that the flag can't be trusted. When on: VAAPI gets
  `deinterlace_vaapi=rate=frame` after `hwupload` and before `scale_vaapi`
  (operates on hardware surfaces, so order matters, and full-resolution
  fields deinterlace better than already-downscaled ones); x265 gets
  `bwdif=mode=send_frame` before `scale`. Both explicitly pin single-rate
  output — bwdif's own default (`send_field`) silently doubles the frame
  rate, one output frame per field, which isn't what a "just fix the
  interlacing" checkbox should do. Verified against a real interlaced
  fixture, not just argument presence: `tests/test_worker.py`'s
  `TestDeinterlace` builds a genuinely-interlaced synthetic source
  (`tinterlace=interleave_top`, confirmed 100% TFF via `idet`), encodes it
  through both paths, and checks the *output* is measured clean by the same
  detector — including a negative control proving the fix comes from the
  deinterlace filter and not incidentally from re-encoding.

**Audio tab**
- **Audio track** — `Track 1`–`4`, by stream index (not probed per file —
  keeps the tool from having to pre-scan the whole queue just to populate a
  dropdown).
- **Copy audio if compatible** — uncheck to always transcode, even for a
  codec that would normally be copied through.
- **Audio bitrate** — used only when a track gets transcoded.

**Below the tabs, left side**
- **Effective Command** — a live, read-only preview of the actual ffmpeg
  argv the current settings resolve to (`worker.build_args(...,
  probe_audio=False, ...)` — same function real jobs use, so the video-side
  flags can never drift from what actually runs). When a file is already
  queued, the audio side is genuinely accurate too — it probes that file's
  real audio track and duration (both cached per file, so dragging a
  slider doesn't shell out to ffprobe repeatedly) instead of guessing. With
  an empty queue there's no real track/duration to reflect, so those are
  omitted rather than asserting values that might be wrong. If building the
  preview fails (e.g. VAAPI selected on a machine with no Intel render
  node), it shows an inline message instead of taking the app down. Broken
  into a handful of lines (input / video encode / stream mapping /
  container flags, `_format_preview_text`) purely for readability — it's
  still the exact same argv underneath, just joined with newlines instead
  of spaces at the display step. **Collapsed by default** (a checkable
  `QGroupBox`, repurposed as a disclosure toggle rather than its usual
  enable/disable meaning — see `_make_collapsible_group`) since this is a
  technical double-check, not something the default view needs open; state
  persists across launches the same way window geometry does. A **Copy**
  button next to it puts the exact text on the clipboard.

The old hardware-status caption ("Hardware encode available via
/dev/dri/renderD129 (Intel iGPU)") now lives in the window's status bar —
a permanent widget in the bottom-right corner (`QMainWindow.statusBar()`,
`addPermanentWidget` specifically so nothing that later shows a temporary
status message can clobber it), qBittorrent-style, rather than competing
with the actual settings for space in the left column.

**Queue pane (right side)**
- **The queue itself** — drag files in from a file manager to add them, or
  drag existing rows to reorder them (`QAbstractItemView.InternalMove`;
  `DropListWidget` tells the two apart by whether the drag carries URLs).
  Shows placeholder text when empty instead of a blank box. Each added file
  immediately kicks off an async, non-blocking interlace probe
  (`worker.build_idet_args`/`parse_idet_output`, ~20s sample) and flips
  Deinterlace on the *individual file* on or off once it lands — a real
  override in both directions, not a one-way ratchet, so a progressive file
  added after an interlaced one doesn't inherit a stale "on." Each row also
  picks up a status icon once a run starts (▶ encoding, ✓ done, ⚠ failed —
  a failed row's tooltip holds the failure reason), and a finished row gets
  its result appended: `movie.mkv [...]  →  301.1MB (73% smaller)`.
- **Selecting a row edits it live** — every control above is only the
  *default* baked into a file the moment it's added. Select one or more
  queued rows and the controls populate from the first one; change any
  control from there and it applies to every selected row immediately —
  no separate "Apply" step. (`_on_queue_selection_changed` populates
  controls from a selection; `_sync_settings_to_selected_queue_items`,
  reached through `_on_control_changed`, pushes control changes back out.
  `_syncing_controls_from_selection` guards the loop between them — without
  it, merely *selecting* several differently-configured rows would
  silently homogenize them to the first one's settings before any control
  was even touched.) Add/Remove/Clear (and reordering, and selection-edits)
  are all disabled for the duration of a run (`_set_queue_editable`) —
  `TranscodeQueue.start()` snapshots the job list once, so editing the
  visible queue after Start can't affect what's actually running; it can
  only make the list lie about it, or (for selection-edits specifically)
  overwrite a finished row's now-historical settings.
- **Clear Queue** asks for confirmation first (skipped entirely if the
  queue is already empty) — it can discard real per-file setup, so it
  gets the same treatment Delete Preset already had.
- **Output folder** — deliberately *not* the first thing in the window; it's
  a per-run detail, so it sits right next to Start, where it's used.
- **Open** — opens the current output folder in the desktop file manager.
- **Live stats line** (under the progress bar) — fps / bitrate / speed /
  ETA for the job currently running, parsed from ffmpeg's `-progress`
  stream (`TranscodeQueue._emit_stats`). Separate from the full scrolling
  **Log** further down, which stays raw ffmpeg stderr — also collapsed by
  default now, same disclosure pattern and same reasoning as Effective
  Command above (a debugging aid, not default-view material). The queue
  list happily reclaims the freed space when it's collapsed, since it was
  already the only other stretch-factor widget sharing this column.

## Testing

```
python3 -m unittest discover -s tests -v
```

Plain stdlib `unittest`, no extra install (main.py's tests need
`QT_QPA_PLATFORM=offscreen` to run headless, but they set that themselves
before importing Qt, so the plain command above works with or without a
real display). Most `test_worker.py` tests check the argv `build_args()`
produces; several actually run ffmpeg against tiny synthetic clips (real
hardware encode included, skipped automatically if `/dev/dri/by-path`
doesn't exist) or drive a real `TranscodeQueue` end to end through a Qt
event loop — the whole point of this module is producing a command line
ffmpeg accepts and running real jobs correctly, and neither is something a
test that never calls ffmpeg/never starts a real QProcess can catch.
`test_main.py` covers GUI-level behavior that isn't `build_args`'
responsibility: startup ordering, the command preview's error handling,
audio accuracy and line grouping, the queue being locked during a run,
Clear Queue's confirmation, per-row status icons/result-size text, and the
preset-modified indicator. One gotcha if you're adding to it:
`QWidget.isVisible()` reflects the whole ancestor chain, not just a
widget's own `setVisible()` calls — it's always `False` until the
top-level window has been `.show()`n at least once, even under the
offscreen platform.

## Known gaps

- The File Size rate-control mode's kbps estimate (both the caption under
  the size field and the real `-b:v` value `build_args` computes) reserves
  whatever the Audio bitrate setting says for the audio track's share, even
  when that track is actually being *copied*, not transcoded -- the real
  copied bitrate isn't known without an extra ffprobe this doesn't do. Close
  enough for "land near this file size," not exact.
- A checkable `QGroupBox` used as a collapse toggle (`_make_collapsible_group`
  -- both Effective Command and Log use it) needs its size *policy*, not just
  its content's visibility, toggled on collapse: a hidden child alone still
  left the group claiming its full stretch-factor share of the layout,
  which looked like a large empty box where the Log used to be -- confirmed
  by screenshot before the size-policy fix went in. If a third collapsible
  section gets added later and looks like it's not actually shrinking, this
  is almost certainly why.
- `QWidget.isVisible()` is always `False` until the top-level window has had
  `.show()` called on it, regardless of the widget's own `setVisible()`
  state -- it reflects the whole ancestor chain, not just one widget. Tests
  that assert a control *is* visible need `window.show()` first (tests
  asserting it's hidden don't strictly need it, but the whole suite's
  existing convention is to call it anyway rather than have some tests rely
  on the distinction). Already flagged once in `test_main.py` itself
  (`TestPresetModifiedIndicator`); recorded here too since it bit three new
  tests in the same sitting that added the Rate Control buttons.
- `QSettings` round-trips a Python `bool` through its on-disk store as the
  literal string `"true"`/`"false"` (confirmed on this Linux/INI backend) --
  a plain `if value:` truthiness check on a restored value is a bug, since
  the *string* `"false"` is itself truthy. `_restore_window_state` compares
  `str(value) != "false"` for exactly this reason (window_geometry/
  splitter_state predate this and get away with it because `restoreGeometry`/
  `restoreState` take the raw QByteArray directly, never a bool).
- `style.qss` references its checkmark/arrow SVGs as `url($ASSETS/...)` —
  `$ASSETS` is a literal token, not real QSS syntax; `_load_stylesheet()` in
  main.py substitutes it for `assets/`'s absolute path before the text ever
  reaches `setStyleSheet()`. Loading `style.qss` any other way (a quick
  screenshot/test harness, say) leaves that token in place, which Qt's QSS
  parser rejects outright (`Could not parse application stylesheet`) rather
  than just failing to find the image — confirmed by hitting exactly that
  while testing this. Go through `_load_stylesheet()`, don't
  `setStyleSheet(open("style.qss").read())` directly.
- Touching a subcontrol in QSS at all (`::indicator`, `::up-button`, …)
  replaces Fusion's *entire* native paint for it, not just the property you
  set — there's no partial opt-in. `style.qss` already worked around this
  once, deliberately (see the comment above `QComboBox QAbstractItemView`
  about not touching `::drop-down`, to keep its native arrow glyph). The
  checkbox and spinbox rules didn't get the same treatment originally: a
  checked `QCheckBox` rendered as a flat colored square with no checkmark,
  and `QSpinBox`'s up/down buttons rendered close to invisible (Fusion's
  default arrow color, un-adjusted for dark mode since this app has no
  QPalette of its own, on a background this stylesheet also darkened) —
  both confirmed by screenshot, both now fixed via the SVGs in `assets/`.
  A data-URI `image: url(data:image/svg+xml;...)` was tried first instead
  of a real file — Qt's QSS parser can't reliably handle one inline
  (`Could not parse application stylesheet` again); a real file is the only
  approach confirmed to work here.
- Deinterlace auto-detect samples ~20s per file, not the whole thing — a
  file that's only partially interlaced (spliced from multiple sources)
  can be misjudged depending on which part gets sampled. Also: the sample
  is a real decode-only ffmpeg pass per file, so it costs some CPU even for
  files that turn out not to need it (async/non-blocking, so it doesn't
  freeze the UI, but it's not free).

  Testing note if you touch this: `ffmpeg -vf idet` itself false-positives
  on bare `testsrc2` test patterns (confirmed: 100% TFF on a genuinely
  progressive synthetic clip) — its high-frequency edges apparently read as
  combing to idet's heuristic. `tests/test_main.py`'s
  `_make_progressive_clip` works around this with a mild blur; don't swap
  in a bare `testsrc2` source for a "confirmed progressive" fixture without
  re-checking it against `idet` first.
- `-compression_level 1` not A/B'd against remembered QSV output quality —
  try the range (1–7, lower = slower/better) if output doesn't match
  expectations.
- No subtitle passthrough (explicitly `-sn`'d — originally to avoid an
  MP4-incompatible subtitle codec failing the mux; MKV output removes that
  specific risk but nothing maps subtitle streams on either container yet)
  and no foreign-audio-search/burn-in — the old "Stuff Tuned" HandBrake
  preset had both, neither is replicated.
- No batch folder-watch.
- Error recovery is per-job, not per-failure-class: a bad job (missing
  audio track that doesn't exist, output path colliding with the input,
  `probe_duration`/`ffprobe` failing) is caught and reported via
  `job_failed`, and the queue moves on to the next file — but there's no
  retry, and a systemic problem (e.g. ffmpeg itself missing) will just fail
  every remaining job in the queue one at a time rather than aborting the
  batch early.
