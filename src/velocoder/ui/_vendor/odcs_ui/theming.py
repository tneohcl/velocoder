"""ODCS theming for Qt (PySide6): theme choice, system accent, QSS rendering.

    from odcs_ui.theming import ThemeController, THEME_CHOICES
    theme = ThemeController(app, stylesheets=[odcs_ui.theming.BASE_QSS, Path("style.qss")])
    theme.set_choice("system")          # "dark" | "light" | "system"
    theme.changed.connect(on_theme)     # (theme_name, tokens) after every re-apply

Stylesheets use $TOKEN placeholders (e.g. $BG_PANEL). Tokens come from
tokens.json for the resolved theme, with ACCENT/ACCENT_HOVER/ACCENT_PRESSED/
TEXT_ON_ACCENT taken from the desktop's accent color, plus any app extras.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterable, Mapping

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from . import color, tokens

# Same values and labels in every ODCS app (View > Theme or Settings).
THEME_CHOICES = [("dark", "Dark"), ("light", "Light"), ("system", "Match System")]
BASE_QSS = Path(__file__).with_name("base.qss")

_TOKEN = re.compile(r"\$([A-Z][A-Z0-9_]*)")

# Tokens of the most recently applied theme, for widgets that paint colors
# themselves (status icons). Set by ThemeController.apply().
_CURRENT: dict[str, str] = {}


def current_tokens() -> dict[str, str]:
    """The active theme's tokens; before any ThemeController has applied, the
    dark theme with its fallback accent."""
    if _CURRENT:
        return dict(_CURRENT)
    values = tokens.theme("dark")
    values["TEXT_ON_ACCENT"] = color.on_accent(values["ACCENT"])
    return values


def resolve_theme(choice: str, app: QApplication | None = None) -> str:
    """"dark"/"light" pass through; "system" follows the desktop: the Qt color
    scheme when the platform reports one, else the palette's window lightness."""
    if choice in ("dark", "light"):
        return choice
    app = app or QApplication.instance()
    scheme = app.styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    if scheme == Qt.ColorScheme.Light:
        return "light"
    window = app.palette().color(QPalette.Window).name()
    return "dark" if color.luminance(window) < 0.5 else "light"


def accent_tokens(palette: QPalette, fallback: str) -> dict[str, str]:
    """ACCENT family from the desktop accent (QPalette.Accent, Qt 6.6+;
    Highlight before). An invalid or pure-black role means nothing resolved,
    so the theme's fallback accent is used instead."""
    role = getattr(QPalette, "Accent", QPalette.Highlight)
    qcolor = palette.color(role)
    accent = qcolor.name() if qcolor.isValid() and qcolor != QColor(0, 0, 0) else fallback
    return {
        "ACCENT": accent,
        "ACCENT_HOVER": color.shade(accent, 0.12),
        "ACCENT_PRESSED": color.shade(accent, -0.13),
        "TEXT_ON_ACCENT": color.on_accent(accent),
    }


def build_tokens(theme_name: str, palette: QPalette,
                 extra: Mapping[str, str] | None = None) -> dict[str, str]:
    values = tokens.theme(theme_name)
    values.update(accent_tokens(palette, values["ACCENT"]))
    values["ON_ACCENT_IS_DARK"] = "true" if values["TEXT_ON_ACCENT"] == color.DARK_ON_ACCENT else "false"
    if extra:
        values.update(extra)
    return values


def render(qss: str, values: Mapping[str, str]) -> str:
    """Replace $TOKEN placeholders. Unknown tokens raise, so a typo can't ship
    as a literal "$BG_PANLE" that Qt silently ignores."""
    missing = sorted({m for m in _TOKEN.findall(qss) if m not in values})
    if missing:
        raise KeyError(f"unknown QSS tokens: {', '.join(missing)}")
    return _TOKEN.sub(lambda m: str(values[m.group(1)]), qss)


