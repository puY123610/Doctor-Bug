"""durlib — a tiny duration parsing, conversion and formatting library."""

from .convert import convert, from_seconds, to_seconds
from .format import format_duration
from .parse import parse_duration

__all__ = [
    "parse_duration",
    "convert",
    "to_seconds",
    "from_seconds",
    "format_duration",
]
