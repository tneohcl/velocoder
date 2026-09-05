#!/bin/bash
# Shares the transcoder venv (identical dependencies -- PySide6 etc. --
# no reason to duplicate a ~500MB install for a sibling app).
source /mnt/data/tools/venv/transcoder/bin/activate
cd /mnt/data/tools/titan-video-consumer

LOG=/tmp/titan_video_consumer_debug.log
echo "===== launch $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
python3 main.py >> "$LOG" 2>&1
