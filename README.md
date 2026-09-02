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
    "audio_downmix_stereo": bool,  # forces a stereo mixdown, only when the source has >2 channels
}
```

## Presets

Nine built-in presets ship in `constants.BUILTIN_PRESETS`: a High/Balanced/
Low trio per engine, in the same CPU → Intel iGPU → AMD GPU order the
Encoder dropdown itself uses (`Encoder` in Controls below) — but that's
*display* order, not load order: whichever one is actually loaded on
startup is pinned explicitly (`MainWindow.__init__`'s
`_refresh_preset_combo(select=...)`), independent of where it sits in the
list, so reordering this list alone can't silently change what a fresh
launch defaults to. All nine are protected — `Save As…` refuses to reuse
their names, `Delete` refuses to remove them — so there's always a
known-good starting point.

Each engine's three differ only in `quality_value` (moved to a
meaningfully different point on that engine's own ~50-value ICQ/CQP/CRF
scale — the same scale the Quality slider's own 6-tier fuzzy captions
divide up) and, for CPU, nothing else at all: speed stays whatever that
engine's Balanced preset already uses, since the tier these three move
along is quality/size, not effort-vs-time (a separate axis Balanced
already settled for engine-specific reasons of its own, see below).

**Balanced**, the original three, described first since they're what the
High/Low siblings are relative to:

1. **720p CPU Balanced (Software / x265)** — named to match the other two
   built-ins ("Balanced", `<category> / <encoder>`); its original name
   ("Stuff Tuned") was carried over verbatim from the user's own real
   HandBrake preset of that name during the port and never revisited. Was
   already pure CPU x265, so this is a clean 1:1 mapping: `-preset medium
   -crf 23`, plus the original's
   `-x265-params "strong-intra-smoothing=0:aq-mode=3:psy-rdoq=1.0"` (fixed,
   not exposed as a control — nobody asked to tune it independently).
2. **720p Intel Balanced (Hardware / VAAPI)** — named "Intel", not "QSV" (Quick
   Sync's own technology name), to match "AMD" and "CPU" on either side of it
   in this same list — both already bare vendor/type words, not a brand or
   technology name, so QSV was the one actually out of step, not something
   this app invented fresh. Was `qsv_h265_10bit`, ICQ 26,
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

**High** and **Low**, added later to give each engine a real tier instead
of a single fixed point:

- CPU's CRF 18 (High) and 28 (Low) are real, widely-used x265 community
  reference points ("visually lossless" and "noticeably smaller, still
  watchable") — not this app's own guess, x265's CRF scale has enough
  established practice around it to just use those directly.
- Intel/AMD's ICQ/CQP 16 (High) and 36 (Low) don't have that same body of
  outside practice to draw on, so they're this app's own estimate by rough
  analogy to the CPU pair's offset from its own Balanced (23) — not
  independently validated against remembered output quality any more than
  Intel's Balanced `-compression_level` estimate above was. Worth A/B'ing
  for real at some point, same as that one.

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
- **Speed** — a slider from "Faster" to "More Thorough" for *either* engine
  now (`-compression_level` 1–7 for VAAPI, x265's own preset ladder
  ultrafast…placebo for CPU), whichever encoder applies — two separate
  sliders (`speed_slider`/`speed_x265_slider`) sharing one row and one set
  of "Faster"/"More Thorough"/fuzzy-tier-caption labels, only one of each
  slider pair actually visible at a time. Originally a dropdown for x265
  specifically (ten named presets is a lot of menu to scan), converted to
  match VAAPI's slider once VAAPI already had one and the inconsistency
  became obvious sitting right next to it. Deliberately kept as its own
  control rather than fused with Quality into a single dial — they're
  different axes (what quality/size to target, vs. how much effort to
  spend getting there), and fusing them would mean two controls fighting
  over the same stored value the moment both were shown at once. If you
  want one-click "good bundle for this scenario" behavior, that's what
  Presets are for.

  VAAPI's slider uses the same inverted-appearance treatment as Quality
  above (**Faster** left, **More Thorough** right) — not just a guess at
  which end feels right: timed real encodes at compression_level 1/4/7
  confirmed lower values are genuinely both slower *and* more
  size-efficient at a fixed quality target, so a lower number belongs on
  the "more effort" side, not the "faster" side a raw ffmpeg option list
  would suggest. x265's slider needs no inversion at all: `X265_PRESETS`
  is already ordered fastest-to-slowest (`ultrafast`…`placebo`), so index 0
  landing on the visual left is already correct without flipping anything
  — an index into that list, not a value with arithmetic meaning of its
  own, same reasoning as Audio Bitrate's slider below. A shared fuzzy tier
  label under whichever slider is showing ("Thorough — best efficiency,
  worth it for archival masters", etc., same 3 captions either way, just
  opposite fraction direction since the two underlying scales run opposite
  ways) sits in the same `#fuzzyGroup` outlined box as Quality above, and
  is unconditionally visible now — there's always a real slider to caption
  regardless of which encoder is selected, so it no longer needs to hide
  for one of them the way it did back when x265 only had a plain dropdown.
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
- **Audio bitrate** — used only when a track gets transcoded. A slider over
  the 5 real stops (96k/128k/160k/192k/256k), same visual language as
  Quality/Speed on the Video tab (outlined box, fuzzy caption underneath) —
  originally a plain dropdown, converted for consistency with those two.
  An index into the fixed list, not the kbps number itself: the real values
  aren't evenly spaced (96→128→160→192 are +32 each, 192→256 is +64), which
  a linear slider can't represent as uniform tick spacing without either
  lying about the middle stops or leaving the last one oddly cramped.
