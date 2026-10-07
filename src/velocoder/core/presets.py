"""Loader for builtin_presets.json -- the specialist build's built-in
preset definitions (High/Balanced/Low per engine). This consumer fork has
no Presets feature (see CONSUMER_FORK_PLAN.md) and never reads or writes a
user presets.json, but builtin_presets.json's entries are still useful as
realistic, hand-vetted full-settings-dict fixtures for tests that need one
(e.g. TestSettingsSummary in tests/test_main.py) -- keeping load_builtin_
presets() around avoids hand-duplicating those fixture dicts.
"""
import json
from pathlib import Path

BUILTIN_PRESETS_PATH = Path(__file__).parent / "builtin_presets.json"


def load_builtin_presets(path: Path = BUILTIN_PRESETS_PATH) -> list[dict]:
    # Deliberately no try/except here -- this file ships with the app and
    # is never written by it, so a missing/corrupt copy is a packaging
    # problem, not recoverable user state.
    with open(path) as f:
        return json.load(f)["presets"]
