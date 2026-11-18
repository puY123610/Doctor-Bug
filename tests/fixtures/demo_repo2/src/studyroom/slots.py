"""Time slots and availability arithmetic.

A slot is a half-open interval ``[start, end)`` measured in minutes since
midnight.  Availability is computed by subtracting busy intervals from a
larger window; touching busy intervals are merged before the gaps are read.
"""

from __future__ import annotations

from dataclasses import dataclass

OPENING = 9 * 60
CLOSING = 21 * 60

_MINUTES_PER_DAY = 24 * 60


def parse_time(value: str) -> int:
    """Convert ````HH:MM```` to minutes since midnight."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hours, minutes = (int(part) for part in parts)
    if not (0 <= hours < 24) or not (0 <= minutes < 60):
        raise ValueError(f"time out of range: {value!r}")
    return hours * 60 + minutes


def format_time(minute: int) -> str:
    """Convert minutes since midnight to ``HH:MM``."""
    if not 0 <= minute < _MINUTES_PER_DAY:
        raise ValueError(f"minute out of range: {minute!r}")
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True, order=True)
class Slot:
    """A half-open interval ``[start, end)`` in minutes since midnight."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(
                f"slot start must be before end: {self.start} >= {self.end}"
            )

    @classmethod
    def parse(cls, start: str, end: str) -> "Slot":
        return cls(parse_time(start), parse_time(end))

    def duration(self) -> int:
        return self.end - self.start

    def label(self) -> str:
        return f"{format_time(self.start)}-{format_time(self.end)}"


def available_slots(window: Slot, busy: list[Slot]) -> list[Slot]:
    """Return the parts of *window* not covered by any slot in *busy*.

    Busy slots may be unsorted, overlapping or outside the window; they are
    clipped to the window and merged before the free gaps are emitted.
    """
    clipped: list[Slot] = []
    for item in busy:
        start = max(window.start, item.start)
        end = min(window.end, item.end)
        if start < end:
            clipped.append(Slot(start, end))
    clipped.sort()

    gaps: list[Slot] = []
    cursor = window.start
    for item in clipped:
        if item.start > cursor:
            gaps.append(Slot(cursor, item.start))
        cursor = max(cursor, item.end)
    if cursor < window.end:
        gaps.append(Slot(cursor, window.end))
    return gaps
