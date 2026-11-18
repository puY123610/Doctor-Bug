"""Three dispatch policies (arms A/B/C) for the CORE1 experiment."""

from __future__ import annotations

from typing import Callable

from acceptor.acceptor import Verdict
from kb.lookup import DispatchKB

from .actors import self_report

# Organisational folklore used by arm B.  The orders entry is the naive part:
# order failures are handed to the database group merely because orders are
# persisted, even when the real cause is billing/inventory/auth.
SELF_REPORT_MAP: dict[str, str] = {
    "billing": "billing-specialist",
    "inventory": "inventory-specialist",
    "auth": "auth-specialist",
    "orders": "db-specialist",
}


def policy_a(verdict: Verdict, history: list[dict]) -> dict[str, list[str]]:
    """Arm A: framework-native feedback to the same executor every round."""
    return {"generalist": list(verdict.failed_tests)}


def policy_b(verdict: Verdict, history: list[dict]) -> dict[str, list[str]]:
    """Arm B: first round generalist, then reassign by self report."""
    failed = list(verdict.failed_tests)
    if not history:
        return {"generalist": failed}
    module = self_report(failed)
    specialist_id = SELF_REPORT_MAP.get(module, "generalist")
    return {specialist_id: failed}


def policy_c(
    verdict: Verdict,
    history: list[dict],
    kb: DispatchKB,
) -> dict[str, list[str]]:
    """Arm C: frozen-acceptance evidence looked up in the dispatch KB."""
    plan = kb.lookup(verdict.evidence)
    return {actor_id: list(nodes) for actor_id, nodes in plan.assignments.items()}


def policy_for(
    arm: str,
    kb: DispatchKB | None = None,
) -> Callable[[Verdict, list[dict]], dict[str, list[str]]]:
    """Return the callable for one arm; arm C needs a KB instance."""
    arm = arm.upper()
    if arm == "A":
        return policy_a
    if arm == "B":
        return policy_b
    if arm == "C":
        if kb is None:
            raise ValueError("arm C requires a DispatchKB")
        return lambda verdict, history: policy_c(verdict, history, kb)
    raise ValueError(f"unknown arm: {arm!r}")
