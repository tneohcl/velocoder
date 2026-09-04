"""The drag-drop queue list widget, split out of main.py -- fully
self-contained (only needs a files-dropped callback), so it has no
MainWindow coupling to carry along."""
from pathlib import Path

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeWidget,
)

from theming import _current_theme_palette, _fuzzy_text_color

# Queue table columns -- source-file properties only; output settings live
# in the right-hand panel and apply live to whatever row is selected
# instead of being duplicated here. Four now, not six: a dedicated Audio
# column (and the separate File column the Video one has absorbed, see
# STATUS_COL/_VideoCellDelegate below) read as spreadsheet-like density
# for information most people never needed at a glance -- still there,
# just in the row's own tooltip (_row_tooltip, queue_controller.py) via
# the full settings_summary instead of a permanent column of its own.
VIDEO_COL, DURATION_COL, SIZE_COL, RESULT_COL = range(4)
# The run-status icon (play/done/failed) lives on the Video cell itself --
# QTreeWidgetItem supports an icon and text on the same column
# simultaneously -- rather than a dedicated narrow column of its own. A
# separate status column started out at 24px, as unobtrusive as it could
# reasonably be, but an empty, unlabeled column with nothing in it (every
# row's icon is blank until a run actually starts) still read as a stray
# gap rather than a deliberate part of the design -- confirmed by
# feedback, not just a guess. STATUS_COL is kept as a name (rather than
# writing VIDEO_COL at every icon/job-dict call site below) purely so
# those call sites stay self-explanatory about *why* they're touching
# this column -- NOT the same thing as RESULT_COL below (the actually
# visible "Ready / Converting… / done / failed" status text column);
# STATUS_COL is only ever an anonymous Qt.UserRole storage slot for each
# row's job settings dict.
STATUS_COL = VIDEO_COL
# A second, distinct data slot on that same column -- now that VIDEO_COL
# and STATUS_COL are the same index (they weren't before this file
# merged the old separate File column into Video), item.data(VIDEO_COL,
# Qt.UserRole) and item.data(STATUS_COL, Qt.UserRole) would otherwise be
# the exact same (column, role) pair in the model, so the job dict and
# the codec/resolution subtitle text would silently overwrite each other.
# Qt.UserRole stays reserved for STATUS_COL's job dict (unchanged,
# dozens of existing call sites); the subtitle uses this role instead.
VIDEO_SUBTITLE_ROLE = Qt.UserRole + 1
QUEUE_COLUMN_HEADERS = ["Video", "Duration", "Size", "Status"]


