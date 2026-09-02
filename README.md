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

## VAAPI mid-stream reconfiguration crash

A real ~2-hour source file (`ffprobe` showed `color_range`/`color_space`/
`color_transfer`/`color_primaries` all `unknown` at the container level —
nothing overriding what the decoder infers from the bitstream itself as it
goes) failed a real VAAPI encode partway through, every time, at the exact
same timestamp:

```
[vf#0:0] Reconfiguring filter graph because video parameters changed to yuv420p(tv, bt709), 1920x1080, unspecified alph
Impossible to convert between the formats supported by the filter 'Parsed_scale_vaapi_2' and the filter 'auto_scale_1'
[vf#0:0] Error reinitializing filters!
```

What's actually happening: this specific file's encoded bitstream signals
slightly different color parameters partway through (no container-level
metadata around to keep that consistent), which is a real, valid thing for
ffmpeg's decoder to detect and triggers a genuine filter-graph
reconfiguration mid-job — not a bug in this app's own command line. The
crash is ffmpeg's *response* to that: its default behavior tries to
auto-insert a software scale filter (`auto_scale_1`, a filter this app
never asked for) to bridge what it thinks is a format mismatch, and that
auto-inserted filter can never actually work against a `vaapi` hardware
surface — plain software scale filters don't understand hardware frames at
all. The whole job dies on a source property that has nothing to do with
resolution, bit depth, or any setting actually exposed in this app's UI.

Fix: `-noautoscale` on the output, for `hevc_vaapi` jobs only
(`worker.build_args`). It disables ffmpeg's auto-insert entirely — this
app's own explicit `scale_vaapi` already does all the scaling that's
actually needed, so there was never anything for the auto-inserted filter
to usefully contribute in the first place. Confirmed against the real
file, not assumed: the exact same "Reconfiguring filter graph" line still
appears at the exact same timestamp (the underlying trigger is real and
unavoidable), but encoding now continues cleanly past it instead of
crashing, verified against a real output file all the way through. Scoped
to `is_vaapi` specifically, not applied to `libx265` too — there's no
vaapi surface in that pipeline for this exact failure to happen to, so
there's nothing to fix there, and carrying an untested behavior change
into a path that was never broken isn't worth the risk. See
`tests.TestBuildArgsVaapi.test_sets_noautoscale` /
`TestBuildArgsX265.test_no_noautoscale_for_cpu_encoder`.

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
               queue/progress/log on the right), DropTreeWidget
               (drag-and-drop, multi-column queue).
themes.py      Dark/Light color-token dicts (see Theming below); style.qss
               is otherwise theme-agnostic.
style.qss      Layout/structure for every widget, with $TOKEN color
               placeholders substituted by _load_stylesheet() in main.py
               against themes.py — see Theming below.
assets/        SVG glyphs style.qss paints on top of Fusion's native
               checkbox/spinbox subcontrols (see Known gaps for why), one
               set per theme (_dark/_light suffix).
tests/         unittest suite for constants.py/presets.py/worker.py/main.py.
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
    "gpu_vendor": "intel" | "amd" | None,  # only meaningful for hevc_vaapi -- see GPU device selection gotcha
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

