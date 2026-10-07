# VeloCoder architecture

How VeloCoder is built and why: the source layout, the ffmpeg/VAAPI
findings behind the engine, theming, testing and known gaps. For using
the app, see [GUIDE.md](GUIDE.md).

## Source layout

```
src/velocoder/
  __init__.py          APP_NAME / APP_VERSION / APP_ORGANIZATION (the one
                       place for them; core/constants.py re-exports them).
                       See constants.py for why APP_ORGANIZATION never
                       reaches QSettings/QStandardPaths.
  __main__.py          `python -m velocoder`.
  core/                Qt-free logic (tests/test_layout.py enforces it).
    constants.py       Encoders, rate-control modes, resolutions, quality
                       tiers, audio bitrates.
    ffmpeg.py          The engine: hardware detection (a real one-frame
                       encode per GPU), probing, interlace detection, the
                       settings dict -> ffmpeg argv (build_args), and a
                       failure's reason. No preset concept here, by design.
    formatting.py      Display-formatting helpers (codec/channel names,
                       size/ETA strings, fuzzy-tier captions).
    help_content.py    Loads help/index.json + help/*.md, plain-text
                       search and a small markdown-subset renderer.
    presets.py         load_builtin_presets() -- builtin_presets.json is
                       now test-fixture data only (the app has no Presets
                       feature).
  ui/                  The Qt application.
    main_window.py     MainWindow's core (settings <-> control sync, theme
                       handlers) and main(), the `velocoder` entry point.
                       Most behaviour lives in the two mixins below.
    ui_builder.py      _UiBuilderMixin: widget construction for every tab,
                       the panels, the collapsible group, the command preview.
    queue_controller.py _QueueControllerMixin: queue add/probe/run, the file
                       and output pickers, the TranscodeQueue signal handlers.
    queue_widget.py    DropTreeWidget (drag-and-drop queue table, column
                       fitting, focus ring) and the queue's column constants.
    transcode_queue.py TranscodeQueue: runs one ffmpeg job at a time via
                       QProcess and reports progress/results as signals.
    session.py         Cross-session queue/output-folder persistence (a small
                       JSON file under QStandardPaths.AppDataLocation).
    theming.py         Stylesheet loading/token substitution, system-accent
                       derivation, the QSS-gap event filters.
    themes.py          Dark/Light colour tokens (from odcs-ui) plus
                       VeloCoder's icon tokens.
    help_window.py     The non-modal, searchable Help window.
    about_dialogs.py   About / System Information / Licenses.
    assets/            style.qss (layout and structure with $TOKEN colour
                       placeholders) and the SVG glyphs, one set per theme.
    _vendor/odcs_ui/   The shared ODCS design system, bundled at a pinned
                       release (scripts/sync-odcs-ui.sh; never hand-edited).
  help/                Help content: index.json + one *.md per topic, images/.
data/                  The desktop entry (named by the app ID).
scripts/               run_tests_chunked.py, sync-odcs-ui.sh,
                       install-desktop-entry.sh.
tests/                 unittest suite: one file per module where the module
                       is small (test_ffmpeg, test_presets, test_session,
                       test_help_content, test_about_dialogs, test_themes,
                       test_vendor, test_layout); test_main.py drives the
                       whole assembled GUI through one real MainWindow.
docs/                  This file, GUIDE.md, screenshots/, CONSUMER_FORK_PLAN.md.
```


The GUI is a fixed-width (456px) settings inspector on the left, wrapped
in a `QScrollArea` (a vertical scrollbar appears only if Expert's content
ever exceeds the window height -- none under normal conditions), and a
flexible workspace on the right, laid out with a plain `QHBoxLayout` (not
a resizable `QSplitter` — window resizing goes entirely to the right
pane). Left pane: a `QTabWidget` (**Video** / **Audio** — see Controls
below for the current Normal/Expert breakdown of each). Right pane: the
queue, output folder, run controls, progress bar, a live stats line, and
the full log. Window geometry (and Expert's own expanded/collapsed state)
is remembered across launches via `QSettings(APP_NAME, APP_NAME)` (on
Linux: `~/.config/VeloCoder/VeloCoder.conf`).

The unfinished queue (path + per-video settings, in order) and the output
folder also survive a restart — a separate mechanism from the `QSettings`
scalars above, since this is a structured, growing list rather than a
handful of preferences: see `session.py` for the small JSON file
(`session.json`, under `QStandardPaths.AppDataLocation`) and
`queue_controller.py`'s `_schedule_session_save`/`_restore_session` for
when it's written (debounced, after every real queue/output-folder
change, and force-flushed on close) and read (once at startup, right
after `_restore_window_state`). Already-completed videos are never
persisted; a video that was still `Converting…`, `Failed`, or plain
`Ready` when the app closed comes back next launch as a fresh `Ready`
row — this app never attempts partial ffmpeg resume, so there's nothing
else to restore it *to*. A restored job's hardware selection is
sanitized through the same `_effective_current_settings` machinery any
other real job boundary uses, in case the machine's available GPUs
changed since the session was saved. A path that no longer resolves to a
real file (moved, deleted, or on a drive that isn't connected) is
silently dropped, with a one-line status notice if anything was actually
lost this way.

The settings dict that flows from the GUI into `build_args()`:

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
    "deinterlace": bool,     # bwdif (x265) or deinterlace_vaapi (VAAPI), see Deinterlace in GUIDE.md
    "audio_track": int,      # 0-based
    "audio_copy_if_compatible": bool,
    "audio_bitrate": str,    # e.g. "160k", used only when transcoding audio
    "audio_downmix_stereo": bool,  # forces a stereo mixdown, only when the source has >2 channels
}
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
args; it isn't available for HEVC here and will fail. (`tests/test_ffmpeg.py`
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
  ever a fallback now, not what actually ships. `main_window.py`'s
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
  `_load_stylesheet()` in main_window.py substitutes every `$TOKEN` against the
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

