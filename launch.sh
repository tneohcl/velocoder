#!/bin/bash
source /mnt/data/tools/venv/transcoder/bin/activate
cd /mnt/data/tools/transcoder

LOG=/tmp/titan_transcoder_debug.log
echo "===== launch $(date '+%Y-%m-%d %H:%M:%S') =====" >> "$LOG"
python3 main.py >> "$LOG" 2>&1
