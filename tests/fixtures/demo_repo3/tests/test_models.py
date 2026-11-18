"""Task model rules."""

import pytest

from todolib.models import Task


def test_task_requires_positive_priority():
    with pytest.raises(ValueError):
        Task(id="t1", title="Write report", priority=0, created=1)


def test_task_preserves_tag_display_case():
    task = Task(id="t1", title="Write report", priority=2, created=1,
                tags=["Work", "Urgent"])

    assert task.tags == ("Work", "Urgent")
