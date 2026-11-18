"""Member daily booking quota."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

DAILY_BOOKING_LIMIT = 3


class BookingRecord(Protocol):
    """The two fields needed to count a booking toward a daily quota."""

    member_id: str
    day: str


class QuotaExceeded(Exception):
    """Raised when a member already used their daily booking allowance."""


def bookings_used(
    member_id: str,
    day: str,
    bookings: Iterable[BookingRecord],
) -> int:
    """Count one member's live bookings on one calendar day."""
    return sum(
        1
        for booking in bookings
        if booking.member_id == member_id and booking.day == day
    )


def check_daily_quota(
    member_id: str,
    day: str,
    bookings: Iterable[BookingRecord],
    limit: int = DAILY_BOOKING_LIMIT,
) -> None:
    """Raise :class:`QuotaExceeded` when the member's day is already full."""
    used = bookings_used(member_id, day, bookings)
    if used >= limit:
        raise QuotaExceeded(
            f"member {member_id!r} already has {used} bookings on {day}; "
            f"limit is {limit}"
        )
