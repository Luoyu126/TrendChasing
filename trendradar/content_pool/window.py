"""Calendar-day boundaries in the reader's timezone, including DST days."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def previous_day(stamp, zone):
    current = datetime.fromisoformat(stamp) if isinstance(stamp, str) else stamp
    if current.tzinfo is None:
        raise ValueError("timezone_aware_clock_required")
    tz = ZoneInfo(zone)
    end = datetime.combine(current.astimezone(tz).date(), time.min, tz)
    start = datetime.combine(end.date() - timedelta(days=1), time.min, tz)
    return {
        "timezone": zone,
        "date": start.date().isoformat(),
        "start": start.astimezone(UTC).isoformat(),
        "end": end.astimezone(UTC).isoformat(),
    }


def position(stamp, window):
    if not stamp:
        return "unknown"
    try:
        value = datetime.fromisoformat(stamp)
        if value.tzinfo is None:
            return "unknown"
        value = value.astimezone(UTC)
    except (TypeError, ValueError):
        return "unknown"
    if value >= datetime.fromisoformat(window["end"]):
        return "future"
    return (
        "supplement" if value < datetime.fromisoformat(window["start"]) else "primary"
    )