Three built-in presets ship in `constants.BUILTIN_PRESETS`, in the same
CPU → Intel iGPU → AMD GPU order the Encoder dropdown itself uses (`Encoder`
in Controls below) — but that's *display* order, not load order: whichever
one is actually loaded on startup is pinned explicitly
(`MainWindow.__init__`'s `_refresh_preset_combo(select=...)`), independent
of where it sits in the list, so reordering this list alone can't silently
change what a fresh launch defaults to. Two are mapped from the user's
actual HandBrake custom presets
(`~/.var/app/fr.handbrake.ghb/config/ghb/presets.json`); all three are
protected — `Save As…` refuses to reuse their names, `Delete` refuses to
remove them — so there's always a known-good starting point.

1. **720p Stuff Tuned (CPU / x265)** — was already pure CPU x265, so this is
   a clean 1:1 mapping: `-preset medium -crf 23`, plus the original's
   `-x265-params "strong-intra-smoothing=0:aq-mode=3:psy-rdoq=1.0"` (fixed,
   not exposed as a control — nobody asked to tune it independently).
2. **720p QSV Balanced (Hardware / VAAPI)** — was `qsv_h265_10bit`, ICQ 26,
   main10. `-compression_level 1` (the vaapi "speed" value) is an estimate
   for QSV's "quality" preset, not validated by A/B — see Known gaps. This
   is the one that actually loads on startup, regardless of list position
   (see above) — this app exists to get real hardware encoding working
   again, so every launch should land there by default.
3. **720p AMD Balanced (Hardware / VAAPI)** — no HandBrake/QSV legacy to
   map from, unlike the Intel preset. CQP 26 mirrors the Intel preset's
   ICQ 26 (AMD's driver has no ICQ — see Controls' Rate control below —
   so CQP is the closest quality-family equivalent); speed "4" is a
   genuine middle-of-the-ladder value (the scale runs 1..7), matching what
   "Balanced" actually means in this app's own speed semantics rather than
   inheriting Intel's "1" (chosen there for HandBrake-mapping reasons that
   don't apply here).

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
- **Modified indicator** — the preset dropdown's own text goes bold,
  italic, and `$ACCENT`-colored the moment any control drifts from the
  loaded preset's saved values, and back to normal if you change it back.
  Originally a separate "(modified)" label next to the dropdown — its
  appearing/disappearing took and gave back layout space every time,
  visibly reflowing the whole window on every single control change
  (confirmed by screenshot). Recoloring the combo's own text needs no
  space of its own: a dynamic Qt property (`preset_combo.setProperty(
  "modified", bool)` + `QComboBox[modified="true"]` in `style.qss`,
  `_update_preset_modified_indicator` in main.py) rather than a widget
  that comes and goes.

The rest is grouped into two tabs, by what kind of setting they are. Four
iterations on the tab bar's look, in order: a plain underline (no fill, no
outline) read as floating clickable text, nothing marking it as an actual
tab shape; a filled `$BG_PANEL` selected-tab plus a full outline box read
as too heavy a highlight; uniform `$BG_WINDOW` on both states (outline
only, no fill at all) fixed *that*, but a selected tab colored like the
window instead of like its own content didn't visually belong to the pane
it was selecting either; filling the selected tab with `$BG_PANEL` *and*
setting its border-color to match (next attempt, to blend it into the
pane) went a step too far — that killed the border's visibility on all
four sides at once, when only the bottom edge (the one touching the pane)
was ever supposed to disappear. Settled on: `border-bottom: none` on the
base rule handles the seamless-with-content edge on its own, regardless of
selected state; the other three sides keep a real, visible color the
whole time — `$ACCENT` for the selected tab (marking it "active" the same
way this app already uses accent color elsewhere), `$BORDER` for
unselected. `QTabBar::tab` also carries a small `margin-top` — without it,
the tab's own top border/corner rendered clipped against the top of its
allocated space (invisible back when that border was transparent, obvious
once it had a real color — confirmed by screenshot). One more knock-on
effect worth knowing about if this is touched again: with the selected
tab's border invisible on all sides (the "one step too far" version
above), the content pane's own top-left corner radius rendered as a
stray straight line with no visible curve at all right where it met the
tab — not a separate bug, just that corner having nothing of its own
covering that exact pixel area once the tab stopped drawing one there.
Fixed as a side effect of restoring the tab's own visible corner, not
touched separately.

**Video tab** (grouped into "Encoding" and "Format")

The controls here deliberately lead with plain English, not ffmpeg's own
names for things — a "Apple-style" simplification pass over what used to be
five separate rate-control modes and a raw `-compression_level` readout. The
underlying settings dict and `worker.build_args` are completely unaffected;
this is presentation only. See `constants.RC_MODE_FRIENDLY` for the mapping.

- **Encoder** — "CPU" (`libx265`), "Intel (iGPU)", or "AMD (GPU)" (the
  latter two both `hevc_vaapi`, distinguished by the `gpu_vendor`
  settings-dict key — this machine has both a real Intel iGPU and an AMD
  discrete GPU), in that order. No NVIDIA option — no such hardware here,
  and there's no NVENC code in `worker.py` to back one.
  `constants.ENCODERS` is a list of `(ffmpeg codec, gpu_vendor, label)`
  triples, in this same CPU/Intel/AMD order, specifically so one ffmpeg
  codec (`hevc_vaapi`) can back two distinct menu entries;
  `constants.encoder_profile_key()` turns a resolved `(encoder,
  gpu_vendor)` pair back into the key `RC_MODES`/`RC_MODE_FRIENDLY` are
  keyed by (`"hevc_vaapi_intel"`, `"hevc_vaapi_amd"`, or plain
  `"libx265"`).
