"""Tests for the dispatch main loop, scripted actors and the three arms."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from core.actors import CAN_FIX, apply_fix, mutations_for_nodes, self_report
from core.runner import run_arm

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
IGNORE_CACHES = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

ALL_EIGHT_NODES = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
    "tests/test_orders.py::test_checkout_total_with_coupon",
    "tests/test_orders.py::test_stock_after_single_checkout",
    "tests/test_orders.py::test_refund_rejected_for_support_role",
    "tests/test_orders.py::test_full_checkout_flow",
]


class CoreMechanismTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="core-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.work = self.tmp / "work"

    def copy_demo(self, name: str) -> Path:
        target = self.tmp / name
        shutil.copytree(DEMO_REPO, target, ignore=IGNORE_CACHES)
        return target

    def _run_arm(
        self,
        arm: str,
        source: Path,
        kb_enabled: bool = True,
        max_rounds: int = 6,
        name: str = "",
    ):
        root = self.work / (name or f"{arm}-{kb_enabled}-{max_rounds}")
        return run_arm(
            arm,
            source,
            kb_enabled=kb_enabled,
            max_rounds=max_rounds,
            work_root=root,
            vault_dir=PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault",
            kb_path=PLATFORM_ROOT / "kb" / "dispatch_table.json",
        )

    def test_capability_roster_is_the_experimental_variable(self):
        self.assertEqual(
            CAN_FIX,
            {
                "billing-specialist": frozenset({"billing"}),
                "inventory-specialist": frozenset({"inventory"}),
                "auth-specialist": frozenset({"auth"}),
                "db-specialist": frozenset(),
                "generalist": frozenset({"billing"}),
            },
        )

    def test_kb_attribution_of_the_eight_nodes(self):
        self.assertEqual(
            mutations_for_nodes(ALL_EIGHT_NODES),
            {"billing", "inventory", "auth"},
        )

    def test_generalist_self_report_counts_test_modules(self):
        self.assertEqual(
            self_report(
                [
                    "tests/test_billing.py::test_a",
                    "tests/test_billing.py::test_b",
                    "tests/test_orders.py::test_c",
                    "tests/test_orders.py::test_d",
                    "tests/test_orders.py::test_e",
                ]
            ),
            "orders",
        )

    def test_coupled_c_passes_within_three_rounds(self):
        record = self._run_arm("C", DEMO_REPO, name="coupled-c")

        self.assertEqual(record.outcome, "PASS")
        self.assertEqual(record.final_failed, [])
        self.assertGreaterEqual(len(record.rounds), 1)
        self.assertLessEqual(len(record.rounds), 3)
        self.assertEqual(
            set(record.rounds[0]["assignments"]),
            {"billing-specialist", "inventory-specialist", "auth-specialist"},
        )
        self.assertTrue(
            all(item["applied"] for item in record.rounds[0]["fix_results"])
        )

    def test_coupled_a_caps_after_billing_only_generalist_repair(self):
        record = self._run_arm("A", DEMO_REPO, name="coupled-a")

        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(len(record.rounds), 6)
        self.assertEqual(len(record.final_failed), 5)
        self.assertEqual(set(record.rounds[0]["assignments"]), {"generalist"})
        self.assertTrue(record.rounds[0]["fix_results"][0]["applied"])
        self.assertFalse(record.rounds[1]["fix_results"][0]["applied"])

    def test_coupled_b_caps_after_naive_db_handoff(self):
        record = self._run_arm("B", DEMO_REPO, name="coupled-b")

        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(len(record.rounds), 6)
        self.assertEqual(len(record.final_failed), 5)
        self.assertEqual(set(record.rounds[0]["assignments"]), {"generalist"})
        self.assertEqual(set(record.rounds[1]["assignments"]), {"db-specialist"})
        self.assertFalse(record.rounds[1]["fix_results"][0]["applied"])

    def test_coupled_c_without_kb_caps(self):
        record = self._run_arm("C", DEMO_REPO, kb_enabled=False, name="ablation-c")

        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(len(record.rounds), 6)
        self.assertEqual(len(record.final_failed), 5)
        self.assertEqual(
            [set(item["assignments"]) for item in record.rounds],
            [{"generalist"}] * 6,
        )

    def test_tamper_is_detected_before_any_repair_round(self):
        workspace = self.copy_demo("tampered")
        target = workspace / "tests" / "test_billing.py"
        text = target.read_text(encoding="utf-8")
        changed = text.replace(
            "assert bill.tax_cents == 540",
            "assert bill.tax_cents == 541",
            1,
        )
        self.assertNotEqual(text, changed)
        target.write_text(changed, encoding="utf-8")

        record = self._run_arm("C", workspace, name="tamper-c")

        self.assertEqual(record.outcome, "TAMPERED")
        self.assertEqual(record.rounds, [])
        self.assertEqual(record.final_failed, [])

    def test_honesty_fixture_passes_for_all_three_arms(self):
        workspace = self.copy_demo("honesty")
        self.assertTrue(apply_fix("inventory-specialist", workspace, "inventory"))
        self.assertTrue(apply_fix("auth-specialist", workspace, "auth"))

        for arm in ("A", "B", "C"):
            with self.subTest(arm=arm):
                record = self._run_arm(arm, workspace, name=f"honesty-{arm}")
                self.assertEqual(record.outcome, "PASS")
                self.assertEqual(record.final_failed, [])
                self.assertEqual(len(record.rounds), 1)


if __name__ == "__main__":
    unittest.main()
