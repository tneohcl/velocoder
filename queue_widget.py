"""The drag-drop queue list widget, split out of main.py -- fully
self-contained (only needs a files-dropped callback), so it has no
MainWindow coupling to carry along."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget

from theming import _fuzzy_text_color

# Queue table columns -- source-file properties only; output settings live
# in the right-hand panel and apply live to whatever row is selected
# instead of being duplicated here. Resolution rides along in the Video
# cell ("H.264 1280x720") rather than getting its own column -- the panel
# this table lives in isn't wide enough to give lots of columns room
# without squeezing File down to nothing (confirmed against this app's own
# real persisted window geometry, not just its fresh-install default).
FILE_COL, VIDEO_COL, DURATION_COL, AUDIO_COL, SIZE_COL, RESULT_COL = range(6)
# The run-status icon (play/done/failed) lives on the File cell itself --
# QTreeWidgetItem supports an icon and text on the same column
# simultaneously -- rather than a dedicated narrow column of its own. A
# separate status column started out at 24px, as unobtrusive as it could
# reasonably be, but an empty, unlabeled column with nothing in it (every
# row's icon is blank until a run actually starts) still read as a stray
# gap rather than a deliberate part of the design -- confirmed by
# feedback, not just a guess. STATUS_COL is kept as a name (rather than
# writing FILE_COL at every icon/tooltip call site below) purely so those
# call sites stay self-explanatory about *why* they're touching this
# column.
STATUS_COL = FILE_COL
QUEUE_COLUMN_HEADERS = ["File", "Video", "Duration", "Audio", "Size", "Result"]


class DropTreeWidget(QTreeWidget):
    """Flat (no hierarchy) QTreeWidget -- gives the queue a real multi-column
    grid with a header bar while keeping the same "one item per row, holding
    its job dict via Qt.UserRole" shape QListWidgetItem had, so drag-drop and
    row reordering carry over unchanged. Accepts files dragged in from a file
    manager, and also supports dragging its own rows to reorder the queue."""

    PLACEHOLDER_TEXT = "Drag video files here,\nor click “Add Files…”"

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

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)  # internal row-reorder drag

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
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
            painter.setPen(QColor(_fuzzy_text_color(self)))
            painter.drawText(self.viewport().rect(), Qt.AlignCenter, self.PLACEHOLDER_TEXT)
            painter.end()
