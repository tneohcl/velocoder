"""VeloCoder's colour tokens now come from odcs-ui (_vendor/odcs_ui/tokens.json),
the shared ODCS App Studio palette; the hand-tuning history behind each value
lives there. Only VeloCoder's own icon tokens are added here.
"""
from velocoder.ui._vendor.odcs_ui import tokens as _odcs_tokens

DARK = {
    **_odcs_tokens.DARK,
    "CHECK_ICON": "check_dark.svg",
    "ARROW_UP_ICON": "arrow_up_dark.svg",
    "ARROW_DOWN_ICON": "arrow_down_dark.svg",
}

LIGHT = {
    **_odcs_tokens.LIGHT,
    "CHECK_ICON": "check_light.svg",
    "ARROW_UP_ICON": "arrow_up_light.svg",
    "ARROW_DOWN_ICON": "arrow_down_light.svg",
}

THEMES = {"dark": DARK, "light": LIGHT}
