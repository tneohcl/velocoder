# TITAN Video — Consumer Fork Plan

Status: **v1 scope revised and built.** This folder is a fork of
`titan-video` (cloned 2026-09-05, pushed to its own repo,
`tneohcl/titan-video-consumer`, private). The original v1 pass
subtracted down to a near-minimal control set (Quality tier + Resolution
+ Audio choice, everything else cut); after using it, the positioning
itself was revisited -- see "v1 positioning" and "v1 scope" below for
the actual, current decision. The positioning questions were answered
with pragmatic defaults rather than left open,
specifically so work has somewhere concrete to start -- revisit any of
them before Phase 4 (packaging) if they turn out wrong, they're cheap to
change this early and expensive to change after distribution.

## v1 positioning (revised)

- **Name:** keep "TITAN Video" for now. A studio-brand name on a
  consumer app is a real mismatch long-term, but inventing a new brand
  isn't a v1-blocking decision -- renaming a fork before its first real
  release costs nothing.
- **Price:** free for v1. No payment infrastructure to build, no App
  Store review economics to plan around yet. Revisit once there's an
  actual user asking for a paid tier's specific feature.
- **Platform:** Linux first (this is the only machine anything can
  actually be tested on right now). macOS/Windows packaging is real,
  separate work per platform -- explicitly deferred, not silently
  assumed to be the same effort.
- **Target user, one sentence (revised):** the app's own primary user
  first -- someone who understands codec/container/bitrate vocabulary
  and wants a professional transcoding utility, not a from-scratch
  reimplementation of the specialist build's own audience. The original
  "someone who's never heard the word codec" framing drove the first
  subtraction pass too far in one direction; the real gap turned out to
  be a level of depth in between that specialist build and a fully
  minimal one, not a ground-up rebuild for total newcomers. Concretely:
  Normal mode now surfaces the meaningful *output/media* decisions a
  knowledgeable user actually wants (Processing, Codec, Quality-vs-
  Size, Resolution, File Format, Color Depth, and every real Audio
  setting) while Expert still exists for genuine encoder-mechanics
  precision (exact CRF/ICQ/CQP, raw Rate Control mode, Encoding Speed,
  Tune, Force Deinterlace) -- see the "v1 scope" table below for the
  full item-by-item breakdown.

## Why fork instead of adding a "simple mode" toggle

TITAN Video, as it stands, was independently assessed as landing around
**"professional specialist desktop application"** — not amateurish, but
also not a mass-market consumer app, on purpose: explicit CPU/Intel/AMD
processing choice, an Expert section exposing codec/CRF/bit depth/tuning,
and live encode telemetry (fps, bitrate, speed multiplier) are exactly
the kind of detail a specialist tool should show and a consumer app
shouldn't.

A single codebase trying to be both tends to converge on one of two bad
outcomes: the specialist tool gets dumbed down to protect a "simple mode"
toggle, or the "simple mode" toggle becomes a thin coat of paint over
still-visible complexity. A fork keeps the specialist build exactly as
opinionated as it is now, and lets the consumer build make its own calls
about what to remove entirely rather than just hide.

The transcoding engine (`worker.py`'s ffmpeg command construction,
hardware-encoder detection, the queue/job model) is the part that should
stay shared/ported between both, not reinvented per fork — the fork is
primarily a **UI/UX and packaging** exercise, not a new backend.

## v1 scope (revised -- current)

The first pass (see git history) cut almost everything down to Quality
tier + Resolution + a single Audio choice. After using it, that read as
*too* minimal for the app's real target user (see "v1 positioning"
above) -- the fix wasn't to re-add every specialist control, but to
re-promote specifically the ones that are genuine *output/media*
decisions, while keeping true encoder-mechanics precision in Expert.
The organizing rule: **Normal = meaningful output/media decisions a
knowledgeable user understands. Expert = encoder mechanics and
precision overrides.**