- **Downmix to stereo** — mixes 5.1/7.1/etc. sources down to plain stereo,
  for playback on a phone/laptop/anything without a surround setup. Only
  actually does anything when the source genuinely has more than 2
  channels (`worker.probe_audio_channels`, only ever probed when this is
  checked at all -- most jobs never touch it) — checking it on a source
  that's already stereo or mono has no effect, matching the control's own
  label. A stream copy can't remix channels, so on a source that does need
  it, checking this forces a transcode even when "Copy audio if
  compatible" would otherwise have applied — the box always means what it
  says, not "usually, unless copy already claimed the track first." `-ac 2`
  (libswresample's own remix), not a hand-written `pan` filter with fixed
  5.1-shaped coefficients — that would mis-handle anything that isn't
  exactly that layout (7.1, quad, ...), where `-ac 2` remixes correctly
  from whatever the source's real layout turns out to be.

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
  stay self-explanatory about *why* they're touching that column. The
  ▶/✓/⚠ glyphs are custom SVGs now (`assets/status_play_*`/`status_done_*`/
  `status_warning_*`), not `style().standardIcon(...)` — an Apple-design-
  language pass's "one icon family, one weight, throughout" moved these
  onto the same custom family Save/Delete already used, rather than the
  other way around: Save/Delete switched *away* from `standardIcon()`
  earlier specifically because `SP_TrashIcon` had a confirmed contrast bug
  (see Known gaps), so reverting them back to get a "native" icon set
  would have reintroduced that bug just to avoid drawing two more SVGs.
  Done is green, Warning is red — color reinforcing the shape, not
  carrying the meaning alone (the accessibility baseline this whole pass
  was checked against).
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
way and reliably does work. `QStatusBar` also carries its own
`background-color: $BG_CONTROL` and a `border-top` now (an Apple-design-
language pass's "chrome should read as a distinct layer from content"
principle — it had no background of its own before, so it just blended
into `$BG_WINDOW` instead of reading as its own strip). The choice
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

- **`themes.py`** — `DARK`/`LIGHT` dicts, one color per token (`BG_WINDOW`,
  `TEXT_PRIMARY`, …) plus three `*_ICON` tokens per theme naming that
  theme's own checkmark/arrow SVG in `assets/`. Hand-tuned per theme, not a
  mechanical inversion of one another — a color that reads fine on a dark
  background often doesn't just because its lightness got flipped; hover
  directions flip too (lighter-on-hover for Dark, darker-on-hover for
  Light). **`ACCENT`/`ACCENT_HOVER`/`ACCENT_PRESSED`/`TEXT_ON_ACCENT` are
  the one exception** — `themes.py`'s own values for these four are only
  ever a fallback now, not what actually ships. `main.py`'s
  `_system_accent_tokens()` overrides all four at stylesheet-load time
  from the desktop's own accent color (`QPalette.Accent`, Qt 6.6+;
  `QPalette.Highlight` on older Qt) — an Apple-design-language pass's "one
  accent color, used consistently" read as "the *user's* accent," not a
  blue this app picked for them; confirmed on a real KDE session that
  `QPalette.Accent` already resolves to that session's actual configured
  color (`#308cc6`), not a generic Fusion default. Hover/pressed are
  `QColor.lighter()`/`.darker()` of that same color; `TEXT_ON_ACCENT` is
  picked from the accent's own perceived luminance (white text below a
  0.5 threshold, near-black above) rather than assumed, since a user's
  chosen accent could be any hue or lightness, not just the blue this app
  shipped with before. Falls back to that previous fixed blue only if the
  palette role comes back invalid or pure black (a real desktop session's
  accent is never actually black, so that's a reliable "nothing resolved"
  signal). Because the resolved accent no longer varies by theme the way
  every other token still does, `TEXT_ON_ACCENT` doesn't either now — it's
  a property of the *accent*, not of Dark vs. Light, which is the
  self-consistent outcome once the accent itself stopped being
  theme-specific. `BG_ACCENT_DISABLED`/`TEXT_ACCENT_DISABLED` (Start
  button, disabled) are deliberately left theme-fixed, not derived the
  same way — a muted echo of an arbitrary accent hue is a harder thing to
  get right by formula than the four tokens above, and wasn't what
  "follow the system accent" was actually asking for. `CHECK_ICON` rides
  along with this derivation too, and turned out to need to: the checkmark
  drawn inside a checked `QCheckBox` sits directly on the accent fill,
  exactly like button text does, so it needs `TEXT_ON_ACCENT`'s own
  contrast decision, not whichever of `check_dark.svg`/`check_light.svg`
  happened to match the *old*, theme-fixed accent. This was a real, live
  bug this change introduced and then caught in the same pass, not a
  hypothetical: once the accent stopped varying by theme, `TEXT_ON_ACCENT`
  correctly switched to the same color for both Dark and Light (it's a
  property of the accent now, not of the theme) — but `CHECK_ICON`'s file
  selection was still keyed off theme name specifically, so Dark's
  checkbox ended up with a near-black check sitting on the exact same blue
  fill Start's white text was on. Confirmed by screenshot before and
  after, not assumed.
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

