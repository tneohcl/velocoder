#!/bin/bash
# Runs VeloCoder from this source tree (the menu entry points here).
# Shares the transcoder venv (identical dependencies -- PySide6 etc. --
# no reason to duplicate a ~500MB install for a sibling app).
here="$(cd "$(dirname "$0")" && pwd)"
source /mnt/data/tools/venv/transcoder/bin/activate

# Deliberately no isolated XDG_CONFIG_HOME -- overriding it also hides
# kdeglobals and the rest of the real KDE session config from Qt's own
# KDE platform theme integration, which needs it to resolve the
# desktop's actual accent color (QPalette.Accent/Highlight) and
# PlaceholderText role -- without it, both silently fall back to Qt's
# bare Fusion defaults instead (white-ish "accent", secondary/caption
# text unreadable in dark mode), reported live and confirmed directly
# (accent read back as #ffffff under an isolated config, the real
# session's own #308cc6 without it).

LOG=/tmp/velocoder_debug.log
echo "===== launch $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m velocoder >> "$LOG" 2>&1
