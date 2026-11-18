"""Member daily quota rules."""

import pytest

from studyroom.bookings import BookingService
from studyroom.members import DAILY_BOOKING_LIMIT, QuotaExceeded
from studyroom.slots import Slot


def test_fourth_booking_on_same_day_is_rejected():
    service = BookingService()
    day = "2026-12-06"

    for start, end in (
        ("09:00", "10:00"),
        ("11:00", "12:00"),
        ("13:00", "14:00"),
    ):
        service.book("m1", "A", day, Slot.parse(start, end))

    with pytest.raises(QuotaExceeded):
        service.book("m1", "A", day, Slot.parse("15:00", "16:00"))

    assert DAILY_BOOKING_LIMIT == 3


def test_quota_is_per_calendar_day():
    service = BookingService()
    first_day = "2026-12-07"
    second_day = "2026-12-08"

    for start, end in (
        ("09:00", "10:00"),
        ("11:00", "12:00"),
        ("13:00", "14:00"),
    ):
        service.book("m1", "A", first_day, Slot.parse(start, end))

    booking = service.book(
        "m1", "A", second_day, Slot.parse("15:00", "16:00")
    )
    assert booking.day == second_day


def test_quota_is_per_member():
    service = BookingService()
    day = "2026-12-09"

    for start, end in (
        ("09:00", "10:00"),
        ("11:00", "12:00"),
        ("13:00", "14:00"),
    ):
        service.book("m1", "A", day, Slot.parse(start, end))

    booking = service.book("m2", "A", day, Slot.parse("15:00", "16:00"))
    assert booking.member_id == "m2"