**Keyboard focus indicators.** Buttons, checkboxes, the Speed/Quality
sliders, the queue table, and the tab bar had no focus indicator of their
own at all — confirmed by screenshot: Tab-ing to any of them showed no
visible change whatsoever, only the text fields (which already had a
`:focus` border-color rule) showed anything. Not a style preference —
someone navigating by keyboard alone needs to be able to see where focus
actually is, on every focusable control. Fixed with `outline: 2px solid
$ACCENT; outline-offset: 1px;` rather than reusing the fields' `border-
color` swap — `outline` draws outside a widget's own box without changing
its layout size, which matters here since several of these (the segmented
Rate Control buttons, sliders) already have exact-fit borders/radii a
border-*width* change would visibly disrupt. One real edge case this
turned up: an `$ACCENT`-colored ring is invisible on a control that's
already `$ACCENT`-filled — confirmed by screenshot, Tab-ing to the
already-selected Quality button (or to Start, always accent-filled while
enabled) showed no visible focus change even with the new rule in place,
while the same rule worked cleanly everywhere else. Fixed with a second,
more specific rule swapping just `outline-color` to `$TEXT_ON_ACCENT` for
`#startButton` and the segmented buttons' `:checked:focus` state — the
color this app already picks for exactly this contrast problem elsewhere
(button text sitting on an accent fill), not a new one invented just for
this.