| Item | Current (specialist) | v1 decision (revised) |
|---|---|---|
| Processing (Automatic/CPU/Intel/AMD) | Always visible | **Restored to Normal** -- a real output decision (speed/quality tradeoff, which hardware runs the encode), not encoder-internal minutiae |
| Codec (H.265/H.264) | Expert-only | **Promoted to Normal**, next to Processing -- a real playback-compatibility decision; disabled + forced to H.265 whenever Processing picks a hardware engine (VAAPI is HEVC-only here) |
| Compatibility (Modern/Most Compatible) | Visible toggle | **Cut** -- once Codec is a direct, visible H.265/H.264 choice, Compatibility would just be a second control describing the same decision |
| Mode (Quality vs. File Size) | Only reachable via Expert's 3-way Rate Control row | **New, Normal-only 2-way toggle** (Quality/File Size, no Advanced) over the same shared rc_mode_combo Expert's own row drives -- picks which of the next two rows shows |
| Quality tier (Smaller File/Balanced/Better Quality) | Already plain language | **Kept**, shown only when Mode = Quality |
| Target Size (MB) | Expert-only (shared row with the exact-quality slider) | **Promoted to Normal**, shown only when Mode = File Size -- moved out of Expert entirely, not duplicated: there's no more-precise form of "how big should the file be" than a plain MB number (a raw kbps control is deliberately never exposed anywhere) |
| Resolution dropdown | Keep Original/720p/1080p/etc. | **Kept**, unchanged |
| File Format (container) | Expert-only | **Promoted to Normal** -- a real output characteristic (MP4 vs. MKV), not encoder-internal |
| Color Depth (bit depth) | Expert-only, "10-bit -- smoother gradients, larger file" | **Promoted to Normal**, item text trimmed to just the tradeoff word ("smoother gradients" / "maximum compatibility") |
| Rate Control (exact ICQ/CQP/VBR/CRF/bitrate), Exact Quality slider, Encoding Speed, Tune, Deinterlace | Expert-only | **Stay Expert** -- real encoder-mechanics/precision controls, restored as a genuine collapsible section again (was permanently hidden in the first pass; the extra Normal-mode depth above earns Expert its place back) |
| Live telemetry (fps · Mb/s · speed×) | Visible under the ETA line | **Stays cut** -- ETA alone is still the right consumer-facing line |
| Presets (dropdown, Save As/Delete, technical names) | "720p AMD Balanced (Hardware / VAAPI)" | **Stays cut entirely** -- see "Presets" below |
| Audio: Track | Expert-only | **Promoted to Normal** -- a real media decision (multi-track sources) |
| Audio: Handling (copy-if-compatible vs. force AAC) | Expert-only checkbox, bundled with Channels into one Normal "Automatic/Convert to Stereo" toggle | **Split into its own Normal row** -- copy-vs-transcode and channel layout are independent decisions, previously conflated |
| Audio: Channels (Keep Original/Stereo) | Same bundled toggle as above | **Split into its own Normal row** |
| Audio: AAC Bitrate | Expert-only slider | **Promoted to Normal** -- stays adjustable even under Automatic handling, since a source the copy path can't handle still needs transcoding at this bitrate |
| Audio Expert section | -- | **Removed entirely** -- once Track/Handling/Channels/Bitrate are all Normal, there's nothing genuine left to put in an Audio Expert; revisit only once the backend gains something real (multi-track passthrough, language selection, loudness normalization, ...) |
| Frame Rate conversion | Not implemented at all -- no output `-r`/fps setting anywhere in `worker.py`'s `build_args()`, only source-probing for display | **Not v1** -- this is new backend work, not a UI promotion; needs real design thought (23.976 vs. 24, 29.97 vs. 30, frame duplication/dropping, VFR sources) before it's a v1.1 candidate, not something to fold into a "make Normal richer" pass |
| Audio Codec selection (a literal "Codec:" dropdown) | -- | **Not added** -- the backend has exactly one transcode target (AAC) plus stream-copy; "Handling: Automatic/Convert to AAC" already is the correct control for that. A real Codec dropdown only makes sense once a second output codec actually exists |
| Custom resolution (freeform WxH) | -- | **Not added** -- Keep Original/1080p/720p/480p covers real usage; a freeform field opens aspect-ratio/validation questions not worth it yet |
| Subtitle controls | `-sn`, subtitles always dropped | **Not added** -- don't build UI implying support the backend doesn't have; document the limitation instead |
| Metadata preservation (`-map_metadata 0`) | Automatic, no UI | **Stays automatic**, no UI needed |

## Presets (stays cut)

