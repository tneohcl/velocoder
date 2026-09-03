"""Theme/QSS engine, split out of main.py: stylesheet loading and token
substitution, system-accent derivation, the two QSS-gap event filters
(combo-popup background, focus-visible), and theme-choice resolution.
Nothing here depends on MainWindow -- everything took an explicit `app`
(or, for the event filters, whatever QObject the event landed on)
already, so this was already a self-contained concern before the move."""
from pathlib import Path

from PySide6.QtCore import Qt, QEvent, QObject
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QWidget

import themes

# Populated by _load_stylesheet on every load/theme switch; read back by
# _ComboPopupBackgroundFilter so a popup styled after a theme change gets
# the theme it was opened under, not whatever was loaded at startup.
_current_theme_palette: dict = {}


def _system_accent_tokens(app) -> dict:
    """Derives ACCENT/ACCENT_HOVER/ACCENT_PRESSED/TEXT_ON_ACCENT from the
    desktop's own accent color instead of a color this app picks on its
    own -- QPalette.Accent (Qt 6.6+; QPalette.Highlight on older Qt, where
    Accent doesn't exist yet) is what a properly-integrated Qt app is
    supposed to derive its accent from, and on a real KDE session it
    already resolves to that session's actual configured accent (confirmed
    on this machine: #308cc6, a real chosen color, not some generic
    Fusion-style default blue). Hover/pressed are lighter/darker variants
    of that same color; TEXT_ON_ACCENT is picked from the accent's own
    perceived luminance rather than assumed, since a user's chosen system
    accent could be any hue or lightness, not just the blue this app
    previously shipped with fixed per theme. Falls back to that previous
    fixed blue only if the palette role comes back invalid or pure black --
    a real desktop session's accent is never actually black, so that's a
    reliable signal nothing resolved rather than a legitimate choice.
    BG_ACCENT_DISABLED/TEXT_ACCENT_DISABLED (Start button, disabled) are
    deliberately left theme-fixed, not derived here -- a muted echo of an
    arbitrary accent hue is a harder thing to get right by formula than
    the four tokens above, and isn't what "follow the system accent"
    was actually asking for.

    CHECK_ICON rides along too, and needs to -- the checkmark drawn inside
    a checked QCheckBox sits directly on the $ACCENT fill, exactly like
    button text does, so it needs the same contrast-partner color
    TEXT_ON_ACCENT already picks, not whichever of check_dark.svg/
    check_light.svg happened to match the *old*, theme-fixed accent.
    Confirmed this was a real, live bug, not a hypothetical: Dark's
    accent used to be light enough that a near-black checkmark
    (check_dark.svg -- named for the theme it shipped with, not the
    stroke color) made sense; once the accent became this session's
    real KDE accent (#308cc6) instead, TEXT_ON_ACCENT correctly switched
    to white for *both* themes (the accent doesn't vary by theme
    anymore), but the checkmark file selection was still keyed off
    theme name, not off that same decision -- Dark's checkbox ended up
    with a near-black check on the same blue fill Start's white text
    sits on. Screenshotted both themes to confirm the mismatch before
    fixing it this way.
    """
    role = getattr(QPalette, "Accent", QPalette.Highlight)
    accent = app.palette().color(role)
    if not accent.isValid() or accent == QColor(0, 0, 0):
        accent = QColor("#4fa8e0")
    luminance = 0.299 * accent.redF() + 0.587 * accent.greenF() + 0.114 * accent.blueF()
    on_accent_is_dark = luminance > 0.5
    return {
        "ACCENT": accent.name(),
        "ACCENT_HOVER": accent.lighter(118).name(),
        "ACCENT_PRESSED": accent.darker(115).name(),
        "TEXT_ON_ACCENT": "#0d1117" if on_accent_is_dark else "#ffffff",
        "CHECK_ICON": "check_dark.svg" if on_accent_is_dark else "check_light.svg",
    }


