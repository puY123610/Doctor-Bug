"""Tests for the dispatch knowledge base (data table + lookup rules)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from acceptor.evidence import Evidence, evidence_from_failed
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = PLATFORM_ROOT / "kb" / "dispatch_table.json"

FAILED_NODE_IDS = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
    "tests/test_orders.py::test_checkout_total_with_coupon",
    "tests/test_orders.py::test_stock_after_single_checkout",
    "tests/test_orders.py::test_refund_rejected_for_support_role",
    "tests/test_orders.py::test_full_checkout_flow",
]

CHECKOUT_TOTAL = "tests/test_orders.py::test_checkout_total_with_coupon"


class DispatchTableContentTest(unittest.TestCase):
    def setUp(self):
        self.table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))

    def test_roster_contains_required_specialists(self):
        ids = [item["id"] for item in self.table["specialists"]]
        self.assertEqual(
            ids,
            [
                "billing-specialist",
                "inventory-specialist",
                "auth-specialist",
                "db-specialist",
                "generalist",
            ],
        )
        for item in self.table["specialists"]:
            self.assertTrue(item["name"])
            self.assertTrue(item["scope"])

    def test_module_defaults_and_fallback(self):
        self.assertEqual(
            self.table["module_defaults"],
            {
                "billing": ["billing-specialist"],
                "inventory": ["inventory-specialist"],
                "auth": ["auth-specialist"],
                "orders": ["generalist"],
            },
        )
        self.assertEqual(self.table["fallback"], "generalist")

    def test_node_overrides_are_exact_and_sourced(self):
        overrides = self.table["node_overrides"]
        self.assertEqual(
            set(overrides),
            {
                "tests/test_orders.py::test_checkout_total_with_coupon",
                "tests/test_orders.py::test_stock_after_single_checkout",
                "tests/test_orders.py::test_refund_rejected_for_support_role",
                "tests/test_orders.py::test_full_checkout_flow",
            },
        )
        expected = {
            "tests/test_orders.py::test_checkout_total_with_coupon": [
                "billing-specialist"
            ],
            "tests/test_orders.py::test_stock_after_single_checkout": [
                "inventory-specialist"
            ],
            "tests/test_orders.py::test_refund_rejected_for_support_role": [
                "auth-specialist"
            ],
            "tests/test_orders.py::test_full_checkout_flow": [
                "billing-specialist",
                "inventory-specialist",
                "auth-specialist",
            ],
        }
        for node_id, specialists in expected.items():
            self.assertEqual(overrides[node_id]["specialists"], specialists)
            self.assertGreater(len(overrides[node_id]["rationale"]), 20)
            self.assertIn("REPO1", overrides[node_id]["source"])
            self.assertIn("demo_repo/", overrides[node_id]["source"])


class DispatchLookupTest(unittest.TestCase):
    def setUp(self):
        self.table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
        self.kb = DispatchKB.from_file(TABLE_PATH)
        self.evidence = evidence_from_failed(FAILED_NODE_IDS)

    def test_real_eight_evidence_plan(self):
        plan = self.kb.lookup(self.evidence)

        self.assertEqual(
            plan.assignments,
            {
                "auth-specialist": [
                    "tests/test_auth.py::test_support_role_cannot_refund",
                    "tests/test_orders.py::test_refund_rejected_for_support_role",
                    "tests/test_orders.py::test_full_checkout_flow",
                ],
                "billing-specialist": [
                    "tests/test_billing.py::test_tax_on_discounted_total",
                    "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
                    "tests/test_orders.py::test_checkout_total_with_coupon",
                    "tests/test_orders.py::test_full_checkout_flow",
                ],
                "inventory-specialist": [
                    "tests/test_inventory.py::test_reserve_decrements_available_once",
                    "tests/test_orders.py::test_stock_after_single_checkout",
                    "tests/test_orders.py::test_full_checkout_flow",
                ],
            },
        )
        # full_checkout_flow 是三耦合题：结账总额依赖 billing，库存读数依赖 inventory，
        # 退款拦截依赖 auth，故该 node 同时出现在三方名下。
        # billing 4 条 = 两条 test_billing + checkout_total_with_coupon + full_checkout_flow；
        # inventory 3 条 = test_inventory + stock_after_single_checkout + full_checkout_flow；
        # auth 3 条 = test_auth + refund_rejected_for_support_role + full_checkout_flow。
        self.assertEqual(len(plan.assignments["billing-specialist"]), 4)
        self.assertEqual(len(plan.assignments["inventory-specialist"]), 3)
        self.assertEqual(len(plan.assignments["auth-specialist"]), 3)
        self.assertIn(
            "tests/test_orders.py::test_full_checkout_flow",
            plan.assignments["billing-specialist"],
        )
        self.assertIn(
            "tests/test_orders.py::test_full_checkout_flow",
            plan.assignments["inventory-specialist"],
        )
        self.assertIn(
            "tests/test_orders.py::test_full_checkout_flow",
            plan.assignments["auth-specialist"],
        )
        self.assertEqual(plan.unmapped, [])

    def test_node_ablation_falls_back_through_orders_default(self):
        ablated = copy.deepcopy(self.table)
        del ablated["node_overrides"][CHECKOUT_TOTAL]

        plan = DispatchKB(ablated).lookup(self.evidence)

        self.assertEqual(
            plan.assignments["billing-specialist"],
            [
                "tests/test_billing.py::test_tax_on_discounted_total",
                "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
                "tests/test_orders.py::test_full_checkout_flow",
            ],
        )
        self.assertEqual(plan.assignments["generalist"], [CHECKOUT_TOTAL])
        self.assertNotIn(CHECKOUT_TOTAL, plan.assignments["billing-specialist"])
        self.assertEqual(plan.unmapped, [])

    def test_full_ablation_sends_everything_to_generalist(self):
        plan = DispatchKB(self.table, enabled=False).lookup(self.evidence)

        self.assertEqual(plan.assignments, {"generalist": FAILED_NODE_IDS})
        self.assertEqual(plan.unmapped, FAILED_NODE_IDS)

    def test_unknown_evidence_is_unmapped_and_goes_to_fallback(self):
        unknown = Evidence(
            node_id="tests/test_ghost.py::test_never_seen",
            module="ghost-module",
        )

        plan = self.kb.lookup([unknown])

        self.assertEqual(plan.assignments, {"generalist": [unknown.node_id]})
        self.assertEqual(plan.unmapped, [unknown.node_id])

    def test_unknown_module_does_not_disturb_known_evidence(self):
        unknown = Evidence(
            node_id="tests/test_ghost.py::test_never_seen",
            module="ghost-module",
        )

        plan = self.kb.lookup([*self.evidence, unknown])

        self.assertEqual(plan.unmapped, [unknown.node_id])
        self.assertEqual(len(plan.assignments["billing-specialist"]), 4)
        self.assertEqual(len(plan.assignments["inventory-specialist"]), 3)
        self.assertEqual(len(plan.assignments["auth-specialist"]), 3)
        self.assertEqual(plan.assignments["generalist"], [unknown.node_id])

    def test_table_is_the_source_of_truth(self):
        table = copy.deepcopy(self.table)
        table["module_defaults"]["ghost-module"] = ["db-specialist"]
        unknown = Evidence(
            node_id="tests/test_ghost.py::test_never_seen",
            module="ghost-module",
        )

        plan = DispatchKB(table).lookup([unknown])

        self.assertEqual(plan.assignments, {"db-specialist": [unknown.node_id]})
        self.assertEqual(plan.unmapped, [])


if __name__ == "__main__":
    unittest.main()
