"""Cross-session queue persistence: the unfinished queue (path + settings,
in order) and the output folder, written to a small JSON file under the
platform's app-data directory. Deliberately NOT QSettings -- QSettings in
this app holds only small scalar preferences (theme_choice, window_
geometry, video_expert_expanded); a structured, growing list of job dicts
belongs in its own file, not shoehorned into that key-value store.

Every function here is a plain, Qt-widget-free function operating on
dicts/paths -- main.py/queue_controller.py own the app-specific meaning of
a "job" (which keys it has, hardware sanitization, ...); this module only
knows how to get a list of them and an output dir on and off disk safely.
"""
import json
import os
from pathlib import Path

from PySide6.QtCore import QStandardPaths

# Bumped only if the on-disk shape changes in a way an older load_session()
# couldn't safely interpret. load_session() below refuses anything else,
# rather than guessing -- "nothing to restore" is always a safe fallback,
# guessing wrong about a future format is not.
SESSION_VERSION = 1


def session_file_path() -> Path:
    app_data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    return app_data_dir / "session.json"


def save_session(jobs: list[dict], output_dir: Path) -> None:
    """Writes ONLY the jobs the caller passes -- this module has no
    opinion on which jobs count as "unfinished"; queue_controller.py
    filters before calling. Deliberately excludes progress/status/probe
    results: restoring always re-runs detection fresh, the same as a real
    Add Files would, rather than trying to preserve exactly what was on
    screen."""
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SESSION_VERSION,
        "output_dir": str(output_dir),
        "jobs": [_job_to_json(job) for job in jobs],
    }
    # Atomic write -- an app crash, killed process, or power loss mid-write
    # must never leave a half-written session.json behind. A truncated
    # file wouldn't just lose *this* write, it would permanently and
    # silently lose every job the *previous* successful write held too,
    # for good, the next time anything tries to load it.
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2))
    os.replace(tmp_path, path)


def load_session() -> dict | None:
    """None for anything short of a clean, version-matched read: a
    missing file, corrupt/truncated JSON, or an unrecognized version are
    all just "nothing to restore" -- never a startup crash. Callers get
    back the raw dict (output_dir as a string, each job's path as a
    string) -- turning those into real Path objects is the caller's job,
    since only it knows what to do with a path that no longer exists."""
    path = session_file_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != SESSION_VERSION:
        return None
    return data


def _job_to_json(job: dict) -> dict:
    job = dict(job)
    job["path"] = str(job["path"])
    # Defensive -- the caller is expected to have already filtered out
    # completed jobs entirely, so this key shouldn't be present at all.
    # Stripped here too rather than trusted, the same "protect the
    # invariant at the source" reasoning already used elsewhere in this
    # app (e.g. _remove_selected's own _queue_editable guard).
    job.pop("_completed_output_path", None)
    return job
