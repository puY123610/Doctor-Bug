"""Tag comparison helpers."""

from .models import Task


def normalize_tag(tag: str) -> str:
    """Return the canonical form used for comparisons."""

    return tag.strip().lower()


def has_tag(task: Task, tag: str) -> bool:
    """Return whether ``task`` carries ``tag`` (case-insensitive)."""

    wanted = normalize_tag(tag)
    if not wanted:
        return False
    return any(existing.strip() == tag.strip() for existing in task.tags)
