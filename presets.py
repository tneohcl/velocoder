"""User preset persistence.

A preset is just a saved settings dict (see worker.build_args) plus a
"name" key. The built-in presets in constants.py are seed data, not stored
here — this module only ever reads/writes what the user has saved.
"""
import json
from pathlib import Path

USER_PRESETS_PATH = Path(__file__).parent / "user_presets.json"


def load_user_presets(path: Path = USER_PRESETS_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        presets = data.get("presets", [])
        if not isinstance(presets, list):
            raise ValueError("'presets' key is not a list")
        return presets
    except (json.JSONDecodeError, ValueError, OSError, AttributeError) as exc:
        # This file is only ever written by save_user_presets() below, but a
        # crash or a disk-full condition mid-write can still leave it
        # corrupt. Losing the app entirely on next launch over that is worse
        # than losing the saved presets -- preserve the bad file for manual
        # recovery and start fresh rather than crashing here.
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.rename(backup)
        except OSError:
            backup = path
        print(f"Warning: {path} was invalid ({exc}); starting with no saved presets. Backed up to {backup}.")
        return []


def save_user_presets(presets: list[dict], path: Path = USER_PRESETS_PATH):
    with open(path, "w") as f:
        json.dump({"presets": presets}, f, indent=2)
