"""Non-modal, built-in Help window -- search field + category/topic tree
on the left, article viewer on the right. Fully self-contained, no
MainWindow coupling (same design intent as queue_widget.py's own
docstring) -- main.py's _show_help_window is the only thing that
constructs one, as a lazy singleton (same pattern as _log_window/
_settings_dialog there).
"""
from PySide6.QtCore import Qt, QSettings
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QTextBrowser, QSplitter,
)

import help_content
from constants import APP_NAME
from theming import _current_theme_palette

_TOPIC_ID_ROLE = Qt.UserRole + 1


class HelpWindow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # Qt.Window, not the default Qt.Widget flag a QWidget with a
        # parent otherwise gets -- this needs to be a genuine top-level
        # window (its own taskbar/alt-tab entry, independent of whether
        # the main window is minimized) rather than staying visually
        # attached to it, since it's meant to stay open and usable while
        # the user keeps working in the main window.
        self.setWindowFlag(Qt.Window, True)
        self.setWindowTitle(f"{APP_NAME} Help")
        # Matched by a dedicated style.qss rule (QWidget#helpWindow) --
        # a plain QWidget doesn't match the app's QMainWindow/QDialog
        # background rule on its own.
        self.setObjectName("helpWindow")
        # Same (org, app) pair main.py's own QSettings uses -- a separate
        # QSettings instance pointed at the identical backing store, not
        # a separate settings file. This module never imports main.py
        # (would be circular), so it can't just reuse MainWindow's
        # existing instance.
        self._qsettings = QSettings(APP_NAME, APP_NAME)
        self._topics = help_content.load_topics()
        self._current_topic_id: str | None = None

        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search Help")
        self.search_field.textChanged.connect(self._on_search_changed)

        self.topic_tree = QTreeWidget()
        self.topic_tree.setHeaderHidden(True)
        self.topic_tree.itemSelectionChanged.connect(self._on_topic_selected)

        self.article_view = QTextBrowser()
        self.article_view.setOpenExternalLinks(False)

        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search_field)
        left_layout.addWidget(self.topic_tree)
        left_panel = QWidget()
        left_panel.setLayout(left_layout)

        splitter = QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(self.article_view)
        # Nav pane keeps a fixed-ish starting width, the article gets the
        # rest -- same "one side is the inspector, the other is the real
        # content" shape as the main window's own left/right split.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 540])

        layout = QVBoxLayout(self)
        # Zeroed -- Qt's own default margins otherwise leave a border of
        # this window's own background showing around the splitter, on
        # top of already being unnecessary padding for a window that's
        # just the splitter and nothing else.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._populate_tree(self._topics)
        if self._topics:
            self._select_topic(self._topics[0].id)

        QShortcut(QKeySequence("Ctrl+F"), self, self.search_field.setFocus)

        self._restore_geometry()

    def _populate_tree(self, topics: list[help_content.Topic]):
        matching_ids = {topic.id for topic in topics}
        self.topic_tree.clear()
        category_items: dict[str, QTreeWidgetItem] = {}
        bold = QFont(self.topic_tree.font())
        bold.setWeight(QFont.Weight(600))
        # Always walks self._topics (every topic, canonical order), not
        # the possibly-filtered `topics` list itself -- membership in
        # matching_ids decides what gets shown, but the category
        # grouping and topic order inside each category never changes
        # just because a search narrowed things down.
        for topic in self._topics:
            if topic.id not in matching_ids:
                continue
            category_item = category_items.get(topic.category)
            if category_item is None:
                category_item = QTreeWidgetItem([topic.category])
                # Enabled but not selectable -- a category heading, not
                # a topic of its own.
                category_item.setFlags(Qt.ItemIsEnabled)
                category_item.setFont(0, bold)
                self.topic_tree.addTopLevelItem(category_item)
                category_items[topic.category] = category_item
            topic_item = QTreeWidgetItem([topic.title])
            topic_item.setData(0, _TOPIC_ID_ROLE, topic.id)
            category_item.addChild(topic_item)
        self.topic_tree.expandAll()

    def _on_search_changed(self, text: str):
        matches = help_content.search_topics(self._topics, text)
        self._populate_tree(matches)

    def _on_topic_selected(self):
        items = self.topic_tree.selectedItems()
        if not items:
            return
        topic_id = items[0].data(0, _TOPIC_ID_ROLE)
        if topic_id:
            self._current_topic_id = topic_id
            self._render_article(topic_id)

    def _select_topic(self, topic_id: str):
        for i in range(self.topic_tree.topLevelItemCount()):
            category_item = self.topic_tree.topLevelItem(i)
            for j in range(category_item.childCount()):
                topic_item = category_item.child(j)
                if topic_item.data(0, _TOPIC_ID_ROLE) == topic_id:
                    self.topic_tree.setCurrentItem(topic_item)
                    return

    def _render_article(self, topic_id: str):
        topic = next((t for t in self._topics if t.id == topic_id), None)
        if topic is None:
            return
        body_html = help_content.render_markdown_subset(topic.body)
        self.article_view.setHtml(self._wrap_html(topic.title, body_html))

    @staticmethod
    def _wrap_html(title: str, body_html: str) -> str:
        # Explicit theme-derived colors in the HTML itself, not left to
        # inherited QSS -- QTextBrowser's rich-text engine renders its
        # own document, which the app-level stylesheet's token
        # substitution never reaches. Read fresh every render (here and
        # in refresh_theme below) rather than baked in once, matching
        # every other dynamic-per-theme read in this app.
        fg = _current_theme_palette.get("TEXT_PRIMARY", "#e6e8eb")
        bg = _current_theme_palette.get("BG_PANEL", "#21252c")
        return (
            f"<html><head><style>"
            f"body {{ background-color: {bg}; color: {fg};"
            f" font-family: sans-serif; font-size: 10pt; }}"
            f"h2 {{ color: {fg}; }} h3 {{ color: {fg}; }} b {{ color: {fg}; }}"
            f"li {{ margin-bottom: 6px; }} p {{ line-height: 1.4; }}"
            f"</style></head><body><h2>{title}</h2>{body_html}</body></html>"
        )

    def refresh_theme(self):
        # Called from main.py's _apply_theme/_on_system_theme_changed --
        # _load_stylesheet reloading the app-level QSS alone doesn't
        # touch already-rendered rich-text HTML content, so the
        # currently displayed article needs an explicit re-render to
        # pick up the new theme's colors.
        if self._current_topic_id is not None:
            self._render_article(self._current_topic_id)

    def _restore_geometry(self):
        geometry = self._qsettings.value("help_window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(760, 520)

    def closeEvent(self, event):
        self._qsettings.setValue("help_window_geometry", self.saveGeometry())
        super().closeEvent(event)
