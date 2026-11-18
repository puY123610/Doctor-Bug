"""Deterministic scripted actors for the CORE1 dispatch experiment.

These are *scripted* actors, not language-model agents.  Their capability
table is the experimental variable of this task:

* specialists can repair only their own mutation domain;
* ``generalist`` can repair only the most common billing mutation;
* ``db-specialist`` has no matching mutation in this work order.

The WIRE task will later replace these stubs with real agents while keeping
the same ``apply_fix`` / ``act`` interface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from acceptor.evidence import module_from_node_id

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DISPATCH_TABLE_PATH = PLATFORM_ROOT / "kb" / "dispatch_table.json"

# Capability roster, keyed by the same ids as kb/dispatch_table.json.
# This table is deliberately unequal: a generalist is NOT a universal fixer.
CAN_FIX: dict[str, frozenset[str]] = {
    "billing-specialist": frozenset({"billing"}),
    "inventory-specialist": frozenset({"inventory"}),
    "auth-specialist": frozenset({"auth"}),
    "db-specialist": frozenset(),
    "generalist": frozenset({"billing"}),
}

# Alias kept for readability at call sites that talk about the whole roster.
ROSTER = CAN_FIX

# Mutation -> (file, mutated line, correct line).  The strings match the
# repair fixture used by ACC1 / REPO1-fix1.
_FIXES: dict[str, tuple[str, str, str]] = {
    "billing": (
        "backoffice/billing.py",
        "    tax = _round_cents(Decimal(subtotal) * TAX_RATE)",
        "    tax = _round_cents(Decimal(subtotal - discount) * TAX_RATE)",
    ),
    "inventory": (
        "backoffice/inventory.py",
        "        return self._stock.get(sku, 0) - 2 * self._reserved.get(sku, 0)",
        "        return self._stock.get(sku, 0) - self._reserved.get(sku, 0)",
    ),
    "auth": (
        "backoffice/auth.py",
        '    return "finance" in user.roles or "support" in user.roles',
        '    return "finance" in user.roles',
    ),
}

# Only the three domain specialists express a root-cause attribution.  The
# generalist is a fallback and db-specialist has no mutation in this work
# order, so neither contributes a mutation domain here.
_SPECIALIST_DOMAIN = {
    "billing-specialist": "billing",
    "inventory-specialist": "inventory",
    "auth-specialist": "auth",
}


@dataclass
class FixResult:
    actor_id: str
    applied: bool
    note: str

    def to_dict(self) -> dict:
        return {
            "actor_id": self.actor_id,
            "applied": self.applied,
            "note": self.note,
        }


def _load_dispatch_table() -> dict:
    return json.loads(DISPATCH_TABLE_PATH.read_text(encoding="utf-8"))


_TABLE: dict | None = None


def _table() -> dict:
    """Lazily load the scripted backend's dispatch table.

    The scripted backend is a development/test harness keyed to the
    demo scenario; the installed product ships no demo data, so the
    table file is absent there and scripted runs must fail with a
    clear reason instead of crashing at import time.
    """
    global _TABLE
    if _TABLE is None:
        try:
            _TABLE = _load_dispatch_table()
        except OSError as exc:
            raise RuntimeError(
                "scripted 后端依赖开发用派单表 kb/dispatch_table.json，"
                "安装包不含演示数据；请改用 agentscope 后端"
            ) from exc
    return _TABLE


def _attributed_specialists(node_id: str, module: str) -> list[str]:
    """Follow the same order as kb.lookup: override -> module default -> fallback."""
    override = _table().get("node_overrides", {}).get(node_id)
    if override is not None:
        return list(override.get("specialists", []))
    module_defaults = _table().get("module_defaults", {})
    specialists = module_defaults.get(module)
    if specialists is not None:
        return list(specialists)
    return [str(_table()["fallback"])]


def mutations_for_node(node_id: str, module: str | None = None) -> set[str]:
    """Return the mutation domains attributed to one failed node."""
    if module is None:
        module = module_from_node_id(node_id)
    mutations: set[str] = set()
    for specialist_id in _attributed_specialists(node_id, module):
        domain = _SPECIALIST_DOMAIN.get(specialist_id)
        if domain is not None:
            mutations.add(domain)
    return mutations


def mutations_for_nodes(node_ids: list[str]) -> set[str]:
    """Union of mutation domains attributed to a set of failed nodes."""
    mutations: set[str] = set()
    for node_id in node_ids:
        mutations.update(mutations_for_node(node_id))
    return mutations


def apply_fix(actor_id: str, workspace: Path, mutation: str) -> bool:
    """Apply one string-level repair if the actor is capable and it is pending.

    Returns True only when this call actually rewrote the file.  An already
    repaired workspace returns False without touching anything.  An unknown
    file state raises: we never silently claim success.
    """
    if actor_id not in CAN_FIX:
        raise KeyError(f"unknown actor id: {actor_id!r}")
    if mutation not in CAN_FIX[actor_id]:
        return False

    relative, broken, fixed = _FIXES[mutation]
    path = Path(workspace) / relative
    text = path.read_text(encoding="utf-8")

    occurrences = text.count(broken)
    if occurrences == 1:
        path.write_text(text.replace(broken, fixed, 1), encoding="utf-8")
        return True
    if occurrences == 0:
        if fixed in text:
            return False
        raise RuntimeError(
            f"target line for mutation {mutation!r} not found in {path}; "
            f"expected either the mutated or the repaired line"
        )
    raise RuntimeError(
        f"target line for mutation {mutation!r} occurs {occurrences} times in {path}"
    )


def act(
    actor_id: str,
    workspace: Path,
    assigned_nodes: list[str],
    failed_nodes: list[str],
) -> FixResult:
    """Run one scripted actor on its assigned nodes.

    The target mutation set comes from ``assigned_nodes`` via the KB
    attribution rules, never from the actor's own guesses.  ``failed_nodes``
    is part of the stable interface and is used by self-reporting policies.
    """
    if actor_id not in CAN_FIX:
        raise KeyError(f"unknown actor id: {actor_id!r}")

    targets = sorted(mutations_for_nodes(list(assigned_nodes)))
    capable = [mutation for mutation in targets if mutation in CAN_FIX[actor_id]]
    outside = [mutation for mutation in targets if mutation not in CAN_FIX[actor_id]]

    fixed: list[str] = []
    for mutation in capable:
        if apply_fix(actor_id, workspace, mutation):
            fixed.append(mutation)

    parts: list[str] = []
    if fixed:
        parts.append("fixed " + ", ".join(fixed))
    elif capable:
        parts.append("no change for " + ", ".join(capable) + " (already repaired)")
    if outside:
        parts.append("outside capability " + ", ".join(outside))
    if not targets:
        parts.append("no mutation attributed to assigned nodes")
    note = "; ".join(parts)

    return FixResult(actor_id=actor_id, applied=bool(fixed), note=note)


def self_report(failed_nodes: list[str]) -> str:
    """The generalist's naive self report: module with the most failures.

    This is intentionally simplistic: it looks only at test-file module names,
    not at root causes.  On a tie the first module encountered wins.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for node_id in failed_nodes:
        module = module_from_node_id(node_id)
        if module not in counts:
            counts[module] = 0
            order.append(module)
        counts[module] += 1
    if not order:
        return ""
    return max(order, key=lambda module: counts[module])
