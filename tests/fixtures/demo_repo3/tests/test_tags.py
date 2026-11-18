"""Tag comparison rules."""

from todolib.models import Task
from todolib.tags import has_tag


def _task(tags):
    return Task(id="t1", title="Write report", priority=2, created=1, tags=tags)


def test_has_tag_is_case_insensitive():
    task = _task(["Work", "Urgent"])

    assert has_tag(task, "work")
    assert has_tag(task, "WORK")


def test_has_tag_ignores_surrounding_space():
    assert has_tag(_task(["Work"]), "  Work  ")


def test_has_tag_rejects_missing_tag():
    assert not has_tag(_task(["Work"]), "home")