That `outline: 2px solid $ACCENT` rule above was written against plain
`:focus`, which -- reported directly, confirmed by screenshot -- means a
checkbox clicked with the mouse gets the exact same ring Tab-ing to it
does. QSS has no `:focus-visible` equivalent (the CSS feature this is
really asking for: show the ring for keyboard navigation, not a pointer
click that already knows where it landed). `QFocusEvent.reason()` is the
same distinction `:focus-visible`'s own heuristic is standing in for --
`Qt.TabFocusReason`/`BacktabFocusReason` for real keyboard navigation,
`MouseFocusReason` for a click, plus a handful of others
(`ActiveWindowFocusReason`, `PopupFocusReason`, ...) that aren't keyboard
navigation either. `main.py`'s `_FocusVisibleFilter` -- a second
`QApplication`-level event filter alongside `_ComboPopupBackgroundFilter`
above, kept separate rather than merged since the two handle unrelated
concerns -- watches `FocusIn`/`FocusOut` app-wide and mirrors that
distinction onto a `focusVisible` dynamic property; every `:focus`
selector this applies to became `[focusVisible="true"]` instead
(`QTabBar::tab:focus` stayed a real `:focus` — a subcontrol isn't its own
`QObject`, so it can't carry a dynamic property the way a `QWidget` can).
Applied to every focusable widget app-wide rather than a specific type
list: a property no QSS rule references is a harmless no-op, so there's
nothing to gain scoping it down. One real bug this turned up before it
shipped: `FocusIn`/`FocusOut` also reach plain `QWindow` objects (a
top-level window gaining/losing OS-level focus, not any widget inside
it), which have no `.style()` — crashed the filter the first real run
against the actual X11 display until guarded with an `isinstance(obj,
QWidget)` check, a case the offscreen test suite's synthetic
`setFocus()` calls never happened to exercise.

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

## Reliability fixes

A round of external review (two independent passes against this codebase)
turned up several real, confirmed bugs in the batch-transcoding path itself
— not just UI polish. Each was verified directly (a real repro against
real ffmpeg, or a real `QProcess`) before being fixed, not just patched on
the strength of the report alone:

- **A missing/broken ffmpeg install left the whole queue stuck forever,
  silently.** `TranscodeQueue` only connected `QProcess.finished` --
  confirmed directly against a real nonexistent binary that `finished`
  genuinely never fires when a process fails to even start, only
  `errorOccurred` does (a crash still reaches `finished` too, a crash is a
  way of finishing; only `FailedToStart` skips it entirely). No job_failed,
  no all_finished, nothing in the log, nothing to click -- just permanently
  "running." Now connects `errorOccurred` too, filtered to `FailedToStart`
  specifically so every other error kind still goes through the existing
  `finished`-based path unchanged.
- **Two source files with the same stem (different folders) silently
  overwrote each other's output**, and **a failed or stopped job could
  destroy a pre-existing file at its output path.** `-y` used to write
  straight to the final name -- confirmed directly with the exact reported
  scenario (`folderA/shot01.mov` + `folderB/shot01.mkv`, both wanting
  `shot01.mp4`) that the second job's completed output silently replaced
  the first's. Fixed two ways together: output paths are now disambiguated
  within a run (and against whatever's already on disk) as `name.ext`,
  `name (2).ext`, ... before a job starts, matching how most file managers
  already resolve the same kind of collision; and ffmpeg now writes to a
  hidden temp name during the encode, renamed onto the real name only after
  a confirmed successful exit -- so a failed/stopped job's cleanup only
  ever deletes its own temp file, never anything that was already there.
  (The refuse-if-output-equals-input guard from before is unchanged and
  still checked first, against the un-disambiguated name specifically --
  otherwise the disambiguation logic would have "solved" that case by
  quietly picking a different name instead of refusing outright, which is
  the wrong fix for a source-file-safety guard.)
- **A File Size target too small for the file's length silently produced
  an arbitrary-quality encode instead of erroring.** `target_size_to_
  bitrate_kbps` returning 0 flowed straight into `-b:v 0k` -- confirmed
  directly against real ffmpeg/libx265 that this doesn't error, it makes
  x265 silently fall back to its own default CRF (28.0), with no actual
  relationship to the size that was requested. `build_args` now raises
  instead, which the two real callers (the queue, the live command
  preview) already had exception handling for; the size-estimate label
  shows the same message before the user ever gets that far.
- **The "modified" preset indicator misfired on all three CPU presets.**
  `_current_settings()` always includes `gpu_vendor` (`None` for a
  non-VAAPI encoder), but the CPU presets in `constants.py` never define
  that key at all -- confirmed directly that a plain `!=` comparison
  treats a dict missing a key as different from one where it's explicitly
  `None`, so selecting any CPU preset showed "modified" immediately with
  nothing actually changed. Fixed with a key-by-key comparison that treats
  "absent" and "explicitly `None`" as equivalent, robust against any future
  settings key with the same shape, not just this one field.
- **An auto-detected deinterlace race, in both directions.** Adding a file
  and clicking Start immediately could begin encoding before the ~20s
  interlace sample landed, using whichever deinterlace value the file
  started with -- Start now refuses (with a status message) while any
  sample is still in flight. Separately, manually toggling Deinterlace for
  an already-queued, selected file could get silently reverted when that
  file's detection result landed moments later -- a new per-job
  `deinterlace_user_set` flag, stamped only by a genuine user edit to an
  already-queued item (not by selecting a different item, and not by the
  detector's own result), makes a manual override stick.
- **Downmix to stereo didn't check the source's actual channel count.**
  Documented under Controls above -- forced an unnecessary transcode on an
  already-stereo source, and would have upmixed a mono one, the opposite of
  what "downmix" means.
- **Dragging to reorder the queue wasn't actually blocked during a run**,
  despite Remove/Clear already being locked for exactly the same reason
  (none of the three can affect a job already running or finished without
  the visible list lying about what's actually executing). `DropTreeWidget`
  now refuses an internal-move drop while a run is in progress, while still
  accepting external file drops the same as before.
- **The Copy button under Effective Command wasn't reliably paste-safe.**
  It copied the *display* text -- grouped onto several lines, plain-space-
  joined with no shell quoting -- so a path containing a space
  (`/media/My Video.mov`) pasted as two separate shell arguments instead of
  one. It now copies `shlex.join()` over the real argv this preview was
  actually built from instead, confirmed to round-trip exactly through
  `shlex.split()`.
- **Selecting an audio track that doesn't exist on the source silently
  produced video-only output.** `build_args` already handled this
  correctly (skips mapping a stream that isn't there rather than failing
  the whole job) -- the gap was that nothing told the user their output
  would have no audio until they noticed on playback. The real queue now
  logs a note when this happens; the command-building logic itself is
  unchanged.

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
  as invisible. First fixed with an explicit `QFrame { background-color:
  $BG_PANEL; border: none; }` -- except `QLabel` is *also* a `QFrame`
  subclass (confirmed via `issubclass()`, a fact easy to not know), so
  without an explicit `QLabel { background-color: transparent; }`
  override afterward (more specific than the bare `QFrame` rule, so it
  wins regardless of which one appears first in the file), that same fix
  would have silently reintroduced the exact grey-label bug two entries
  up. Caught before it shipped, not after, by checking the class
  hierarchy directly instead of assuming `QFrame` only meant "the
  combobox popup thing." Couldn't be confirmed by screenshot the usual
  way either -- the `offscreen` QPA platform this test suite runs under
  doesn't do real window compositing, so a popup grabbed under it never
  reproduces the black bars regardless of whether the fix is correct --
  so this shipped on the strength of the `view.window() is
  view.parentWidget()` structural check alone, without a real render to
  confirm it.

  That structural reasoning was sound and the `QFrame` rule is still in
  style.qss, but a later real screen capture (`QScreen.grabWindow()` /
  `spectacle` against the actual X11 display, not `QWidget.grab()` --
  confirmed separately that `.grab()` reads a widget's own paint buffer
  and misses real compositor output, making it useless for verifying
  *this specific* class of bug) proved the `QFrame` rule alone doesn't
  actually reach this popup: the bars were still there. The real cause
  turned up inspecting the popup frame directly -- `autoFillBackground`
  is `False` and `frameShape` is `NoFrame`, so nothing paints its
  background by ordinary `QWidget` means, and for reasons that didn't
  surface from the widget's own properties, the app-wide QSS cascade
  doesn't repaint it the way it does the `QAbstractItemView` nested
  inside it (confirmed that one's background *does* apply correctly).
  What does work, also confirmed via real capture: a stylesheet assigned
  directly on the frame instance. `main.py`'s `_ComboPopupBackgroundFilter`
  -- a `QApplication`-level event filter -- does exactly that at `Show`
  time, matched via `metaObject().className() == "QComboBoxPrivateContainer"`
  rather than `isinstance`/`type().__name__`: PySide6 has no Python
  binding for this private Qt class, so its Python-visible type reports
  as the nearest exposed base (`QFrame`) -- indistinguishable that way
  from every other `QFrame` in the app, where `metaObject().className()`
  still reports Qt's real C++ class name regardless of Python bindings.

  The same real capture surfaced a second, separate bug riding along on
  the same popups: `QComboBox[modified="true"]`'s italic/`$ACCENT`-colored
  styling (the "you've changed something since loading this preset"
  indicator, see below) was bleeding into that combo's own popup list
  items too -- every preset name shown italic and accent-colored, not
  just the closed combo's own text. Not a cascade problem, and not fixed
  by anything targeting the view or the frame -- confirmed by resetting
  font/color directly on both, immediately and deferred by an event-loop
  tick, with zero effect. What actually stopped it: temporarily clearing
  the `modified` property on the combo box *itself* while its popup is
  open (restored on `Hide`, so the closed combo still shows its own
  indicator correctly afterward). That only makes sense if the popup's
  item delegate paints using the owning combo's own currently-matched QSS
  state directly rather than anything inherited or copied onto the popup
  widgets -- confirmed indirectly, by process of elimination, rather than
  by reading Qt's own source for it. `_ComboPopupBackgroundFilter` does
  both fixes together, keyed off the same `Show`/`Hide` pair.
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
  second subprocess per file, on top of the interlace sample. Neither has a
  concurrency cap — dropping a large batch (a season of episodes) launches
  that many of each at once. Fine for a handful of files; a small queue/
  semaphore would be worth adding before this gets used on 30+ files at a
  time.
- `_preview_duration`/`_preview_audio_codec`/`_preview_audio_channels`
  (the live command-preview and size-estimate probes, `_update_command_
  preview`/`_update_size_estimate_label`) run real, synchronous `ffprobe`
  calls directly on the GUI thread — cached per file so it only actually
  shells out once, but that first call still blocks the whole window
  (Qt's own 30s subprocess timeout is the upper bound) if it lands on a
  slow network mount or a file ffprobe struggles with. A probe failure no
  longer crashes the app (see Reliability fixes above), but a *slow* one
  still freezes it for however long it takes. Making these genuinely async
  (QProcess-based, matching the interlace/source-property probes above)
  would fix this properly; not done here since it's a larger structural
  change than the correctness fixes in this pass.

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
- Intel/AMD's High/Low preset ICQ/CQP values (16/36) are this app's own
  estimate by analogy to CPU's real x265 community reference points, not
  independently A/B'd against real output either — see Presets above.
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
- Error recovery is per-job, not per-failure-class: a bad job (an
  already-existing output name, `probe_duration`/`ffprobe` failing, ffmpeg
  itself missing -- see Reliability fixes above, this specific case used
  to hang the queue rather than fail cleanly, fixed now) is caught and
  reported via `job_failed`, and the queue moves on to the next file — but
  there's no retry, and a systemic problem (ffmpeg missing, a full disk)
  will still fail every remaining job in the queue one at a time rather
  than detecting the pattern and aborting the batch early.