def _fuzzy_text_color(widget) -> QColor:
    """Muted-text color for de-emphasized captions/placeholders -- the
    queue's empty-state "Drag video files here..." text (queue_widget.py)
    and the quality/speed/audio-bitrate tier captions (main.py) both call
    this rather than reading QPalette.PlaceholderText directly.

    Returns a real QColor, not a string -- queue_widget.py's paintEvent
    needs one directly for painter.setPen(), and re-parsing a formatted
    string back into a QColor turned out to be its own bug (see below):
    each caller that needs a string (main.py's QSS `color:` property)
    formats this return value itself instead.

    QPalette.PlaceholderText was trusted directly at first -- confirmed
    correct via a real screenshot on a real desktop session -- but two
    separate problems turned up after it stopped looking muted on a real
    live session:

    1. Fusion's own *default* PlaceholderText (no platform theme plugin
       enriching the palette, e.g. under the offscreen QPA platform) isn't
       a distinct color at all -- it's WindowText's exact same RGB at
       roughly half alpha (128 vs 255), meant to read as muted by
       blending partially into whatever's behind it, not by being a
       different solid color.
    2. `.color(...).name()` silently drops that alpha channel -- so even
       when the role legitimately *was* "WindowText, but translucent",
       reading it back through plain hex `.name()` collapsed it to a
       fully-opaque color indistinguishable from ordinary text. This is
       reproducible even outside any offscreen/live-session difference:
       any translucent PlaceholderText role loses its muting through
       `.name()` regardless of environment.

    Fixed by keeping the alpha channel (a real QColor, not hex) whenever
    the role is genuinely translucent or otherwise distinct from
    WindowText, and only falling back to this app's own $TEXT_SECONDARY
    token for the genuine "nothing distinct here at all" case -- same
    defensive shape as _system_accent_tokens' own accent fallback above:
    don't trust a system palette role blindly, fall back rather than
    silently losing the visual hierarchy the muted color exists for.

    That fallback used to be formatted as a "rgba(...)" CSS string here
    (fine for a QSS `color:` property, which does parse that syntax) --
    but queue_widget.py's paintEvent needed a real QColor for
    painter.setPen(), and re-wrapped that same string in QColor(...) to
    get one. QColor's own string constructor does NOT understand CSS
    rgba() syntax (confirmed directly: QColor("rgba(20, 20, 20, 0.5)")
    comes back invalid, i.e. solid black) -- reported live as "always
    black text" in both themes. Returning the QColor itself sidesteps
    that string round-trip entirely.
    """
    palette = widget.palette()
    placeholder = palette.color(QPalette.PlaceholderText)
    if placeholder != palette.color(QPalette.WindowText):
        return placeholder
    return QColor(_current_theme_palette.get("TEXT_SECONDARY", "#8b93a1"))


def _load_stylesheet(app, theme_name: str = "dark", style_path: Path = Path(__file__).parent / "style.qss"):
    try:
        text = style_path.read_text()
        # QSS url() is resolved relative to the process's working directory,
        # not the .qss file's location -- not safe to hardcode given launch.sh
        # cd's first but a direct `python3 main.py` from elsewhere wouldn't.
        # Substituting an absolute path in for each *_ICON token below (e.g.
        # $CHECK_ICON) keeps style.qss itself portable.
        assets_dir = style_path.parent / "assets"
        # A copy, not the THEMES dict itself -- mutating that shared dict
        # in place would leak this call's system-accent override into every
        # later read of themes.DARK/LIGHT, theme switches included.
        palette = {**themes.THEMES.get(theme_name, themes.THEMES["dark"]), **_system_accent_tokens(app)}
        # Longest token first: "$BG_CONTROL" is a literal prefix of
        # "$BG_CONTROL_HOVER" and "$BG_CONTROL_PRESSED" (same for
        # $ACCENT/$ACCENT_HOVER/$ACCENT_PRESSED, $BORDER/$BORDER_STRONG/
        # $BORDER_HOVER) -- replacing the short one first would consume
        # the start of the longer token's name too, corrupting it before
        # its own turn came up. Sorting longest-first is what makes plain
        # str.replace() safe here regardless of which tokens exist.
        for token in sorted(palette, key=len, reverse=True):
            value = palette[token]
            if token.endswith("_ICON"):
                value = str(assets_dir / value)
            text = text.replace(f"${token}", value)
        app.setStyleSheet(text)
        _current_theme_palette.clear()
        _current_theme_palette.update(palette)
    except OSError as exc:
        # Missing/unreadable style.qss shouldn't take the whole app down --
        # fall back to plain Fusion rather than crash at startup over theming.
        print(f"Warning: couldn't load {style_path} ({exc}); using unstyled Fusion.")


