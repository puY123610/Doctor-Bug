"""Parsing duration strings such as ``1h30m``."""

import re

from .convert import UNIT_SECONDS

_TOKEN = re.compile(r"(\d+)([hms])")


def parse_duration(text: str) -> int:
    """Parse a duration string into a whole number of seconds."""

    text = text.strip()
    if not text:
        raise ValueError("duration must not be empty")

    total = 0
    position = 0
    seen = set()
    for match in _TOKEN.finditer(text):
        if match.start() != position:
            raise ValueError(f"invalid duration: {text!r}")
        amount = int(match.group(1))
        unit = match.group(2)
        if unit in seen:
            raise ValueError(f"duplicate unit {unit!r} in {text!r}")
        seen.add(unit)
        total += amount * UNIT_SECONDS[unit]
        position = match.end()

    if position != len(text):
        raise ValueError(f"invalid duration: {text!r}")
    return total
