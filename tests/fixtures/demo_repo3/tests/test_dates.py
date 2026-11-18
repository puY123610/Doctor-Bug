"""Due-date parsing rules."""

from datetime import date

import pytest

from todolib.dates import parse_due


def test_parse_iso_due():
    assert parse_due("2026-12-31") == date(2026, 12, 31)


def test_parse_day_first_due():
    assert parse_due("03/04/2026") == date(2026, 4, 3)


def test_parse_day_first_due_with_late_day():
    assert parse_due("25/12/2026") == date(2026, 12, 25)


def test_parse_due_rejects_unknown_format():
    with pytest.raises(ValueError):
        parse_due("2026.01.09")
