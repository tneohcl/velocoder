"""Preset persistence -- a single presets.json holds every preset the app
shows, built-ins first and any user-saved preset appended after them, so
the whole list reads together in one file (closer to how HandBrake's own
presets file works) rather than splitting built-in/user across two files.
builtin_presets.json is the separate, version-controlled seed: used only
to (re)create presets.json when it doesn't exist yet, and to know which
names stay protected (Save As/Delete refuse to touch them) no matter how
many user presets get appended after them -- see BUILTIN_PRESET_NAMES in
constants.py, which reads directly from builtin_presets.json rather than
from whatever presets.json currently contains.
"""
import json
from pathlib import Path

PRESETS_PATH = Path(__file__).parent / "presets.json"
BUILTIN_PRESETS_PATH = Path(__file__).parent / "builtin_presets.json"


def load_builtin_presets(path: Path = BUILTIN_PRESETS_PATH) -> list[dict]:
    # Deliberately no try/except here, unlike load_presets below -- this
    # file ships with the app and is never written by it, so a missing/
    # corrupt copy is a packaging problem, not recoverable user state.
    # Failing loudly at startup beats silently running with zero built-in
    # presets to seed presets.json from.
    with open(path) as f:
        return json.load(f)["presets"]


def load_presets(path: Path = PRESETS_PATH, builtin_path: Path = BUILTIN_PRESETS_PATH) -> list[dict]:
    if not path.exists():
        # First run (or presets.json got deleted) -- seed it from the
        # built-ins so it's never just missing, and so the very next
        # Save As appends after real content instead of starting a file
        # from scratch.
        presets = load_builtin_presets(builtin_path)
        save_presets(presets, path)
        return presets
    try:
        with open(path) as f:
            data = json.load(f)
        presets = data.get("presets", [])
        if not isinstance(presets, list):
            raise ValueError("'presets' key is not a list")
        return presets
    except (json.JSONDecodeError, ValueError, OSError, AttributeError) as exc:
        # This file is only ever written by save_presets() below, but a
        # crash or a disk-full condition mid-write can still leave it
        # corrupt. Losing the app entirely on next launch over that is
        # worse than losing any saved presets -- preserve the bad file for
        # manual recovery and reseed from the built-ins rather than
        # crashing here.
        backup = path.with_suffix(path.suffix + ".corrupt")
        try:
            path.rename(backup)
        except OSError:
            backup = path
        print(f"Warning: {path} was invalid ({exc}); reseeding from built-ins. Backed up to {backup}.")
        presets = load_builtin_presets(builtin_path)
        save_presets(presets, path)
        return presets


def save_presets(presets: list[dict], path: Path = PRESETS_PATH):
    with open(path, "w") as f:
        json.dump({"presets": presets}, f, indent=2)
