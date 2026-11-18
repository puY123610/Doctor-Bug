"""Time parsing, slot validity and availability gaps."""

import pytest

from studyroom.slots import Slot, available_slots, format_time, parse_time


def test_parse_and_format_time():
    assert parse_time("09:30") == 570
    assert format_time(570) == "09:30"
    assert Slot.parse("10:00", "11:30").label() == "10:00-11:30"


@pytest.mark.parametrize(
    "start,end",
    [
        (660, 660),
        (700, 600),
    ],
)
def test_slot_rejects_empty_or_reversed_interval(start, end):
    with pytest.raises(ValueError):
        Slot(start, end)


def test_availability_subtracts_booked_interval():
    window = Slot.parse("09:00", "12:00")

    gaps = available_slots(window, [Slot.parse("10:00", "11:00")])

    assert [gap.label() for gap in gaps] == [
        "09:00-10:00",
        "11:00-12:00",
    ]


def test_availability_merges_touching_and_overlapping_busy_slots():
    window = Slot.parse("09:00", "12:00")
    busy = [
        Slot.parse("10:00", "11:00"),
        Slot.parse("11:00", "11:30"),
        Slot.parse("10:30", "10:45"),
    ]

    gaps = available_slots(window, busy)

    assert [gap.label() for gap in gaps] == [
        "09:00-10:00",
        "11:30-12:00",
    ]
