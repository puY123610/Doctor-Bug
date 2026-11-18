"""Half-open overlap rules between requested and existing slots."""

from studyroom.conflicts import conflicts_with, is_free, overlaps
from studyroom.slots import Slot


def test_back_to_back_slots_do_not_conflict():
    first = Slot.parse("10:00", "11:00")
    second = Slot.parse("11:00", "12:00")

    assert not overlaps(first, second)
    assert conflicts_with(second, [first]) == []
    assert is_free(second, [first])


def test_partial_overlap_conflicts():
    existing = Slot.parse("10:00", "11:00")
    candidate = Slot.parse("10:30", "11:30")

    assert overlaps(existing, candidate)
    assert conflicts_with(candidate, [existing]) == [existing]
    assert not is_free(candidate, [existing])


def test_contained_slot_conflicts():
    existing = Slot.parse("10:00", "12:00")
    candidate = Slot.parse("10:30", "11:00")

    assert conflicts_with(candidate, [existing]) == [existing]


def test_disjoint_slots_are_free():
    existing = [Slot.parse("09:00", "10:00")]
    candidate = Slot.parse("11:00", "12:00")

    assert conflicts_with(candidate, existing) == []
    assert is_free(candidate, existing)
