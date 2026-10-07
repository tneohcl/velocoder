"""The desktop's accent colour, read where the desktop keeps it. No Qt.

Qt reports the accent through QPalette only when its platform-theme plugin
loads, and PySide6's bundled Qt can't load the system's plugins (LXQt's
libqtlxqt.so, for one), so apps saw only Qt's default blue. This reads it
directly: the XDG desktop portal first (GNOME, KDE; works inside a Flatpak),
then the desktop's own settings (KDE kdeglobals, LXQt lxqt.conf).
"""
from __future__ import annotations

import configparser
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_RGB = re.compile(r"\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*")
_TRIPLE = re.compile(r"\(\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*,\s*([-\d.eE]+)\s*\)")


def accent(env: Mapping[str, str] | None = None, home: Path | None = None) -> str | None:
    """'#rrggbb' from the desktop, or None when it doesn't say."""
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    return portal_accent() or config_accent(env, home)


def portal_accent() -> str | None:
    """org.freedesktop.appearance accent-color from the settings portal."""
    try:
        result = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
             "--object-path", "/org/freedesktop/portal/desktop",
             "--method", "org.freedesktop.portal.Settings.ReadOne",
             "org.freedesktop.appearance", "accent-color"],
            capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_portal_accent(result.stdout) if result.returncode == 0 else None


def parse_portal_accent(output: str) -> str | None:
    """gdbus prints the (ddd) value as '(<(r, g, b)>,)', each 0..1; a value
    outside that range means the desktop has no accent set."""
    match = _TRIPLE.search(output or "")
    if not match:
        return None
    try:
        channels = [float(value) for value in match.groups()]
    except ValueError:
        return None
    if not all(0.0 <= value <= 1.0 for value in channels):
        return None
    return "#" + "".join(f"{round(value * 255):02x}" for value in channels)


def config_accent(env: Mapping[str, str], home: Path) -> str | None:
    """The accent from the running desktop's own settings file."""
    desktops = {name.strip().lower() for name in env.get("XDG_CURRENT_DESKTOP", "").split(":")}
    for folder in _config_dirs(env, home):
        if "kde" in desktops:
            ini = _read(folder / "kdeglobals")
            if ini is not None:
                for section, key in (("General", "AccentColor"), ("Colors:Selection", "BackgroundNormal")):
                    value = _rgb(ini.get(section, key, fallback=""))
                    if value:
                        return value
        if "lxqt" in desktops:
            ini = _read(folder / "lxqt" / "lxqt.conf")
            if ini is not None:
                value = ini.get("Palette", "highlight_color", fallback="").strip()
                if _HEX.fullmatch(value):
                    return value.lower()
    return None


def _config_dirs(env: Mapping[str, str], home: Path) -> list[Path]:
    # XDG_CONFIG_HOME first; then ~/.config, since inside a Flatpak
    # XDG_CONFIG_HOME is the app's private folder, not the desktop's.
    dirs = [Path(env["XDG_CONFIG_HOME"])] if env.get("XDG_CONFIG_HOME") else []
    dirs.append(home / ".config")
    return list(dict.fromkeys(dirs))


def _read(path: Path) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # KDE keys are case-sensitive
    try:
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, configparser.Error):
        return None
    return parser


def _rgb(value: str) -> str | None:
    match = _RGB.fullmatch(value or "")
    if not match:
        return None
    channels = [int(part) for part in match.groups()]
    if any(part > 255 for part in channels):
        return None
    return "#" + "".join(f"{part:02x}" for part in channels)