The built-in presets today bundle resolution + encoder + quality tier +
compatibility into one saved combination, named after its own technical
specs ("720p AMD Balanced (Hardware / VAAPI)", "1080p CPU Better Quality
(Software / x265)"). That naming is the opposite of approachable, and
even with this revision restoring real depth to Normal mode, nothing
about that conclusion changes: Mode + Quality tier + Target Size +
Resolution + File Format + Color Depth already cover every axis a
preset used to bundle, each as its own plain, directly-visible control
-- there's still nothing left for a saved preset to actually add over
just setting those controls directly, and the Save As/Delete/dropdown
management overhead isn't worth it for that.

If real usage later shows people want to *save* a specific combination
of these (now more numerous) controls as a shortcut, that's worth
reconsidering post-v1 with its own (still plain-language) naming — not
worth building speculatively now.

## Fork mechanics

Three real options, in order of how much ongoing connection to the
specialist repo you want:

1. **GitHub's own Fork** of `tneohcl/titan-video`. Keeps a visible link
   back to the original and makes `git fetch upstream` easy if the two
   ever need to share a backend fix. Best if the engine (worker.py etc.)
   is expected to get bugfixes on the specialist side that the consumer
   side wants too.
2. **New repo, seeded by clone.** `git clone` the current repo into a new
   directory, `git remote add origin <new-repo-url>`, push. No ongoing
   GitHub-level link; treat it as a snapshot to diverge from freely. Best
   if the consumer UI is expected to restructure enough of `ui_builder.py`
   that upstream merges would rarely apply cleanly anyway.
3. **Long-lived branch in the same repo.** Simplest to set up, but a
   consumer UI this different in scope will make the branch diff-noisy
   and awkward to keep rebasing. Not recommended given how much of
   `ui_builder.py`/`style.qss` this cut list touches.

**Decision made: option 2.** This folder (`titan-video-consumer`,
sibling to `titan-video`) is a plain `git clone` of `titan-video` at
commit `d9c4cdc`, given the cut list above is subtractive enough that
upstream UI changes won't merge cleanly either direction, and the two
products may end up branded separately per the naming question above.
`origin` was removed right after cloning (it defaulted to the local
`titan-video` path, not a real remote); a real one now exists --
`https://github.com/tneohcl/titan-video-consumer` (private), created and
pushed the same way `titan-video` itself was (`gh repo create
titan-video-consumer --private --source=. --remote=origin --push`).

## Packaging (the other reviewer's core point: this matters more than more UI polish)

This applies to *either* fork, but matters far more for a consumer
release than an internal/specialist one — right now `launch.sh` assumes
a shared venv at a machine-specific path (`/mnt/data/tools/venv/
transcoder`), which is fine for a personal tool and unacceptable for
something handed to someone else.

- [ ] Bundled Python runtime — no expectation the user has Python/PySide6/
      a venv set up. (PyInstaller or similar for a single-file/single-folder
      build; needs evaluating against PySide6's own packaging quirks.)
- [ ] Bundled FFmpeg binary, not a system dependency — a consumer install
      shouldn't require `apt install ffmpeg` first.
- [ ] Real application icon (not the current placeholder), used
      consistently: taskbar, window, `.desktop` entry / app bundle.
- [ ] `.desktop` entry (Linux) / `.app` bundle (macOS) / installer (Windows)
      so it launches like installed software, not a script.
- [ ] About dialog: version number, one-line description, licensing
      credit for FFmpeg (LGPL/GPL notice depending on build — check which
      before shipping, this is a real compliance item, not cosmetic).
- [ ] Proper per-OS app-data directory instead of `~/.config/TITAN/` (or
      keep the same QSettings mechanism if it already resolves correctly
      per-OS — confirm, don't assume).
- [ ] Auto-update story (or explicit decision to skip one for v1).

## Phase order

1. ~~**Positioning**~~ — done above (name/price/platform/target user;
   target user itself later revised, see "v1 positioning").
2. ~~**Fork the repo**~~ — done (this folder, now pushed to its own
   GitHub repo).
3. ~~**Subtraction pass**~~ — done, then partially reversed. The first
   pass cut down to Quality tier + Resolution + one Audio toggle, no
   Expert at all. After using it, Normal mode read as too minimal for
   the app's actual target user -- Processing, Codec, Mode, File
   Format, Color Depth, and every real Audio setting were promoted back
   into Normal (each a genuine output/media decision), Expert was
   restored as a real collapsible section for encoder-mechanics
   precision, and Presets/Compatibility/live-telemetry/Frame-Rate/
   Audio-Codec/custom-resolution/subtitles stayed cut or unbuilt -- see
   the current "v1 scope" table above for the final, settled shape.
   369 tests passing.
4. **Packaging** — next actionable step. Bundle runtime + ffmpeg, icon,
   `.desktop` entry, About dialog (Linux only for v1, per the platform
   decision above).
5. **Fresh-machine QA** — install and run on a machine that has never had
   Python/ffmpeg/this dev environment on it. This is the test that
   actually validates packaging; testing on the dev machine won't catch
   a missing bundled dependency.
6. **Distribution decision** — direct download vs. GitHub Releases is the
   realistic v1 choice given "free, Linux, no brand yet" from the
   positioning decisions above; an actual app store is a later-phase
   question once/if platform or pricing changes.

## Explicitly out of scope for this plan

- Any new features beyond what the specialist build already has.
- Mobile — this is a desktop transcoder; a mobile consumer app would be
  a different codebase entirely, not a fork.
- Monetization implementation details (payment processing etc.) — flag
  as a phase-1 decision, not something to design here.
