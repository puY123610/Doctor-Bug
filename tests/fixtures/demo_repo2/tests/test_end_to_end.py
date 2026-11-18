"""Cross-module booking journey: two rooms, back-to-back slots, cancel."""

from studyroom.bookings import BookingService
from studyroom.slots import Slot


def test_cross_room_back_to_back_then_cancel_releases():
    service = BookingService()
    day = "2026-12-10"

    first = service.book("m1", "A", day, Slot.parse("10:00", "11:00"))
    other = service.book("m2", "B", day, Slot.parse("10:00", "11:00"))
    follow = service.book("m1", "A", day, Slot.parse("11:00", "12:00"))

    service.cancel(first.booking_id)

    assert service.is_free("A", day, Slot.parse("10:00", "11:00"))
    rebooked = service.book("m3", "A", day, Slot.parse("10:00", "11:00"))

    assert other.room == "B"
    assert follow.slot.label() == "11:00-12:00"
    assert rebooked.status == "active"
    assert not service.is_free("A", day, Slot.parse("11:00", "12:00"))
