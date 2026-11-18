"""Formatting seconds as compact duration strings."""


def format_duration(total_seconds: int) -> str:
    """Render a number of seconds using ``h``, ``m`` and ``s`` components."""

    if total_seconds < 0:
        raise ValueError("total_seconds must not be negative")

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return "".join(parts)