class _ComboPopupBackgroundFilter(QObject):
    """Fixes two real, confirmed-via-real-screen-capture combo-popup bugs
    that QWidget.grab() had wrongly suggested were already fixed --
    grab() renders a widget's own paint buffer, not real compositor
    output, and missed both.

    Bug 1, solid black top/bottom bars on every popup: the popup list's
    own top-level QFrame (Qt's internal QComboBoxPrivateContainer) has
    autoFillBackground False and frameShape NoFrame, so nothing paints
    its background by ordinary QWidget means -- it's meant to rely
    entirely on the QSS engine, which for some reason doesn't reach it
    via the app-wide cascade the way it does the QComboBoxListView nested
    inside it (that one's background applies correctly, via the existing
    "QComboBox QAbstractItemView" rule). Setting a stylesheet directly on
    the frame instance at Show time -- confirmed via real capture --
    paints it correctly where the cascade alone didn't.

    Matched by metaObject().className(), not isinstance/type(obj).__name__:
    PySide6 has no Python binding for this private class, so its Python
    type reports as the nearest exposed base (QFrame), indistinguishable
    that way from every *other* QFrame in the app. metaObject().className()
    reads Qt's real C++ class name regardless of Python bindings.

    Bug 2, QComboBox[modified="true"]'s italic/colored styling bleeding
    into its own popup's list items (every preset name shown italic and
    accent-colored, not just the closed combo's own text): not a cascade
    problem at all -- confirmed by resetting font/color directly on the
    frame and the QListView inside it, immediately, deferred by one event
    loop tick, every combination, with zero effect on the popup's
    rendering. What did work: temporarily clearing the "modified" property
    on the combo box *itself* while its popup is open. That means the
    popup's item delegate paints using the owning combo's own currently-
    matched QSS state directly, not anything inherited or copied onto the
    view/frame -- so the only way to keep the popup's rendering plain is
    to make the combo's own matched state plain for as long as the popup
    is on screen, then restore it on Hide so the closed combo still shows
    its modified indicator afterward.
    """

    def eventFilter(self, obj, event):
        if obj.metaObject().className() != "QComboBoxPrivateContainer":
            return False
        if event.type() == QEvent.Type.Show:
            bg = _current_theme_palette.get("BG_PANEL", "#21252c")
            obj.setStyleSheet(f"background-color: {bg};")
            for child in obj.children():
                if isinstance(child, QWidget):
                    child.setStyleSheet(f"background-color: {bg};")
            combo = obj.parent()
            if isinstance(combo, QComboBox) and combo.property("modified"):
                combo.setProperty("modified", False)
                combo.setProperty("_popupSuppressedModified", True)
                combo.style().unpolish(combo)
                combo.style().polish(combo)
        elif event.type() == QEvent.Type.Hide:
            combo = obj.parent()
            if isinstance(combo, QComboBox) and combo.property("_popupSuppressedModified"):
                combo.setProperty("modified", True)
                combo.setProperty("_popupSuppressedModified", False)
                combo.style().unpolish(combo)
                combo.style().polish(combo)
        return False


class _FocusVisibleFilter(QObject):
    """QSS has no :focus-visible equivalent -- plain :focus matches a
    mouse click exactly the same as Tab, so a checkbox clicked with the
    mouse picked up the same accent-colored ring Tab-ing to it does
    (reported directly, confirmed by screenshot) -- wrong the same way it
    would be in a browser without :focus-visible: a pointer click doesn't
    need a keyboard-navigation aid pointing at where it already is.

    QFocusEvent.reason() is exactly the signal a browser's own
    :focus-visible heuristic is standing in for -- TabFocusReason/
    BacktabFocusReason for real keyboard navigation, MouseFocusReason for
    a click, plus a handful of others (ActiveWindowFocusReason,
    PopupFocusReason, ShortcutFocusReason, ...) that aren't keyboard
    navigation either. This filter watches FocusIn/FocusOut app-wide and
    mirrors that distinction onto a "focusVisible" dynamic property,
    which style.qss matches instead of :focus for every control this
    applies to. Applied universally rather than scoped to specific widget
    types: a property no QSS rule references is a harmless no-op, so
    there's nothing to lose covering every focusable widget the same way
    instead of maintaining a matching type list here.
    """

    def eventFilter(self, obj, event):
        # FocusIn/FocusOut also reach plain QWindow objects (a top-level
        # window gaining/losing OS-level focus, not any widget inside it)
        # -- confirmed by a real crash, QWindow has no .style(). Only
        # QWidgets carry the QSS-matched property this filter sets.
        if not isinstance(obj, QWidget):
            return False
        if event.type() == QEvent.Type.FocusIn:
            visible = event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason)
            obj.setProperty("focusVisible", visible)
            obj.style().unpolish(obj)
            obj.style().polish(obj)
        elif event.type() == QEvent.Type.FocusOut:
            obj.setProperty("focusVisible", False)
            obj.style().unpolish(obj)
            obj.style().polish(obj)
        return False


def _validate_theme_choice(value) -> str:
    """QSettings hands back whatever was last stored there, which could be
    anything -- a hand-edited config file, a future/foreign version of this
    app, or simply nothing yet on first launch. Anything other than one of
    the three real choices falls back to dark rather than propagating into
    _resolve_theme (which only knows what to do with those three)."""
    return value if value in ("dark", "light", "system") else "dark"


def _resolve_theme(choice: str) -> str:
    """"dark"/"light" pass straight through; "system" resolves against the
    desktop's actual live color-scheme preference (confirmed this reports
    correctly on this machine's real desktop, not just assumed available
    because the Qt version is new enough) -- Unknown (a platform that
    doesn't expose one) falls back to dark, this app's original default."""
    if choice != "system":
        return choice
    scheme = QApplication.instance().styleHints().colorScheme()
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return "dark"
