"""Sorting and filtering rules."""

from todolib.models import Task
from todolib.queries import list_tasks, sort_tasks


def _task(task_id, priority, created, tags=(), done=False):
    return Task(
        id=task_id,
        title=task_id.upper(),
        priority=priority,
        created=created,
        tags=tags,
        done=done,
    )


def test_sort_by_priority_then_creation():
    tasks = [
        _task("b", priority=2, created=20),
        _task("a", priority=1, created=30),
        _task("c", priority=2, created=10),
        _task("d", priority=1, created=10),
        _task("e", priority=3, created=5),
    ]

    assert [task.id for task in sort_tasks(tasks)] == [
        "d", "a", "c", "b", "e",
    ]


def test_sort_does_not_mutate_input():
    tasks = [
        _task("b", priority=2, created=20),
        _task("a", priority=1, created=30),
    ]
    original = list(tasks)

    result = sort_tasks(tasks)

    assert tasks == original
    assert result is not tasks


def test_list_hides_done_by_default():
    tasks = [
        _task("done", priority=1, created=1, done=True),
        _task("open", priority=2, created=2),
    ]

    assert [task.id for task in list_tasks(tasks)] == ["open"]


def test_list_can_include_done():
    tasks = [
        _task("done", priority=2, created=2, done=True),
        _task("open", priority=1, created=1),
    ]

    assert [task.id for task in list_tasks(tasks, include_done=True)] == [
        "open", "done",
    ]


def test_list_filters_tag_case_insensitively():
    tasks = [
        _task("other", priority=1, created=1, tags=["Home"]),
        _task("work", priority=2, created=2, tags=["Work"]),
    ]

    assert [task.id for task in list_tasks(tasks, tag="work")] == ["work"]


def test_list_tag_filter_keeps_stable_order():
    tasks = [
        _task("late", priority=2, created=20, tags=["Work"]),
        _task("early", priority=2, created=10, tags=["work"]),
        _task("home", priority=2, created=5, tags=["Home"]),
        _task("first", priority=1, created=30, tags=["WORK"]),
    ]

    assert [task.id for task in list_tasks(tasks, tag="work")] == [
        "first", "early", "late",
    ]
