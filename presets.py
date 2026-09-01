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
    with open(path) as f:
        return json.load(f).get("presets", [])


def save_user_presets(presets: list[dict], path: Path = USER_PRESETS_PATH):
    with open(path, "w") as f:
        json.dump({"presets": presets}, f, indent=2)
