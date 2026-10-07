"""ODCS shared widgets (PySide6 Widgets). Styled by base.qss via objectNames.

    ViewSwitch        neutral segmented view switch (Status | Restore)
    SettingsList      grouped list of SettingRow: whole row is a button, label over
                      value (wraps), optional leading icon and status dot
    StatusDot         small status dot, always beside words
    StatusFacts       separate facts with their own result and date
                      (completed / checked / tested), "Not yet recorded" when never
    CollapsibleSection  leading ▸/▾ header, collapses to the header only
    EmptyState        icon + an instruction that names the action
    AboutDialog       the ODCS About dialog

Geometry never changes between states (DESIGN.md, Component states): selection
changes colors only, never font weight, border width or padding.
"""
from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .theming import current_tokens, set_role

ORGANIZATION = "ODCS App Studio"


def _set_property(widget, name: str, value) -> None:
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


# --------------------------------------------------------------------------
class ViewSwitch(QFrame):
    """Segmented switch between views. Selection is shown with a quiet raised
    segment, never the accent (the accent belongs to the task's action)."""

    currentChanged = Signal(int)

    def __init__(self, labels: Iterable[str], parent: QWidget | None = None, accessible_name: str = "View",
                 fill: bool = False):
        """fill=True: take the full width available (e.g. a sidebar's, lined up
        with the cards under it), split evenly between the segments."""
        super().__init__(parent)
        self.setObjectName("odcsViewSwitch")
        self._fill = fill
        self.setAccessibleName(accessible_name)
        # Hug the segments: in a taller row (a toolbar with larger buttons) a
        # stretched frame put the extra height above and below them, so the
        # 2 px inset looked wider at the top and bottom than at the sides.
        self.setSizePolicy(QSizePolicy.Expanding if fill else QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, label in enumerate(labels):
            button = QPushButton(label, self)
            button.setObjectName("odcsSegment")
            button.setCheckable(True)
            button.setAutoDefault(False)
            self._group.addButton(button, index)
            if fill:
                # Equal segments whatever their labels: ignore each label's own
                # width and share the frame evenly (minimumSizeHint keeps every
                # segment at least as wide as the widest label).
                button.setSizePolicy(QSizePolicy.Ignored, button.sizePolicy().verticalPolicy())
            layout.addWidget(button, 1 if fill else 0)
        self._group.idToggled.connect(lambda index, on: on and self.currentChanged.emit(index))
        if self._group.buttons():
            self._group.button(0).setChecked(True)

    def buttons(self) -> list[QPushButton]:
        return [self._group.button(i) for i in range(len(self._group.buttons()))]

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        hint = super().minimumSizeHint()
        buttons = self.buttons()
        if not self._fill or not buttons:
            return hint
        margins = self.layout().contentsMargins()
        widest = max(button.sizeHint().width() for button in buttons)
        width = (widest * len(buttons) + self.layout().spacing() * (len(buttons) - 1)
                 + margins.left() + margins.right())
        return QSize(max(hint.width(), width), hint.height())

    def currentIndex(self) -> int:  # noqa: N802 (Qt naming)
        return self._group.checkedId()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        button = self._group.button(index)
        if button is not None:
            button.setChecked(True)

    def keyPressEvent(self, event):  # noqa: N802 — arrows move between segments
        step = {Qt.Key_Left: -1, Qt.Key_Right: 1}.get(event.key())
        if step is None:
            return super().keyPressEvent(event)
        count = len(self._group.buttons())
        index = (self.currentIndex() + step) % count
        self.setCurrentIndex(index)
        self._group.button(index).setFocus(Qt.TabFocusReason)


# --------------------------------------------------------------------------
class StatusDot(QWidget):
    """Small filled status dot (ok / warning / error / never) in the live
    theme's colours. Always paired with words; never the only signal."""

    COLORS = {"ok": "SUCCESS", "warning": "WARNING", "error": "ERROR", "never": "TEXT_DISABLED"}

    def __init__(self, state: str = "ok", size: int = 8, parent: QWidget | None = None):
        super().__init__(parent)
        self._state = state
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def state(self) -> str:
        return self._state

    def setState(self, state: str) -> None:  # noqa: N802
        self._state = state
        self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(current_tokens()[self.COLORS.get(self._state, "TEXT_DISABLED")]))
        p.drawEllipse(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1))