class _VideoCellDelegate(QStyledItemDelegate):
    """Paints the Video column as a two-line card -- filename (bold) over
    a muted resolution/codec subtitle, e.g. "1920x1080 H.264" -- instead
    of QTreeWidgetItem's native single-line text, so the queue reads
    closer to a list of videos than a spreadsheet row. The status icon
    (play/done/warning, set via item.setIcon(STATUS_COL, ...) same as
    always) and selection/hover background still come from the base
    delegate's own CE_ItemViewItem painting; only the text itself is
    custom-drawn here.

    Colors come from theming._current_theme_palette (the module-level
    dict every theme switch keeps current), matching the same pattern
    DropTreeWidget's own empty-state placeholder text already uses below
    -- not option.palette, which isn't reliably kept in sync with this
    app's QSS-driven (not QPalette-driven) color scheme for custom-painted
    content."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        title = opt.text
        # QIcon(opt.icon), not bare opt.icon -- confirmed directly that
        # PySide6's property accessor here doesn't hand back an independent
        # copy the way Qt's own value-type semantics would suggest: without
        # the explicit copy constructor, this variable went null the moment
        # opt.icon = QIcon() ran below too, silently dropping the icon from
        # every real render (caught by screenshot -- the checkmark just
        # wasn't there -- not by any exception, since nothing raises).
        icon = QIcon(opt.icon)
        has_icon = not icon.isNull()
        icon_width = opt.decorationSize.width() + 6 if has_icon else 0
        opt.text = ""  # background/selection/hover only -- text is custom-painted below
        # Icon cleared too, painted manually further down instead: Fusion's
        # own CE_ItemViewItem icon placement centers against a single line
        # of text (its own internal layout has no idea this delegate paints
        # two custom lines below it) -- confirmed by screenshot, the status
        # icon sat visibly low, roughly level with the title/subtitle
        # boundary rather than centered against the whole two-line row.
        opt.icon = QIcon()
        style = opt.widget.style() if opt.widget else self.parent().style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        text_rect = opt.rect.adjusted(icon_width + 4, 2, -4, -2)
        subtitle = index.data(VIDEO_SUBTITLE_ROLE) or ""

        if has_icon:
            size = opt.decorationSize
            icon_rect = QRect(
                opt.rect.x() + 4, opt.rect.y() + (opt.rect.height() - size.height()) // 2,
                size.width(), size.height(),
            )
            icon.paint(painter, icon_rect, Qt.AlignCenter)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        title_font = QFont(opt.font)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(_current_theme_palette.get("TEXT_PRIMARY", "#e6e8eb")))
        half_height = text_rect.height() / 2
        # Elided against the live cell width, not the raw string -- this is
        # paint-only: item.text(VIDEO_COL)/VIDEO_SUBTITLE_ROLE stay full-length
        # underneath (the tooltip, settings, everything else reads those, not
        # this call), and re-runs on its own on every resize since paint()
        # itself does, so a dragged column border doesn't need its own
        # resize hook to stay in sync.
        elided_title = painter.fontMetrics().elidedText(title, Qt.ElideRight, int(text_rect.width()))
        painter.drawText(
            QRectF(text_rect.x(), text_rect.y(), text_rect.width(), half_height),
            Qt.AlignLeft | Qt.AlignVCenter, elided_title,
        )
        if subtitle:
            subtitle_font = QFont(opt.font)
            subtitle_font.setPointSizeF(max(opt.font.pointSizeF() - 1, 7))
            painter.setFont(subtitle_font)
            painter.setPen(QColor(_current_theme_palette.get("TEXT_SECONDARY", "#8b93a1")))
            elided_subtitle = painter.fontMetrics().elidedText(subtitle, Qt.ElideRight, int(text_rect.width()))
            painter.drawText(
                QRectF(text_rect.x(), text_rect.y() + half_height, text_rect.width(), half_height),
                Qt.AlignLeft | Qt.AlignVCenter, elided_subtitle,
            )
        painter.restore()

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        # Room for two text lines plus the same vertical padding
        # QTreeWidget::item's own QSS rule (style.qss) already gives
        # every row -- not an arbitrary constant, so this stays in step
        # if that padding is ever tuned.
        return QSize(size.width(), int(size.height() * 1.8) + 8)


def _empty_state_icon() -> QIcon:
    # No self._theme_choice reachable here -- this file deliberately has
    # no MainWindow coupling (its own module docstring). Reuses the theme
    # name already baked into _current_theme_palette's own CHECK_ICON
    # entry (themes.py: "check_dark.svg"/"check_light.svg") rather than
    # tracking a second, separate copy of which theme is currently active.
    suffix = "dark" if "_dark" in _current_theme_palette.get("CHECK_ICON", "check_dark.svg") else "light"
    return QIcon(str(Path(__file__).parent / "assets" / f"video_{suffix}.svg"))


class DropTreeWidget(QTreeWidget):
    """Flat (no hierarchy) QTreeWidget -- gives the queue a real multi-column
    grid with a header bar while keeping the same "one item per row, holding
    its job dict via Qt.UserRole" shape QListWidgetItem had, so drag-drop and
    row reordering carry over unchanged. Accepts files dragged in from a file
    manager, and also supports dragging its own rows to reorder the queue."""

    PLACEHOLDER_TEXT = "Drop videos here\nor click “Add Videos…”"
    # Shown instead of PLACEHOLDER_TEXT while a valid file drag is actively
    # hovering an empty queue -- a present-tense confirmation instead of
    # static instructions, so the empty state visibly acknowledges the drag
    # in progress rather than just sitting there. Only the empty-queue paint
    # path uses this; a populated queue mid-drag shows the accent border
    # (see _set_drag_active/style.qss's QTreeWidget[dragActive]) over its
    # real rows, no text overlay -- covering actual queue content with
    # placeholder text would be worse, not better.
    DRAG_ACTIVE_PLACEHOLDER_TEXT = "Drop to add videos"

    def __init__(self, on_files_dropped, on_reordered=None, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self._on_files_dropped = on_files_dropped
        self._drag_active = False
        # Called right before a real internal-move drop is about to
        # reorder rows (undo/redo's push point for this one operation --
        # see _push_undo_snapshot in queue_controller.py). Optional so
        # this widget stays usable standalone without wiring undo/redo.
        self._on_reordered = on_reordered
        # Dropping a file in mid-run is fine (add_files pushes it straight
        # into the run in progress) and was never gated by this -- but
        # reordering *existing* rows mid-run is different: the running
        # queue captured its own execution-order snapshot at Start
        # (MainWindow._running_items / TranscodeQueue._jobs), which a
        # drag here doesn't touch. Confirmed this was a real gap: nothing
        # stopped a mid-run drag before, so the visible order could show
        # something other than what was actually executing, and status
        # icons (looked up by position) could land on the wrong row.
        # Blocking only the internal-move branch of dropEvent, not
        # dropEvent entirely, keeps external file drops working during a
        # run exactly as before.
        self.reorder_locked = False
        # Parented to self so the delegate's own paint() can reach back to
        # self.parent().style() for the real widget style (Fusion, in this
        # app) when Qt hands it an option.widget of None -- observed under
        # some paint paths, not just a defensive guess.
        self.setItemDelegateForColumn(VIDEO_COL, _VideoCellDelegate(self))

    def _has_local_file_url(self, mime_data) -> bool:
        # Same validity bar dropEvent already applies when actually acting
        # on a drop (Path(u.toLocalFile()) for ... if u.isLocalFile()) --
        # the accent border below should promise exactly what a drop here
        # will really do, not a looser "any URL at all" check that could
        # light up for e.g. a dragged browser link.
        return mime_data.hasUrls() and any(u.isLocalFile() for u in mime_data.urls())

    def _set_drag_active(self, active: bool):
        if self._drag_active == active:
            return
        self._drag_active = active
        # Same dynamic-property + unpolish/polish pattern already used
        # throughout this app for state QSS alone can't select on (theming.
        # py's focusVisible, the preset combo's modified property, rc_
        # filesize_btn's segEnd) -- not a bespoke mechanism for this one case.
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()  # repaint -- also swaps the empty-state placeholder text

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_drag_active(self._has_local_file_url(event.mimeData()))
        else:
            super().dragEnterEvent(event)  # internal row-reorder drag

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_drag_active(False)
        if event.mimeData().hasUrls():
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            self._on_files_dropped(paths)
        elif not self.reorder_locked:
            self._reorder_rows(event)
        else:
            event.ignore()

    def _reorder_rows(self, event):
        # QTreeWidget's own InternalMove drop handling is built for real
        # trees: dropping squarely on top of a row (rather than near its
        # top/bottom edge) reparents the dragged row as a CHILD of the
        # target instead of reordering siblings. This list is flat by
        # design (setRootIsDecorated(False), items never expanded), so
        # that child silently stops being drawn -- it reads as the
        # dragged file "disappearing". Reimplemented as a manual
        # top-level-only move so every drop, anywhere on a row, is a
        # sibling reorder and nothing ever becomes a child.
        selected = sorted(self.selectedItems(), key=self.indexOfTopLevelItem)
        if not selected:
            event.ignore()
            return

        if self._on_reordered:
            self._on_reordered()

        pos = event.position().toPoint()
        target_item = self.itemAt(pos)
        if target_item is None or target_item in selected:
            insert_at = self.topLevelItemCount()
        else:
            insert_at = self.indexOfTopLevelItem(target_item)
            row_rect = self.visualItemRect(target_item)
            if pos.y() >= row_rect.center().y():
                insert_at += 1  # dropped on the lower half: insert after

        removed_before_target = sum(
            1 for it in selected if self.indexOfTopLevelItem(it) < insert_at
        )
        for it in selected:
            self.takeTopLevelItem(self.indexOfTopLevelItem(it))
        insert_at = max(0, min(insert_at - removed_before_target, self.topLevelItemCount()))
        for offset, it in enumerate(selected):
            self.insertTopLevelItem(insert_at + offset, it)
            it.setSelected(True)

        event.acceptProposedAction()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.topLevelItemCount() == 0:
            painter = QPainter(self.viewport())
            rect = self.viewport().rect()
            if self._drag_active:
                # Text only during an active drag -- no icon overlay here,
                # matching the original drag-hover design intent of not
                # adding decoration on top of an in-progress action.
                painter.setPen(_fuzzy_text_color(self))
                painter.drawText(rect, Qt.AlignCenter, self.DRAG_ACTIVE_PLACEHOLDER_TEXT)
            else:
                # One restrained icon above the idle placeholder text --
                # centered as one block, not each centered independently,
                # so the icon reads as sitting just above the text rather
                # than floating unrelated to it.
                icon_size = 32
                gap = 12
                text_height = painter.fontMetrics().height() * 2  # PLACEHOLDER_TEXT is two lines
                block_top = rect.center().y() - (icon_size + gap + text_height) // 2
                icon_rect = QRect(rect.center().x() - icon_size // 2, block_top, icon_size, icon_size)
                _empty_state_icon().paint(painter, icon_rect)
                text_rect = QRect(rect.x(), icon_rect.bottom() + gap, rect.width(), text_height)
                painter.setPen(_fuzzy_text_color(self))
                painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop, self.PLACEHOLDER_TEXT)
            painter.end()
