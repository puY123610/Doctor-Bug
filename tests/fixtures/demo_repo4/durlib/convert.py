"""Conversions between hours, minutes and seconds."""

UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def _factor(unit: str) -> int:
    try:
        return UNIT_SECONDS[unit]
    except KeyError as exc:
        raise ValueError(f"unknown unit: {unit!r}") from exc


def to_seconds(hours: float = 0, minutes: float = 0, seconds: float = 0) -> float:
    """Combine hour/minute/second components into seconds."""

    return hours * 3600 + minutes * 60 + seconds


def from_seconds(total_seconds: float):
    """Split a non-negative number of seconds into (hours, minutes, seconds)."""

    if total_seconds < 0:
        raise ValueError("total_seconds must not be negative")
    total = int(total_seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return hours, minutes, seconds


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value`` between ``h``, ``m`` and ``s`` (base 60)."""

    return value * _factor(from_unit) / _factor(to_unit)
