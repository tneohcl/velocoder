#!/bin/bash
# Shares the transcoder venv (identical dependencies -- PySide6 etc. --
# no reason to duplicate a ~500MB install for a sibling app).
source /mnt/data/tools/venv/transcoder/bin/activate
cd /mnt/data/tools/velocoder-1.1

# No isolated XDG_CONFIG_HOME -- tried that (this being a parallel dev
# worktree of the same app, same QSettings org/app name as the real 1.0
# build, would otherwise read/write the real ~/.config/VeloCoder
# alongside it), but it's a real, confirmed regression, not just a minor
# tradeoff: overriding XDG_CONFIG_HOME also hides kdeglobals and the rest
# of the real KDE session config from Qt's own KDE platform theme
# integration, which needs it to resolve the desktop's actual accent
# color (QPalette.Accent/Highlight) and PlaceholderText role -- without
# it, both silently fall back to Qt's bare Fusion defaults instead
# (white-ish "accent", secondary/caption text unreadable in dark mode),
# reported live and confirmed directly (accent read back as #ffffff
# under the isolated config, the real session's own #308cc6 without it).
# 1.0 and 1.1 sharing window geometry/theme/expert-expanded state during
# dev testing is the smaller problem by far.

LOG=/tmp/velocoder-1.1-debug.log
echo "===== launch $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
python3 main.py >> "$LOG" 2>&1
