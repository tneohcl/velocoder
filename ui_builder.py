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
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMenu, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QSlider, QSpinBox, QTabWidget, QToolButton, QVBoxLayout, QWidget,
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
# real section break rather than just another row.
SECTION_SPACING = 20

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
        # hardware status is now silent during normal operation --
        # Automatic Processing already just works, nobody needs ambient
        # reassurance a render node exists. See _maybe_note_no_hardware
        # (main.py) for the one case it still speaks up: no hardware
        # acceleration found at all, so Processing will always mean CPU.
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
        left = QWidget()
        layout = QVBoxLayout(left)
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
        return left

    @staticmethod
    def _capped_row(row: QHBoxLayout, max_width: int) -> QWidget:
        # QLayout has no setMaximumWidth of its own (that's a QWidget
        # method) -- wraps a segmented-button row in a plain container
        # widget just to carry the cap, same reasoning as start_btn's own
        # ceiling (ui_builder.py, run_row): the buttons' equal addWidget(
        # btn, 1) stretch factors still divide whatever width the
        # container actually gets evenly, so this only stops the row from
        # stretching to fill an unusually wide left pane -- it doesn't
        # change how the segments share space among themselves.
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

    def _build_video_group(self) -> QGroupBox:
        """Consumer build: Quality and Resolution share one "Video" card --
        previously two separate cards (Quality, Format), split when Format
        also held Processing/Compatibility/File Format; once those were
        all cut (CONSUMER_FORK_PLAN.md's v1 scope table) Resolution was
        the only thing left in Format, too thin to keep its own card
        border for one row, so it moved in with Quality instead (merged
        on explicit request, along with renaming this card "Video" -- the
        plainer, more recognizable label once it's the one card covering
        every video-side decision in the app). The app always runs
        main.py's _apply_automatic_processing() at startup instead of a
        visible Processing choice (same worker.best_available_engine()
        pick the removed "Automatic" button used to trigger on click), and
        always encodes Modern/HEVC-when-available rather than exposing a
        Most-Compatible/H.264 fallback as a user decision. Quality itself
        is a thin remote control over the same settings Expert's raw
        controls used to drive (main.py's _on_quality_tier_clicked ->
        _apply_settings_to_controls) -- no parallel state, no new logic
        beyond QUALITY_TIERS."""
        group = QGroupBox("Video")
        form = QFormLayout(group)
        form.setVerticalSpacing(14)

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
        # 340 -- 3 buttons at equal (1,1,1) stretch clipped "Better Quality"
        # (own sizeHint 114px) since 340/3 lands just under that even
        # before segment-border adjustments; reported live, confirmed by
        # measuring the actual button width against its own sizeHint
        # rather than guessing. 360 clears the tightest button's sizeHint
        # comfortably under equal-thirds division (114 * 3 = 342).
        form.addRow("Quality:", self._capped_row(quality_tier_row, 360))

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        # Keep Original, not 720p -- the sibling TITAN-i Transcoder app
        # defaults here to match its own starting preset, but silently
        # downscaling whatever a user drags in (a 4K master, say) is a bad
        # default for an app whose whole premise is "just make it work".
        self.res_combo.setCurrentIndex(0)  # Keep Original
        self.res_combo.currentIndexChanged.connect(self._on_control_changed)
        # Capped, not left to stretch -- same reasoning as start_btn's own
        # ceiling above: content here ("Keep Original", "1080p", ...) never
        # needs anywhere near this much width, so letting it fill an
        # unusually wide left pane just reads as an oversized web-form
        # field, not a deliberately sized control.
        self.res_combo.setMaximumWidth(200)
        form.addRow("Resolution:", self.res_combo)

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
        outer.addWidget(self._build_video_group())
        # Explicit trailing stretch -- the Video card (Quality+Resolution
        # merged into one) is the only item in this tab's own layout now,
        # and a lone Preferred-policy widget with nothing else to share
        # leftover space with gets stretched to fill it rather than
        # staying at its own sizeHint. Same fix the Audio tab below
        # already needed for the same one-group-per-tab reason.
        outer.addStretch()

        # Consumer build: no visible File Format row -- always MP4 (index 0
        # of CONTAINERS), the broadest-compatibility choice, per
        # CONSUMER_FORK_PLAN.md's v1 scope table. container_combo stays
        # constructed (not shown or added to any layout) so
        # _current_settings()/_apply_settings_to_controls() keep working
        # unchanged; it just never leaves its default index 0 ("mp4") since
        # nothing ever changes it now.
        self.container_combo = QComboBox()
        self.container_combo.addItems(CONTAINERS)

        # Everything below is the full technical control set the specialist
        # build always shows, unchanged -- see this file's own comment
        # further down (where this card used to be wrapped in a
        # collapsible group) for why it's never shown here.
        # self._video_expert_content, not a bare local -- a QWidget()
        # constructed with no C++ parent (never true here before: the old
        # collapsible-group wrapping gave it one via layout.addWidget)
        # is owned by Python reference counting alone, and nothing else
        # keeps a live reference to expert_content itself once this
        # function returns (only to specific *children* of it, like
        # self.encoder_combo -- which does not keep their parent alive).
        # Confirmed as a real crash, not a theoretical one: without this,
        # expert_content got garbage-collected right after construction,
        # taking every control inside it down with it -- the exact same
        # class of bug as the Settings-dialog crash fixed earlier in the
        # specialist build's own history (a parentless QComboBox there).
        self._video_expert_content = expert_content = QWidget()
        self.video_form = form = QFormLayout(expert_content)
        # Default Fusion spacing reads as cramped once every row has a small
        # secondary line under it (quality/speed tiers, the bit-depth combo's
        # own description) -- confirmed by screenshot, this is the fix.
        form.setVerticalSpacing(14)

        self.encoder_combo = QComboBox()
        for _, _, label in ENCODERS:
            self.encoder_combo.addItem(label)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        self.encoder_combo.setToolTip(
            "CPU (software): best quality-per-bitrate, but far slower --\n"
            "minutes to hours depending on length and settings. Choose\n"
            "H.265 or H.264 separately just below, in Codec.\n"
            "Intel (iGPU) / AMD (GPU) (hardware, HEVC only): much faster\n"
            "and barely touches the CPU, but generally trades away some\n"
            "quality-per-bitrate versus a well-tuned software encode at\n"
            "the same file size.\n"
            "Pick hardware for speed or large batches; CPU when quality\n"
            "matters most, or when you specifically want H.264 (Codec has\n"
            "no effect on hardware -- it's HEVC-only here). The Quality\n"
            "section's Processing/Compatibility rows are the friendly\n"
            "front end for this same choice."
        )
        form.addRow("Encoder:", self.encoder_combo)

        # rc_mode_combo stays the source of truth (everything downstream --
        # _on_rc_mode_changed, _current_settings, presets -- reads it) but
        # is never shown: the visible control is the two/three buttons
        # below, which just drive this combo's index. Two ways to reach the
        # same state would risk them drifting apart; one hidden model plus
        # a friendlier view over it can't.
        self.rc_mode_combo = QComboBox(expert_content)
        self.rc_mode_combo.hide()
        self.rc_mode_combo.currentIndexChanged.connect(self._on_rc_mode_changed)
        self.rc_mode_combo.currentIndexChanged.connect(self._sync_rc_buttons_to_combo)

        rc_row = QHBoxLayout()
        rc_row.setSpacing(0)
        self.rc_button_group = QButtonGroup(self)
        self.rc_quality_btn = QPushButton("Quality")
        self.rc_quality_btn.setObjectName("segLeft")
        self.rc_quality_btn.setToolTip("Aim for a consistent perceptual quality; file size follows.")
        self.rc_filesize_btn = QPushButton("File Size")
        self.rc_filesize_btn.setObjectName("segMid")
        self.rc_filesize_btn.setToolTip("Aim for a target output size; quality follows.")
        self.rc_advanced_btn = QPushButton("Advanced")
        self.rc_advanced_btn.setObjectName("segRight")
        self.rc_advanced_btn.setToolTip(
            "Fixed quantizer (CQP): the same compression level on every\n"
            "frame, regardless of content complexity. Rarely needed --\n"
            "Quality (ICQ) adapts per-frame and usually looks better for\n"
            "the same average bitrate."
        )
        for btn in (self.rc_quality_btn, self.rc_filesize_btn, self.rc_advanced_btn):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.rc_button_group.addButton(btn)
            rc_row.addWidget(btn, 1)
        self.rc_quality_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["quality"])
        )
        self.rc_filesize_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["file_size"])
        )
        self.rc_advanced_btn.clicked.connect(
            lambda: self._set_rc_mode(RC_MODE_FRIENDLY[self._current_encoder_key()]["advanced"])
        )
        form.addRow("Rate control:", self._capped_row(rc_row, 280))

        quality_row = QHBoxLayout()
        self.quality_slider = QSlider(Qt.Horizontal)
        # Left = worse quality/smaller, right = better quality/larger --
        # the intuitive direction for a horizontal slider. The underlying
        # ICQ/CQP/CRF value this drives is the opposite (lower number is
        # better quality), so invertedAppearance/-Controls flips the visual
        # and interaction direction while .value() keeps returning the real
        # number untouched -- Qt handles the remapping, nothing downstream
        # (settings, presets, build_args) needs to know this happened.
        self.quality_slider.setInvertedAppearance(True)
        self.quality_slider.setInvertedControls(True)
        self.quality_slider.setToolTip(
            "Left: more compression, smaller file.\n"
            "Right: higher quality, larger file."
        )
        self.quality_slider.valueChanged.connect(self._on_quality_changed)
        self.quality_label = QLabel()
        self.quality_label.setStyleSheet("font-size: 9pt;")
        self.size_spin = QSpinBox()
        self.size_spin.setRange(10, 20000)
        self.size_spin.setSingleStep(50)
        self.size_spin.setSuffix(" MB")
        self.size_spin.setValue(1000)
        self.size_spin.setToolTip("Target output size -- the actual bitrate is computed from this file's length.")
        self.size_spin.valueChanged.connect(self._on_control_changed)
        quality_row.addWidget(self.quality_slider, 1)
        quality_row.addWidget(self.quality_label)
        quality_row.addWidget(self.size_spin, 1)

        # Only one of these two is ever visible at a time (is_bitrate in
        # _on_rc_mode_changed) -- same one-row-two-widgets pattern as
        # quality_slider/size_spin just above, rather than two separate rows
        # where one is always an empty gap.
        # stretch=1 on both (only one is ever visible at a time) so each
        # claims the row's full width and its own AlignCenter has something
        # to center within -- otherwise a shrink-wrapped label sits flush
        # left with nothing to visually tie it to the slider above it.
        quality_detail_row = QHBoxLayout()
        self.quality_tier_label = QLabel()
        self.quality_tier_label.setAlignment(Qt.AlignCenter)
        self._apply_fuzzy_caption_style(self.quality_tier_label)
        self.size_estimate_label = QLabel()
        self.size_estimate_label.setAlignment(Qt.AlignCenter)
        self.size_estimate_label.setStyleSheet("font-size: 9pt;")
        quality_detail_row.addWidget(self.quality_tier_label, 1)
        quality_detail_row.addWidget(self.size_estimate_label, 1)

        # The slider and its fuzzy caption underneath share one outlined
        # box (objectName carries the QSS rule -- see style.qss's
        # #fuzzyGroup, shared with Speed's identical box below) instead of
        # being two independent-looking form rows -- the caption explains
        # *that specific slider*, so it reads better visually grouped with
        # it rather than just sitting in the row underneath.
        quality_group = QWidget()
        quality_group.setObjectName("fuzzyGroup")
        quality_group_layout = QVBoxLayout(quality_group)
        quality_group_layout.setContentsMargins(8, 6, 8, 6)
        # Default QVBoxLayout spacing (Fusion's ~11px) read as the caption
        # floating unrelated to the slider above it rather than explaining
        # it -- confirmed by screenshot.
        quality_group_layout.setSpacing(2)
        quality_group_layout.addLayout(quality_row)
        quality_group_layout.addLayout(quality_detail_row)
        form.addRow("Quality:", quality_group)

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
        form.addRow("Speed:", speed_group)

        # Bit depth's tradeoff used to live in a separate caption row below
        # the combo -- folded directly into the item text instead (one less
        # row fighting Quality/Speed for space, and the tradeoff is right
        # there the moment the dropdown opens rather than a beat later).
        self.bitdepth_combo = QComboBox()
        self.bitdepth_combo.addItem("10-bit -- smoother gradients, larger file", userData=10)
        self.bitdepth_combo.addItem("8-bit -- smaller, maximum compatibility", userData=8)
        self.bitdepth_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Bit depth:", self.bitdepth_combo)

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

        self.deinterlace_check = QCheckBox("Deinterlace (interlaced or telecined source)")
        self.deinterlace_check.setToolTip(
            "Container-level progressive/interlaced flags are frequently wrong,\n"
            "especially on camcorder-sourced footage. New files are sampled\n"
            "and this is set automatically, but detection only checks the\n"
            "first ~20s -- override it here if the output still shows\n"
            "combing/interlacing artifacts."
        )
        self.deinterlace_check.stateChanged.connect(self._on_deinterlace_checkbox_changed)
        form.addRow("", self.deinterlace_check)

        # H.265/H.264 -- only meaningful for the CPU engine (no h264_vaapi
        # wired up, hardware is HEVC-only here), so it's disabled and
        # forced to H.265 whenever Encoder above is set to a hardware
        # engine -- see main.py's _on_encoder_changed, which this cascades
        # into exactly like encoder_combo's own change does. This whole
        # form is unshown internal state in this build (see this file's
        # own comment where Expert used to be wrapped and added) -- always
        # stays at its DEFAULT_SETTINGS value ("None"/H.265) now that
        # there's no Compatibility row to drive it.
        self.codec_combo = QComboBox()
        for value, label in CODECS:
            self.codec_combo.addItem(label, userData=value)
        self.codec_combo.setToolTip(
            "H.265 (HEVC): better compression -- smaller file at the\n"
            "same quality -- but not universally supported by older\n"
            "devices/TVs/browsers, or only with extra licensing hassle.\n"
            "H.264 (AVC): larger files for the same quality, but plays\n"
            "back on virtually anything. Pick this over H.265 when\n"
            "playback compatibility matters more than file size.\n"
            "Only applies to the CPU encoder -- Intel/AMD hardware\n"
            "encoding here is HEVC-only, so this is disabled and forced\n"
            "to H.265 whenever Encoder above is set to one of those.\n"
            "Same as the Quality section's Compatibility row above."
        )
        self.codec_combo.currentIndexChanged.connect(self._on_encoder_changed)
        form.addRow("Codec:", self.codec_combo)

        # Consumer build: expert_content (and everything built into it
        # above -- encoder_combo, rc_mode_combo, codec_combo, bitdepth_combo,
        # tune_combo, deinterlace_check) is deliberately never wrapped in a
        # collapsible group or added to outer, unlike the specialist build.
        # It stays fully constructed and live -- _current_settings()/
        # _apply_settings_to_controls() still read and drive it exactly as
        # before, so that plumbing didn't need touching -- it's just never
        # shown to the user. See CONSUMER_FORK_PLAN.md's v1 scope table.
        return tab

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        # tabPageCard -- see _build_video_tab's own comment on this same
        # objectName for why the card border/radius/fill lives here now
        # instead of on QTabWidget::pane.
        tab.setObjectName("tabPageCard")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(12, 12, 12, 12)

        normal_group = QGroupBox("Audio")
        normal_form = QFormLayout(normal_group)
        normal_form.setVerticalSpacing(14)

        audio_choice_row = QHBoxLayout()
        audio_choice_row.setSpacing(0)
        self.audio_choice_button_group = QButtonGroup(self)
        self.audio_automatic_btn = QPushButton("Automatic")
        self.audio_automatic_btn.setObjectName("segLeft")
        self.audio_automatic_btn.setToolTip(
            "Keeps the original audio track untouched whenever the\n"
            "source is already AAC/AC-3/E-AC-3 (a fast, lossless stream\n"
            "copy); re-encodes to AAC only when it isn't."
        )
        self.audio_stereo_btn = QPushButton("Convert to Stereo")
        self.audio_stereo_btn.setObjectName("segRight")
        self.audio_stereo_btn.setToolTip(
            "Mixes 5.1/7.1/etc. sources down to plain stereo, for a\n"
            "phone, laptop, or anything without a surround setup. No\n"
            "effect on a source that's already stereo or mono."
        )
        for btn, choice in (
            (self.audio_automatic_btn, "automatic"),
            (self.audio_stereo_btn, "stereo"),
        ):
            btn.setCheckable(True)
            btn.setMinimumWidth(self._segmented_btn_min_width(btn))
            self.audio_choice_button_group.addButton(btn)
            audio_choice_row.addWidget(btn, 1)
            btn.clicked.connect(lambda _checked, c=choice: self._on_audio_choice(c))
        normal_form.addRow("Audio:", self._capped_row(audio_choice_row, 320))

        outer.addWidget(normal_group)
        # Explicit trailing stretch -- normal_group is the only item in
        # this tab's own layout, and a lone Preferred-policy widget with
        # nothing else to share leftover space with gets stretched to
        # fill it rather than staying at its own sizeHint (confirmed
        # live: a large blank gap opened up *inside* the Audio box's own
        # border). Same fix the Video tab above needs for the same
        # one-group-per-tab reason.
        outer.addStretch()

        # Full technical audio control set, same "internal state, never
        # shown" treatment as the Video tab's Expert section -- see that
        # section's own comment on self._video_expert_content for why this
        # needs a real, lasting Python reference (self._audio_expert_
        # content) rather than a bare local variable: a parentless QWidget
        # with nothing keeping it alive gets garbage-collected, taking
        # every control inside it down too, which is a real crash, not a
        # theoretical one -- confirmed directly when this was first tried
        # as a bare local (audio_bitrate_slider came back as "Internal
        # C++ object already deleted" the moment __init__ tried to use it).
        self._audio_expert_content = expert_content = QWidget()
        form = QFormLayout(expert_content)
        # Matches the Video tab's Expert form exactly (see video_form
        # above) -- Fusion's default (~11px) was never applied here, so
        # this tab's rows sat visibly tighter than Video's despite both
        # using the same fuzzy-caption-box row pattern. Reported live.
        form.setVerticalSpacing(14)

        self.audio_combo = QComboBox()
        self.audio_combo.addItems(AUDIO_TRACK_LABELS)
        self.audio_combo.currentIndexChanged.connect(self._on_control_changed)
        form.addRow("Audio track:", self.audio_combo)

        self.audio_copy_check = QCheckBox("Copy audio if compatible (aac/ac3/eac3)")
        self.audio_copy_check.setChecked(True)
        self.audio_copy_check.setToolTip(
            "When the source audio is already AAC, AC-3, or E-AC-3, this\n"
            "passes it through untouched (a stream copy) instead of\n"
            "re-encoding it -- zero quality loss and much faster, since\n"
            "ffmpeg never has to decode and re-compress that track.\n"
            "Unchecked, or when the source is some other codec (DTS, PCM,\n"
            "MP3, ...), audio is always re-encoded to AAC at the bitrate\n"
            "set below."
        )
        self.audio_copy_check.stateChanged.connect(self._on_control_changed)
        form.addRow("", self.audio_copy_check)

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
            "Only applies when the source audio is actually being "
            "transcoded -- see \"Copy audio if compatible\" above."
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
        form.addRow("Audio bitrate (if transcoded):", audio_bitrate_group)

        self.audio_downmix_check = QCheckBox("Downmix to stereo (if source has more channels)")
        self.audio_downmix_check.setToolTip(
            "Mixes 5.1/7.1/etc. sources down to plain stereo -- for\n"
            "playback on a phone, laptop, or anything without a surround\n"
            "setup. Has no effect on a source that's already stereo or\n"
            "mono. A stream copy can't remix channels, so on a source\n"
            "that does have more channels, checking this transcodes the\n"
            "audio track even if it would otherwise have been copied\n"
            "through untouched."
        )
        self.audio_downmix_check.stateChanged.connect(self._on_control_changed)
        form.addRow("", self.audio_downmix_check)

        # Consumer build: expert_content (audio_combo, audio_copy_check,
        # audio_bitrate_slider, audio_downmix_check) stays fully constructed
        # and live -- same reasoning as the Video tab's own Expert section
        # above -- but is never wrapped in a collapsible group or added to
        # normal_form, so it's never shown. See CONSUMER_FORK_PLAN.md.
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
        layout.addWidget(QLabel("Videos"))
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
        layout.addWidget(self.queue_list, 1)

        q_btns = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Videos…")
        self.add_files_btn.clicked.connect(self._pick_files)
        q_btns.addWidget(self.add_files_btn)
        q_btns.addStretch()
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
        layout.addLayout(q_btns)

        # Output folder is a per-run detail, not the first decision anyone
        # makes -- it lives below the queue, not up with Quality/Format,
        # where it's actually used (Convert itself now sits further below
        # still, trailing status/progress/ETA/stats -- see run_row's own
        # comment further down for why).
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_dir))
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
