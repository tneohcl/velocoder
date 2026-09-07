"""Color tokens for style.qss's $TOKEN placeholders, one dict per theme.
Hand-tuned per theme, not a mechanical inversion of one another -- a color
that reads fine on a dark background often doesn't just because its
lightness got flipped (e.g. ACCENT is deliberately a different shade in
each palette: light enough for good contrast as *text* on the dark
theme's near-black backgrounds, deep enough for good contrast as text on
the light theme's white/near-white ones).

Layout/spacing lives in style.qss directly and is never tokenized here --
only color should differ between themes.
"""

DARK = {
    "BG_WINDOW": "#1a1d23",
    "BG_PANEL": "#21252c",
    "BG_FIELD": "#171a1f",
    "BG_CONTROL": "#2a2f38",
    "BG_CONTROL_HOVER": "#333a45",
    "BG_CONTROL_PRESSED": "#23272e",
    "BG_DISABLED": "#1e2127",
    "BORDER": "#333944",
    # Reported live: BORDER_STRONG (buttons) and the old #tabPageCard
    # border (plain BORDER) read too close to BG_PANEL/BG_CONTROL to
    # register as a real edge at normal viewing distance -- three
    # deliberately distinct levels now: BORDER (quiet, inner section
    # cards/fields, unchanged), BORDER_OUTER (the main tab card + tab
    # bar's own structural edge), BORDER_STRONG (ordinary buttons --
    # interactive controls should read *more* clickable than a container
    # organizes, not less, hence strongest of the three). BORDER_STRONG's
    # new value matches BORDER_HOVER almost exactly on purpose -- that
    # was already the right amount of contrast against BG_PANEL/BG_
    # CONTROL, just previously reserved for hover-only states.
    "BORDER_STRONG": "#454c59",
    "BORDER_OUTER": "#414956",
    "BORDER_HOVER": "#454c59",
    "TEXT_PRIMARY": "#e6e8eb",
    "TEXT_SECONDARY": "#8b93a1",
    "TEXT_TERTIARY": "#c3c8d1",
    "TEXT_READONLY": "#9aa1ac",
    # Reported live: the quality/speed/audio-bitrate tier captions ("Balanced",
    # "Streaming -- efficient") shared TEXT_SECONDARY's own real
    # QPalette.PlaceholderText-driven treatment (theming._fuzzy_text_color)
    # with the queue's empty-state hint text -- correct for that hint (pure
    # decoration), wrong for these, since on a real desktop session
    # PlaceholderText can be translucent enough to read as almost invisible,
    # and these captions carry real information. A dedicated, static token
    # instead: quieter than TEXT_SECONDARY (still clearly a secondary-tier
    # caption, not body text) but nowhere near as faint as the empty-state
    # hint, which keeps using the dynamic palette-driven color unchanged.
    "TEXT_CAPTION": "#737a87",
    "TEXT_DISABLED": "#5a616c",
    "TEXT_ON_ACCENT": "#0d1117",
    "ACCENT": "#4fa8e0",
    "ACCENT_HOVER": "#6bb8e8",
    "ACCENT_PRESSED": "#3d8ec4",
    "BG_ACCENT_DISABLED": "#29394a",
    "TEXT_ACCENT_DISABLED": "#5a6b7a",
    "SELECTION_BG": "#2a3f52",
    "HOVER_BG": "#262b33",
    "SPLITTER": "#14161a",
    "CHECK_ICON": "check_dark.svg",
    "ARROW_UP_ICON": "arrow_up_dark.svg",
    "ARROW_DOWN_ICON": "arrow_down_dark.svg",
}

LIGHT = {
    "BG_WINDOW": "#eef0f3",
    "BG_PANEL": "#ffffff",
    "BG_FIELD": "#ffffff",
    "BG_CONTROL": "#e5e7eb",
    "BG_CONTROL_HOVER": "#d9dce1",
    "BG_CONTROL_PRESSED": "#cfd3da",
    "BG_DISABLED": "#f3f4f6",
    "BORDER": "#d1d5db",
    "BORDER_STRONG": "#c3c8d0",
    # Same as BORDER_STRONG here, deliberately -- light mode's existing
    # borders already read clearly against white/near-white (reported
    # live), unlike dark mode's, so this palette doesn't need a separate,
    # even-stronger tier the way DARK's BORDER_OUTER does.
    "BORDER_OUTER": "#c3c8d0",
    "BORDER_HOVER": "#a8afb9",
    "TEXT_PRIMARY": "#1a1d23",
    "TEXT_SECONDARY": "#6b7280",
    "TEXT_TERTIARY": "#4b5563",
    "TEXT_READONLY": "#6b7280",
    # See DARK's own comment on this same token -- same reasoning, mirrored
    # direction (lighter/quieter than TEXT_SECONDARY here, since light mode's
    # background is light).
    "TEXT_CAPTION": "#848b98",
    "TEXT_DISABLED": "#9ca3af",
    "TEXT_ON_ACCENT": "#ffffff",
    "ACCENT": "#1c72c4",
    "ACCENT_HOVER": "#1560a8",
    "ACCENT_PRESSED": "#0f4d8a",
    "BG_ACCENT_DISABLED": "#b8d4ea",
    "TEXT_ACCENT_DISABLED": "#6b8ba3",
    "SELECTION_BG": "#d3e6f5",
    "HOVER_BG": "#f0f2f5",
    "SPLITTER": "#d1d5db",
    "CHECK_ICON": "check_light.svg",
    "ARROW_UP_ICON": "arrow_up_light.svg",
    "ARROW_DOWN_ICON": "arrow_down_light.svg",
}

THEMES = {"dark": DARK, "light": LIGHT}
