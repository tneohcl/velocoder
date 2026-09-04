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


def migrate_missing_builtins(current: list[dict], builtins: list[dict]) -> list[dict]:
    """Adds any built-in preset (by name) that isn't already in `current`
    -- e.g. a newly-added built-in (a new encoder's own High/Balanced/Low
    trio, say) that an existing presets.json predates and would
    otherwise never pick up. load_presets() above only ever seeds from
    builtins when the file is missing or corrupt, never on a normal
    successful load, so without this an already-installed presets.json
    stays frozen at whatever built-ins existed when it was first
    created, permanently missing anything added to builtin_presets.json
    afterward. Deliberately NOT folded into load_presets() itself --
    that function's own tests (see test_save_overwrites_file_contents)
    rely on it returning exactly what was saved, unmigrated; this is
    called explicitly by main.py instead, right after load_presets().

    Preserves the built-ins-first-then-user-presets ordering: each
    missing built-in is inserted at its own position in `builtins`' own
    order, relative to whichever built-ins are already present, rather
    than just appended past the user's own saved presets at the very
    end. A no-op (returns `current` itself, unchanged) when nothing's
    missing -- confirmed this doesn't reorder or otherwise touch a list
    that already has every built-in.
    """
    current_names = {p["name"] for p in current}
    if all(p["name"] in current_names for p in builtins):
        return current
    builtin_names_in_order = [p["name"] for p in builtins]
    by_name = {p["name"]: p for p in current}
    for p in builtins:
        by_name.setdefault(p["name"], p)
    merged_builtins = [by_name[name] for name in builtin_names_in_order]
    builtin_name_set = set(builtin_names_in_order)
    user_presets = [p for p in current if p["name"] not in builtin_name_set]
    return merged_builtins + user_presets


def save_presets(presets: list[dict], path: Path = PRESETS_PATH):
    with open(path, "w") as f:
        json.dump({"presets": presets}, f, indent=2)
