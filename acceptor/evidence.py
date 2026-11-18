"""Failure evidence mapped back to the module under test."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass
class Evidence:
    node_id: str
    module: str


def module_from_node_id(node_id: str) -> str:
    """Derive ``billing`` from ``tests/test_billing.py::test_x``."""
    file_part = node_id.split("::", 1)[0]
    stem = PurePosixPath(file_part).stem
    if stem.startswith("test_"):
        stem = stem[len("test_") :]
    for suffix in (".test", ".spec"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


def evidence_from_failed(node_ids: list[str]) -> list[Evidence]:
    return [
        Evidence(node_id=node_id, module=module_from_node_id(node_id))
        for node_id in node_ids
    ]
