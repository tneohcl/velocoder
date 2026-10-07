"""Color math for ODCS: WCAG luminance/contrast, compositing, accent pairing.

Qt-free; colors are "#rrggbb" or "#rrggbbaa" strings.
"""
from __future__ import annotations

DARK_ON_ACCENT = "#0d1117"
LIGHT_ON_ACCENT = "#ffffff"


def parse(color: str) -> tuple[float, float, float, float]:
    """"#rgb", "#rrggbb" or "#rrggbbaa" -> (r, g, b, a), each 0..1."""
    h = color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) not in (6, 8):
        raise ValueError(f"not a hex color: {color!r}")
    values = [int(h[i:i + 2], 16) / 255 for i in range(0, len(h), 2)]
    if len(values) == 3:
        values.append(1.0)
    return tuple(values)  # type: ignore[return-value]


def to_hex(r: float, g: float, b: float) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in (r, g, b))


def luminance(color: str) -> float:
    """WCAG 2.x relative luminance of an opaque color."""
    r, g, b, _ = parse(color)
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two opaque colors (1.0 .. 21.0)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def composite(color: str, background: str) -> str:
    """What a translucent color actually looks like painted over background."""
    r, g, b, a = parse(color)
    br, bg, bb, _ = parse(background)
    return to_hex(a * r + (1 - a) * br, a * g + (1 - a) * bg, a * b + (1 - a) * bb)


def on_accent(accent: str) -> str:
    """Text/icon color for content drawn on an accent fill: whichever of
    near-black and white contrasts more with it (the accent is the user's
    system choice, so it can be any hue or lightness)."""
    if contrast(accent, LIGHT_ON_ACCENT) >= contrast(accent, DARK_ON_ACCENT):
        return LIGHT_ON_ACCENT
    return DARK_ON_ACCENT


def shade(color: str, amount: float) -> str:
    """Lighten (amount > 0) toward white or darken (amount < 0) toward black."""
    r, g, b, _ = parse(color)
    if amount >= 0:
        return to_hex(*(c + (1 - c) * amount for c in (r, g, b)))
    return to_hex(*(c * (1 + amount) for c in (r, g, b)))


def readable(color: str, background: str, fallback: str, minimum: float = 4.5) -> str:
    """color if it reaches `minimum` contrast on background (after compositing
    any transparency), else fallback. For system roles such as
    PlaceholderText that can be too faint to read."""
    painted = composite(color, background) if len(color.lstrip("#")) == 8 else color
    return color if contrast(painted, background) >= minimum else fallback
