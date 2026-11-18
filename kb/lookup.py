"""Data-driven dispatch lookup: failure evidence -> specialist plan.

The knowledge lives in ``dispatch_table.json``.  This module only implements
the lookup order (node override -> module default -> fallback); it contains no
test-to-specialist mapping of its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from acceptor.evidence import Evidence


@dataclass
class DispatchPlan:
    """Result of one lookup: who owns which node, and what hit only fallback."""

    assignments: dict[str, list[str]] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)


class DispatchKB:
    """Lookup table for assigning failed evidence nodes to specialists."""

    def __init__(self, table: dict, enabled: bool = True):
        self.table = table
        self.enabled = enabled

    @classmethod
    def from_file(cls, path: str | Path, enabled: bool = True) -> "DispatchKB":
        with open(path, encoding="utf-8") as handle:
            return cls(json.load(handle), enabled=enabled)

    def lookup(self, evidence: Iterable[Evidence]) -> DispatchPlan:
        plan = DispatchPlan()
        fallback = self.table["fallback"]
        overrides = self.table.get("node_overrides", {})
        module_defaults = self.table.get("module_defaults", {})

        for item in evidence:
            if not self.enabled:
                self._assign(plan.assignments, fallback, item.node_id)
                self._remember(plan.unmapped, item.node_id)
                continue

            override = overrides.get(item.node_id)
            if override is not None:
                for specialist_id in override["specialists"]:
                    self._assign(plan.assignments, specialist_id, item.node_id)
                continue

            specialists = module_defaults.get(item.module)
            if specialists is not None:
                for specialist_id in specialists:
                    self._assign(plan.assignments, specialist_id, item.node_id)
                continue

            self._assign(plan.assignments, fallback, item.node_id)
            self._remember(plan.unmapped, item.node_id)

        return plan

    @staticmethod
    def _assign(assignments: dict[str, list[str]], specialist_id: str, node_id: str) -> None:
        nodes = assignments.setdefault(specialist_id, [])
        if node_id not in nodes:
            nodes.append(node_id)

    @staticmethod
    def _remember(node_ids: list[str], node_id: str) -> None:
        if node_id not in node_ids:
            node_ids.append(node_id)
