"""WCAG AA floor for the shared color tokens (2026-09-25 UI audit).

themes.py is shared with Keep; every readable TEXT_* token must reach 4.5:1
on the surfaces text actually sits on. TEXT_DISABLED is WCAG-exempt.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import themes  # noqa: E402


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


class ThemeContrastTests(unittest.TestCase):
    def test_text_tokens_meet_wcag_aa(self):
        for name, theme in themes.THEMES.items():
            for fg in ("TEXT_PRIMARY", "TEXT_SECONDARY", "TEXT_TERTIARY", "TEXT_READONLY", "TEXT_CAPTION"):
                for bg in ("BG_WINDOW", "BG_PANEL", "BG_FIELD"):
                    with self.subTest(theme=name, fg=fg, bg=bg):
                        self.assertGreaterEqual(_ratio(theme[fg], theme[bg]), 4.5)

    def test_caption_stays_quieter_than_secondary(self):
        for name, theme in themes.THEMES.items():
            with self.subTest(theme=name):
                bg = _luminance(theme["BG_WINDOW"])
                caption_gap = abs(_luminance(theme["TEXT_CAPTION"]) - bg)
                secondary_gap = abs(_luminance(theme["TEXT_SECONDARY"]) - bg)
                self.assertLess(caption_gap, secondary_gap)


if __name__ == "__main__":
    unittest.main()