class FocusVisibleFilter(QObject):
    """QSS has no :focus-visible. A plain :focus ring also lights up on a
    mouse click and when a window is re-activated, pointing at a control the
    person is already looking at. This mirrors the browser heuristic onto a
    `focusVisible` property: true only when focus arrived by Tab/Shift+Tab.
    base.qss styles `[focusVisible="true"]` instead of `:focus`.
    (Proven in VeloCoder first; moved here in 0.3.)"""

    KEYBOARD_REASONS = (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        # FocusIn/Out also reach QWindow objects, which have no style().
        if not isinstance(obj, QWidget):
            return False
        if event.type() == QEvent.Type.FocusIn:
            self._set(obj, event.reason() in self.KEYBOARD_REASONS)
        elif event.type() == QEvent.Type.FocusOut:
            self._set(obj, False)
        return False

    @staticmethod
    def _set(widget, visible):
        if bool(widget.property("focusVisible")) != visible:
            widget.setProperty("focusVisible", visible)
            widget.style().unpolish(widget)
            widget.style().polish(widget)


def install_focus_visible(app: QApplication) -> FocusVisibleFilter:
    """Install the keyboard-only focus ring once per application."""
    existing = getattr(app, "_odcs_focus_visible", None)
    if existing is None:
        existing = FocusVisibleFilter(app)
        app.installEventFilter(existing)
        app._odcs_focus_visible = existing
    return existing


class ThemeController(QObject):
    """Owns the application stylesheet. Re-applies on theme choice changes and,
    for "system", on live desktop palette / color-scheme changes."""

    changed = Signal(str, dict)

    def __init__(self, app: QApplication, stylesheets: Iterable[Path | str],
                 extra_tokens: Callable[[str, dict], Mapping[str, str]] | None = None,
                 choice: str = "system"):
        super().__init__(app)
        self.app = app
        self.stylesheets = [Path(p) for p in stylesheets]
        self.extra_tokens = extra_tokens
        self.choice = choice if choice in dict(THEME_CHOICES) else "system"
        self.theme_name = ""
        self.values: dict[str, str] = {}
        self._applied = None
        self._busy = False
        app.installEventFilter(self)
        app.styleHints().colorSchemeChanged.connect(lambda *_: self._on_system_change())
        install_focus_visible(app)

    def set_choice(self, choice: str) -> None:
        self.choice = choice if choice in dict(THEME_CHOICES) else "system"
        self.apply()

    def apply(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            name = resolve_theme(self.choice, self.app)
            values = build_tokens(name, self.app.palette())
            if self.extra_tokens:
                values.update(self.extra_tokens(name, values))
            qss = "\n".join(render(p.read_text(encoding="utf-8"), values) for p in self.stylesheets)
            self.theme_name, self.values = name, values
            _CURRENT.clear()
            _CURRENT.update(values)
            if qss != self._applied:
                self._applied = qss
                self.app.setStyleSheet(qss)
            self.changed.emit(name, dict(values))
        finally:
            self._busy = False

    def _on_system_change(self) -> None:
        # Always re-apply: even with a forced Dark/Light choice, the desktop's
        # accent may have changed. Unchanged output is skipped in apply().
        self.apply()

    def eventFilter(self, obj, event):  # noqa: N802 (Qt API)
        if obj is self.app and event.type() in (QEvent.ApplicationPaletteChange, QEvent.PaletteChange):
            self._on_system_change()
        return False


def set_surface(widget, surface: str = "window") -> None:
    """Give a plain QWidget the window or panel background (QMainWindow and
    QDialog get BG_WINDOW automatically)."""
    widget.setProperty("odcsSurface", surface)
    widget.setAttribute(Qt.WA_StyledBackground, True)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_role(widget, role: str) -> None:
    """Set the QSS `role` property (e.g. "primary") and re-polish so it applies."""
    if widget.property("role") != role:
        widget.setProperty("role", role)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
