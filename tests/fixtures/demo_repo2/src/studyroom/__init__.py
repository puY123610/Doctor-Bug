"""Study-room booking domain."""

from .bookings import (
    ACTIVE,
    CANCELLED,
    Booking,
    BookingNotFound,
    BookingService,
    InvalidCancellation,
    SlotTaken,
)
from .conflicts import conflicts_with, is_free, overlaps
from .members import DAILY_BOOKING_LIMIT, QuotaExceeded, bookings_used, check_daily_quota
from .slots import Slot, available_slots, format_time, parse_time

__all__ = [
    "ACTIVE",
    "CANCELLED",
    "Booking",
    "BookingNotFound",
    "BookingService",
    "DAILY_BOOKING_LIMIT",
    "InvalidCancellation",
    "QuotaExceeded",
    "Slot",
    "SlotTaken",
    "available_slots",
    "bookings_used",
    "check_daily_quota",
    "conflicts_with",
    "format_time",
    "is_free",
    "overlaps",
    "parse_time",
]
