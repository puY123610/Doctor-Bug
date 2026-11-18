"""Tests for the frozen acceptor (freeze, verify, check)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from acceptor.acceptor import FrozenAcceptor
from acceptor.evidence import evidence_from_failed
from acceptor.frozen import freeze, verify_against_manifest

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
IGNORE_CACHES = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

EXPECTED_FAILED = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
    "tests/test_orders.py::test_checkout_total_with_coupon",
    "tests/test_orders.py::test_stock_after_single_checkout",
    "tests/test_orders.py::test_refund_rejected_for_support_role",
    "tests/test_orders.py::test_full_checkout_flow",
]

EXPECTED_SURFACE = {
    "conftest.py",
    "pytest.ini",
    "tests/test_auth.py",
    "tests/test_billing.py",
    "tests/test_inventory.py",
    "tests/test_orders.py",
}


class FrozenAcceptorTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="acceptor-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.vault = self.tmp / "vault"
        self.manifest = freeze(DEMO_REPO, self.vault)

    def copy_demo(self, name: str = "workspace") -> Path:
        target = self.tmp / name
        shutil.copytree(DEMO_REPO, target, ignore=IGNORE_CACHES)
        return target

    def test_freeze_verify_roundtrip(self):
        self.assertEqual(set(self.manifest["files"]), EXPECTED_SURFACE)
        self.assertTrue(Path(self.manifest["source"]).name == "demo_repo")
        for digest in self.manifest["files"].values():
            self.assertRegex(digest, r"^[0-9a-f]{64}$")

        on_disk = json.loads(
            (self.vault / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(on_disk, self.manifest)
        self.assertEqual(verify_against_manifest(self.manifest, DEMO_REPO), [])

    def test_changed_byte_is_tampering(self):
        workspace = self.copy_demo()
        target = workspace / "tests" / "test_billing.py"
        original = target.read_text(encoding="utf-8")
        changed = original.replace(
            "assert bill.tax_cents == 540",
            "assert bill.tax_cents == 541",
        )
        self.assertNotEqual(original, changed)
        target.write_text(changed, encoding="utf-8")

        self.assertEqual(
            verify_against_manifest(self.manifest, workspace),
            ["tests/test_billing.py"],
        )

    def test_missing_file_is_tampering(self):
        workspace = self.copy_demo()
        (workspace / "tests" / "test_orders.py").unlink()

        self.assertEqual(
            verify_against_manifest(self.manifest, workspace),
            ["tests/test_orders.py"],
        )

    def test_extra_test_file_is_tampering(self):
        workspace = self.copy_demo()
        (workspace / "tests" / "test_extra.py").write_text(
            "def test_extra():\n    assert True\n",
            encoding="utf-8",
        )

        self.assertEqual(
            verify_against_manifest(self.manifest, workspace),
            ["tests/test_extra.py"],
        )

    def test_check_returns_tampered_without_trusting_workspace(self):
        workspace = self.copy_demo()
        target = workspace / "tests" / "test_billing.py"
        target.write_text(
            target.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )

        verdict = FrozenAcceptor(self.vault).check(workspace)

        self.assertEqual(verdict.status, "TAMPERED")
        self.assertIn("tests/test_billing.py", verdict.tampered_files)
        self.assertEqual(verdict.passed_count, 0)
        self.assertEqual(verdict.failed_count, 0)
        self.assertEqual(verdict.evidence, [])

    def test_check_fails_on_mutated_repo(self):
        workspace = self.copy_demo()

        verdict = FrozenAcceptor(self.vault).check(workspace)

        self.assertEqual(verdict.status, "FAIL")
        self.assertEqual(verdict.failed_count, 8)
        self.assertEqual(verdict.passed_count, 16)
        self.assertEqual(sorted(verdict.failed_tests), sorted(EXPECTED_FAILED))
        self.assertEqual(verdict.tampered_files, [])
        self.assertEqual(
            [item.node_id for item in verdict.evidence],
            verdict.failed_tests,
        )
        self.assertEqual(
            {item.module for item in verdict.evidence},
            {"auth", "billing", "inventory", "orders"},
        )

    def test_check_passes_on_repaired_repo(self):
        workspace = self.copy_demo()

        billing = workspace / "backoffice" / "billing.py"
        billing_text = billing.read_text(encoding="utf-8")
        billing_fixed = billing_text.replace(
            "tax = _round_cents(Decimal(subtotal) * TAX_RATE)",
            "tax = _round_cents(Decimal(subtotal - discount) * TAX_RATE)",
        )
        self.assertNotEqual(billing_text, billing_fixed)
        billing.write_text(billing_fixed, encoding="utf-8")

        inventory = workspace / "backoffice" / "inventory.py"
        inventory_text = inventory.read_text(encoding="utf-8")
        inventory_fixed = inventory_text.replace(
            "return self._stock.get(sku, 0) - 2 * self._reserved.get(sku, 0)",
            "return self._stock.get(sku, 0) - self._reserved.get(sku, 0)",
        )
        self.assertNotEqual(inventory_text, inventory_fixed)
        inventory.write_text(inventory_fixed, encoding="utf-8")

        auth = workspace / "backoffice" / "auth.py"
        auth_text = auth.read_text(encoding="utf-8")
        auth_fixed = auth_text.replace(
            'return "finance" in user.roles or "support" in user.roles',
            'return "finance" in user.roles',
        )
        self.assertNotEqual(auth_text, auth_fixed)
        auth.write_text(auth_fixed, encoding="utf-8")

        verdict = FrozenAcceptor(self.vault).check(workspace)

        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.failed_count, 0)
        self.assertEqual(verdict.passed_count, 24)
        self.assertEqual(verdict.failed_tests, [])
        self.assertEqual(verdict.tampered_files, [])
        self.assertEqual(verdict.evidence, [])

    def test_evidence_module_derivation(self):
        evidence = evidence_from_failed(
            [
                "tests/test_billing.py::test_tax",
                "tests/test_inventory.py::test_stock",
                "tests/test_auth.py::test_role",
                "tests/test_orders.py::test_order",
            ]
        )
        self.assertEqual(
            [(item.node_id, item.module) for item in evidence],
            [
                ("tests/test_billing.py::test_tax", "billing"),
                ("tests/test_inventory.py::test_stock", "inventory"),
                ("tests/test_auth.py::test_role", "auth"),
                ("tests/test_orders.py::test_order", "orders"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
