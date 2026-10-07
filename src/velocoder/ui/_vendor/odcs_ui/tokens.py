"""ODCS design tokens — loaded from tokens.json, the single source of truth.

Qt-free on purpose: the web CSS generator and the tests use this module
without importing PySide6.

    from odcs_ui import tokens
    tokens.THEMES["dark"]["TEXT_PRIMARY"]   # "#e6e8eb"
    tokens.SCALE["controlHeight"]           # 28

The DARK / LIGHT / THEMES names match the per-app themes.py this replaces,
so an app can switch with `from odcs_ui.tokens import DARK, LIGHT, THEMES`.
"""
from __future__ import annotations

import json
from importlib.resources import files

_DATA = json.loads(files(__package__).joinpath("tokens.json").read_text(encoding="utf-8"))

VERSION: int = _DATA["version"]
THEMES: dict[str, dict[str, str]] = {name: dict(values) for name, values in _DATA["themes"].items()}
DARK = THEMES["dark"]
LIGHT = THEMES["light"]
SCALE: dict = _DATA["scale"]

# Text roles that must stay readable (WCAG 2.2 AA, 4.5:1) on every surface
# text sits on. TEXT_DISABLED is exempt by WCAG; TEXT_ON_ACCENT is chosen
# live against the system accent (see color.on_accent).
READABLE_TEXT = ("TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_TERTIARY", "TEXT_READONLY",
                 "TEXT_CAPTION", "SUCCESS", "WARNING_TEXT", "ERROR")
TEXT_SURFACES = ("BG_WINDOW", "BG_PANEL", "BG_FIELD")
MIN_TEXT_CONTRAST = 4.5


def theme(name: str) -> dict[str, str]:
    """A fresh copy of one theme's tokens ("dark" or "light"; anything else -> dark)."""
    return dict(THEMES.get(name, DARK))