- **Rate control** — a three-button row, not a dropdown: **Quality** / **File
  Size** / **Advanced**. Quality and File Size mean the same thing regardless
  of encoder (mapped to ICQ/VBR for Intel VAAPI, CQP/VBR for AMD VAAPI,
  CRF/bitrate for x265); Advanced is CQP (fixed quantizer). **AMD's VAAPI
  driver (Mesa radeonsi) rejects ICQ outright** — confirmed via both a real
  `vainfo` capability listing and a real failed test encode, not assumed —
  so on AMD, Quality *is* CQP and the Advanced button has nothing further to
  offer and hides entirely, same treatment x265 already got for lacking CQP.
  The three are really just three positions of `rc_mode_combo`, still the
  actual source of truth for everything downstream — the combo itself stays
  alive but hidden (`main.py`'s `_set_rc_mode` / `_sync_rc_buttons_to_combo`)
  rather than being replaced, so there's exactly one place rc_mode can drift
  out of sync with what the UI shows.
- **Quality** (the slider, when Rate control is Quality or Advanced) — range
  follows the specific mode (e.g. ICQ 1–51 vs CRF 0–51 aren't the same
  scale, so this re-ranges itself on every encoder/rc_mode change). The raw
  number and mode name (e.g. "26 (ICQ)") show as small secondary text next
  to the slider, not the primary label. `setInvertedAppearance`/
  `setInvertedControls` flip the slider so **left is worse/smaller, right is
  better/larger** — matching every rc_mode's underlying scale direction is
  the opposite of intuitive otherwise (e.g. ICQ/CRF: a *lower* number means
  *better* quality). A fuzzy tier label underneath ("Movies & TV — a solid
  general-purpose target", etc., centered under the slider) names what the
  current position is actually good for, and a hover tooltip spells out
  which end is which, so the raw number was never the only thing to go on
  (the quality slider's tooltip is static; the speed slider's below
  updates live with the actual compression_level, since that's meaningful
  context the quality slider's number already shows on-screen anyway). The
  handle itself is `$ACCENT`, not `$TEXT_PRIMARY` -- TEXT_PRIMARY reads
  fine as a near-white dot on Dark but rendered as a heavy black one on
  Light, with nothing about the accent-blue groove to suggest that's what
  would happen -- confirmed by screenshot in Light specifically, not
  assumed from the token values alone. The slider and its fuzzy tier
  caption underneath share one outlined box (`style.qss`'s
  `QWidget#fuzzyGroup`, also used by Speed's identical slider+caption pair
  below) rather than reading as two independent, unrelated form rows —
  the caption explains *that specific slider*, so grouping it visually
  with the control it's actually describing reads better than leaving
  that relationship implicit.
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
  (`-compression_level` 1–7 underneath, exact value on the tooltip), or
  x265's own preset ladder (ultrafast…placebo) in a dropdown, whichever
  encoder applies. Deliberately kept as its own control rather than fused
  with Quality into a single dial — they're different axes (what
  quality/size to target, vs. how much effort to spend getting there), and
  fusing them would mean two controls fighting over the same stored value
  the moment both were shown at once. If you want one-click "good bundle
  for this scenario" behavior, that's what Presets are for. Same
  inverted-appearance treatment as Quality above (**Faster** left, **More
  Thorough** right) — not just a guess at which end feels right: timed real
  encodes at compression_level 1/4/7 confirmed lower values are genuinely
  both slower *and* more size-efficient at a fixed quality target, so a
  lower number belongs on the "more effort" side, not the "faster" side a
  raw ffmpeg option list would suggest. A fuzzy tier label under the slider
  ("Thorough — best efficiency, worth it for archival masters", etc.),
  sharing Quality's `#fuzzyGroup` outlined box (see above), and a tooltip
  explain the tradeoff the same way Quality's do. Hiding that caption for
  x265's dropdown is plain `setVisible` now, not `setRowVisible` — the
  latter was needed back when it was the sole widget on its own dedicated
  `QFormLayout` row (`setVisible` alone left that row's own spacing
  reserved, a residual gap under Speed specifically that Intel/AMD didn't
  have); now that it's nested inside `#fuzzyGroup`'s plain `QVBoxLayout`
  instead, that layout already collapses a hidden child's space correctly
  on its own, without `QFormLayout`'s row-specific quirk to work around.
- **Bit depth** — 8-bit or 10-bit (`main`/`nv12` vs `main10`/`p010le` for
  VAAPI; `yuv420p` vs `yuv420p10le` for x265). The tradeoff is folded
  straight into each dropdown item's own text ("10-bit — smoother
  gradients, larger file" / "8-bit — smaller, maximum compatibility")
  rather than a separate caption row underneath — one fewer line, and the
  explanation is attached to the specific option it's about instead of
  floating below whichever one happens to be selected.
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
  persists across launches the same way window geometry does. A **▸/▾**
  glyph appended to the title text (not a separate button) shows which way
  it'll go next — an earlier version tried a dedicated header button/arrow
  instead, but that changed the section title's layout relative to every
  other (non-collapsible) section title in the app, which broke the
  design-consistency the rest of the UI relies on; the native
  `QGroupBox` title bar's own click-to-toggle hit region already covers the
  whole title, arrow glyph included, confirmed via a direct `QTest.
  mouseClick`. A **Copy** button puts the exact text on the clipboard --
  below the command text, not above it, and visibly smaller than a normal
  button: it's a power-user convenience for a section that's already
  collapsed by default, and doesn't need Start/preset-button-level visual
  weight.

The old hardware-status caption ("Hardware encode available via
/dev/dri/renderD129 (Intel iGPU)") now lives in the window's status bar —
a permanent widget in the bottom-right corner (`QMainWindow.statusBar()`,
`addPermanentWidget` specifically so nothing that later shows a temporary
status message can clobber it), qBittorrent-style, rather than competing
with the actual settings for space in the left column.

**Queue pane (right side)**
- **The queue itself** — a `DropTreeWidget` (flat `QTreeWidget`, no actual
  hierarchy): a real multi-column grid with a header bar, not a single-line
  list. Columns are deliberately **source-file properties only** — File /
  Video (codec + resolution, e.g. "HEVC 3840x2160") / Duration / Audio
  (codec + channel layout, e.g. "AAC 5.1") / Size / Result (populated only
  once that row finishes encoding, e.g. "301.1MB (73% smaller)"). The
  chosen *output* settings (encoder, quality, container, …) are
  deliberately **not** repeated here — they already live in, and edit live
  from, the right-hand settings panel for whichever row is selected (see
  "Selecting a row edits it live" below), so a second copy in the grid
  would just be the same information twice. `_make_queue_row` fills in
  File/Size synchronously (no I/O beyond a `stat()`); Video/Duration/Audio
  arrive from an async `ffprobe` metadata probe once it lands (see below).
  Drag files in from a file manager to add them, or drag existing rows to
  reorder them (`QAbstractItemView.InternalMove`; `DropTreeWidget` tells
  the two apart by whether the drag carries URLs) — reordering relies on
  Qt's own row-move handling for a flat item-per-row model, the same
  mechanism `QListWidget` used before this became a table, deliberately
  *not* `QTableWidget`, whose internal-move is cell-based rather than
  row-based and a known rough edge for exactly this kind of drag-reorder.
  Shows placeholder text when empty instead of a blank box. Every column
  but Result (see below), File included, is Interactive/user-resizable by
  dragging its header divider — initial widths are hand-tuned to this
  app's own real panel
  width rather than Qt's generic per-column default, sized to each
  column's actual content, but they're starting points, not fixed. File
  was originally `QHeaderView.Stretch` (auto-claims leftover space) --
  looked reasonable, but Stretch sections silently refuse to be
  drag-resized at all, with no visual indication why; switched to
  Interactive with an explicit initial width once that turned out to
  matter more in practice than the auto-fit convenience did. Result, the
  *last* column, is the one deliberate exception:
  `header().setStretchLastSection(True)` makes it claim whatever's left
  over on the right instead of leaving a bare gap between it and the
  panel's edge on a wide enough window. Trading away Result's own
  manual-resizability for that is an easy call, unlike it was for File —
  "612.3MB (71% smaller)"-style text doesn't vary anywhere near as much in
  length as a filename does.
- **Source metadata probe** — each added file immediately kicks off an
  async, non-blocking `ffprobe -show_entries format=duration:stream=...`
  call (`worker.build_probe_args`/`parse_probe_output`, header-only, no
  decoding — fast even for a large file) that fills in the Video/Duration/
  Audio columns once it lands. Runs alongside the existing interlace probe
  below rather than instead of it — both append to the same
  `_detection_processes` list, so anything that already waited on that list
  (tests included) transparently waits for both. The Video cell's text
  depends on *both* async results (the probe's codec+resolution label and
  the interlace detector's `deinterlace` flag, appended as "(interlaced)")
  and they can land in either order — `_refresh_video_cell` recomputes the
  full cell text from both pieces of stashed state every time either one
  arrives, rather than concatenating piecemeal, so the result is correct
  regardless of which finishes first.
- **Interlace probe** — kicks off the same moment as the source-metadata
  probe above and flips Deinterlace on the *individual file* on or off once
  it lands (`worker.build_idet_args`/`parse_idet_output`, ~20s sample) — a
  real override in both directions, not a one-way ratchet, so a progressive
  file added after an interlaced one doesn't inherit a stale "on." Each row
  also picks up a status icon once a run starts (▶ encoding, ✓ done, ⚠
  failed — a failed row's tooltip holds the failure reason, replacing the
  File cell's usual path tooltip specifically, since that's exactly where
  the eye already goes to see why). The icon lives *on* the File cell
  itself (`QTreeWidgetItem` supports an icon and text on the same column
  at once) rather than a dedicated status column of its own — a separate
  narrow column was tried first, as unobtrusive as it could reasonably be
  made (24px, blank header), but an empty column with nothing in every row
  until a run actually starts still read as a stray gap rather than a
  deliberate part of the design (confirmed by feedback, not just a
  guess). `STATUS_COL` still exists as a name in main.py — an alias for
  `FILE_COL` — purely so call sites that touch the icon/failure-tooltip
  stay self-explanatory about *why* they're touching that column.
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
  was even touched.) Remove/Clear (and reordering, and selection-edits) are
  all disabled for the duration of a run (`_set_queue_editable`) —
  `TranscodeQueue.start()` snapshots the job list once, so editing the
  visible queue after Start can't affect what's actually running; it can
  only make the list lie about it, or (for selection-edits specifically)
  overwrite a finished row's now-historical settings. **Add Files is
  deliberately exempt** — see next.
- **Adding files mid-run joins the run in progress** — dropping a file in
  (or clicking Add Files) while a run is already going doesn't just sit
  there waiting for a second click of Start; `add_files` pushes it straight
  into `TranscodeQueue` (`queue.add_job`) and it runs once its turn comes
  up. This needed its own patch path (`TranscodeQueue.update_pending_job`)
  rather than sharing a live reference with the visible row, because
  `QTreeWidgetItem.setData`/`.data()` round-trips a **copy** of the job
  dict, confirmed empirically — the running queue's snapshot, taken at add
  time, wouldn't otherwise see the async interlace-detection result land
  moments later.
- **Clear Queue** asks for confirmation first (skipped entirely if the
  queue is already empty) — it can discard real per-file setup, so it
  gets the same treatment Delete Preset already had.
- **Output folder** — deliberately *not* the first thing in the window; it's
  a per-run detail, so it sits right next to Start, where it's used. The
  field is directly editable, not just settable via **Change…**'s browse
  dialog — typing a path doesn't check it exists (`editingFinished` just
  updates `self.output_dir`), the same as a browsed-to path already
  didn't; both get created on demand (`mkdir(parents=True, exist_ok=True)`)
  right before they're actually needed, at Start or Open.
- **Open** — opens the current output folder in the desktop file manager.
- **Live stats line** (under the progress bar) — fps / bitrate / speed /
  ETA for the job currently running, parsed from ffmpeg's `-progress`
  stream (`TranscodeQueue._emit_stats`). Separate from the full scrolling
  **Log** further down, which stays raw ffmpeg stderr — also collapsed by
  default now, same disclosure pattern and same reasoning as Effective
  Command above (a debugging aid, not default-view material). The queue
  list happily reclaims the freed space when it's collapsed, since it was
  already the only other stretch-factor widget sharing this column.

## Theming

**A "Theme:" combo in the status-bar footer**: Dark / Light / Match System,
at the left edge, separate from the hardware-status text over on the right.
Originally a View menu, moved here after using both live — a whole menu bar
for one three-item setting was more chrome than the setting warranted.
`QStatusBar` genuinely has two different widget areas, not just one row
with a visual left/right split: `addWidget` (theme picker) puts something
on the left — also where a `showMessage()` temporary message would appear,
which specifically hides `addWidget` widgets for its duration, though
nothing in this app calls `showMessage()` today — and `addPermanentWidget`
(hardware status) puts something on the right, immune to that. The footer
strip's own `setContentsMargins(8, 4, 8, 4)` gives its content some
vertical breathing room — tried as a QSS `padding` rule on `QStatusBar`
first, which turned out not to reach `addWidget`/`addPermanentWidget`
content at all (confirmed by screenshot: zero visible difference); Qt's
own contents-margins property isn't mediated by the stylesheet the same
way and reliably does work. The choice
persists across launches (`QSettings` key
`theme_choice`) and, for Match System, keeps following the OS live via
`QApplication.instance().styleHints().colorSchemeChanged` (Qt 6.5+) — no
restart needed if the desktop's own theme flips while this app is open.
`_resolve_theme(choice)` turns "system" into a concrete "dark"/"light" at
the moment it's needed (`Qt.ColorScheme.Light` → light, anything else,
including `Unknown`, → dark); `_validate_theme_choice` guards whatever
`QSettings` hands back on startup, falling back to "dark" for `None`, an
empty string, a stale/typo'd value, or the wrong case ("Dark" ≠ "dark") —
a corrupted or hand-edited config file degrades to the default theme, not
a crash.

Colors and structure are deliberately in two separate files:

- **`themes.py`** — `DARK`/`LIGHT` dicts, one color per token
  (`BG_WINDOW`, `TEXT_PRIMARY`, `ACCENT`, …) plus three `*_ICON` tokens per
  theme naming that theme's own checkmark/arrow SVG in `assets/`. Hand-tuned
  per theme, not a mechanical inversion of one another — a color that reads
  fine on a dark background often doesn't just because its lightness got
  flipped. `ACCENT` is the clearest example: a lighter blue on Dark (reads
  as *text*, e.g. slider values, against near-black), a deliberately deeper
  one on Light (needs to hold its own as text against white, where the
  Dark theme's lighter shade would wash out) — same for hover directions,
  which flip (lighter-on-hover for Dark, darker-on-hover for Light).
- **`style.qss`** — every other rule (layout, padding, radius, borders-as-
  structure), with `$TOKEN` placeholders standing in for colors.
  `_load_stylesheet()` in main.py substitutes every `$TOKEN` against the
  chosen theme's dict before the text ever reaches `setStyleSheet()` —
  **tokens are substituted longest-name-first**
  (`sorted(palette, key=len, reverse=True)`), because a couple of these are
  literal prefixes of others (`$BG_CONTROL` / `$BG_CONTROL_HOVER` /
  `$BG_CONTROL_PRESSED`, similarly for `$ACCENT_*` and `$BORDER_*`) —
  substituting the short one first would consume the start of the longer
  token's name too, corrupting it before its own turn came up.

**Icons set from Python don't get this for free.** `$TOKEN` substitution
only ever touches `style.qss` text, so it covers every icon reached via a
QSS `image: url(...)` rule (the checkbox indicator, spinbox arrows, the
combobox drop-down arrow) — but a `QPushButton`'s icon is a `QIcon` object
built once in Python via `.setIcon(...)`, which a later `setStyleSheet()`
call has no way to reach or refresh. Save As/Delete used
`style().standardIcon(...)` originally, which sidestepped this (Qt redraws
those from the palette automatically) but had its own problem instead —
see Known gaps. The fix for both: `_themed_icon(name)` builds a `QIcon`
from `assets/{name}_{dark|light}.svg` for whichever theme is current, and
`_refresh_themed_icons()` re-calls it for every such button, wired into
both `_apply_theme` and `_on_system_theme_changed` alongside the stylesheet
reload — so switching themes updates these the same moment it updates
everything QSS-driven, not on the next restart.

Switching themes swaps the color set only; nothing about layout, spacing,
or which controls exist changes. Verified with real screenshots of both
themes side by side, not just by reading the substitution logic — dark and
light both keep the header bar / gridlines / alternating-row structure the
queue table above relies on, just recolored.

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
audio accuracy and line grouping, the queue being locked during a run
(except Add Files, which stays live), live mid-run queue append, Clear
Queue's confirmation, per-row status icons/result-size text, the queue
table's source-metadata probe, theming, and the preset-modified indicator.
One gotcha if you're adding to it:
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
- `style.qss` is not valid QSS on its own — every color, plus the
  checkmark/arrow SVG paths (`$CHECK_ICON`, `$ARROW_UP_ICON`,
  `$ARROW_DOWN_ICON`), is a literal `$TOKEN` placeholder that only becomes
  real QSS once `_load_stylesheet()` in main.py substitutes it against a
  `themes.py` palette (see Theming above). Loading `style.qss` any other
  way (a quick screenshot/test harness, say) leaves those tokens in place,
  which Qt's QSS parser rejects outright (`Could not parse application
  stylesheet`) rather than just failing to find the image — confirmed by
  hitting exactly that while testing this. Go through `_load_stylesheet()`,
  don't `setStyleSheet(open("style.qss").read())` directly. Relatedly:
  `tests.TestStylesheetLoading.test_every_token_gets_substituted` asserts
  no bare `$` survives a real load, which is why style.qss's own header
  comment has to describe this mechanism in prose without ever writing a
  literal `$WORD` — the substitution is a blind, global `str.replace`
  across the whole file, comments included.
- Touching a subcontrol in QSS at all (`::indicator`, `::up-button`, …)
  replaces Fusion's *entire* native paint for it, not just the property you
  set — there's no partial opt-in. Three subcontrols hit this in practice,
  all fixed the same way (a themed background/border + an SVG from
  `assets/` for the glyph, one file per theme since a `$TOKEN` can supply a
  path but not recolor an already-rasterized image): a checked `QCheckBox`
  rendered as a flat colored square with no checkmark; `QSpinBox`'s
  up/down buttons rendered close to invisible (Fusion's default arrow
  color, un-adjusted for either theme since this app has no `QPalette` of
  its own); `QComboBox`'s drop-down button was originally left alone
  *specifically to avoid this* (see git history) — the native Fusion
  button it kept was a bigger problem once everything around it was
  themed, standing out as generic against a fully flat/themed app, so it
  got the same background+SVG treatment after all. All three confirmed by
  screenshot before and after, not assumed from the token values alone. A
  data-URI `image: url(data:image/svg+xml;...)` was tried first instead of
  a real file for the checkmark — Qt's QSS parser can't reliably handle
  one inline (`Could not parse application stylesheet`); a real file is
  the only approach confirmed to work here.
- `QGroupBox`'s `margin-top` has to comfortably clear the title text's own
  rendered height, not just roughly match it -- at 14px (this app's
  original value) the title sat straddling the border line instead of
  above it, the native/default GroupBox look where a rounded corner
  visibly cuts through the title text. Reads as dated once the rest of
  the app is flat and themed (confirmed by screenshot, not assumed); 22px
  clears it with room to spare. If a group's title ever looks like it's
  touching or overlapping the box border again, this is almost certainly
  why -- it's font-size-dependent, so a larger title font later would need
  more than 22px, not the same value assumed to still be enough. A related
  mistake on the very first pass at this fix: nudging `::title`'s `top`
  negative to tighten the gap between the (now-cleared) title and the
  border below it seemed reasonable, but a negative `top` offset moves the
  title *above the QGroupBox's own bounding box*, not just up within the
  margin area already reserved for it -- it got clipped by whatever's
  above the group in the layout instead (confirmed by screenshot). `top`
  should stay `0` (or positive) once `margin-top` is already sized
  correctly; there's no need to reach for a negative one at all. Left
  alignment had a similar rough edge -- `left: 12px` plus this rule's own
  `padding: 0 6px` put the title a good ~18px in from the box's actual
  left edge, more than the 6px `padding` alone would suggest and more than
  every other field/label in this app indents by; `left: 0` (padding
  still supplies the 6px) fixes it.
- The "no `QPalette` of its own" issue above isn't just a QSS-subcontrol
  thing — `QPushButton(style().standardIcon(...), ...)` (Save As/Delete's
  icons, until this was hit) draws from the same un-set palette, and
  `SP_TrashIcon` specifically rendered with poor contrast against a
  Light-theme button (confirmed by screenshot; `SP_DialogSaveButton` looked
  fine, so this isn't uniform across every standard icon). Fixed by
  replacing both with real theme-aware SVGs instead, same as everything
  else in `assets/` — see Theming's `_themed_icon`/`_refresh_themed_icons`
  for why a `QIcon` built once in Python needs its own explicit refresh
  hook that a `$TOKEN`-driven QSS icon doesn't.
- Any plain `QWidget` with no background rule of its own used to fall
  through to a top-level `QWidget { background-color: $BG_WINDOW; }` rule
  -- painting $BG_WINDOW behind it regardless of what it was actually
  sitting on. First noticed on `QLabel`/`QCheckBox`/`QSlider` specifically
  (every label and the Deinterlace checkbox sitting in a visibly
  mismatched grey box against a `QGroupBox`'s `$BG_PANEL`), fixed there,
  then noticed *again* around Effective Command's Copy button -- the bare
  `QWidget()` holding the command text box + Copy button is exactly the
  same class of bug, just a layout-only container this time instead of a
  visible control. Two confirmed instances of the same root cause was the
  signal to fix the actual cause instead of patching each widget type as
  it turned up: the top-level rule is `background-color: transparent` now,
  and only the specific widgets that truly need an opaque background of
  their own (`QMainWindow`/`QSplitter`, `QGroupBox`, form fields,
  `QPushButton`, `QTreeWidget`, ...) carry an explicit rule, which still
  wins over the default regardless of what it is. Dark's
  `BG_WINDOW`/`BG_PANEL` are close enough (#1a1d23/#21252c) that none of
  this was ever visible there; Light's aren't (#eef0f3/#ffffff), which is
  what actually exposed a bug latent since before Light theme existed —
  confirmed by screenshot each time, not assumed from the token values.
  Third instance (a fourth follows below), and the one that finally
  explains why transparent-by-default isn't unconditionally safe: every
  `QComboBox` dropdown rendered
  with a solid black bar top and bottom of the list. A combobox's popup
  isn't a nested child widget the way everything else in this app is --
  Qt wraps the list (`QComboBox QAbstractItemView`, already styled) in its
  own `QFrame`, and that `QFrame` *is* the popup's own top-level window
  (confirmed directly: `view.window()` is the same object as
  `view.parentWidget()`). Transparent is exactly the right default for a
  nested child -- show whatever's already opaquely painted behind it --
  but wrong for a genuinely top-level window, which has nothing behind it
  to show through to; Qt/the platform renders that as black rather than
  as invisible. Fixed with an explicit `QFrame { background-color:
  $BG_PANEL; border: none; }` -- except `QLabel` is *also* a `QFrame`
  subclass (confirmed via `issubclass()`, a fact easy to not know), so
  without an explicit `QLabel { background-color: transparent; }`
  override afterward (more specific than the bare `QFrame` rule, so it
  wins regardless of which one appears first in the file), that same fix
  would have silently reintroduced the exact grey-label bug two entries
  up. Caught before it shipped, not after, by checking the class
  hierarchy directly instead of assuming `QFrame` only meant "the
  combobox popup thing." This one couldn't be confirmed by screenshot the
  usual way either -- the `offscreen` QPA platform this test suite runs
  under doesn't do real window compositing, so a popup grabbed under it
  never reproduces the black bars regardless of whether the fix is
  correct; the `view.window() is view.parentWidget()` structural check
  above is the real evidence this fix targets the right widget, verified
  a different way than usual because the usual way doesn't apply here.
  Fourth instance, same root cause again: `QMessageBox` (Clear Queue's
  confirmation, Delete Preset's, `Save As…`'s Overwrite? prompt) is
  *also* a genuinely top-level window (`QMessageBox` → `QDialog` →
  `QWidget`, confirmed via `issubclass()` — never a nested child the way
  the rest of this app's plain `QWidget`s are), reported directly with a
  solid black dialog body, same as the combobox popup and not
  reproducible under `offscreen` for the same reason. `QInputDialog`
  (`Save Preset`) and `QFileDialog` (`Add Files…`/`Change…`, though the
  latter may use the native OS picker instead of Qt's own rendering) are
  `QDialog` subclasses too, confirmed the same way — fixed by targeting
  `QDialog` itself rather than `QMessageBox` alone, after first
  confirming (same `issubclass()` check that caught the `QLabel`/`QFrame`
  overlap above) that nothing already styled elsewhere in this file is
  secretly a `QDialog` too.
- Deinterlace auto-detect samples ~20s per file, not the whole thing — a
  file that's only partially interlaced (spliced from multiple sources)
  can be misjudged depending on which part gets sampled. Also: the sample
  is a real decode-only ffmpeg pass per file, so it costs some CPU even for
  files that turn out not to need it (async/non-blocking, so it doesn't
  freeze the UI, but it's not free). Every added file now also kicks off a
  second, separate `ffprobe` process for the queue table's source-property
  columns (see Queue pane above) — header-only and fast, but it's still a
  second subprocess per file, on top of the interlace sample.

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
- Drag-to-reorder the queue (`QAbstractItemView.InternalMove`) can't be
  driven through an automated headless test the way everything else in
  this app is — Qt's internal-move drag-drop goes through a real native
  `QDrag`/platform drag session, which the `offscreen` QPA platform this
  suite runs under doesn't implement; synthetic `QTest` mouse press/move/
  release events don't trigger it (confirmed by trying). Structurally this
  should be sound — a flat `QTreeWidget`'s row-move is the same shape as
  `QListWidget`'s (one item per row; deliberately not `QTableWidget`'s
  cell-based model, see Queue pane above), and reordering worked the same
  way before this became a table — but it's the one piece of this feature
  that's only had a real interactive check, not an automated one.
- Error recovery is per-job, not per-failure-class: a bad job (missing
  audio track that doesn't exist, output path colliding with the input,
  `probe_duration`/`ffprobe` failing) is caught and reported via
  `job_failed`, and the queue moves on to the next file — but there's no
  retry, and a systemic problem (e.g. ffmpeg itself missing) will just fail
  every remaining job in the queue one at a time rather than aborting the
  batch early.
