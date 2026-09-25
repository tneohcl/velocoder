"""Times shown the ODCS way: the desktop locale's own clock format, with
Today / Yesterday / Tomorrow / weekday / date wording.

    friendly_clock(4, 0)            -> "4:00 AM" (en_US) or "04:00" (24-hour locales)
    friendly_datetime(datetime(...)) -> "Today at 8:51 AM", "Monday at 3:22 PM", "Sep 1 at 6:25 AM"

Every time an ODCS app displays goes through here, so two parts of one
screen can't disagree about the format (Keep once showed "Daily at 04:00"
beside "Tomorrow at 4:00 AM").
"""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QLocale, QTime


def friendly_clock(hour: int, minute: int, locale: QLocale | None = None) -> str:
    return (locale or QLocale.system()).toString(QTime(hour, minute), QLocale.FormatType.ShortFormat)


def friendly_datetime(dt: datetime, now: datetime | None = None, locale: QLocale | None = None) -> str:
    now = now or (datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now())
    time_str = friendly_clock(dt.hour, dt.minute, locale)
    today, day = now.date(), dt.date()
    if day == today:
        return f"Today at {time_str}"
    if day == today - timedelta(days=1):
        return f"Yesterday at {time_str}"
    if day == today + timedelta(days=1):
        return f"Tomorrow at {time_str}"
    if abs((day - today).days) < 7:
        return f"{dt.strftime('%A')} at {time_str}"
    if day.year == today.year:
        return f"{dt.strftime('%b')} {dt.day} at {time_str}"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year} at {time_str}"
