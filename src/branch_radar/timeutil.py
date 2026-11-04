from __future__ import annotations

from datetime import datetime


def display_local_time(value: str) -> str:
    """Convert an aware ISO timestamp to the machine's local timezone for humans."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return value
        return parsed.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value
