"""Widget-construction methods, split out of MainWindow into a mixin --
this is the bulk of what made main.py huge: every settings control, the
left/right panel shells, the collapsible-group helper, and the command
preview box. Nothing here holds its own state; everything lands on
`self` (the composed MainWindow instance) exactly as it did before the
split, so every other method's `self.quality_slider`-style references
keep working unchanged regardless of which class in the MRO actually
built the widget.

Mixed in as `class MainWindow(QMainWindow, _UiBuilderMixin)`, not
`_UiBuilderMixin(QObject)` -- Qt/PySide6 handles one QObject ancestor
in the MRO fine, but multiple-inheriting from two QObject-derived
classes is a well-known source of real, hard-to-diagnose problems. A
plain mixin sidesteps that entirely."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QSlider, QSpinBox, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

import formatting
from constants import (
    AUDIO_BITRATES, AUDIO_TRACK_LABELS, CODECS, CONTAINERS, ENCODERS, RC_MODE_FRIENDLY,
    RESOLUTIONS, X265_PRESETS, X265_TUNES,
)
from queue_widget import (
    DropTreeWidget, VIDEO_COL, DURATION_COL, SIZE_COL,
    RESULT_COL, QUEUE_COLUMN_HEADERS,
)
from theming import _NoItemFocusRectStyle

PANEL_MARGIN = 12
PANEL_SPACING = 10
# Distinct from PANEL_SPACING -- that one governs the left/right panels'
# own outer layouts (Presets group to tab widget, queue list to buttons
# to Save-to...), unrelated spacing this constant shouldn't also change.
# This is specifically the gap between one card (Quality/Format/Expert)
# and the next, one tier looser than PANEL_SPACING's 10px to read as a
# real section break rather than just another row. 18, not 20 -- reported
# live the Normal Video page read as slightly more spacious than it
# needed to, without wanting a real redesign; a ~10% tightening here
# (plus form.setVerticalSpacing's own 14->12, and QGroupBox's own
# padding-top in style.qss) was judged enough on its own.
SECTION_SPACING = 18

THEME_CHOICES = [("dark", "Dark"), ("light", "Light"), ("system", "Match System")]


class _UiBuilderMixin:
    def _build_ui(self):
        # Was a QSplitter (draggable handle, stretch factors 0/1). Dropped
        # entirely -- setStretchFactor only governs how *extra* space from
        # a window resize gets distributed, it doesn't stop the user from
        # dragging the handle itself to any proportion at all, including
        # ones nothing in this panel was ever designed for (Processing/
        # Quality/Compatibility's own controls all have their own width
        # ceilings via _capped_row -- widen the left panel past what they
        # need and the extra space just sits there as dead space next to
        # them, not more useful control). Worse, that accidental
        # proportion used to get saved (QSettings "splitter_state") and
        # silently restored on every future launch. This panel isn't a
        # sidebar with variable-width content the way a file browser's is
        # -- it's a settings inspector with deliberately fixed-width
        # controls, so the left panel is now genuinely fixed (470px,
        # matching the splitter's own original default split against this
        # window's default resize(1240, 820) in main.py -- the interface
        # has effectively been designed around that width throughout every
        # iteration since) and 100% of window resizing goes to Videos,
        # where extra width actually does something useful (longer
        # filenames, longer Save-to paths, more queue-row breathing room).
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        left = self._build_left_panel()
        left.setFixedWidth(470)
        left.setObjectName("leftPanel")
        layout.addWidget(left)
        layout.addWidget(self._build_right_panel(), 1)
        self._build_status_bar()

    def _build_status_bar(self):
        # The footer itself is gone -- Theme and hardware-acceleration
        # status used to sit here permanently ("a qBittorrent-style
        # footer strip"), reported live as the most generic-utility-
        # feeling part of an otherwise much friendlier window. Theme
        # moved into Settings… (the overflow menu, _build_right_panel);
        # hardware status is silent now even when no acceleration exists
        # at all -- CPU is a completely valid, unremarkable Automatic
        # outcome, not something worth greeting a non-technical user with
        # on startup (main.py's __init__ has the fuller reasoning).
        #
        # self.theme_combo is still built here, still populated and set
        # to the current choice exactly as before -- just never added to
        # the (now nonexistent) status bar. _open_settings_dialog
        # (main.py) reparents this exact widget into its dialog on
        # demand, same lazy-reparent pattern _show_log_window uses for
        # log_view; every existing test/call site that already expects
        # self.theme_combo to exist right after construction still finds
        # it, functioning identically.
        self.theme_combo = QComboBox()
        for value, label in THEME_CHOICES:
            self.theme_combo.addItem(label, userData=value)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self._theme_choice))
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_combo.currentData())
        )

    def _build_left_panel(self) -> QWidget:
        # Wrapped in a QScrollArea, not returned as a bare QWidget --
        # the richer Video tab (Encoding/Quality/Format cards plus an
        # Expert section that itself holds several fairly tall slider/
        # caption groups) can genuinely exceed the window's default
        # 820px height once Expert is expanded. No visible scrollbar
        # under normal conditions (Expert collapsed, or a taller window)
        # -- setWidgetResizable(True) lets the inner content size itself
        # naturally and only grow a vertical scrollbar (ScrollBarAsNeeded,
        # Qt's own default) once it genuinely can't fit, rather than
        # letting Expert's bottom rows get compressed/clipped or forcing
        # the whole window taller just to accommodate its one tallest
        # possible state. setFixedWidth(470) (in _build_ui) still applies
        # to this outer scroll area, so the fixed-width contract is
        # unaffected -- only vertical overflow ever scrolls.
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        # Consumer build: no Presets group at all -- see
        # CONSUMER_FORK_PLAN.md's "Presets -> plain-language quality"
        # section. With Processing/Compatibility/File Format all cut above,
        # there's very little left for a saved preset to actually bundle;
        # the Quality tier row (inside the Video tab below) is the entire
        # "which preset" decision now, always visible, no save/delete/
        # manage step. _build_preset_row/_save_preset_as/_delete_preset
        # are deleted, not just unused; presets.py itself is kept but
        # trimmed to just load_builtin_presets(), which nothing in the
        # app calls anymore -- only test fixtures do (see presets.py's
        # own module docstring).

        # Went briefly (this same fork's own history) to a single flat
        # panel with Video/Audio's cards stacked directly, no tab bar at
        # all, on the reasoning that so few controls remained per section
        # that a tab click was pure overhead. Reverted back to two tabs
        # on explicit request -- back to the same QTabWidget/QTabBar/
        # tabPageCard treatment (style.qss) this app has used since before
        # the flat-panel experiment, including the corner-rendering-defect
        # history documented there. video_tab/audio_tab build their own
        # tabPageCard-styled page and add it here, same shape as before
        # that experiment.
        tabs = QTabWidget()
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
        # The two tabs have very different row counts, and the left panel
        # is always stretched to the full window height by its layout cell
        # regardless of content -- capping the tab widget to its natural
        # size (instead of letting it stretch into that forced height)
        # keeps the sparser tab from looking broken. The command preview
        # below puts the leftover space to use instead of leaving it blank.
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(tabs)

        # Effective Command no longer sits in the main layout at all, even
        # collapsed -- "Copy FFmpeg Command" (the overflow menu built in
        # _build_right_panel) is now the only exposed entry point for it,
        # since _copy_command_to_clipboard (main.py) already reads
        # self._last_preview_args directly rather than this widget's own
        # text. self.command_preview itself still gets constructed and
        # still keeps receiving every _update_command_preview() call
        # exactly as before (main.py has no reason to know it isn't
        # visible) -- just never added to a layout, so nothing shows it.
        self._build_command_preview()
        # tabs is the last real content here now -- an explicit trailing
        # stretch, not tabs itself absorbing it (tabs is capped to
        # Maximum, deliberately, a few lines up) or leaving it to a lone
        # leftover item the way command_preview used to (see that
        # widget's own now-removed stretch-factor comment, superseded by
        # this simpler shape once it left the visible layout).
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        # NoFrame -- QScrollArea's own native frame (Fusion draws a
        # sunken box border by default) would otherwise sit underneath
        # #leftPanel's QSS border-right (style.qss), doubling up as two
        # visibly different border treatments on the same edge.
        scroll.setFrameShape(QFrame.NoFrame)
        # Never horizontal -- content already fits the fixed 470px width
        # by design (every row/card in it is sized for exactly this
        # panel); only vertical overflow (Expert expanded) is the real
        # concern here.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Kept for _update_left_panel_min_height (main.py) -- reported
        # live, explicitly accepting the tradeoff: shrinking the window
        # below what this panel's *current* state (Expert collapsed or
        # expanded) actually needs should be refused outright rather than
        # just scrolling silently, which read as accidentally hiding
        # content rather than a deliberate choice. Scrolling itself stays
        # as a fallback for a real screen too short even for that.
        self._left_panel_scroll = scroll
        self._left_panel_content = content
        return scroll

    @staticmethod
    def _capped_row(row: QHBoxLayout, max_width: int | None = None) -> QWidget:
        # QLayout has no setMaximumWidth of its own (that's a QWidget
        # method) -- wraps a segmented-button row in a plain container
        # widget just to carry the cap, same reasoning as start_btn's own
        # ceiling (ui_builder.py, run_row): the buttons' equal addWidget(
        # btn, 1) stretch factors still divide whatever width the
        # container actually gets evenly, so this only stops the row from
        # stretching to fill an unusually wide left pane -- it doesn't
        # change how the segments share space among themselves.
        #
        # max_width=None skips the cap entirely -- still worth wrapping a
        # row that should stay full width, for the margin fix below and
        # for consistency with every other row in this panel going
        # through this same helper.
        container = QWidget()
        # Qt's own default QHBoxLayout margins (~9px top/bottom) would
        # otherwise inflate this wrapper well past the buttons' own 32px
        # height -- reported live as the row label ("Processing:", etc.)
        # sitting noticeably above center relative to its segmented
        # buttons; confirmed by measuring the wrapper's actual height
        # (50px) against the buttons inside it (32px), not by guessing.
        # QFormLayout sizes the row to the field's height, and every field
        # using this helper was inheriting that inflated height, throwing
        # the label off center. Zeroing the margins here fixes every row
        # that goes through _capped_row at once.
        row.setContentsMargins(0, 0, 0, 0)
        container.setLayout(row)
        if max_width is not None:
            container.setMaximumWidth(max_width)
        return container

    @staticmethod
    def _segmented_btn_min_width(btn: QPushButton) -> int:
        # Every segLeft/segMid/segRight button goes bold (font-weight: 600,
        # style.qss) when checked, and bold text is wider than the same
        # string at regular weight -- "Better Quality" reported live as
        # still clipped after widening its row's overall cap, even though
        # the cap math looked right for the *unchecked* (regular-weight)
        # sizeHint. Root cause, confirmed by measuring: Qt's own
        # QPushButton.sizeHint() doesn't get recomputed when a QSS-only
        # pseudo-state change (:checked) alters the font weight -- it stays
        # cached at whatever the *regular*-weight text needed, so the
        # button never grows to fit its own bold state no matter how much
        # room the row's cap leaves available. Reserving the bold-width
        # up front, unconditionally, sidesteps that Qt limitation entirely
        # instead of trying to force a sizeHint recompute on every toggle
        # -- the button is simply never narrower than its bold state
        # needs, checked or not. ensurePolished() first -- a button this
        # freshly constructed hasn't had Qt's stylesheet cascade actually
        # applied to it yet (confirmed directly: btn.font().pointSize()
        # reads 9, the plain Qt/OS default, until polished; only after
        # does it become 10, this app's actual QSS font-size), so reading
        # btn.font() without this first measures a smaller, wrong font and
        # under-computes the needed width by exactly the gap between the
        # two sizes. 30 = QPushButton's own padding (6px top/bottom, 14px
        # each side, style.qss) + 1px border on each side.
        btn.ensurePolished()
        bold_font = QFont(btn.font())
        bold_font.setWeight(QFont.Weight(600))
        return QFontMetrics(bold_font).horizontalAdvance(btn.text()) + 30

    @staticmethod
    def _make_collapsible_group(title: str, content: QWidget, *, expanded: bool) -> QGroupBox:
        # A real title string, via the exact same native QGroupBox::title
        # subcontrol every other section (Encoding, Format, Audio Settings)
        # uses -- same font, color, border-overlapping position, no
        # separate mechanism to keep visually in sync with those. The only
        # difference is the checkbox indicator's rendered size is zeroed
        # out in style.qss (QGroupBox#collapsibleGroup::indicator), with a
        # trailing arrow baked into the title text instead of a checkbox
        # glyph -- confirmed empirically that hiding the indicator doesn't
        # shrink the *clickable* area down to where the glyph would have
        # been: Qt/Fusion already treats a checkable QGroupBox's whole
        # title bar as one hit region, glyph size notwithstanding, so this
        # is a skin change, not a rebuild of how clicking it works. (An
        # earlier version of this used a separate flat QPushButton sitting
        # below the border instead -- functioned fine, but visually broke
        # from every other section's title, which sits overlapping the
        # border -- reverted for exactly that inconsistency.)
        group = QGroupBox(title)
        group.setObjectName("collapsibleGroup")
        group.setCheckable(True)
        group.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(group)
        layout.addWidget(content)

        # A stretch factor in the parent layout (the Log group has one, to
        # share space with the queue list) still applies to the *group*
        # even with its content hidden, so collapsing needs a fixed size
        # policy too or the group keeps claiming its full stretch share --
        # confirmed by screenshot, a large empty box where the log used to be.
        expanded_policy = group.sizePolicy()
        collapsed_policy = QSizePolicy(expanded_policy.horizontalPolicy(), QSizePolicy.Fixed)

        def _toggle(checked):
            group.setTitle(f"{title}  {'▾' if checked else '▸'}")
            content.setVisible(checked)
            group.setSizePolicy(expanded_policy if checked else collapsed_policy)
            group.updateGeometry()

        group.setChecked(expanded)
        _toggle(expanded)
        group.toggled.connect(_toggle)
        return group

    def _build_command_preview(self):
        # Not shown anywhere in the main window (see _build_left_panel's
        # own comment) -- constructed only so main.py's
        # _update_command_preview keeps a real widget to call
        # setPlainText() on exactly as before. No parent, no layout;
        # "Copy FFmpeg Command" (the overflow menu, _build_right_panel)
        # is the only exposed entry point now, and it reads
        # self._last_preview_args directly, not this widget's text.
        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setLineWrapMode(QPlainTextEdit.WidgetWidth)

    def _build_encoding_group(self) -> QGroupBox:
        """Processing/Codec -- restored from the specialist build (this
        fork had cut Processing entirely) plus Codec's own promotion from
        Expert, per the final Normal/Expert control-hierarchy decision:
        Normal = meaningful output/media decisions, Expert = encoder
        mechanics and precision overrides. Compatibility (Modern/Most
        Compatible) stays cut -- now that Codec is a direct, visible
        H.265/H.264 choice, Compatibility would just be a second control
        describing substantially the same decision. Each button is a thin
        remote control over the exact same settings Expert's own (now
        hidden-but-live) encoder_combo/codec_combo drive -- see main.py's
        _on_processing_choice, which goes through _apply_settings_to_
        controls exactly like every other Normal control here."""
        group = QGroupBox("Encoding")
        form = QFormLayout(group)
        form.setVerticalSpacing(12)

        processing_row = QHBoxLayout()
        processing_row.setSpacing(6)
        self.processing_auto_btn = QPushButton("Automatic")
        self.processing_auto_btn.setToolTip(
            "Automatically chooses the fastest available method for this\n"
            "computer. Recommended."
        )
        self.processing_auto_btn.clicked.connect(lambda: self._on_processing_choice("automatic"))
        processing_row.addWidget(self.processing_auto_btn)

        processing_seg_row = QHBoxLayout()
        processing_seg_row.setSpacing(0)
        self.processing_button_group = QButtonGroup(self)
        # Intel/AMD only exist as attributes at all when that vendor's
        # hardware was actually detected -- _sync_normal_video_controls
        # (main.py) guards every reference to them for exactly this
        # reason.
        self.processing_intel_btn = None
        self.processing_amd_btn = None
        attr_by_id = {
            "cpu": "processing_cpu_btn",
            "intel": "processing_intel_btn",
            "amd": "processing_amd_btn",
        }
        # Cached once in MainWindow.__init__, not re-probed here -- this
        # session's hardware is fixed for its whole lifetime (no hot-plug
        # monitoring), and _resolved_engine_vendor (main.py) checks
        # against this exact same snapshot, so Automatic's silent pick
        # and this row's own button set can never disagree.
        detected = self._available_backends
        seg_buttons = []
        for backend in detected:
            btn = QPushButton(backend.display_name)
            setattr(self, attr_by_id[backend.id], btn)
            seg_buttons.append((btn, backend.id))

        # Corner-rounding follows actual visible position, not a fixed
        # CPU/Intel/AMD identity -- style.qss's segLeft/segMid/segRight
        # only round the row's real outer two edges.
        for i, (btn, _choice) in enumerate(seg_buttons):
            if i == 0:
                btn.setObjectName("segLeft")
            elif i == len(seg_buttons) - 1:
                btn.setObjectName("segRight")
            else:
                btn.setObjectName("segMid")

        for btn, choice in seg_buttons:
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.processing_button_group.addButton(btn)
            processing_seg_row.addWidget(btn, 1)
            btn.clicked.connect(lambda _checked, c=choice: self._on_processing_choice(c))
        processing_row.addWidget(self._capped_row(processing_seg_row, 240), 1)
        form.addRow("Processing:", processing_row)
        if len(detected) == 1:
            # Only CPU exists at all -- Automatic and the lone CPU segment
            # would always mean the exact same outcome, so asking "which
            # Processing?" is a decision with only one possible answer.
            # Hide the whole row (label included) rather than show a
            # single-button segmented control with nothing to actually
            # choose between; _on_processing_choice/_sync_normal_video_
            # controls still work normally on the buttons underneath,
            # they're just never shown.
            form.setRowVisible(processing_row, False)

        # H.265/H.264 -- only meaningful for the CPU engine (no h264_vaapi
        # wired up, hardware is HEVC-only here), so it's disabled and
        # forced to H.265 whenever Processing above is set to a hardware
        # engine -- see main.py's _on_encoder_changed, which this cascades
        # into exactly like encoder_combo's own (now hidden) change does.
        self.codec_combo = QComboBox()
        for value, label in CODECS:
            self.codec_combo.addItem(label, userData=value)
        self.codec_combo.setToolTip(
            "H.265 (HEVC): better compression -- smaller file at the\n"
            "same quality -- but not universally supported by older\n"
            "devices/TVs/browsers, or only with extra licensing hassle.\n"
            "H.264 (AVC): larger files for the same quality. 8-bit H.264\n"
            "offers the broadest playback compatibility of any option\n"
            "here; 10-bit H.264 (see Color Depth, below) trades some of\n"
            "that away again -- it needs compatible software/devices\n"
            "too, just less broadly required than H.265. Pick H.264 over\n"
            "H.265 when playback compatibility matters more than file\n"
            "size, and 8-bit Color Depth when it matters most of all.\n"
            "Only applies to the CPU engine -- Intel/AMD hardware\n"
            "encoding here is HEVC-only, so this is disabled and forced\n"
            "to H.265 whenever Processing above is set to one of those."
        )
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        form.addRow("Codec:", self.codec_combo)

        return group

    def _build_quality_group(self) -> QGroupBox:
        """Mode/Quality/Target Size. Mode (new) is a simplified, Normal-
        only 2-way front end (Quality vs. File Size) over the exact same
        rc_mode_combo Expert's own 3-way Rate Control row (Quality/File
        Size/Advanced) drives -- same "thin remote control over one
        shared hidden model" pattern this file already uses for Quality
        tier vs. Expert's exact quality slider. Advanced (CQP) is
        deliberately not offered here -- it's a precision override with
        no real "media decision" framing, stays Expert-only. Target Size
        (size_spin) moved here from Expert entirely, not duplicated --
        there's no more-precise representation of "how big should the
        file be" than a plain MB number (the derived kbps value is
        exactly the raw-bitrate-as-a-control this app deliberately never
        exposes), so there's nothing left for Expert to show once Normal
        already has it -- unlike Quality, whose exact numeric CRF/ICQ/CQP
        form is still meaningfully more precise than the three named
        tiers, and stays duplicated in Expert for that reason."""
        group = QGroupBox("Quality")
        self.quality_form = form = QFormLayout(group)
        form.setVerticalSpacing(12)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(0)
        self.mode_button_group = QButtonGroup(self)
        self.mode_quality_btn = QPushButton("Quality")
        self.mode_quality_btn.setObjectName("segLeft")
        self.mode_quality_btn.setToolTip("Aim for a consistent perceptual quality; file size follows.")
        self.mode_filesize_btn = QPushButton("File Size")
        self.mode_filesize_btn.setObjectName("segRight")
        self.mode_filesize_btn.setToolTip("Aim for a target output size; quality follows.")
        for btn in (self.mode_quality_btn, self.mode_filesize_btn):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.mode_button_group.addButton(btn)
            mode_row.addWidget(btn, 1)
        self.mode_quality_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["quality"])
        )
        self.mode_filesize_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["file_size"])
        )
        # Uncapped -- full width, matching Target Size/Format below it now
        # (reported live that Mode's own 240px cap made it visibly
        # narrower than everything else in this panel for no reason a
        # user could tell apart, same issue the Format group's dropdowns
        # had before their own caps were removed).
        mode_field = self._capped_row(mode_row)
        form.addRow("Mode:", mode_field)

        quality_tier_row = QHBoxLayout()
        quality_tier_row.setSpacing(0)
        self.quality_tier_button_group = QButtonGroup(self)
        self.quality_smaller_btn = QPushButton("Smaller File")
        self.quality_smaller_btn.setObjectName("segLeft")
        self.quality_balanced_btn = QPushButton("Balanced")
        self.quality_balanced_btn.setObjectName("segMid")
        self.quality_better_btn = QPushButton("Better Quality")
        self.quality_better_btn.setObjectName("segRight")
        for btn, tier in (
            (self.quality_smaller_btn, "smaller"),
            (self.quality_balanced_btn, "balanced"),
            (self.quality_better_btn, "better"),
        ):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.quality_tier_button_group.addButton(btn)
            quality_tier_row.addWidget(btn, 1)
            btn.clicked.connect(lambda _checked, t=tier: self._on_quality_tier_clicked(t))
        # Uncapped -- full width, matching Mode/Target Size/Format (each
        # button's own setMinimumWidth above, from a real clipping bug at
        # 340px total width -- 3 buttons at equal (1,1,1) stretch clipped
        # "Better Quality", own sizeHint 114px, since 340/3 lands just
        # under that even before segment-border adjustments -- already
        # guards against clipping regardless of how wide this row grows,
        # so there's nothing left for a maximum-width cap to protect).
        self._quality_tier_field = self._capped_row(quality_tier_row)
        form.addRow("Quality:", self._quality_tier_field)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(10, 20000)
        self.size_spin.setSingleStep(50)
        self.size_spin.setSuffix(" MB")
        self.size_spin.setValue(1000)
        self.size_spin.setToolTip("Target output size -- the actual bitrate is computed from this file's length.")
        self.size_spin.valueChanged.connect(self._on_control_changed)
        self.size_estimate_label = QLabel()
        self.size_estimate_label.setStyleSheet("font-size: 9pt;")
        # Unlike the fuzzy-caption labels above (which sit on their own
        # line, full box width), this one shares a row with size_spin --
        # noticeably less horizontal room. Shortened every message this
        # can show (see _update_size_estimate_label) to fit one line at
        # that width, but word wrap stays on as a safety net -- the
        # probe-failure branch interpolates nothing unbounded any more,
        # but there's no guarantee some future message stays short.
        self.size_estimate_label.setWordWrap(True)
        target_size_row = QHBoxLayout()
        # Default QHBoxLayout spacing reads too tight here -- size_spin's
        # own spin-arrow buttons sit right up against the caption text
        # (reported live, confirmed by screenshot) -- unlike quality_row's
        # slider+label above, which has enough visual breathing room from
        # the slider's own end padding without needing this.
        target_size_row.setSpacing(10)
        target_size_row.addWidget(self.size_spin)
        target_size_row.addWidget(self.size_estimate_label, 1)
        # _capped_row(row) with no max_width -- wraps this in a QWidget
        # the same way _quality_tier_field above already is, rather than
        # handing setRowVisible a bare QHBoxLayout. See _capped_row's own
        # comment for why.
        self._target_size_field = self._capped_row(target_size_row)
        form.addRow("Target Size:", self._target_size_field)
        # Only one of Quality/Target Size is ever visible at a time
        # (is_bitrate in main.py's _on_rc_mode_changed, which toggles
        # both rows via self.quality_form.setRowVisible) -- Mode above
        # picks which.

        # "Target Size:" starts hidden (Mode defaults to Quality) and is
        # the widest label in this form -- confirmed directly (not
        # guessed) that QFormLayout's automatic label-column width, once
        # computed from only the rows visible at first show(), doesn't
        # widen correctly when a longer-labeled row is revealed later via
        # setRowVisible(): the label's own sizeHint() already reports the
        # right width, but its actual allocated size stays clipped to
        # whatever the column was sized to while still hidden. Forcing
        # every label in this form to the true widest sizeHint up front
        # sidesteps that Qt timing quirk entirely instead of chasing a
        # relayout call that convinces it to recompute.
        labels = [form.labelForField(f) for f in (mode_field, self._quality_tier_field, self._target_size_field)]
        widest = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setMinimumWidth(widest)

        return group

    def _build_format_group(self) -> QGroupBox:
        """Resolution/File Format/Color Depth -- File Format and Color
        Depth promoted here from Expert (container_combo, bitdepth_combo)
        per the final control-hierarchy decision: both are real output/
        media characteristics a professional user reasonably decides
        about, not encoder mechanics. Compatibility's old container-
        forcing-to-MP4 behavior is gone along with Compatibility itself --
        File Format is now directly this dropdown, nothing else touches
        it."""
        group = QGroupBox("Format")
        form = QFormLayout(group)
        form.setVerticalSpacing(12)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        # Keep Original, not 720p -- the sibling TITAN-i Transcoder app
        # defaults here to match its own starting preset, but silently
        # downscaling whatever a user drags in (a 4K master, say) is a bad
        # default for an app whose whole premise is "just make it work".
        self.res_combo.setCurrentIndex(0)  # Keep Original
        self.res_combo.currentIndexChanged.connect(self._on_control_changed)
        # Left to stretch full width, matching Color Depth below and the
        # Mode buttons above -- reported live that a 200px cap here (while
        # Color Depth had none) read as visibly inconsistent, dropdowns of
        # different widths for no reason a user could tell.
        form.addRow("Resolution:", self.res_combo)

        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)
        self.container_combo.setToolTip(
            "MP4: broadest compatibility -- phones, TVs, browsers,\n"
            "streaming platforms. Includes a \"fast start\" flag so\n"
            "playback can begin before the whole file has downloaded.\n"
            "MKV: the more flexible container, common for media-server\n"
            "and archival libraries (Plex, Jellyfin, ...). No real\n"
            "downside here otherwise -- this app doesn't carry subtitle\n"
            "tracks through on either container yet."
        )
        self.container_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("File Format:", self.container_combo)

        # Item text folds the tradeoff directly in, no separate caption
        # row needed (one less row fighting the others for space, and the
        # tradeoff is right there the moment the dropdown opens).
        self.bitdepth_combo = QComboBox()
        self.bitdepth_combo.addItem("10-bit — smoother gradients", userData=10)
        self.bitdepth_combo.addItem("8-bit — maximum compatibility", userData=8)
        self.bitdepth_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Color Depth:", self.bitdepth_combo)

        return group

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        # tabPageCard -- the Video/Audio tab page's own border+radius+fill
        # lives on this plain QWidget, not on QTabWidget::pane (style.qss,
        # that rule's own history comment has the full story: pane is a
        # special Qt subcontrol with its own native, non-QSS-overridable
        # corner-painting logic that produced a real, only-on-the-real-
        # display broken corner no matter what was tried there; a plain
        # QWidget's border-radius never showed that defect, same
        # rendering path QGroupBox already uses everywhere else in this
        # file).
        tab.setObjectName("tabPageCard")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(12, 12, 12, 12)
        # SECTION_SPACING (card-to-card), not PANEL_SPACING -- Encoding to
        # Quality to Format to Expert are real section breaks, not just
        # another row in the same list.
        outer.setSpacing(SECTION_SPACING)

        # Reported live: nothing in the UI said whether these controls were
        # about to become defaults for newly-added videos, or were editing
        # whatever's currently selected in the queue -- both are real,
        # frequently-used states (see _on_queue_selection_changed/add_files)
        # with identical-looking controls either way. _update_settings_
        # scope_label (main.py) keeps this and audio_scope_label below in
        # sync with the real selection state.
        self.video_scope_label = QLabel()
        self._apply_fuzzy_caption_style(self.video_scope_label)
        outer.addWidget(self.video_scope_label)

        outer.addWidget(self._build_encoding_group())
        outer.addWidget(self._build_quality_group())
        outer.addWidget(self._build_format_group())

        # Expert: Rate Control/Exact Quality/Encoding Speed/Tune/Force
        # Deinterlace -- a real collapsible section again (this fork
        # briefly made it permanently unreachable; the final control-
        # hierarchy decision restored enough Normal-mode depth --
        # Processing/Codec/Mode/File Format/Color Depth all promoted
        # above -- that a genuine Expert escape hatch earns its place
        # again, same as the specialist build). encoder_combo below keeps
        # a real, lasting Python reference (self._video_expert_content,
        # not a bare local) for the same reason as before: a QWidget()
        # with no C++ parent and no surviving Python reference gets
        # garbage-collected, taking every control inside it down with it
        # -- confirmed as a real crash earlier in this fork's history.
        self._video_expert_content = expert_content = QWidget()
        self.video_form = form = QFormLayout(expert_content)
        # Default Fusion spacing reads as cramped once every row has a small
        # secondary line under it (quality/speed tiers, the bit-depth combo's
        # own description) -- confirmed by screenshot, this is the fix.
        form.setVerticalSpacing(12)

        # encoder_combo, not shown as its own row anymore -- Processing
        # (Encoding group, above) is now the only user-facing entry point
        # for this exact same choice, so a second, Expert-only copy of it
        # would just be a duplicate control. Stays fully live as the real
        # backing model (_current_encoder_id/_current_gpu_vendor read it
        # directly) -- same hidden-model-plus-friendly-view pattern
        # rc_mode_combo just below already uses, just for a different
        # setting. Constructed with expert_content as its real C++
        # parent (not added to any layout) for the same reason
        # rc_mode_combo needs one.
        self.encoder_combo = QComboBox(expert_content)
        self.encoder_combo.hide()
        for _, _, label in ENCODERS:
            self.encoder_combo.addItem(label)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)

        # rc_mode_combo is the real, directly-visible Expert control now --
        # previously hidden behind a friendly Quality/File Size/Advanced
        # 3-button row that just duplicated Normal's own Mode toggle
        # (mode_quality_btn/mode_filesize_btn, Quality group above) with
        # different labels. Per the final control-hierarchy decision
        # ("Normal = intent, Expert = actual encoder mechanics"), Expert
        # should show the real underlying modes (ICQ/CQP/VBR/CRF/bitrate,
        # RC_MODES' own technical labels, constants.py) instead of a
        # second friendly abstraction -- removing duplication, not adding
        # an option. main.py's _on_rc_mode_changed/_sync_mode_buttons_to_
        # combo still keep this and Normal's Mode toggle in sync in both
        # directions.
        self.rc_mode_combo = QComboBox(expert_content)
        self.rc_mode_combo.setToolTip(
            "The real underlying rate-control mode for the current\n"
            "encoder -- Normal's Mode toggle picks Quality or File Size;\n"
            "this shows (and, for a mode with no Normal equivalent like\n"
            "CQP, is the only way to reach) the exact mode actually\n"
            "driving the encode."
        )
        self.rc_mode_combo.currentIndexChanged.connect(self._on_rc_mode_changed)
        self.rc_mode_combo.currentIndexChanged.connect(self._sync_mode_buttons_to_combo)
        form.addRow("Rate Control:", self.rc_mode_combo)

        quality_row = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Horizontal)
        # Left = worse quality/smaller, right = better quality/larger --
        # the intuitive direction for a horizontal slider. The underlying
        # ICQ/CQP/CRF value this drives is the opposite (lower number is
        # better quality), so invertedAppearance/-Controls flips the visual
        # and interaction direction while .value() keeps returning the real
        # number untouched -- Qt handles the remapping, nothing downstream
        # (settings, build_args) needs to know this happened.
        self.quality_slider.setInvertedAppearance(True)
        self.quality_slider.setInvertedControls(True)
        self.quality_slider.setToolTip(
            "Left: more compression, smaller file.\n"
            "Right: higher quality, larger file."
        )
        self.quality_slider.valueChanged.connect(self._on_quality_changed)
        self.quality_label = QLabel()
        self.quality_label.setStyleSheet("font-size: 9pt;")
        quality_row.addWidget(self.quality_slider, 1)
        quality_row.addWidget(self.quality_label)

        self.quality_tier_label = QLabel()
        self.quality_tier_label.setAlignment(Qt.AlignCenter)
        self._apply_fuzzy_caption_style(self.quality_tier_label)

        # The slider and its fuzzy caption underneath share one outlined
        # box (objectName carries the QSS rule -- see style.qss's
        # #fuzzyGroup, shared with Speed's identical box below) instead of
        # being two independent-looking form rows -- the caption explains
        # *that specific slider*, so it reads better visually grouped with
        # it rather than just sitting in the row underneath. Target Size
        # (size_spin/size_estimate_label) used to share this same box,
        # toggled visible/invisible opposite this slider -- moved out
        # entirely to the Quality group in Normal mode (see
        # _build_quality_group's own docstring for why it isn't
        # duplicated here too), so this box is just the exact-quality
        # slider and its caption now, same shape as speed_group below.
        quality_group = QWidget()
        quality_group.setObjectName("fuzzyGroup")
        quality_group_layout = QVBoxLayout(quality_group)
        quality_group_layout.setContentsMargins(8, 6, 8, 6)
        # Default QVBoxLayout spacing (Fusion's ~11px) read as the caption
        # floating unrelated to the slider above it rather than explaining
        # it -- confirmed by screenshot.
        quality_group_layout.setSpacing(2)
        quality_group_layout.addLayout(quality_row)
        quality_group_layout.addWidget(self.quality_tier_label)
        form.addRow("Exact Quality:", quality_group)

        speed_row = QHBoxLayout()
        self.speed_faster_label = QLabel("Faster")
        speed_row.addWidget(self.speed_faster_label)
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(1, 7)
        # Same reasoning as the Quality slider's inversion above: left=fast,
        # right=thorough is the intuitive direction, and it's now also the
        # *correct* one -- confirmed by timing real encodes at
        # compression_level 1/4/7 (22.4s/18.8s/11.3s, smallest to largest
        # output in that order too), a lower value is genuinely slower, not
        # just assumed. Before this, "Faster" sat on the end that was
        # actually the slowest.
        self.speed_slider.setInvertedAppearance(True)
        self.speed_slider.setInvertedControls(True)
        self.speed_slider.valueChanged.connect(self._on_speed_slider_changed)
        speed_row.addWidget(self.speed_slider, 1)

        self.speed_x265_slider = QSlider(Qt.Horizontal)
        # Index into X265_PRESETS, not a value with real arithmetic meaning
        # of its own -- same reasoning as Audio Bitrate's slider. No
        # invertedAppearance/-Controls needed here unlike the VAAPI slider
        # above: X265_PRESETS is already ordered fastest-to-slowest
        # (ultrafast..placebo), so index 0 landing on the visual left and
        # the last index on the right is already the correct "Faster ...
        # Slower" direction without flipping anything.
        self.speed_x265_slider.setRange(0, len(X265_PRESETS) - 1)
        self.speed_x265_slider.setToolTip(
            "Left: faster encode.\n"
            "Right: slower, more size-efficient at the same quality.\n"
            "(x265 preset)"
        )
        self.speed_x265_slider.valueChanged.connect(self._on_speed_x265_slider_changed)
        speed_row.addWidget(self.speed_x265_slider, 1)

        # "Slower" (not "More Thorough") -- plain opposite of "Faster",
        # layman-friendly. The *why* (smaller file / better efficiency at
        # this end) already lives in speed_tier_label's own caption right
        # underneath ("Maximum effort -- best compression" etc.), so the
        # axis-end label itself doesn't need to carry that too. Discussed
        # directly, not a unilateral call.
        self.speed_thorough_label = QLabel("Slower")
        speed_row.addWidget(self.speed_thorough_label)
        # No separate x265-preset-name label here anymore (used to show
        # "(medium)" etc. right after Slower) -- discussed directly,
        # dropped as redundant with speed_tier_label's own caption right
        # underneath, which now folds the preset name into that same line
        # instead (see _on_speed_x265_slider_changed).

        self.speed_tier_label = QLabel()
        self.speed_tier_label.setAlignment(Qt.AlignCenter)
        self.speed_tier_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._apply_fuzzy_caption_style(self.speed_tier_label)

        # Same outlined-box grouping as Quality above, same #fuzzyGroup rule.
        speed_group = QWidget()
        speed_group.setObjectName("fuzzyGroup")
        speed_group_layout = QVBoxLayout(speed_group)
        speed_group_layout.setContentsMargins(8, 6, 8, 6)
        speed_group_layout.setSpacing(2)  # see Quality's identical fix above
        speed_group_layout.addLayout(speed_row)
        speed_group_layout.addWidget(self.speed_tier_label)
        form.addRow("Encoding Speed:", speed_group)

        # Initial population matches whichever encoder the app actually
        # starts on (libx265 at construction time -- see main.py's
        # __init__ order) -- immediately repopulated by _on_encoder_changed
        # regardless, which also handles switching to X264_TUNES, so this
        # is just a reasonable non-empty starting point, not load-bearing.
        self.tune_combo = QComboBox()
        self.tune_combo.addItems(X265_TUNES)
        self.tune_combo.currentIndexChanged.connect(self._on_control_changed)
        self.tune_combo.setToolTip(
            "x264/x265's built-in tuning presets -- \"None\" (the default)\n"
            "is right for most sources. Only switch if one clearly applies:\n"
            "animation -- flat colors, sharp edges (cartoons/anime).\n"
            "grain -- preserves film grain instead of smoothing it away.\n"
            "fastdecode -- eases decoding for low-power playback devices.\n"
            "zerolatency -- minimizes encode delay for live streaming,\n"
            "not useful for this app's batch transcodes.\n"
            "psnr/ssim -- optimizes for a benchmark metric, not\n"
            "perceptual quality -- rarely what you actually want.\n"
            "film/stillimage -- x264 only; x265 doesn't offer either\n"
            "(this exact libx265 build rejects \"film\" outright)."
        )
        form.addRow("Tune (software only):", self.tune_combo)

        # "Force Deinterlace" -- renamed from "Deinterlace (interlaced or
        # telecined source)" now that TITAN performs its own frame
        # analysis and sets this automatically on every new file (see
        # queue_controller.py's _on_deinterlace_checkbox_changed); this
        # checkbox is only ever a manual override of that, not the
        # primary way deinterlacing happens, so its label should say so.
        self.deinterlace_check = QCheckBox("Force Deinterlace")
        self.deinterlace_check.setToolTip(
            "Container-level progressive/interlaced flags are frequently wrong,\n"
            "especially on camcorder-sourced footage. New files are sampled\n"
            "and this is set automatically, but detection only checks the\n"
            "first ~20s -- override it here if the output still shows\n"
            "combing/interlacing artifacts."
        )
        self.deinterlace_check.stateChanged.connect(self._on_deinterlace_checkbox_changed)
        form.addRow("", self.deinterlace_check)

        self.video_expert_group = self._make_collapsible_group("Expert", expert_content, expanded=False)
        outer.addWidget(self.video_expert_group)

        # Without this, Video and Audio's tab pages are forced to the same
        # height (QStackedWidget sizes every page to the tallest one) but
        # only Audio had a trailing stretch to absorb the resulting surplus
        # -- Video's surplus space had nowhere to go but into its own
        # items, inflating video_scope_label past its own sizeHint (real,
        # confirmed: rendered 22px vs a 17px sizeHint) and pushing Encoding
        # down with it, out of alignment with Audio's card below its own,
        # correctly unstretched, scope label.
        outer.addStretch()

        return tab

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        # tabPageCard -- see _build_video_tab's own comment on this same
        # objectName for why the card border/radius/fill lives here now
        # instead of on QTabWidget::pane.
        tab.setObjectName("tabPageCard")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(12, 12, 12, 12)
        # SECTION_SPACING, matching _build_video_tab's own outer layout --
        # reported live, real bug: this was never set here at all, so the
        # gap between audio_scope_label and the Audio card below it used
        # Qt's own default spacing instead, however that happened to
        # compare to SECTION_SPACING -- the Audio card visibly started at
        # a different height than Encoding did on the Video tab, for a
        # reason that had nothing to do with either tab's actual content.
        outer.setSpacing(SECTION_SPACING)

        # See video_scope_label's own comment (_build_video_tab) -- same
        # label, kept in sync with it by _update_settings_scope_label
        # (main.py), just a second instance since a widget can't sit in
        # two tabs' layouts at once.
        self.audio_scope_label = QLabel()
        self._apply_fuzzy_caption_style(self.audio_scope_label)
        outer.addWidget(self.audio_scope_label)

        # No Audio Expert section -- every genuine audio setting the
        # backend currently supports (Track, Handling, Channels, AAC
        # Bitrate) is promoted directly into this one Normal group below.
        # An Expert section with nothing left to put in it (or a
        # duplicated copy of a Normal control) would be worse than none;
        # revisit this once the backend gains something genuinely more
        # advanced (multi-track passthrough, language selection, per-
        # track mapping, loudness normalization, ...).
        normal_group = QGroupBox("Audio")
        normal_form = QFormLayout(normal_group)
        normal_form.setVerticalSpacing(12)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(AUDIO_TRACK_LABELS)
        self.audio_combo.currentIndexChanged.connect(self._on_control_changed)
        normal_form.addRow("Track:", self.audio_combo)

        # Handling and Channels used to be one combined "Audio: Automatic
        # / Convert to Stereo" row (_on_audio_choice) -- that bundled two
        # genuinely independent decisions (copy-vs-transcode, and
        # channel layout) into one control, and silently never exposed
        # copy-vs-transcode at all (audio_copy_if_compatible stayed
        # permanently True, whatever "Automatic" happened to mean).
        # Split into two real rows now, each a direct replacement for
        # what used to be a hidden-Expert-only checkbox.
        handling_row = QHBoxLayout()
        handling_row.setSpacing(0)
        self.audio_handling_button_group = QButtonGroup(self)
        self.audio_handling_automatic_btn = QPushButton("Automatic")
        self.audio_handling_automatic_btn.setObjectName("segLeft")
        self.audio_handling_automatic_btn.setToolTip(
            "Keeps the original audio track untouched whenever the\n"
            "source is already AAC/AC-3/E-AC-3 (a fast, lossless stream\n"
            "copy); re-encodes to AAC only when it isn't."
        )
        self.audio_handling_convert_btn = QPushButton("Convert to AAC")
        self.audio_handling_convert_btn.setObjectName("segRight")
        self.audio_handling_convert_btn.setToolTip(
            "Always re-encodes to AAC at the bitrate set below, even if\n"
            "the source is already a compatible codec -- use this if you\n"
            "specifically need a fresh AAC stream regardless."
        )
        for btn, choice in (
            (self.audio_handling_automatic_btn, "automatic"),
            (self.audio_handling_convert_btn, "convert"),
        ):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.audio_handling_button_group.addButton(btn)
            handling_row.addWidget(btn, 1)
            btn.clicked.connect(lambda _checked, c=choice: self._on_audio_handling_clicked(c))
        # Uncapped -- full width, matching Track above and AAC Bitrate
        # below (same consistency fix as the Video tab's Mode/Quality/
        # Format rows).
        normal_form.addRow("Handling:", self._capped_row(handling_row))

        channels_row = QHBoxLayout()
        channels_row.setSpacing(0)
        self.audio_channels_button_group = QButtonGroup(self)
        self.audio_channels_keep_btn = QPushButton("Keep Original")
        self.audio_channels_keep_btn.setObjectName("segLeft")
        self.audio_channels_stereo_btn = QPushButton("Stereo")
        self.audio_channels_stereo_btn.setObjectName("segRight")
        self.audio_channels_stereo_btn.setToolTip(
            "Mixes 5.1/7.1/etc. sources down to plain stereo, for a\n"
            "phone, laptop, or anything without a surround setup. No\n"
            "effect on a source that's already stereo or mono. A stream\n"
            "copy can't remix channels, so on a source that does have\n"
            "more channels, choosing this transcodes the audio track\n"
            "even if it would otherwise have been copied through\n"
            "untouched."
        )
        for btn, choice in (
            (self.audio_channels_keep_btn, "keep"),
            (self.audio_channels_stereo_btn, "stereo"),
        ):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.audio_channels_button_group.addButton(btn)
            channels_row.addWidget(btn, 1)
            btn.clicked.connect(lambda _checked, c=choice: self._on_audio_channels_clicked(c))
        normal_form.addRow("Channels:", self._capped_row(channels_row))

        audio_bitrate_row = QHBoxLayout()
        self.audio_bitrate_slider = QSlider(Qt.Horizontal)
        # Index into AUDIO_BITRATES, not the kbps number itself -- the real
        # values (96/128/160/192/256) aren't evenly spaced (some steps are
        # 32, the last is 64), which a linear QSlider can't represent
        # directly without irregular, confusing tick spacing. An index is
        # exact and trivial to map back to the real string everywhere this
        # setting is read (_current_settings, _apply_settings_to_controls).
        self.audio_bitrate_slider.setRange(0, len(AUDIO_BITRATES) - 1)
        self.audio_bitrate_slider.setToolTip(
            "Left: more compression, smaller file.\n"
            "Right: higher quality, larger file.\n"
            "Only applies when the source audio is actually being\n"
            "transcoded -- see Handling above. Stays adjustable even on\n"
            "Automatic: a source the copy path can't handle (DTS, PCM, "
            "...) still needs transcoding at whatever this is set to."
        )
        self.audio_bitrate_slider.valueChanged.connect(self._on_audio_bitrate_slider_changed)
        audio_bitrate_row.addWidget(self.audio_bitrate_slider, 1)
        self.audio_bitrate_label = QLabel()
        self.audio_bitrate_label.setStyleSheet("font-size: 9pt;")
        audio_bitrate_row.addWidget(self.audio_bitrate_label)

        self.audio_bitrate_tier_label = QLabel()
        self.audio_bitrate_tier_label.setAlignment(Qt.AlignCenter)
        self._apply_fuzzy_caption_style(self.audio_bitrate_tier_label)

        # Same slider-plus-fuzzy-caption outlined box as Quality/Speed on
        # the Video tab (#fuzzyGroup in style.qss) -- same reasoning: the
        # caption explains *this* slider, so it reads better grouped with
        # it than as a separate form row underneath.
        audio_bitrate_group = QWidget()
        audio_bitrate_group.setObjectName("fuzzyGroup")
        audio_bitrate_group_layout = QVBoxLayout(audio_bitrate_group)
        audio_bitrate_group_layout.setContentsMargins(8, 6, 8, 6)
        audio_bitrate_group_layout.setSpacing(2)  # see Quality's identical fix above
        audio_bitrate_group_layout.addLayout(audio_bitrate_row)
        audio_bitrate_group_layout.addWidget(self.audio_bitrate_tier_label)
        normal_form.addRow("AAC Bitrate:", audio_bitrate_group)

        outer.addWidget(normal_group)
        outer.addStretch()

        return tab

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        # "Videos", not "Queue" -- matches the rest of the user-facing
        # vocabulary this app already uses (Start -> Convert, Add Files ->
        # Add Videos, Container -> File Format): the user is thinking "my
        # videos", not "my queue entries". Purely the visible label --
        # queue_list/TranscodeQueue/_queue_editable and every other
        # internal name stay exactly as they are, and "Clear Queue"/
        # "Queue is empty"/"Queue ETA" elsewhere are correctly-technical
        # as-is, not part of this rename. The fuller instructional text
        # that used to live here ("drag files here, or use Add Files --
        # select a row to edit its settings live") was redundant either
        # way: DropTreeWidget's own empty-state placeholder ("Drop videos
        # here, or click 'Add Videos...'") already carries that message
        # exactly when it's relevant (queue is empty), and disappears once
        # it isn't needed.
        #
        # Not added to layout yet -- Add Videos/the overflow menu (built
        # below, alongside queue_list) share this same header row now
        # (reported live: they're operations *on* Videos, so they read
        # better next to its own heading than sitting below the table,
        # which is where a per-run detail like Save-to naturally starts
        # instead). videos_heading is added once that row is assembled.
        videos_heading = QLabel("Videos")
        self.queue_list = DropTreeWidget(self.add_files, on_reordered=self._push_undo_snapshot)
        # Suppresses the redundant per-cell focus-rect box Fusion draws
        # natively (see _NoItemFocusRectStyle's own docstring for why
        # this can't be done from QSS at all) -- scoped to this one
        # widget, not the whole app.
        #
        # Constructed with NO base-style argument -- confirmed the hard
        # way this matters, not a style preference: passing
        # self.queue_list.style() (QApplication's own shared style
        # singleton, since this widget never had setStyle() called on it
        # before) segfaulted reproducibly on shutdown. QProxyStyle's
        # single-argument constructor takes ownership of whatever style
        # it's given, and that shared singleton is also owned/used by
        # every other widget in the app plus QApplication itself --
        # deleting it out from under them on teardown double-freed it.
        # The no-argument form forwards to QApplication::style()
        # dynamically at paint time instead, without ever taking
        # ownership of it.
        #
        # setParent(self.queue_list) is the actual fix for a SECOND,
        # separate segfault -- confirmed via full-suite runs (not just
        # the quick single-window check above): QWidget.setStyle() does
        # not give the widget C++ ownership of the style either, so
        # nothing in Qt's own object tree was keeping this alive -- only
        # a Python reference (self._queue_list_style, kept below purely
        # for readability now) was, and Python's GC timing isn't
        # guaranteed to stay in sync with Qt's C++ object graph under
        # heavy churn (many MainWindows constructed/destroyed rapidly, as
        # the real test suite does). Explicit QObject parentage makes Qt
        # itself destroy this exactly when queue_list is destroyed,
        # deterministically, regardless of Python-side GC timing.
        self._queue_list_style = _NoItemFocusRectStyle()
        self._queue_list_style.setParent(self.queue_list)
        self.queue_list.setStyle(self._queue_list_style)
        self.queue_list.setColumnCount(len(QUEUE_COLUMN_HEADERS))
        self.queue_list.setHeaderLabels(QUEUE_COLUMN_HEADERS)
        # File is Interactive/user-resizable, not Stretch (which auto-
        # claims leftover space but also makes Qt refuse to let it be
        # dragged at all, silently, with no visible resize handle) --
        # explicit initial widths below instead of Qt's generic default,
        # sized to each column's actual content ("H.264 1280x720",
        # "392.2KB", ...). Result, the last column, is the one exception:
        # setStretchLastSection(True) makes *it* claim whatever's left
        # over on the right rather than leaving a bare gap between it and
        # the panel's edge -- losing manual-resize on Result specifically
        # is an easy trade, unlike File, since its content ("612.3MB (71%
        # smaller)") doesn't vary anywhere near as much as a filename does.
        self.queue_list.header().setStretchLastSection(True)
        # Video absorbed the old separate File column's width too (its
        # own delegate now paints filename + codec/resolution/audio
        # subtitle stacked in this one column, see queue_widget.py) --
        # roughly File + Video's old combined width, not either alone.
        for col, width in (
            (VIDEO_COL, 260), (DURATION_COL, 70), (SIZE_COL, 60),
        ):
            self.queue_list.setColumnWidth(col, width)
        self.queue_list.itemSelectionChanged.connect(self._on_queue_selection_changed)
        self.queue_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self._on_queue_context_menu)
        # Not added to layout yet -- comes after the header row below,
        # which needs add_files_btn/queue_menu_btn built first.

        # header_row, not q_btns -- shares the same row as videos_heading
        # now (see that label's own comment above for why); the name
        # stays q_btns below purely so the rest of this block (queue_menu
        # and its actions) doesn't need touching.
        q_btns = header_row = QHBoxLayout()
        header_row.addWidget(videos_heading)
        header_row.addStretch()
        self.add_files_btn = QPushButton("Add Videos…")
        self.add_files_btn.clicked.connect(self._pick_files)
        q_btns.addWidget(self.add_files_btn)
        # One overflow menu for everything that isn't the primary, always-
        # needed action (Add Videos) -- Remove Selected/Clear Queue keep
        # their existing Delete-key and right-click-menu entry points
        # untouched, this just removes their own permanent toolbar buttons.
        # Attribute names unchanged (self.remove_btn/self.clear_btn/
        # self.pause_after_check) despite now being QActions, not
        # QPushButton/QCheckBox -- QAction supports the same setEnabled/
        # setChecked/isChecked calls every existing queue_controller.py
        # call site for these three already uses, so none of that logic
        # needed to change, only how each one is built and wired here.
        self.queue_menu_btn = QToolButton()
        self.queue_menu_btn.setText("⋯")
        # "More actions", not "More queue actions" -- half of what's in
        # here (Show Conversion Log, Copy FFmpeg Command, Settings…)
        # isn't a queue action at all. Reported live.
        self.queue_menu_btn.setToolTip("More actions")
        self.queue_menu_btn.setPopupMode(QToolButton.InstantPopup)
        queue_menu = QMenu(self.queue_menu_btn)
        self.remove_btn = QAction("Remove Selected", self)
        self.remove_btn.triggered.connect(self._remove_selected)
        self.clear_btn = QAction("Clear Queue", self)
        self.clear_btn.triggered.connect(self._clear_queue)
        queue_menu.addAction(self.remove_btn)
        queue_menu.addAction(self.clear_btn)
        queue_menu.addSeparator()
        self.pause_after_check = QAction("Stop After Current Video", self)
        self.pause_after_check.setCheckable(True)
        self.pause_after_check.setEnabled(False)
        self.pause_after_check.toggled.connect(self._on_pause_after_toggled)
        queue_menu.addAction(self.pause_after_check)
        queue_menu.addSeparator()
        show_log_action = QAction("Show Conversion Log", self)
        show_log_action.triggered.connect(self._show_log_window)
        queue_menu.addAction(show_log_action)
        copy_command_action = QAction("Copy FFmpeg Command", self)
        copy_command_action.triggered.connect(self._copy_command_to_clipboard)
        queue_menu.addAction(copy_command_action)
        queue_menu.addSeparator()
        settings_action = QAction("Settings…", self)
        settings_action.triggered.connect(self._open_settings_dialog)
        queue_menu.addAction(settings_action)
        self.queue_menu_btn.setMenu(queue_menu)
        q_btns.addWidget(self.queue_menu_btn)
        layout.addLayout(header_row)
        layout.addWidget(self.queue_list, 1)

        # Output folder is a per-run detail, not the first decision anyone
        # makes -- it lives below the queue, not up with Quality/Format,
        # where it's actually used (Convert itself now sits further below
        # still, trailing status/progress/ETA/stats -- see run_row's own
        # comment further down for why).
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(formatting.display_path(self.output_dir))
        # Typing a path directly, not just Change...'s browse dialog -- the
        # dialog only ever hands back a real, already-existing directory,
        # so unlike there, a typed path isn't checked to exist here either;
        # it's created on demand (mkdir(parents=True, exist_ok=True)) the
        # same way a browsed-to path already was, right before it's
        # actually used (Start, Open).
        self.output_edit.editingFinished.connect(self._on_output_edit_changed)
        # Visually quieter than an ordinary editable field (see style.qss's
        # #outputPathField rule, $TEXT_READONLY -- the one other muted-text
        # precedent in this app, normally reserved for genuinely read-only
        # QLineEdits) -- reads closer to a location/breadcrumb display than
        # an active text field for the common case of just glancing at or
        # clicking Open, without actually making it read-only: typing a
        # path directly, per the comment above, still needs to keep working.
        self.output_edit.setObjectName("outputPathField")
        browse_btn = QPushButton("Change…")
        browse_btn.clicked.connect(self._pick_output_dir)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_output_dir)
        out_row.addWidget(QLabel("Save to:"))
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_btn)
        out_row.addWidget(open_btn)
        layout.addLayout(out_row)

        # "Stop After Current Video" (self.pause_after_check, still that
        # attribute name) now lives in the overflow menu built above, not
        # a permanent checkbox here -- a one-shot request, not a
        # persistent policy either way: checking it lets the *currently*
        # running job finish untouched (no partial encode lost, unlike
        # Stop) and then halts before starting the next one;
        # queue_controller.py's _on_paused unchecks it again once that
        # happens, and _start()/_on_all_finished both disable it -- there's
        # nothing to "pause after" while idle or already paused.

        self.status_label = QLabel("Idle")
        # Matches what _set_status("Idle") itself would do (main.py) --
        # this initial text is set directly here, not through that
        # method, so the hidden-at-rest state has to be established
        # here too rather than waiting for the first real _set_status
        # call to establish it.
        self.status_label.setVisible(False)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)

        # Human-readable time remaining, prominent -- "About 8 minutes
        # remaining" -- separate from stats_label's own detailed fps/
        # bitrate/speed/ETA telemetry line just below, which stays exactly
        # as technical as it always was. Reported directly: the numbers
        # matter for this app's actual audience, they just shouldn't be
        # the first thing read.
        self.eta_label = QLabel("")
        layout.addWidget(self.eta_label)

        self.stats_label = QLabel("—")
        # palette(mid) reads as near-invisible on a dark theme -- default
        # text color, smaller size instead.
        self.stats_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.stats_label)

        # Convert/Cancel sit at the very bottom of the panel, after status/
        # progress/ETA/stats, not right under Save to -- reported live:
        # while converting, the progress area *is* the content, and Cancel
        # is an action on that content, so it reads better trailing it than
        # sitting above it. Idle/preparing/running/paused all still reach
        # this same row; only stop_btn's text/enabled state changes
        # (_set_status and friends, queue_controller.py), the row itself
        # never moves.
        run_row = QHBoxLayout()
        self.start_btn = QPushButton("Convert")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(36)
        # A capped max width, not just a stretch ratio -- 3:1 alone still
        # let the primary action grow to whatever width a wide window
        # happened to give this row, reported live as reading as an
        # unintentional stretch rather than a deliberately sized button.
        # Real controls (macOS's own default-action buttons included)
        # stay a comfortable width regardless of how much space they're
        # offered; the trailing stretch below is what actually absorbs
        # the leftover instead.
        self.start_btn.setMaximumWidth(280)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setMaximumWidth(110)
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        # Neutral secondary styling, same as every other plain QPushButton
        # (Change…/Open/Add Videos…) -- no #stopButton QSS rule anymore.
        # Cancelling an encode isn't a destructive action in the same sense
        # deleting something permanently is; Apple's own button-role
        # guidance reserves red specifically for the latter. Confirmed the
        # plain `QPushButton {}` rule alone (style.qss) is enough once the
        # destructive override is gone -- QSS properties merge, not reset,
        # so this needed no replacement rule, just deleting the old one.
        self.open_folder_btn = QPushButton("Open Folder")
        self.open_folder_btn.setMinimumHeight(36)
        self.open_folder_btn.clicked.connect(self._open_output_dir)
        # Right-anchored, Convert rightmost -- the leading stretch absorbs
        # all the extra width instead of the buttons (both already capped
        # above), so no per-button stretch factor is needed between them.
        # Borrows macOS's own dialog/sheet convention (secondary action to
        # the left of the primary, primary rightmost and tinted) even
        # though this row isn't a dialog -- reported live as not reading
        # like a deliberate concluding action while left-anchored under
        # Save to; that convention is recognizable enough on its own to be
        # worth reusing here regardless. stop_btn/open_folder_btn share the
        # same slot (left of Convert) and are never both visible at once --
        # _apply_run_phase_visuals (queue_controller.py) toggles between
        # them per phase; a hidden widget in a QHBoxLayout claims no space,
        # already relied on elsewhere in this file (quality_slider/
        # size_spin's own visibility toggles), so no extra layout logic is
        # needed for the mutual exclusivity.
        run_row.addStretch()
        run_row.addWidget(self.stop_btn)
        run_row.addWidget(self.open_folder_btn)
        run_row.addWidget(self.start_btn)
        layout.addLayout(run_row)

        # Not shown in the main layout at all, same treatment as
        # command_preview on the left -- raw ffmpeg stderr is a debugging
        # aid, not something the simplified default view needs permanent
        # space for even collapsed. "Show Conversion Log" (the overflow
        # menu, this same method above) opens it in its own small window
        # on demand, reparenting this exact widget rather than
        # duplicating it -- every _on_job_log/_on_job_started/etc. call
        # keeps appending to it unchanged either way. queue_list's own
        # existing stretch=1 above simply claims the space this used to
        # share with it.
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("ffmpeg output will appear here once a job starts…")
        return right
