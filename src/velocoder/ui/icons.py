"""Control icons: one SVG per glyph in assets/icons/, drawn in currentColor
and tinted per theme and button state when loaded.

One file instead of the _dark/_light pairs the status icons use: the colors
come from the live theme palette (theming._current_theme_palette), so the
system accent's TEXT_ON_ACCENT is right for a checked segment or the Convert
button whatever accent the desktop picked. Call refresh_icons(window) after
every stylesheet load; it re-tints every widget icon_button() set up."""
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QAbstractButton, QLabel, QTabWidget

from velocoder.ui.theming import _current_theme_palette

ICON_DIR = Path(__file__).parent / "assets" / "icons"
# Pixel sizes rendered into each QIcon; Qt picks the closest one at or
# above logical size x device pixel ratio (QT_SCALE_FACTOR=2 included).
_PIXEL_SIZES = (16, 20, 24, 32, 40, 48, 64)
_ICON_NAME_PROPERTY = "odcsIcon"
_ON_ACCENT_PROPERTY = "odcsIconOnAccent"


# Qt leaves only ~3 px between a button's icon and its text; icons beside
# text carry this much transparent space on their right instead (a fraction
# of the icon's side, so it scales with the text).
_TEXT_GAP = 0.25


def _render(svg: str, size: int, gap: float = 0) -> QPixmap:
    pixmap = QPixmap(size + round(size * gap), size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def themed_icon(name: str, on_accent: bool = False, gap: float = 0) -> QIcon:
    """The named icon in the current theme. on_accent: the button is
    accent-filled when enabled (Convert), so its normal state uses
    TEXT_ON_ACCENT. Checked (On) states always do: a checked segment is
    accent-filled (style.qss)."""
    palette = _current_theme_palette
    svg = (ICON_DIR / f"{name}.svg").read_text()
    normal = palette["TEXT_ON_ACCENT"] if on_accent else palette["TEXT_PRIMARY"]
    disabled = palette["TEXT_ACCENT_DISABLED"] if on_accent else palette["TEXT_DISABLED"]
    states = (
        (QIcon.Normal, QIcon.Off, normal),
        (QIcon.Active, QIcon.Off, normal),
        (QIcon.Disabled, QIcon.Off, disabled),
        (QIcon.Normal, QIcon.On, palette["TEXT_ON_ACCENT"]),
        (QIcon.Active, QIcon.On, palette["TEXT_ON_ACCENT"]),
        (QIcon.Disabled, QIcon.On, palette["TEXT_ACCENT_DISABLED"]),
    )
    icon = QIcon()
    for mode, state, color in states:
        tinted = svg.replace("currentColor", color)
        for size in _PIXEL_SIZES:
            icon.addPixmap(_render(tinted, size, gap), mode, state)
    return icon


def icon_size_for(widget, gap: float = 0) -> QSize:
    """Icons follow the text size: about the height of a capital letter plus
    its descenders, never under 16 px, so 14 pt and 22 pt text don't get
    tiny glyphs beside them."""
    side = max(16, round(QFontMetrics(widget.font()).height() * 0.85))
    return QSize(side + round(side * gap), side)


def icon_button(button, name: str, on_accent: bool = False):
    """Gives a QAbstractButton its icon and remembers the name so
    refresh_icons can re-tint it."""
    button.setProperty(_ICON_NAME_PROPERTY, name)
    button.setProperty(_ON_ACCENT_PROPERTY, on_accent)
    gap = _TEXT_GAP if button.text() else 0
    button.setIcon(themed_icon(name, on_accent, gap))
    button.setIconSize(icon_size_for(button, gap))
    return button


def icon_tabs(tabs: QTabWidget, names: list[str]):
    """One icon per tab, in tab order."""
    tabs.setProperty(_ICON_NAME_PROPERTY, names)
    for index, name in enumerate(names):
        tabs.setTabIcon(index, themed_icon(name, gap=_TEXT_GAP))
    tabs.setIconSize(icon_size_for(tabs, _TEXT_GAP))


def icon_label(label: QLabel, name: str):
    """A QLabel showing just the icon (a section heading's glyph)."""
    label.setProperty(_ICON_NAME_PROPERTY, name)
    size = icon_size_for(label)
    label.setPixmap(themed_icon(name).pixmap(size, label.devicePixelRatioF()))
    label.setFixedSize(size)
    return label


def refresh_icons(root):
    """Re-tints every icon set up by this module under root for the current
    theme (and the current text size)."""
    for button in root.findChildren(QAbstractButton):
        name = button.property(_ICON_NAME_PROPERTY)
        if name:
            icon_button(button, name, bool(button.property(_ON_ACCENT_PROPERTY)))
    for tabs in root.findChildren(QTabWidget):
        names = tabs.property(_ICON_NAME_PROPERTY)
        if names:
            icon_tabs(tabs, list(names))
    for label in root.findChildren(QLabel):
        name = label.property(_ICON_NAME_PROPERTY)
        if name:
            icon_label(label, name)