**Keyboard focus indicators.** Every focusable control shows where keyboard
focus is. Fields swap their border to `$ACCENT`. Buttons draw their own 2px
border (`$ACCENT`, or `$TEXT_ON_ACCENT` on an accent fill such as Convert or a
checked segment), with 1px less padding so the button keeps its size and the
label doesn't move. An earlier version used `outline: 2px solid $ACCENT`, but Qt
draws a QSS outline as a focus rect *inside* the button, around the label,
where it touched the glyphs (UI finish-gate review, 2026-10-07). The queue
draws one 2px ring around the whole current row (`DropTreeWidget.drawRow`)
instead of a box around a single cell. Checkboxes, sliders and tabs still use
the outline.

The focus rules were first written against plain
`:focus`, which -- reported directly, confirmed by screenshot -- means a
checkbox clicked with the mouse gets the exact same ring Tab-ing to it
does. QSS has no `:focus-visible` equivalent (the CSS feature this is
really asking for: show the ring for keyboard navigation, not a pointer
click that already knows where it landed). `QFocusEvent.reason()` is the
same distinction `:focus-visible`'s own heuristic is standing in for --
`Qt.TabFocusReason`/`BacktabFocusReason` for real keyboard navigation,
`MouseFocusReason` for a click, plus a handful of others
(`ActiveWindowFocusReason`, `PopupFocusReason`, ...) that aren't keyboard
navigation either. `main_window.py`'s `_FocusVisibleFilter` -- a second
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
python3 scripts/run_tests_chunked.py
```

Plain stdlib `unittest` underneath, no extra install — but run through
`run_tests_chunked.py` rather than `python3 -m unittest discover -s tests -v`
directly, because a single long-lived process running the whole suite
accumulates Qt/PySide6 resources across the many `MainWindow()` instances
`test_main.py` constructs: an unchunked full run was confirmed to still not
be done after 4.5 hours (189 of 387 tests, RSS climbing throughout).
`run_tests_chunked.py` tears the process down every 20 tests instead, and
the same 387 tests pass in about 6 minutes. CI (`.github/workflows/tests.yml`)
uses it too. For running a single file, class, or test during development,
plain `unittest` is still fine and doesn't hit this — e.g.
`python3 -m unittest tests.test_ffmpeg.TestTune -v` (main_window.py's tests need
`QT_QPA_PLATFORM=offscreen` to run headless, but they set that themselves
before importing Qt, so it works with or without a real display). Most
`test_ffmpeg.py` tests check the argv `build_args()`
produces; several actually run ffmpeg against tiny synthetic clips (real
hardware encode included, skipped automatically if `/dev/dri/by-path`
doesn't exist) or drive a real `TranscodeQueue` end to end through a Qt
event loop — the whole point of this module is producing a command line
ffmpeg accepts and running real jobs correctly, and neither is something a
test that never calls ffmpeg/never starts a real QProcess can catch.
`test_main.py` covers GUI-level behavior that isn't `build_args`'
responsibility: startup ordering, the command preview's error handling,
audio accuracy and line grouping, the queue being locked during a run
(except Add Videos, which stays live), live mid-run queue append, Clear
Queue's confirmation, per-row status icons/result-size text, the queue
table's source-metadata probe, theming, and the Normal/Expert control
hierarchy (codec-switch state preservation, the Audio Track constraint,
Expert's collapse/expand behavior). One gotcha if you're adding to it:
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
  Documented under Controls in GUIDE.md -- forced an unnecessary transcode on an
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
  on the distinction). Has bitten real tests more than once in this
  file's own history -- recorded here so the next one doesn't have to
  re-discover it.
- `QSettings` round-trips a Python `bool` through its on-disk store as the
  literal string `"true"`/`"false"` (confirmed on this Linux/INI backend) --
  a plain `if value:` truthiness check on a restored value is a bug, since
  the *string* `"false"` is itself truthy. `_restore_window_state` compares
  `str(value) != "false"` for exactly this reason (window_geometry predates
  this and gets away with it because `restoreGeometry` takes the raw
  QByteArray directly, never a bool).
- `style.qss` is not valid QSS on its own — every color, plus the
  checkmark/arrow SVG paths (`$CHECK_ICON`, `$ARROW_UP_ICON`,
  `$ARROW_DOWN_ICON`), is a literal `$TOKEN` placeholder that only becomes
  real QSS once `_load_stylesheet()` in main_window.py substitutes it against a
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
  their own (`QMainWindow`, `QGroupBox`, form fields,
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
  directly on the frame instance. `main_window.py`'s `_ComboPopupBackgroundFilter`
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
  columns (see Queue pane in GUIDE.md) — header-only and fast, but it's still a
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
- Intel/AMD's Smaller/Better Quality-tier ICQ/CQP values (16/36,
  `constants.QUALITY_TIERS`) are this app's own estimate by analogy to
  CPU's real x265 community reference points, not independently A/B'd
  against real output either.
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
  cell-based model, see Queue pane in GUIDE.md), and reordering worked the same
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
