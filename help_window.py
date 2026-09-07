"""Non-modal, built-in Help window -- search field + category/topic tree
on the left, article viewer on the right. Fully self-contained, no
MainWindow coupling (same design intent as queue_widget.py's own
docstring) -- main.py's _show_help_window is the only thing that
constructs one, as a lazy singleton (same pattern as _log_window/
_settings_dialog there).
"""
from PySide6.QtCore import Qt, QSettings, QSize
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QTextBrowser, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
)

import help_content
from constants import APP_NAME
from theming import _current_theme_palette

_TOPIC_ID_ROLE = Qt.UserRole + 1
# topic_tree runs at indentation()==0 (see _RoundedSelectionDelegate's
# own docstring for why) -- this recreates "topics nested under their
# category" by hand, as a text-only inset, instead.
_TOPIC_TEXT_INSET = 20


class _RoundedSelectionDelegate(QStyledItemDelegate):
    """Paints a genuinely rounded selection/hover highlight for topic
    rows. QSS border-radius on QTreeWidget::item:selected silently
    loses to Fusion's own native item-selection painting -- confirmed
    directly via a pixel scan of the actual rendered corner (a hard
    flat transition, not a curve, at any radius value style.qss gives
    it), the same class of native-subcontrol limitation already
    documented elsewhere in this app's own styling history. Category
    heading rows (not selectable at all) are untouched, painted by the
    base implementation.

    Requires topic_tree to run at indentation()==0 (set in
    HelpWindow.__init__) -- confirmed the hard way that Qt's per-depth
    indent/branch column, once it exists at all, paints its own
    accent-colored current-item indicator there directly (via a native
    code path outside anything a delegate, a QProxyStyle, or QSS could
    intercept: traced and ruled out one at a time -- itemDelegate().
    paint() entirely skipped, drawRoundedRect() calls traced app-wide
    down to exactly one call (this class's own), the queue_list-style
    QProxyStyle applied both per-widget and as the whole application's
    style before any stylesheet existed). Removing indentation (not
    just rootIsDecorated's arrows) removes the column that indicator
    was painted in, which is what actually fixed it -- _TOPIC_TEXT_INSET
    (module level) recreates the lost "nested under its category" look
    as a plain text-position offset instead."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection_color = QColor("#2a3f52")
        self.hover_color = QColor("#262b33")

    def refresh_theme(self):
        self.selection_color = QColor(_current_theme_palette.get("SELECTION_BG", "#2a3f52"))
        self.hover_color = QColor(_current_theme_palette.get("HOVER_BG", "#262b33"))

    def paint(self, painter, option, index):
        if not (index.flags() & Qt.ItemIsSelectable):
            super().paint(painter, option, index)
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            rect = option.rect.adjusted(2, 1, -2, -1)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.selection_color if selected else self.hover_color)
            painter.drawRoundedRect(rect, 6, 6)
            painter.restore()
        # A copy with the selected/hover state bits cleared -- paints
        # the text (and nothing else) on top of the rounded background
        # already drawn above, instead of also letting the base
        # implementation draw its own flat-cornered one underneath it.
        # Shifted right by _TOPIC_TEXT_INSET so topic text still reads
        # as nested under its (unshifted, flush-left) category heading,
        # even though the tree itself no longer reserves any indent
        # column to produce that offset natively.
        plain_option = QStyleOptionViewItem(option)
        plain_option.state &= ~QStyle.StateFlag.State_Selected
        plain_option.state &= ~QStyle.StateFlag.State_MouseOver
        plain_option.rect = plain_option.rect.adjusted(_TOPIC_TEXT_INSET, 0, 0, 0)
        super().paint(painter, plain_option, index)


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
        # Set only while the article pane is showing the "no results"
        # message instead of a real topic -- refresh_theme needs to know
        # which of the two to re-render, since only one of
        # _current_topic_id/_no_results_query is ever meaningful at a time.
        self._no_results_query: str | None = None

        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Search Help")
        self.search_field.textChanged.connect(self._on_search_changed)

        self.topic_tree = QTreeWidget()
        # Matched by a dedicated style.qss rule (QTreeWidget#helpTopicTree)
        # -- more specific than the app's own generic QTreeWidget::item
        # rule (the main queue list's own row separators), so this tree
        # can drop those without touching the queue's. Reported live:
        # a separator under every single topic read as a property/table
        # widget, not a Help sidebar.
        self.topic_tree.setObjectName("helpTopicTree")
        self.topic_tree.setHeaderHidden(True)
        # Categories never collapse and topics never have children of
        # their own, so there's no real expand/collapse arrow anywhere in
        # this tree -- but *indentation* (reserved per-depth space, not
        # just the arrow glyph) is a separate Qt property, on by default
        # regardless. Confirmed the hard way that as long as it's
        # nonzero, Qt paints its own accent-colored current-item
        # indicator inside that reserved column, via a native code path
        # nothing short of removing the column itself could intercept --
        # not the delegate (skipping its own super().paint() entirely
        # didn't stop it), not QSS (`::branch` background/border), not a
        # QProxyStyle (tried both per-widget and as the whole
        # application's style, set before any stylesheet existed).
        # Zeroing indentation removes the column outright.
        # _RoundedSelectionDelegate.paint() recreates the lost "topic
        # nested under its category" look as a plain text-position
        # offset (_TOPIC_TEXT_INSET) instead.
        self.topic_tree.setIndentation(0)
        self.topic_tree.itemSelectionChanged.connect(self._on_topic_selected)
        # Mouse tracking -- the delegate below reads State_MouseOver per
        # row, which needs this on to update as the pointer moves rather
        # than only registering hover on a click.
        self.topic_tree.setMouseTracking(True)
        self._topic_tree_delegate = _RoundedSelectionDelegate(self.topic_tree)
        self._topic_tree_delegate.refresh_theme()
        self.topic_tree.setItemDelegate(self._topic_tree_delegate)

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
        # One point smaller than topics, not just a different color --
        # reported live that the sidebar read like a property/table
        # widget; a category heading reading closer to a topic's own
        # size was part of why nothing distinguished the two roles at a
        # glance.
        category_font = QFont(bold)
        category_font.setPointSize(max(1, bold.pointSize() - 1))
        category_color = self._category_text_color()
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
                # Uppercase display text, not the stored category name
                # itself -- topic.category (used for grouping/lookup
                # elsewhere) stays exactly as index.json wrote it.
                category_item = QTreeWidgetItem([topic.category.upper()])
                # Enabled but not selectable -- a category heading, not
                # a topic of its own.
                category_item.setFlags(Qt.ItemIsEnabled)
                category_item.setFont(0, category_font)
                category_item.setForeground(0, category_color)
                # A little taller than an ordinary row -- the extra
                # breathing room a category heading needs to read as a
                # section break rather than just another item in the
                # same list (reported live: too many thin separator
                # lines made this read as a dense table, not a sidebar).
                category_item.setSizeHint(0, QSize(-1, 30))
                self.topic_tree.addTopLevelItem(category_item)
                category_items[topic.category] = category_item
            topic_item = QTreeWidgetItem([topic.title])
            topic_item.setData(0, _TOPIC_ID_ROLE, topic.id)
            category_item.addChild(topic_item)
        self.topic_tree.expandAll()

    @staticmethod
    def _category_text_color() -> QColor:
        # A muted color for category headings specifically -- QSS has no
        # way to select "the depth-0 items" of a QTreeWidget (its
        # per-role styling is all pseudo-states like :selected/:hover,
        # not structural depth), so this is set directly in Python
        # instead, and re-applied on every theme change the same way
        # the rich-text article view's own colors are (see refresh_theme).
        return QColor(_current_theme_palette.get("TEXT_SECONDARY", "#8b93a1"))

    def _on_search_changed(self, text: str):
        # Reported live, twice: filtering the tree left the article pane
        # showing whatever was selected *before* the search, since
        # _populate_tree's own topic_tree.clear() drops the previous
        # selection without ever choosing a new one -- nothing then
        # fires itemSelectionChanged to re-render anything. A search
        # narrowing to zero matches was the worse version of the same
        # gap: an unrelated old article stayed on screen with nothing on
        # screen explaining why.
        matches = help_content.search_topics(self._topics, text)
        self._populate_tree(matches)
        match_ids = {topic.id for topic in matches}
        if self._current_topic_id in match_ids:
            # Still a real match -- re-select it in the freshly rebuilt
            # tree (its previous QTreeWidgetItem no longer exists) rather
            # than re-rendering, since nothing about the article itself
            # needs to change.
            self._select_topic(self._current_topic_id)
        elif matches:
            self._select_topic(matches[0].id)
        else:
            self._render_no_results(text)

    def _render_no_results(self, query: str):
        self._current_topic_id = None
        self._no_results_query = query
        self.article_view.setHtml(self._wrap_html(
            "No Results", f"<p>No Help topics found for “{query}”.</p>",
        ))

    def _on_topic_selected(self):
        items = self.topic_tree.selectedItems()
        if not items:
            return
        topic_id = items[0].data(0, _TOPIC_ID_ROLE)
        if topic_id:
            self._current_topic_id = topic_id
            self._no_results_query = None
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
        body_html = help_content.render_markdown_subset(topic.body, dark=self._is_dark_theme())
        self.article_view.setHtml(self._wrap_html(topic.title, body_html))

    @staticmethod
    def _is_dark_theme() -> bool:
        # Screenshots embedded in an article ship as dark/light pairs
        # (help_content._render_image) -- picking between them needs to
        # know which one this render is for. No theme *name* is tracked
        # anywhere (_current_theme_palette is colors only), so this
        # reads the same signal a person would: whether the panel
        # background is actually dark or light.
        bg = QColor(_current_theme_palette.get("BG_PANEL", "#21252c"))
        return bg.lightness() < 128

    @staticmethod
    def _wrap_html(title: str, body_html: str) -> str:
        # Explicit theme-derived colors in the HTML itself, not left to
        # inherited QSS -- QTextBrowser's rich-text engine renders its
        # own document, which the app-level stylesheet's token
        # substitution never reaches. Read fresh every render (here and
        # in refresh_theme below) rather than baked in once, matching
        # every other dynamic-per-theme read in this app.
        fg = _current_theme_palette.get("TEXT_PRIMARY", "#e6e8eb")
        secondary = _current_theme_palette.get("TEXT_SECONDARY", "#8b93a1")
        bg = _current_theme_palette.get("BG_PANEL", "#21252c")
        card_bg = _current_theme_palette.get("BG_CONTROL", "#2a2f38")
        border = _current_theme_palette.get("BORDER", "#333944")
        return (
            f"<html><head><style>"
            f"body {{ background-color: {bg}; color: {fg};"
            f" font-family: sans-serif; font-size: 10pt; }}"
            f"h2 {{ color: {fg}; }} h3 {{ color: {fg}; margin-top: 18px; }}"
            f"b {{ color: {fg}; }}"
            f"li {{ margin-bottom: 6px; }} p {{ line-height: 1.4; }}"
            # Deck: the article's one-line summary, larger and quieter
            # than body text -- sits right under the title, before any
            # normal paragraph.
            f"p.deck {{ font-size: 12pt; color: {secondary};"
            f" margin-top: 2px; margin-bottom: 16px; line-height: 1.35; }}"
            # Callout: a quiet card, not a bright warning box -- same
            # restraint as the rest of this app's own "one accent color,
            # used sparingly" rule (see the Apple-design-language notes
            # this app's own visual work has followed throughout).
            f".callout {{ background-color: {card_bg}; border: 1px solid {border};"
            f" border-radius: 8px; padding: 10px 14px; margin: 12px 0; }}"
            f".callout-label {{ color: {fg}; }}"
            f".callout-body {{ color: {fg}; margin-top: 4px; }}"
            f".help-image {{ max-width: 100%; border: 1px solid {border};"
            f" border-radius: 8px; margin-top: 8px; }}"
            f".help-image-caption {{ color: {secondary}; font-size: 9pt;"
            f" margin-top: 4px; margin-bottom: 12px; }}"
            f"</style></head><body><h2>{title}</h2>{body_html}</body></html>"
        )

    def refresh_theme(self):
        # Called from main.py's _apply_theme/_on_system_theme_changed --
        # _load_stylesheet reloading the app-level QSS alone doesn't
        # touch already-rendered rich-text HTML content, so whatever's
        # currently displayed -- a real article, or the no-results
        # message (they're mutually exclusive, see __init__'s own
        # comment) -- needs an explicit re-render to pick up the new
        # theme's colors.
        # Category headings' muted color is set directly in Python too
        # (see _category_text_color's own comment on why QSS can't do
        # this), so it needs the same re-application here -- updated in
        # place rather than a full _populate_tree rebuild, which would
        # otherwise also have to carefully restore whatever selection/
        # search-filtered state was showing.
        color = self._category_text_color()
        for i in range(self.topic_tree.topLevelItemCount()):
            self.topic_tree.topLevelItem(i).setForeground(0, color)
        self._topic_tree_delegate.refresh_theme()
        self.topic_tree.viewport().update()
        if self._current_topic_id is not None:
            self._render_article(self._current_topic_id)
        elif self._no_results_query is not None:
            self._render_no_results(self._no_results_query)

    def _restore_geometry(self):
        geometry = self._qsettings.value("help_window_geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(760, 520)

    def closeEvent(self, event):
        self._qsettings.setValue("help_window_geometry", self.saveGeometry())
        super().closeEvent(event)
