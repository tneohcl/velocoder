#!/bin/bash
# Shares the transcoder venv (identical dependencies -- PySide6 etc. --
# no reason to duplicate a ~500MB install for a sibling app).
source /mnt/data/tools/venv/transcoder/bin/activate
cd /mnt/data/tools/velocoder

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
python3 main.py >> "$LOG" 2>&1
