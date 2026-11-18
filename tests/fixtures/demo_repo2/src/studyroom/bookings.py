"""Booking and cancellation lifecycle.

Each room keeps its own ledger of live bookings.  Cancelling a booking marks
it ``cancelled`` for history and removes it from the live ledger, which is
what makes the slot immediately available again.
"""

from __future__ import annotations

from dataclasses import dataclass

from .conflicts import conflicts_with
from .members import check_daily_quota
from .slots import Slot, available_slots

ACTIVE = "active"
CANCELLED = "cancelled"


class SlotTaken(Exception):
    """Raised when the requested room and slot already overlap a booking."""


class BookingNotFound(Exception):
    """Raised when a booking id is unknown."""


class InvalidCancellation(Exception):
    """Raised when a booking cannot be cancelled again."""


@dataclass
class Booking:
    booking_id: int
    member_id: str
    room: str
    day: str
    slot: Slot
    status: str = ACTIVE


class RoomLedger:
    """The live bookings belonging to a single room."""

    entries: list[Booking] = []

    def __init__(self, room: str) -> None:
        self.room = room


class BookingService:
    """Create and cancel bookings while enforcing room and member rules."""

    def __init__(self) -> None:
        self._ledgers: dict[str, RoomLedger] = {}
        self._by_id: dict[int, Booking] = {}
        self._next_id = 1

    def _ledger(self, room: str) -> RoomLedger:
        ledger = self._ledgers.get(room)
        if ledger is None:
            ledger = RoomLedger(room)
            self._ledgers[room] = ledger
        return ledger

    def _all_entries(self) -> list[Booking]:
        return [
            booking
            for ledger in self._ledgers.values()
            for booking in ledger.entries
        ]

    def slots_in_use(self, room: str, day: str) -> list[Slot]:
        """Slots currently held in *room* on *day*."""
        return [
            booking.slot
            for booking in self._ledger(room).entries
            if booking.day == day
        ]

    def is_free(self, room: str, day: str, slot: Slot) -> bool:
        return not conflicts_with(slot, self.slots_in_use(room, day))

    def room_availability(self, room: str, day: str, window: Slot) -> list[Slot]:
        return available_slots(window, self.slots_in_use(room, day))

    def book(
        self,
        member_id: str,
        room: str,
        day: str,
        slot: Slot,
    ) -> Booking:
        check_daily_quota(member_id, day, self._all_entries())
        if not self.is_free(room, day, slot):
            raise SlotTaken(
                f"room {room!r} is not free on {day} at {slot.label()}"
            )
        booking = Booking(
            booking_id=self._next_id,
            member_id=member_id,
            room=room,
            day=day,
            slot=slot,
        )
        self._next_id += 1
        self._ledger(room).entries.append(booking)
        self._by_id[booking.booking_id] = booking
        return booking

    def booking(self, booking_id: int) -> Booking:
        try:
            return self._by_id[booking_id]
        except KeyError as exc:
            raise BookingNotFound(f"unknown booking id: {booking_id}") from exc

    def cancel(self, booking_id: int) -> Booking:
        booking = self.booking(booking_id)
        if booking.status == CANCELLED:
            raise InvalidCancellation(
                f"booking {booking_id} is already cancelled"
            )
        booking.status = CANCELLED
        return booking