_ZWSP = "\u200b"
_BREAK_AFTER = set("/\\-_·:@+|")
_LONGEST_RUN = 20


def _wrappable(text: str) -> str:
    """Display copy of `text` that word wrap can fit into any column: a
    zero-width space after path and name separators, and every
    _LONGEST_RUN characters of a run that has no break at all (hashes, IDs).
    Ordinary words are shorter and never break mid-word. Qt's own break
    rules around hyphens differ by platform, so they are not relied on."""
    out, run = [], 0
    for char in text:
        if char.isspace():
            run = 0
        elif run == _LONGEST_RUN:  # a longer run: break before its next character
            out.append(_ZWSP)
            run = 1
        else:
            run += 1
        out.append(char)
        if char in _BREAK_AFTER:
            out.append(_ZWSP)
            run = 0
    return "".join(out)


class _FittingLabel(QLabel):
    """A word-wrapped label that sets no minimum width: its width comes from
    the column and the text wraps into it, at any font size. Its size hint
    is the text on one line; QLabel's own hint guesses a squarish block, so
    a list sized from it came out far taller than its rows."""

    def __init__(self, text: str, name: str):
        super().__init__(text)
        self.setObjectName(name)
        self.setWordWrap(True)
        self.setMinimumWidth(1)  # an explicit minimum overrides the longest word's

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        margins = self.contentsMargins()
        width = (self.fontMetrics().horizontalAdvance(self.text().replace(_ZWSP, ""))
                 + margins.left() + margins.right() + 2 * self.margin())
        return QSize(width, self.heightForWidth(width))


