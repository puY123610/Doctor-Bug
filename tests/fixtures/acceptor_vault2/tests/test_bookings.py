"""Booking, room isolation and cancellation lifecycle."""

import pytest

from studyroom.bookings import BookingService, InvalidCancellation
from studyroom.slots import Slot


def test_book_and_cancel_releases_slot():
    service = BookingService()
    day = "2026-12-01"
    slot = Slot.parse("10:00", "11:00")

    booking = service.book("m1", "A", day, slot)
    assert not service.is_free("A", day, slot)

    service.cancel(booking.booking_id)

    assert service.is_free("A", day, slot)
    assert service.book("m2", "A", day, slot).status == "active"


def test_rooms_do_not_share_availability():
    service = BookingService()
    day = "2026-12-02"
    slot = Slot.parse("10:00", "11:00")

    service.book("m1", "A", day, slot)
    other = service.book("m2", "B", day, slot)

    assert other.room == "B"
    assert [gap.label() for gap in service.room_availability(
        "B", day, Slot.parse("09:00", "12:00")
    )] == ["09:00-10:00", "11:00-12:00"]


def test_back_to_back_booking_in_same_room_is_allowed():
    service = BookingService()
    day = "2026-12-03"

    service.book("m1", "A", day, Slot.parse("10:00", "11:00"))
    second = service.book("m1", "A", day, Slot.parse("11:00", "12:00"))

    assert second.slot.label() == "11:00-12:00"


def test_cancelled_booking_no_longer_blocks_availability():
    service = BookingService()
    day = "2026-12-04"
    booking = service.book("m1", "A", day, Slot.parse("10:00", "11:00"))

    service.cancel(booking.booking_id)

    gaps = service.room_availability(
        "A", day, Slot.parse("09:00", "12:00")
    )
    assert [gap.label() for gap in gaps] == ["09:00-12:00"]


def test_cancel_keeps_history_and_rejects_double_cancel():
    service = BookingService()
    booking = service.book(
        "m1", "A", "2026-12-05", Slot.parse("10:00", "11:00")
    )

    service.cancel(booking.booking_id)

    assert service.booking(booking.booking_id).status == "cancelled"
    with pytest.raises(InvalidCancellation):
        service.cancel(booking.booking_id)
