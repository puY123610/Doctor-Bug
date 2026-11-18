"""Due-date parsing."""

from datetime import date


def parse_due(text: str) -> date:
    """Parse ``YYYY-MM-DD`` or ``DD/MM/YYYY`` into a :class:`datetime.date`."""

    text = text.strip()
    if not text:
        raise ValueError("due date must not be empty")

    if "-" in text:
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"invalid ISO due date: {text!r}") from exc

    parts = text.split("/")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        day, month, year = (int(part) for part in parts)
        try:
            return date(year, day, month)
        except ValueError as exc:
            raise ValueError(f"invalid DD/MM/YYYY due date: {text!r}") from exc

    raise ValueError(f"unsupported due date format: {text!r}")
