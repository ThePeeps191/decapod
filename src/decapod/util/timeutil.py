"""Time helpers. All timestamps inside Decapod are timezone-aware UTC."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def now() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def from_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Parse ``"90s"``, ``"5m"``, ``"1h30m"``, ``"2d"`` or a number of seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError(f"duration must be non-negative: {value!r}")
        return timedelta(seconds=float(value))
    text = value.strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if seconds < 0:
            raise ValueError(f"duration must be non-negative: {value!r}")
        return timedelta(seconds=seconds)
    pos = 0
    total = 0.0
    for match in _DURATION_RE.finditer(text):
        if match.start() != pos:
            raise ValueError(f"invalid duration: {value!r}")
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        pos = match.end()
    if pos != len(text):
        raise ValueError(f"invalid duration: {value!r}")
    return timedelta(seconds=total)


def format_duration(delta: timedelta | float) -> str:
    """Human-friendly compact duration, e.g. ``1h 05m`` or ``42s``."""
    seconds = delta.total_seconds() if isinstance(delta, timedelta) else float(delta)
    negative = seconds < 0
    whole = abs(round(seconds))
    days, rem = divmod(whole, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        out = f"{days}d {hours:02d}h"
    elif hours:
        out = f"{hours}h {minutes:02d}m"
    elif minutes:
        out = f"{minutes}m {secs:02d}s"
    else:
        out = f"{secs}s"
    return f"-{out}" if negative else out
