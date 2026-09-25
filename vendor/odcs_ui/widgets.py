"""ODCS shared widgets (PySide6 Widgets). Styled by base.qss via objectNames.

    ViewSwitch        neutral segmented view switch (Status | Restore)
    SettingsList      grouped list of SettingRow: whole row is a button, value wraps
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

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen
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

    def __init__(self, labels: Iterable[str], parent: QWidget | None = None, accessible_name: str = "View"):
        super().__init__(parent)
        self.setObjectName("odcsViewSwitch")
        self.setAccessibleName(accessible_name)
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
            layout.addWidget(button)
        self._group.idToggled.connect(lambda index, on: on and self.currentChanged.emit(index))
        if self._group.buttons():
            self._group.button(0).setChecked(True)

    def buttons(self) -> list[QPushButton]:
        return [self._group.button(i) for i in range(len(self._group.buttons()))]

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
class SettingRow(QPushButton):
    """One setting: label, current value (wraps, never truncates), chevron.
    A real button, so it gets keyboard focus, Space/Enter and a Button
    accessibility role; the child labels ignore the mouse."""

    def __init__(self, label: str, value: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("odcsSettingRow")
        self.setAutoDefault(False)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        policy.setHeightForWidth(True)  # grows only as much as a wrapped value needs
        self.setSizePolicy(policy)
        self._label = QLabel(label)
        self._value = QLabel(value)
        self._value.setObjectName("odcsSettingValue")
        self._value.setWordWrap(True)
        self._value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._chevron = QLabel("›")
        self._chevron.setObjectName("odcsChevron")
        for child in (self._label, self._value, self._chevron):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(8)
        layout.addWidget(self._label)
        layout.addWidget(self._value, 1)
        layout.addWidget(self._chevron)
        self._update_accessible()

    def label(self) -> str:
        return self._label.text()

    def value(self) -> str:
        return self._value.text()

    def setValue(self, value: str, state: str = "") -> None:  # noqa: N802
        """state: "" | "warning" | "error" — colours the value; the words carry the meaning."""
        self._value.setText(value)
        set_role(self._value, state or "secondary")
        self._update_accessible()
        self.updateGeometry()

    def _update_accessible(self) -> None:
        self.setAccessibleName(f"{self._label.text()}: {self._value.text()}. Change")

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
            title = QLabel(heading)
            title.setObjectName("odcsGroupHeading")
            outer.addWidget(title)
        self._box = QFrame()
        self._box.setObjectName("odcsSettingsList")
        self._box.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._rows_layout = QVBoxLayout(self._box)
        self._rows_layout.setContentsMargins(1, 1, 1, 1)
        self._rows_layout.setSpacing(0)
        outer.addWidget(self._box)
        outer.addStretch(1)
        self.rows: list[SettingRow] = []

    def addRow(self, label: str, value: str = "", on_click: Callable[[], None] | None = None) -> SettingRow:  # noqa: N802
        row = SettingRow(label, value)
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
            title = QLabel(heading)
            title.setObjectName("odcsGroupHeading")
            outer.addWidget(title)
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
        for col, widget in enumerate((icon, title_label, desc_label, when_label)):
            cell = QFrame()
            cell.setObjectName("odcsFactCell")
            lay = QHBoxLayout(cell)
            lay.setContentsMargins(0, 8, 10, 8)
            lay.addWidget(widget)
            self._grid.addWidget(cell, row, col)
            cells.append(cell)
        button = None
        if not action:  # keep the hairline running under the action column
            filler = QFrame()
            filler.setObjectName("odcsFactCell")
            self._grid.addWidget(filler, row, 4)
            cells.append(filler)
        if action:
            button = QPushButton(action[0])
            button.setAutoDefault(False)
            button.clicked.connect(action[1])
            cell = QFrame()
            cell.setObjectName("odcsFactCell")
            lay = QHBoxLayout(cell)
            lay.setContentsMargins(0, 4, 0, 4)
            lay.addWidget(button)
            self._grid.addWidget(cell, row, 4)
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
