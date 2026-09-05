# TITAN Video — Consumer Fork Plan

Status: **v1 scope decided (Phases 1 and 3's decisions made on paper);
Phases 2 (fork) done; nothing else built yet.** This folder is that fork
-- cloned from `titan-video` on 2026-09-05, `origin` deliberately left
unset until a real GitHub repo is created for it. The positioning
questions below were answered with pragmatic v1 defaults rather than
left open, specifically so work has somewhere concrete to start --
revisit any of them before Phase 4 (packaging) if they turn out wrong,
they're cheap to change this early and expensive to change after
distribution.

## v1 positioning (decided, not just candidates)

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
- **Target user, one sentence:** someone converting a video to send to
  a friend or post somewhere, who has never heard the word "codec" and
  never will need to.

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

## v1 scope (decided)

Reusing the specific "specialist flavor" items already identified, each
now has a real v1 decision instead of a candidate:

| Item | Current (specialist) | v1 decision |
|---|---|---|
| Processing choice | Automatic / CPU / Intel / AMD, always visible | **Cut.** Automatic only — no visible choice at all |
| Rate control | ICQ/CQP/bitrate modes in Expert | **Cut** — the Quality tier buttons (below) are the only quality control |
| Expert section (codec, CRF, bit depth, tune, deinterlace) | One click away on every tab | **Cut entirely**, not just hidden |
| Live telemetry (fps · Mb/s · speed×) | Visible under the ETA line | **Cut** — ETA alone is already the consumer-facing line |
| Presets (dropdown, Save As/Delete, technical names) | "720p AMD Balanced (Hardware / VAAPI)" | **Cut entirely** — see "Presets → plain-language quality" below, this is the piece you flagged |
| Output format/container choice | Visible dropdown | **Cut** — always MP4, no visible control |
| Compatibility (Modern/Most Compatible) | Visible toggle | **Cut** — folded into Automatic |
| Quality tier (Smaller File / Balanced / Better Quality) | Already plain language | **Kept as-is** — becomes the *only* quality decision in the app, see below |
| Resolution dropdown | Keep Original / 720p / 1080p / etc. | **Kept**, unchanged — the one technical-adjacent control worth keeping since "make it smaller" is a real, understandable consumer need |

Resist adding anything *new* in this pass — this is a subtraction
exercise. Any genuinely new consumer-only feature (e.g. a share-sheet
integration, drag-a-link-in support) is its own separate decision, later.

## Presets → plain-language quality (the piece you specifically flagged)

The built-in presets today bundle resolution + encoder + quality tier +
compatibility into one saved combination, named after its own technical
specs ("720p AMD Balanced (Hardware / VAAPI)", "1080p CPU Better Quality
(Software / x265)"). That naming is the opposite of normie-friendly, and
now that most of what a preset used to bundle is being cut anyway
(Processing choice, compatibility, container format are all gone above),
there's very little left for a preset to actually *save* — the whole
Save As.../Delete/dropdown system stops earning its keep.

**Decision: don't rename presets, remove them.** The Quality tier row
already uses exactly the plain language this needed ("Smaller File" /
"Balanced" / "Better Quality") — no jargon, no encoder name, no
resolution number required to understand what you're picking. That
becomes the *entire* "which preset" decision in the consumer app: three
buttons, always visible, no save/delete/manage step at all. Combined
with the Resolution dropdown (kept, above) for the one other thing a
normal person genuinely wants control over ("make the file smaller"),
that's the app's whole quality-and-size surface — down from Presets +
Quality tier + Compatibility + Processing + Rate Control today.

If real usage later shows people want to *save* a specific
Quality-tier + Resolution combination as a shortcut, that's worth
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
`titan-video` path, not a real remote) -- add a real one once a GitHub
repo name/visibility is chosen, the same way `titan-video` itself was
set up (`gh repo create <name> --private --source=. --remote=origin
--push`, `gh` already installed and authenticated as `tneohcl`).

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

1. ~~**Positioning**~~ — done above (name/price/platform/target user).
2. ~~**Fork the repo**~~ — done (this folder).
3. **Subtraction pass** — next actionable step. Remove/simplify per the
   v1 scope table above. Expect this to *shrink* `ui_builder.py`
   substantially, not add to it — Processing row, Rate Control row,
   Compatibility row, the whole Presets row/Save As/Delete plumbing, the
   Expert sections on both tabs, and the live-telemetry line all come
   out; Quality tier and Resolution stay untouched.
4. **Packaging** — bundle runtime + ffmpeg, icon, `.desktop` entry, About
   dialog (Linux only for v1, per the platform decision above).
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
