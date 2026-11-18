"""Task data model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Task:
    """A single task-list entry.

    ``priority`` 1 is the highest priority.  ``created`` is a monotonically
    increasing creation sequence: smaller means created earlier.
    """

    id: str
    title: str
    priority: int
    created: int
    due: Optional[str] = None
    tags: Tuple[str, ...] = ()
    done: bool = False

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if self.priority < 1:
            raise ValueError("priority must be a positive integer")
        if self.created < 0:
            raise ValueError("created must be a non-negative integer")
        object.__setattr__(self, "tags", tuple(self.tags))
