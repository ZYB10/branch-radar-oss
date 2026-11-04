from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_RELATIVE_BEFORE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[dhw])$")
_UNIT_SECONDS = {
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def parse_retention_cutoff(
    value: str,
    *,
    now: datetime | None = None,
) -> str:
    """Parse a relative duration or ISO timestamp into a UTC cutoff.

    Relative values intentionally use a small, unambiguous grammar: ``24h``,
    ``90d`` and ``12w``.  Naive ISO timestamps are interpreted as UTC so the
    command behaves identically in foreground and service processes.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("保留期限不能为空")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)

    candidate = value.strip()
    relative = _RELATIVE_BEFORE.fullmatch(candidate.casefold())
    if relative:
        try:
            seconds = (
                int(relative.group("amount"))
                * _UNIT_SECONDS[relative.group("unit")]
            )
            cutoff = current - timedelta(seconds=seconds)
        except OverflowError as exc:
            raise ValueError("保留期限超出支持范围") from exc
    else:
        normalized = candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z")) else candidate
        try:
            cutoff = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("保留期限需为 90d、12w、24h 或 ISO 日期时间") from exc
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        else:
            cutoff = cutoff.astimezone(timezone.utc)

    if cutoff >= current:
        raise ValueError("保留期限必须早于当前时间")
    return cutoff.isoformat(timespec="seconds")
