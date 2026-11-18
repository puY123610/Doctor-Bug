"""Sorting and filtering."""

from __future__ import annotations

from typing import Iterable, List, Optional

from .models import Task
from .tags import has_tag


def sort_tasks(tasks: Iterable[Task]) -> List[Task]:
    """Sort by priority, then by creation sequence; never mutate the input."""

    return sorted(tasks, key=lambda task: task.priority)


def list_tasks(
    tasks: Iterable[Task],
    *,
    tag: Optional[str] = None,
    include_done: bool = False,
) -> List[Task]:
    """Filter by done state and tag, then return the sorted result."""

    selected = [task for task in tasks if include_done or not task.done]
    if tag is not None:
        selected = [task for task in selected if has_tag(task, tag)]
    return sort_tasks(selected)