class SettingRow(QPushButton):
    """One setting, two lines: the label, and under it the current value
    (wraps, never truncates) with an optional status dot; a chevron trails.
    Label and value wrap at any font size, so a row fits the width it is
    given rather than widening its column.
    An optional leading icon names the kind of setting. A real button, so it
    gets keyboard focus, Space/Enter and a Button accessibility role; the
    child widgets ignore the mouse."""

    ICON_SIZE = 20

    def __init__(self, label: str, value: str = "", parent: QWidget | None = None,
                 icon: QIcon | None = None):
        super().__init__(parent)
        self.setObjectName("odcsSettingRow")
        self.setAutoDefault(False)
        # Minimum, not Fixed: a Fixed row is capped at its one-line size hint,
        # so a layout could not give a wrapped value its height-for-width.
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        policy.setHeightForWidth(True)  # grows only as much as a wrapped value needs
        self.setSizePolicy(policy)
        self._icon = QLabel()
        self._icon.setObjectName("odcsSettingIcon")
        self._icon.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)
        self._text = value
        self._label = _FittingLabel(_wrappable(label), "odcsSettingLabel")
        self._label_text = label
        # An empty value still takes its line (constant geometry): a row whose
        # value has not loaded yet is already as tall as it will be.
        self._value = _FittingLabel(_wrappable(value) or _ZWSP, "odcsSettingValue")
        self._dot = StatusDot("ok")
        self._dot.hide()
        self._chevron = QLabel("›")
        self._chevron.setObjectName("odcsChevron")
        for child in (self._icon, self._label, self._value, self._chevron):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)
        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(6)
        # The dot sits on the value's first line, however many lines it wraps to.
        dot_box = QVBoxLayout()
        dot_box.setContentsMargins(0, self._value.fontMetrics().height() // 2 - 4, 0, 0)
        dot_box.addWidget(self._dot)
        dot_box.addStretch(1)
        value_row.addLayout(dot_box)
        value_row.addWidget(self._value, 1)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        text.addWidget(self._label)
        text.addLayout(value_row)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(10)
        layout.addWidget(self._icon)
        layout.addLayout(text, 1)
        layout.addWidget(self._chevron)
        self.setLeadingIcon(icon)
        self._update_accessible()

    def label(self) -> str:
        return self._label_text

    def value(self) -> str:
        return self._text

    def indicator(self) -> str | None:
        return self._dot.state() if not self._dot.isHidden() else None

    def setLeadingIcon(self, icon: QIcon | None) -> None:  # noqa: N802
        """None hides the icon column; a QIcon that is null (no theme icon)
        keeps the column empty so rows in one list stay aligned."""
        self._leading_icon = icon
        self._icon.setVisible(icon is not None)
        self._draw_icon()

    def _draw_icon(self) -> None:
        icon = getattr(self, "_leading_icon", None)
        if icon is not None:
            self._icon.setPixmap(icon.pixmap(self.ICON_SIZE, self.ICON_SIZE) if not icon.isNull() else QPixmap())

    def changeEvent(self, event):  # noqa: N802 (Qt API)
        # A theme switch can change the icon theme (match_icon_theme); the
        # label holds a rendered pixmap, so draw it again.
        if event.type() in (QEvent.StyleChange, QEvent.PaletteChange):
            self._draw_icon()
        super().changeEvent(event)

    def setValue(self, value: str, state: str = "", indicator: str | None = None) -> None:  # noqa: N802
        """state: "" | "warning" | "error" colours the value. indicator: a
        status dot before it ("ok" | "warning" | "error" | "never"), or None.
        Either way the words carry the meaning."""
        self._text = value
        self._value.setText(_wrappable(value) or _ZWSP)
        set_role(self._value, state or "secondary")
        self._dot.setVisible(indicator is not None)
        if indicator is not None:
            self._dot.setState(indicator)
        self._update_accessible()
        self.updateGeometry()

    def _update_accessible(self) -> None:
        self.setAccessibleName(f"{self._label_text}: {self._text}. Change")

    # QPushButton sizes itself from its own text; this one is laid out.
    def sizeHint(self) -> QSize:  # noqa: N802
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.layout().totalHeightForWidth(width)


class SettingsList(QFrame):
    """A grouped list of SettingRow with an optional heading above it."""

    def __init__(self, heading: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        if heading:
            outer.addWidget(_FittingLabel(heading, "odcsGroupHeading"))
        self._box = QFrame()
        self._box.setObjectName("odcsSettingsList")
        # Minimum: the rows' own base.qss `min-height: 0` lets each shrink to
        # nothing, so the box holds them at full size and never squeezes them.
        self._box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._rows_layout = QVBoxLayout(self._box)
        self._rows_layout.setContentsMargins(1, 1, 1, 1)
        self._rows_layout.setSpacing(0)
        outer.addWidget(self._box)
        outer.addStretch(1)
        self.rows: list[SettingRow] = []

    def addRow(self, label: str, value: str = "", on_click: Callable[[], None] | None = None,  # noqa: N802
               icon: QIcon | None = None) -> SettingRow:
        row = SettingRow(label, value, icon=icon)
        set_role(row._value, "secondary")
        for previous in self.rows:  # only the last row drops its separator
            _set_property(previous, "last", "false")
        _set_property(row, "last", "true")
        if on_click:
            row.clicked.connect(on_click)
        self._rows_layout.addWidget(row)
        self.rows.append(row)
        return row


# --------------------------------------------------------------------------
class StatusIcon(QWidget):
    """Painted status glyph in the current theme's colors:
    ok (check), warning (triangle), error (cross), never (dash), info (i)."""

    COLORS = {"ok": "SUCCESS", "warning": "WARNING", "error": "ERROR", "never": "TEXT_SECONDARY", "info": "TEXT_SECONDARY"}

    def __init__(self, state: str = "ok", size: int = 16, parent: QWidget | None = None):
        super().__init__(parent)
        self._state = state
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def state(self) -> str:
        return self._state

    def setState(self, state: str) -> None:  # noqa: N802
        self._state = state
        self.update()

    def paintEvent(self, event):  # noqa: N802
        values = current_tokens()
        pen = QPen(QColor(values[self.COLORS.get(self._state, "TEXT_SECONDARY")]))
        pen.setWidthF(max(1.6, self.width() / 8))
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(pen)
        s = float(self.width())
        if self._state == "ok":
            p.drawPolyline([QPointF(s * .2, s * .55), QPointF(s * .42, s * .76), QPointF(s * .82, s * .28)])
        elif self._state == "warning":
            path = QPainterPath(QPointF(s * .5, s * .1))
            path.lineTo(s * .93, s * .88)
            path.lineTo(s * .07, s * .88)
            path.closeSubpath()
            p.drawPath(path)
            p.drawLine(QPointF(s * .5, s * .4), QPointF(s * .5, s * .6))
            p.drawPoint(QPointF(s * .5, s * .74))
        elif self._state == "error":
            p.drawEllipse(QRectF(s * .08, s * .08, s * .84, s * .84))
            p.drawLine(QPointF(s * .36, s * .36), QPointF(s * .64, s * .64))
            p.drawLine(QPointF(s * .64, s * .36), QPointF(s * .36, s * .64))
        elif self._state == "info":
            p.drawEllipse(QRectF(s * .08, s * .08, s * .84, s * .84))
            p.drawLine(QPointF(s * .5, s * .46), QPointF(s * .5, s * .72))
            p.drawPoint(QPointF(s * .5, s * .3))
        else:  # never
            p.drawEllipse(QRectF(s * .12, s * .12, s * .76, s * .76))
            p.drawLine(QPointF(s * .34, s * .5), QPointF(s * .66, s * .5))
        p.end()


class StatusFacts(QFrame):
    """Separate facts, each with its own result and date. A fact that never
    happened says so ("Not yet recorded") instead of being left blank."""

    NEVER = "Not yet recorded"

    def __init__(self, heading: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("odcsStatusFacts")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        if heading:
            outer.addWidget(_FittingLabel(heading, "odcsGroupHeading"))
        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(0)  # cells carry the gap, so hairlines join up
        self._grid.setVerticalSpacing(0)
        self._grid.setColumnStretch(2, 1)
        outer.addLayout(self._grid)
        self.facts: dict[str, dict] = {}
        self._rows: list[list[QFrame]] = []

    def addFact(self, key: str, title: str, description: str = "", state: str = "never",  # noqa: N802
                when: str = "", action: tuple[str, Callable[[], None]] | None = None) -> None:
        row = len(self.facts)
        cells: list[QFrame] = []
        icon = StatusIcon(state)
        title_label = QLabel(title)
        desc_label = QLabel(description)
        set_role(desc_label, "secondary")
        desc_label.setWordWrap(True)
        when_label = QLabel(when or self.NEVER)
        set_role(when_label, "secondary")
        button = None
        if action:
            button = QPushButton(action[0])
            button.setAutoDefault(False)
            button.clicked.connect(action[1])
        for col, widget in enumerate((icon, title_label, desc_label, when_label)):
            cell = QFrame()
            cell.setObjectName("odcsFactCell")
            lay = QVBoxLayout(cell)
            lay.setContentsMargins(0, 8, 10, 8)
            lay.setSpacing(6)
            # Top-aligned, so a two-line fact keeps its icon, title and date
            # level with the first line.
            lay.setAlignment(Qt.AlignTop)
            lay.addWidget(widget)
            if widget is desc_label and button is not None:
                # The fact's action sits under its description, on the same
                # left edge, rather than in a column of its own.
                lay.addWidget(button, 0, Qt.AlignLeft)
            self._grid.addWidget(cell, row, col)
            cells.append(cell)
        # Separators run between facts, not under the last one.
        for previous in self._rows[-1:]:
            for cell in previous:
                _set_property(cell, "last", "false")
        for cell in cells:
            _set_property(cell, "last", "true")
        self._rows.append(cells)
        self.facts[key] = {"icon": icon, "title": title_label, "description": desc_label,
                           "when": when_label, "action": button}

    def setFact(self, key: str, state: str, when: str = "", description: str | None = None) -> None:  # noqa: N802
        fact = self.facts[key]
        fact["icon"].setState(state)
        fact["when"].setText(when or self.NEVER)
        if description is not None:
            fact["description"].setText(description)

    def whenText(self, key: str) -> str:  # noqa: N802
        return self.facts[key]["when"].text()


# --------------------------------------------------------------------------
class CollapsibleSection(QWidget):
    """Header with a leading ▸/▾; collapsed, only the header remains."""

    expandedChanged = Signal(bool)

    def __init__(self, title: str, content: QWidget, expanded: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self._title = title
        self.header = QPushButton(self)  # QPushButton: QSS text-align works
        self.header.setObjectName("odcsSectionHeader")
        self.header.setCheckable(True)
        self.header.setAutoDefault(False)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.content = content
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.header)
        layout.addWidget(content)
        self.header.toggled.connect(self._apply)
        self.header.setChecked(expanded)
        self._apply(expanded, emit=False)

    def isExpanded(self) -> bool:  # noqa: N802
        return self.header.isChecked()

    def setExpanded(self, expanded: bool) -> None:  # noqa: N802
        self.header.setChecked(expanded)

    def _apply(self, expanded: bool, emit: bool = True) -> None:
        self.header.setText(f"{'▾' if expanded else '▸'}  {self._title}")
        self.header.setAccessibleName(f"{self._title}, {'expanded' if expanded else 'collapsed'}")
        self.content.setVisible(expanded)
        if emit:
            self.expandedChanged.emit(expanded)


# --------------------------------------------------------------------------
class EmptyState(QWidget):
    """Icon + one instruction that names the action ("Drop videos here or choose Add Videos…")."""

    def __init__(self, message: str, icon: QIcon | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addStretch(1)  # stretches, not setAlignment: alignment breaks word-wrap height
        if icon is not None:
            glyph = QLabel()
            glyph.setPixmap(icon.pixmap(32, 32))
            glyph.setAlignment(Qt.AlignCenter)
            layout.addWidget(glyph)
        self.message = QLabel(message)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignCenter)
        set_role(self.message, "secondary")
        layout.addWidget(self.message)
        layout.addStretch(1)


# --------------------------------------------------------------------------
class AboutDialog(QDialog):
    """The ODCS About dialog: icon, name, version, one-line description,
    © year ODCS App Studio, optional extra buttons (System Information,
    Licenses), and Close as the default. Button order follows the platform."""

    def __init__(self, app_name: str, version: str, description: str = "", icon: QIcon | None = None,
                 year: int = 2026, extra_buttons: Iterable[tuple[str, Callable[[], None]]] = (),
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"About {app_name}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 16)
        layout.setSpacing(8)
        if icon is not None:
            glyph = QLabel()
            glyph.setPixmap(icon.pixmap(64, 64))
            glyph.setAlignment(Qt.AlignCenter)
            layout.addWidget(glyph)
        name = QLabel(app_name)
        name.setObjectName("odcsAboutName")
        name.setAlignment(Qt.AlignCenter)
        layout.addWidget(name)
        ver = QLabel(f"Version {version}")
        ver.setAlignment(Qt.AlignCenter)
        set_role(ver, "secondary")
        layout.addWidget(ver)
        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setAlignment(Qt.AlignCenter)
            layout.addWidget(desc)
        self.copyright = QLabel(f"© {year} {ORGANIZATION}")
        self.copyright.setAlignment(Qt.AlignCenter)
        set_role(self.copyright, "caption")
        layout.addSpacing(8)
        layout.addWidget(self.copyright)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        for label, callback in extra_buttons:
            box.addButton(label, QDialogButtonBox.ActionRole).clicked.connect(callback)
        box.rejected.connect(self.reject)
        close = box.button(QDialogButtonBox.Close)
        close.setDefault(True)
        layout.addSpacing(8)
        layout.addWidget(box)
