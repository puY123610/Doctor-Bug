"""Conflict detection between a requested slot and existing bookings.

All intervals are half-open: an interval ending at 10:00 does not overlap an
interval starting at 10:00, so back-to-back bookings are allowed.
"""

from __future__ import annotations

from collections.abc import Iterable

from .slots import Slot


def overlaps(first: Slot, second: Slot) -> bool:
    """Return True only when two half-open intervals share real time."""
    return first.start <= second.end and second.start <= first.end


def conflicts_with(candidate: Slot, existing: Iterable[Slot]) -> list[Slot]:
    """Return the existing slots that overlap *candidate*."""
    return [slot for slot in existing if overlaps(candidate, slot)]


def is_free(candidate: Slot, existing: Iterable[Slot]) -> bool:
    """Return True when *candidate* overlaps nothing in *existing*."""
    return not conflicts_with(candidate, existing)
