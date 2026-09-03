"""Widget-construction methods, split out of MainWindow into a mixin --
this is the bulk of what made main.py huge: every tab's controls, the
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
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QSlider, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

import formatting
from constants import (
    AUDIO_BITRATES, AUDIO_TRACK_LABELS, CONTAINERS, ENCODERS, RC_MODE_FRIENDLY,
    RESOLUTIONS, X265_PRESETS, X265_TUNES,
)
from queue_widget import (
    DropTreeWidget, FILE_COL, VIDEO_COL, DURATION_COL, AUDIO_COL, SIZE_COL,
    RESULT_COL, QUEUE_COLUMN_HEADERS,
)

PANEL_MARGIN = 12
PANEL_SPACING = 10

THEME_CHOICES = [("dark", "Dark"), ("light", "Light"), ("system", "Match System")]


class _UiBuilderMixin:
    def _build_ui(self):
        self._splitter = QSplitter(Qt.Horizontal)
        self.setCentralWidget(self._splitter)
        self._splitter.addWidget(self._build_left_panel())
        self._splitter.addWidget(self._build_right_panel())
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._build_status_bar()

    def _build_status_bar(self):
        # A qBittorrent-style footer strip: ambient, persistent, out of the
        # way of the actual controls. Theme picker lives here rather than a
        # menu bar -- a whole menu bar for one three-item setting was more
        # chrome than the setting warranted.
        #
        # QStatusBar has two genuinely different widget areas, not just a
        # single row: addWidget puts something on the left (also where a
        # showMessage() temporary message would appear -- it temporarily
        # hides addWidget widgets specifically, though nothing here calls
        # showMessage today) and addPermanentWidget puts something on the
        # right, immune to that. Theme goes left/addWidget, hardware status
        # stays right/addPermanentWidget -- deliberately different APIs, not
        # just visual left/right positioning of the same call.
        #
        # setContentsMargins, not a QSS padding rule -- QStatusBar manages
        # its own internal layout for addWidget/addPermanentWidget content,
        # which a stylesheet padding rule turned out not to reach at all
        # (tried it, confirmed by screenshot: zero visible difference).
        # Contents margins are a plain widget property, not something QSS
        # has to cooperate with, so they reliably do give the whole footer
        # strip some vertical breathing room instead of sitting flush
        # against its own top/bottom edge.
        self.statusBar().setContentsMargins(8, 4, 8, 4)
        theme_label = QLabel("Theme:")
        theme_label.setStyleSheet("font-size: 9pt;")
        self.statusBar().addWidget(theme_label)
        self.theme_combo = QComboBox()
        for value, label in THEME_CHOICES:
            self.theme_combo.addItem(label, userData=value)
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(self._theme_choice))
        self.theme_combo.currentIndexChanged.connect(
            lambda: self._apply_theme(self.theme_combo.currentData())
        )
        self.statusBar().addWidget(self.theme_combo)

        self.hw_status_label = QLabel(formatting.hardware_status_text())
        self.hw_status_label.setStyleSheet("font-size: 9pt;")
        self.statusBar().addPermanentWidget(self.hw_status_label)

    def _build_preset_row(self) -> QHBoxLayout:
        # Preset is the main lever -- it sets every other control at once --
        # so it sits above the tabs, not buried as one of them. Previously a
        # QToolBar, but that spans the full window and draws a separator
        # below it; a plain row scoped to the left column reads as part of
        # the settings panel instead of a distinct chrome region.
        row = QHBoxLayout()
        label = QLabel("Preset:")
        bold = QFont()
        bold.setBold(True)
        label.setFont(bold)
        row.addWidget(label)

        self.preset_combo = QComboBox()
        # Long preset names (both built-ins, and anything a user later saves)
        # were hard-clipping mid-character against the row's other widgets
        # with no ellipsis -- this floor is measured to comfortably fit the
        # longer built-in name; setToolTip in _on_preset_selected below is
        # the safety net for anything still longer than that.
        self.preset_combo.setMinimumWidth(300)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        row.addWidget(self.preset_combo, 1)

        # Custom icons, not style().standardIcon(...) -- Fusion's standard
        # icons are colored from the app's QPalette, which this app never
        # sets of its own (only this stylesheet), so they stayed locked to
        # whatever Fusion's default happens to be regardless of the chosen
        # theme -- confirmed by screenshot: SP_TrashIcon in particular was
        # all but invisible against a light-theme button. The queue row's
        # status icons (▶/✓/⚠, _on_job_started/_finished/_failed) used to
        # be standardIcon() too, mixing two icon styles in one app -- an
        # Apple-design-language pass's "one icon family throughout" moved
        # those onto the same custom-SVG family this file already used for
        # Save/Delete, not the other way around: reverting Save/Delete back
        # to standardIcon() would have reintroduced the confirmed contrast
        # bug above just to make the family "native" instead of consistent.
        # _refresh_themed_icons re-applies these on every theme change,
        # same reason the SVGs style.qss references have separate dark/
        # light files.
        self.save_btn = QPushButton(self._themed_icon("save"), "Save As…")
        self.save_btn.clicked.connect(self._save_preset_as)
        self.delete_btn = QPushButton(self._themed_icon("delete"), "Delete")
        self.delete_btn.clicked.connect(self._delete_preset)
        row.addWidget(self.save_btn)
        row.addWidget(self.delete_btn)
        return row

    def _build_left_panel(self) -> QWidget:
        left = QWidget()
        layout = QVBoxLayout(left)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        layout.addLayout(self._build_preset_row())

        tabs = QTabWidget()
        tabs.addTab(self._build_video_tab(), "Video")
        tabs.addTab(self._build_audio_tab(), "Audio")
        # The two tabs have very different row counts, and a QSplitter pane
        # is always forced to the full window height regardless of content
        # -- capping the tab widget to its natural size (instead of letting
        # it stretch into that forced height) keeps the sparser tab from
        # looking broken. The command preview below puts the leftover space
        # to use instead of leaving it blank.
        tabs.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        layout.addWidget(tabs)

        self._command_group = self._build_command_preview()
        layout.addWidget(self._command_group)

        layout.addStretch(1)
        return left

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

    def _build_command_preview(self) -> QGroupBox:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)

        self.command_preview = QPlainTextEdit()
        self.command_preview.setReadOnly(True)
        self.command_preview.setMaximumHeight(110)
        self.command_preview.setStyleSheet("font-family: monospace; font-size: 9pt;")
        # _format_preview_text below already breaks the command into one
        # logical group per line (input / filter / mapping / etc.) -- with
        # WidgetWidth wrap, a single long -vf value still doesn't fit one
        # line and gets re-wrapped a second time at an arbitrary character
        # (confirmed by screenshot: "force_divisible_b" / "y=2" mid-token).
        # NoWrap preserves the intended one-line-per-group layout and lets
        # only that one line scroll horizontally instead.
        self.command_preview.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.command_preview)

        # Below the text, not above it, and visibly smaller -- this is a
        # power-user convenience for a section that's already collapsed by
        # default, not an action worth the same visual weight as Start or
        # the preset buttons.
        copy_row = QHBoxLayout()
        copy_row.addStretch()
        copy_btn = QPushButton("Copy")
        copy_btn.setToolTip("Copy the full command to the clipboard")
        copy_btn.setStyleSheet("padding: 2px 10px; font-size: 8pt;")
        copy_btn.clicked.connect(self._copy_command_to_clipboard)
        copy_row.addWidget(copy_btn)
        layout.addLayout(copy_row)

        # Collapsed by default: this is the one control in the whole left
        # panel aimed at a technical reader double-checking the exact ffmpeg
        # invocation, not something the simplified default view needs open.
        return self._make_collapsible_group("Effective Command", content, expanded=False)

    def _build_video_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setSpacing(PANEL_SPACING)

        encoding_group = QGroupBox("Encoding")
        self.video_form = form = QFormLayout(encoding_group)
        # Default Fusion spacing reads as cramped once every row has a small
        # secondary line under it (quality/speed tiers, the bit-depth combo's
        # own description) -- confirmed by screenshot, this is the fix.
        form.setVerticalSpacing(14)

        self.encoder_combo = QComboBox()
        for _, _, label in ENCODERS:
            self.encoder_combo.addItem(label)
        self.encoder_combo.currentIndexChanged.connect(self._on_encoder_changed)
        self.encoder_combo.setToolTip(
            "CPU (libx265, software): best quality-per-bitrate, but far\n"
            "slower -- minutes to hours depending on length and settings.\n"
            "Intel (iGPU) / AMD (GPU) (hevc_vaapi, hardware): much\n"
            "faster and barely touches the CPU, but generally trades away\n"
            "some quality-per-bitrate versus a well-tuned x265 software\n"
            "encode at the same file size.\n"
            "Pick hardware for speed or large batches; CPU when quality\n"
            "matters most."
        )
        form.addRow("Encoder:", self.encoder_combo)

        # rc_mode_combo stays the source of truth (everything downstream --
        # _on_rc_mode_changed, _current_settings, presets -- reads it) but
        # is never shown: the visible control is the two/three buttons
        # below, which just drive this combo's index. Two ways to reach the
        # same state would risk them drifting apart; one hidden model plus
        # a friendlier view over it can't.
        self.rc_mode_combo = QComboBox(encoding_group)
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
        form.addRow("Rate control:", rc_row)

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
        self.speed_x265_label = QLabel()
        self.speed_x265_label.setStyleSheet("font-size: 9pt;")
        speed_row.addWidget(self.speed_x265_label)

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

        self.tune_combo = QComboBox()
        self.tune_combo.addItems(X265_TUNES)
        self.tune_combo.currentIndexChanged.connect(self._on_control_changed)
        self.tune_combo.setToolTip(
            "x265's built-in tuning presets -- \"None\" (the default) is\n"
            "right for most sources. Only switch if one clearly applies:\n"
            "animation -- flat colors, sharp edges (cartoons/anime).\n"
            "grain -- preserves film grain instead of smoothing it away.\n"
            "fastdecode -- eases decoding for low-power playback devices.\n"
            "zerolatency -- minimizes encode delay for live streaming,\n"
            "not useful for this app's batch transcodes.\n"
            "psnr/ssim -- optimizes for a benchmark metric, not\n"
            "perceptual quality -- rarely what you actually want."
        )
        form.addRow("Tune (x265 only):", self.tune_combo)

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

        outer.addWidget(encoding_group)

        output_group = QGroupBox("Format")
        out_form = QFormLayout(output_group)

        self.res_combo = QComboBox()
        for r in RESOLUTIONS:
            self.res_combo.addItem(r["label"])
        self.res_combo.setCurrentIndex(2)  # 720p
        self.res_combo.currentIndexChanged.connect(self._on_control_changed)
        out_form.addRow("Resolution:", self.res_combo)

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
        out_form.addRow("Container:", self.container_combo)

        outer.addWidget(output_group)
        return tab

    def _build_audio_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)

        group = QGroupBox("Audio Settings")
        form = QFormLayout(group)
        # Matches the Video tab's encoding_group exactly (see video_form
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

        outer.addWidget(group)
        return tab

    def _build_right_panel(self) -> QWidget:
        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN)
        layout.setSpacing(PANEL_SPACING)

        # Just "Queue" -- the fuller instructional text used to live here
        # ("drag files here, or use Add Files -- select a row to edit its
        # settings live") but was redundant either way: DropTreeWidget's
        # own empty-state placeholder ("Drag video files here, or click
        # 'Add Files...'") already carries that message exactly when it's
        # relevant (queue is empty), and disappears once it isn't needed.
        layout.addWidget(QLabel("Queue"))
        self.queue_list = DropTreeWidget(self.add_files, on_reordered=self._push_undo_snapshot)
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
        for col, width in (
            (FILE_COL, 150), (VIDEO_COL, 130), (DURATION_COL, 70),
            (AUDIO_COL, 60), (SIZE_COL, 52), (RESULT_COL, 85),
        ):
            self.queue_list.setColumnWidth(col, width)
        self.queue_list.itemSelectionChanged.connect(self._on_queue_selection_changed)
        self.queue_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self._on_queue_context_menu)
        layout.addWidget(self.queue_list, 1)

        q_btns = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Files…")
        self.add_files_btn.clicked.connect(self._pick_files)
        self.remove_btn = QPushButton("Remove Selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        self.clear_btn = QPushButton("Clear Queue")
        self.clear_btn.clicked.connect(self._clear_queue)
        q_btns.addWidget(self.add_files_btn)
        q_btns.addWidget(self.remove_btn)
        q_btns.addWidget(self.clear_btn)
        q_btns.addStretch()
        layout.addLayout(q_btns)

        # Output folder is a per-run detail, not the first decision anyone
        # makes -- it lives right next to Start, where it's actually used.
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit(str(self.output_dir))
        # Typing a path directly, not just Change...'s browse dialog -- the
        # dialog only ever hands back a real, already-existing directory,
        # so unlike there, a typed path isn't checked to exist here either;
        # it's created on demand (mkdir(parents=True, exist_ok=True)) the
        # same way a browsed-to path already was, right before it's
        # actually used (Start, Open).
        self.output_edit.editingFinished.connect(self._on_output_edit_changed)
        browse_btn = QPushButton("Change…")
        browse_btn.clicked.connect(self._pick_output_dir)
        open_btn = QPushButton("Open")
        open_btn.clicked.connect(self._open_output_dir)
        out_row.addWidget(QLabel("Output folder:"))
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(browse_btn)
        out_row.addWidget(open_btn)
        layout.addLayout(out_row)

        run_row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("startButton")
        self.start_btn.setDefault(True)
        self.start_btn.setMinimumHeight(36)
        self.start_btn.clicked.connect(self._start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopButton")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.clicked.connect(self._stop)
        self.stop_btn.setEnabled(False)
        run_row.addWidget(self.start_btn, 1)
        run_row.addWidget(self.stop_btn, 1)
        layout.addLayout(run_row)

        self.status_label = QLabel("Idle")
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        layout.addWidget(self.progress_bar)

        self.stats_label = QLabel("—")
        # Same fix as hw_status_label above: palette(mid) reads as
        # near-invisible on a dark theme. Default text color, smaller size.
        self.stats_label.setStyleSheet("font-size: 10pt;")
        layout.addWidget(self.stats_label)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("ffmpeg output will appear here once a job starts…")
        # Collapsed by default, same reasoning and the same disclosure
        # pattern as Effective Command on the left: raw ffmpeg stderr is a
        # debugging aid, not something the simplified default view needs
        # open, and the queue list above happily reclaims the freed space
        # (already the only other stretch=1 widget in this layout).
        self._log_group = self._make_collapsible_group("Log", self.log_view, expanded=False)
        layout.addWidget(self._log_group, 2)
        return right
