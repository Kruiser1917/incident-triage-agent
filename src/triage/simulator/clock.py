"""UTC time helpers on integer epoch seconds and milliseconds (no floats, no local time)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

MINUTE = 60
HOUR = 3600
DAY = 86400

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def epoch_s(dt: datetime) -> int:
    return (dt - _EPOCH) // timedelta(seconds=1)


def from_epoch_s(ts: int) -> datetime:
    return _EPOCH + timedelta(seconds=ts)


def iso_s(ts: int) -> str:
    return from_epoch_s(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(ts_ms: int) -> str:
    dt = _EPOCH + timedelta(milliseconds=ts_ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"


def relative(ts: int, anchor: int) -> str:
    """Offset from the alert in the incident-review style: T-5h12m, T+3m, T-45s."""
    delta = ts - anchor
    sign = "-" if delta < 0 else "+"
    delta = abs(delta)
    if delta < MINUTE:
        return f"T{sign}{delta}s"
    hours, minutes = divmod(delta // MINUTE, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"T{sign}{days}d{hours}h"
    return f"T{sign}{hours}h{minutes:02d}m" if hours else f"T{sign}{minutes}m"
