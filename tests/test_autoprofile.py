"""GEN2 offline tests: programmatic repository profiling (no model calls)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import FrozenAcceptor, Verdict
from acceptor.evidence import evidence_from_failed
from core.agentscope_actors import roster_from_table
from core.autoprofile import build_profile, default_profile_name
from core.runner import RunRecord, profile_paths, run_arm
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
DEMO_REPO2 = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo2"

DEMO_FAILED = [
    "tests/test_auth.py::test_support_role_cannot_refund",
    "tests/test_billing.py::test_tax_on_discounted_total",
    "tests/test_billing.py::test_total_with_percent_and_fixed_coupon",
    "tests/test_inventory.py::test_reserve_decrements_available_once",
    "tests/test_orders.py::test_checkout_total_with_coupon",
    "tests/test_orders.py::test_stock_after_single_checkout",
    "tests/test_orders.py::test_refund_rejected_for_support_role",
    "tests/test_orders.py::test_full_checkout_flow",
]
REPO2_FAILED = [
    "tests/test_bookings.py::test_book_and_cancel_releases_slot",
    "tests/test_bookings.py::test_rooms_do_not_share_availability",
    "tests/test_bookings.py::test_back_to_back_booking_in_same_room_is_allowed",
    "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability",
    "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict",
    "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases",
]


def _assigned_files(
    table: dict, node_id: str
) -> tuple[list[str], list[str]]:
    plan = DispatchKB(table).lookup(evidence_from_failed([node_id]))
    files = {item["id"]: item["files"] for item in table["specialists"]}
    actor_ids = [
        actor
        for actor, nodes in plan.assignments.items()
        if node_id in nodes
    ]
    covered = sorted(
        {path for actor in actor_ids for path in files[actor]}
    )
    return actor_ids, covered


_ENV_SNAPSHOT: dict[str, str] = {}


def setUpModule() -> None:
    _ENV_SNAPSHOT.update(os.environ)


def tearDownModule() -> None:
    # 本模块的进程内 run 会 setdefault 出 DRBUG_TOKEN_METER 等变量，
    # 模块结束时还原环境，免得漏给后面的测试模块。
    os.environ.clear()
    os.environ.update(_ENV_SNAPSHOT)


class AutoprofileDemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="autoprofile-demo-")
        cls.tmp = Path(cls._temporary.name)
        cls.result = build_profile(
            DEMO_REPO, out_root=cls.tmp / "profiles", name="demo_repo"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_table_has_hand_written_schema_and_specialist_files(self) -> None:
        table = self.result.dispatch_table
        self.assertEqual(table["version"], 1)
        self.assertEqual(table["fallback"], "generalist")
        self.assertEqual(
            set(table),
            {
                "version",
                "specialists",
                "node_overrides",
                "module_defaults",
                "fallback",
                "roster",
            },
        )
        ids = [item["id"] for item in table["specialists"]]
        self.assertEqual(
            ids,
            [
                "auth-specialist",
                "billing-specialist",
                "db-specialist",
                "inventory-specialist",
                "orders-specialist",
                "generalist",
            ],
        )
        by_id = {item["id"]: item for item in table["specialists"]}
        self.assertEqual(by_id["billing-specialist"]["files"], ["backoffice/billing.py"])
        self.assertIn("Order pricing", by_id["billing-specialist"]["blurb"])
        self.assertEqual(
            by_id["generalist"]["files"],
            [
                "backoffice/__init__.py",
                "backoffice/auth.py",
                "backoffice/billing.py",
                "backoffice/db.py",
                "backoffice/inventory.py",
                "backoffice/orders.py",
            ],
        )
        self.assertEqual(
            table["module_defaults"],
            {
                "auth": ["auth-specialist"],
                "billing": ["billing-specialist"],
                "inventory": ["inventory-specialist"],
                "orders": ["orders-specialist"],
            },
        )

    def test_generated_roster_is_consumable_by_native_backend(self) -> None:
        roster = roster_from_table(self.result.dispatch_table)

        self.assertEqual(roster.package, "backoffice")
        self.assertEqual(
            roster.allowed_files("inventory-specialist"),
            ("backoffice/inventory.py",),
        )
        self.assertEqual(len(roster.allowed_files("generalist")), 6)
        self.assertFalse(roster.specialists["generalist"].specialist)

    def test_every_failed_node_covers_its_real_module(self) -> None:
        table = self.result.dispatch_table
        expected = {
            "tests/test_auth.py::test_support_role_cannot_refund": {
                "backoffice/auth.py"
            },
            "tests/test_billing.py::test_tax_on_discounted_total": {
                "backoffice/billing.py"
            },
            "tests/test_billing.py::test_total_with_percent_and_fixed_coupon": {
                "backoffice/billing.py"
            },
            "tests/test_inventory.py::test_reserve_decrements_available_once": {
                "backoffice/inventory.py"
            },
            "tests/test_orders.py::test_checkout_total_with_coupon": {
                "backoffice/billing.py"
            },
            "tests/test_orders.py::test_stock_after_single_checkout": {
                "backoffice/inventory.py"
            },
            "tests/test_orders.py::test_refund_rejected_for_support_role": {
                "backoffice/auth.py"
            },
            "tests/test_orders.py::test_full_checkout_flow": {
                "backoffice/billing.py",
                "backoffice/inventory.py",
                "backoffice/auth.py",
            },
        }
        for node in DEMO_FAILED:
            with self.subTest(node=node):
                _actors, covered = _assigned_files(table, node)
                self.assertTrue(expected[node] <= set(covered), covered)

    def test_failed_dispatch_is_narrow_and_traceback_based(self) -> None:
        table = self.result.dispatch_table
        expected = {
            "tests/test_auth.py::test_support_role_cannot_refund": {
                "auth-specialist"
            },
            "tests/test_billing.py::test_tax_on_discounted_total": {
                "billing-specialist"
            },
            "tests/test_billing.py::test_total_with_percent_and_fixed_coupon": {
                "billing-specialist"
            },
            "tests/test_inventory.py::test_reserve_decrements_available_once": {
                "inventory-specialist"
            },
            "tests/test_orders.py::test_checkout_total_with_coupon": {
                "billing-specialist"
            },
            "tests/test_orders.py::test_stock_after_single_checkout": {
                "inventory-specialist"
            },
            "tests/test_orders.py::test_refund_rejected_for_support_role": {
                "auth-specialist"
            },
            "tests/test_orders.py::test_full_checkout_flow": {
                "auth-specialist",
                "billing-specialist",
                "inventory-specialist",
            },
        }
        expected_files = {
            node: {
                f"backoffice/{actor.removesuffix('-specialist')}.py"
                for actor in actors
            }
            for node, actors in expected.items()
        }
        for node, actors in expected.items():
            with self.subTest(node=node):
                assigned, covered = _assigned_files(table, node)
                self.assertEqual(set(assigned), actors)
                self.assertEqual(set(covered), expected_files[node])

        self.assertEqual(
            set(table["node_overrides"]),
            {
                "tests/test_orders.py::test_checkout_total_with_coupon",
                "tests/test_orders.py::test_stock_after_single_checkout",
                "tests/test_orders.py::test_refund_rejected_for_support_role",
                "tests/test_orders.py::test_full_checkout_flow",
            },
        )
        no_traceback = {
            item.get("node_id")
            for item in self.result.ambiguities
            if item.get("kind") == "no_traceback_import_dispatch"
        }
        self.assertEqual(no_traceback, set(DEMO_FAILED))

    def test_profile_metadata_and_markdown(self) -> None:
        metadata = json.loads(
            self.result.profile_json_path.read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["source_repo"], str(DEMO_REPO.resolve()))
        self.assertEqual(metadata["packages"], ["backoffice"])
        self.assertEqual(metadata["test_total"], 24)
        self.assertEqual(metadata["failed_count"], 8)
        self.assertEqual(metadata["passed_count"], 16)
        self.assertEqual(metadata["generator"], "core.autoprofile")
        self.assertEqual(metadata["generator_version"], "GEN2")
        self.assertIsInstance(metadata["ambiguities"], list)

        markdown = self.result.markdown_path.read_text(encoding="utf-8")
        self.assertIn("# 自动建档报告：demo_repo", markdown)
        self.assertIn("## 失败证据派单", markdown)
        self.assertIn("tests/test_orders.py::test_full_checkout_flow", markdown)
        self.assertIn("billing-specialist", markdown)
        self.assertIn("## 歧义与未把握处", markdown)
        self.assertIn("派单依据", markdown)
        self.assertIn("无 traceback", markdown)

    def test_vault_reproduces_the_same_failure_set(self) -> None:
        verdict = FrozenAcceptor(self.result.vault_dir).check(DEMO_REPO)

        self.assertEqual(verdict.status, "FAIL")
        self.assertEqual(verdict.passed_count, 16)
        self.assertEqual(verdict.failed_count, 8)
        self.assertEqual(sorted(verdict.failed_tests), sorted(DEMO_FAILED))

    def test_rebuild_is_byte_identical(self) -> None:
        first = self.result.dispatch_table_path.read_bytes()
        first_vault = {
            path.relative_to(self.result.vault_dir).as_posix(): path.read_bytes()
            for path in self.result.vault_dir.rglob("*")
            if path.is_file()
        }

        rebuilt = build_profile(
            DEMO_REPO, out_root=self.tmp / "profiles", name="demo_repo"
        )

        self.assertEqual(rebuilt.dispatch_table_path.read_bytes(), first)
        second_vault = {
            path.relative_to(rebuilt.vault_dir).as_posix(): path.read_bytes()
            for path in rebuilt.vault_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(second_vault, first_vault)


class AutoprofileRepo2Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(prefix="autoprofile-repo2-")
        cls.tmp = Path(cls._temporary.name)
        cls.result = build_profile(
            DEMO_REPO2, out_root=cls.tmp / "profiles", name="demo_repo2"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def test_src_layout_package_and_defaults(self) -> None:
        self.assertEqual(self.result.packages, ["src/studyroom"])
        table = self.result.dispatch_table
        self.assertEqual(table["roster"]["package"], "src/studyroom")
        self.assertEqual(
            table["module_defaults"]["bookings"], ["bookings-specialist"]
        )
        self.assertEqual(
            table["module_defaults"]["conflicts"], ["conflicts-specialist"]
        )
        self.assertEqual(
            table["module_defaults"]["end_to_end"], ["bookings-specialist"]
        )

    def test_naming_convention_miss_is_recorded(self) -> None:
        kinds = {
            (item["kind"], item.get("test_file"))
            for item in self.result.ambiguities
        }
        self.assertIn(
            ("naming_convention_miss", "tests/test_end_to_end.py"), kinds
        )

    def test_all_six_failures_cover_bookings_or_conflicts(self) -> None:
        table = self.result.dispatch_table
        expected = {
            "tests/test_bookings.py::test_book_and_cancel_releases_slot": {
                "src/studyroom/bookings.py"
            },
            "tests/test_bookings.py::test_rooms_do_not_share_availability": {
                "src/studyroom/bookings.py"
            },
            "tests/test_bookings.py::test_back_to_back_booking_in_same_room_is_allowed": {
                "src/studyroom/bookings.py"
            },
            "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability": {
                "src/studyroom/bookings.py"
            },
            "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict": {
                "src/studyroom/conflicts.py"
            },
            "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases": {
                "src/studyroom/bookings.py"
            },
        }
        for node in REPO2_FAILED:
            with self.subTest(node=node):
                _actors, covered = _assigned_files(table, node)
                self.assertTrue(expected[node] <= set(covered), covered)

    def test_failed_dispatch_is_narrow_and_traceback_based(self) -> None:
        table = self.result.dispatch_table
        expected = {
            "tests/test_bookings.py::test_book_and_cancel_releases_slot": {
                "bookings-specialist"
            },
            "tests/test_bookings.py::test_rooms_do_not_share_availability": {
                "bookings-specialist"
            },
            "tests/test_bookings.py::test_back_to_back_booking_in_same_room_is_allowed": {
                "bookings-specialist"
            },
            "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability": {
                "bookings-specialist"
            },
            "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict": {
                "conflicts-specialist"
            },
            "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases": {
                "bookings-specialist",
                "conflicts-specialist",
            },
        }
        for node, actors in expected.items():
            with self.subTest(node=node):
                assigned, covered = _assigned_files(table, node)
                self.assertEqual(set(assigned), actors)
        _actors, end_to_end_files = _assigned_files(
            table, "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases"
        )
        self.assertEqual(
            set(end_to_end_files),
            {"src/studyroom/bookings.py", "src/studyroom/conflicts.py"},
        )
        self.assertEqual(
            set(table["node_overrides"]),
            {
                "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases"
            },
        )
        no_traceback = {
            item.get("node_id")
            for item in self.result.ambiguities
            if item.get("kind") == "no_traceback_import_dispatch"
        }
        self.assertEqual(
            no_traceback,
            {
                "tests/test_bookings.py::test_book_and_cancel_releases_slot",
                "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability",
                "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict",
            },
        )

    def test_markdown_lists_traceback_basis(self) -> None:
        markdown = self.result.markdown_path.read_text(encoding="utf-8")
        self.assertIn("派单依据", markdown)
        self.assertIn("traceback 帧", markdown)
        self.assertIn("无 traceback", markdown)
        self.assertIn("src/studyroom/bookings.py", markdown)

    def test_rebuild_is_byte_identical(self) -> None:
        first = self.result.dispatch_table_path.read_bytes()

        rebuilt = build_profile(
            DEMO_REPO2, out_root=self.tmp / "profiles", name="demo_repo2"
        )

        self.assertEqual(rebuilt.dispatch_table_path.read_bytes(), first)

    def test_passing_nodes_have_no_override(self) -> None:
        table = self.result.dispatch_table
        self.assertTrue(
            set(table["node_overrides"]) <= set(REPO2_FAILED)
        )


class AutoprofileWeirdLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-weird-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.repo = self.tmp / "repo"
        package = self.repo / "src" / "acme" / "widgets"
        package.mkdir(parents=True)
        (self.repo / "src" / "acme" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "logic.py").write_text(
            '"""Widget logic helpers."""\n\n\n'
            "def double(value):\n"
            "    return value + 2\n",
            encoding="utf-8",
        )
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_behaviour.py").write_text(
            '"""Behaviour tests; file name intentionally differs."""\n\n'
            "from acme.widgets.logic import double\n\n\n"
            "def test_double():\n"
            "    assert double(3) == 6\n",
            encoding="utf-8",
        )
        # No conftest.py on purpose.
        (self.repo / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\npythonpath = src\n",
            encoding="utf-8",
        )

    def test_nested_package_maps_by_import_and_records_naming_miss(self) -> None:
        result = build_profile(
            self.repo, out_root=self.tmp / "profiles", name="weird"
        )

        self.assertEqual(result.packages, ["src/acme"])
        self.assertEqual(
            result.dispatch_table["module_defaults"]["behaviour"],
            ["logic-specialist"],
        )
        self.assertEqual(
            result.dispatch_table["specialists"][0]["files"],
            ["src/acme/widgets/logic.py"],
        )
        self.assertIn(
            ("naming_convention_miss", "tests/test_behaviour.py"),
            {
                (item["kind"], item.get("test_file"))
                for item in result.ambiguities
            },
        )
        actors, covered = _assigned_files(
            result.dispatch_table,
            "tests/test_behaviour.py::test_double",
        )
        self.assertEqual(actors, ["logic-specialist"])
        self.assertIn("src/acme/widgets/logic.py", covered)
        self.assertEqual(
            FrozenAcceptor(result.vault_dir).check(self.repo).status, "FAIL"
        )


class AutoprofileRootTestFileTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-root-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.repo = self.tmp / "repo"
        package = self.repo / "thing"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "core.py").write_text(
            '"""Thing core."""\n\nVALUE = 1\n', encoding="utf-8"
        )
        # Root-level test, no pytest.ini and no conftest.py: the acceptor's
        # own judging surface is empty, so the profiler must still freeze it.
        (self.repo / "test_root.py").write_text(
            "from thing.core import VALUE\n\n\n"
            "def test_value():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )

    def test_root_test_is_frozen_and_mapped(self) -> None:
        result = build_profile(
            self.repo, out_root=self.tmp / "profiles", name="root"
        )

        self.assertEqual(result.packages, ["thing"])
        self.assertEqual(
            result.dispatch_table["module_defaults"]["root"],
            ["core-specialist"],
        )
        self.assertEqual(
            result.metadata["failed_nodes"], ["test_root.py::test_value"]
        )
        self.assertEqual(
            FrozenAcceptor(result.vault_dir).check(self.repo).status, "FAIL"
        )


class AutoprofileMultiPackageTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-multi-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.repo = self.tmp / "repo"
        for name in ("pkg_a", "pkg_b"):
            package = self.repo / "src" / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "src" / "pkg_a" / "alpha.py").write_text(
            '"""Alpha module."""\n\nVALUE = 1\n', encoding="utf-8"
        )
        (self.repo / "src" / "pkg_b" / "beta.py").write_text(
            '"""Beta module."""\n\nVALUE = 2\n', encoding="utf-8"
        )
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_alpha.py").write_text(
            "from pkg_a.alpha import VALUE\n\n\n"
            "def test_alpha():\n    assert VALUE == 1\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_beta.py").write_text(
            "from pkg_b.beta import VALUE\n\n\n"
            "def test_beta():\n    assert VALUE == 2\n",
            encoding="utf-8",
        )
        (self.repo / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\npythonpath = src\n",
            encoding="utf-8",
        )

    def test_two_packages_share_the_common_src_package(self) -> None:
        result = build_profile(
            self.repo, out_root=self.tmp / "profiles", name="multi"
        )

        self.assertEqual(result.packages, ["src/pkg_a", "src/pkg_b"])
        self.assertEqual(
            result.dispatch_table["roster"]["package"], "src"
        )
        self.assertEqual(
            result.dispatch_table["module_defaults"]["alpha"],
            ["alpha-specialist"],
        )
        self.assertEqual(
            result.dispatch_table["module_defaults"]["beta"],
            ["beta-specialist"],
        )
        self.assertEqual(
            FrozenAcceptor(result.vault_dir).check(self.repo).status, "PASS"
        )


class CliAutoprofileTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-cli-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def _capture(self, argv: list[str]) -> tuple[int, str]:
        from core.cli import main as cli_main

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(argv)
        return code, buffer.getvalue()

    def test_profile_subcommand_prints_summary_and_report_path(self) -> None:
        with mock.patch("core.cli.PROFILES_ROOT", self.tmp / "profiles"):
            code, output = self._capture(
                ["profile", str(DEMO_REPO), "--name", "cli-demo"]
            )

        self.assertEqual(code, 0)
        self.assertIn("自动建档", output)
        self.assertIn("PROFILE.md", output)
        self.assertIn("billing-specialist", output)
        self.assertTrue(
            (self.tmp / "profiles" / "cli-demo" / "PROFILE.md").is_file()
        )

    def test_run_profile_auto_builds_then_passes_profile_dir(self) -> None:
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        out = self.tmp / "auto-run.json"
        with mock.patch("core.cli.PROFILES_ROOT", self.tmp / "profiles"):
            with mock.patch(
                "core.runner.run_arm", return_value=record
            ) as run_mock:
                code, output = self._capture(
                    [
                        "run",
                        "--arm",
                        "C",
                        "--workspace",
                        str(DEMO_REPO),
                        "--profile",
                        "auto",
                        "--out",
                        str(out),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("自动建档", output)
        profile_dir = self.tmp / "profiles" / default_profile_name(DEMO_REPO)
        self.assertTrue(profile_dir.is_dir())
        self.assertEqual(run_mock.call_args.kwargs["profile_dir"], profile_dir)
        self.assertTrue(out.is_file())


    def test_run_profile_auto_reuses_existing_profile(self) -> None:
        profile_dir = self.tmp / "profiles" / default_profile_name(DEMO_REPO)
        build_profile(DEMO_REPO, out_root=self.tmp / "profiles")
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        with mock.patch("core.cli.PROFILES_ROOT", self.tmp / "profiles"):
            with mock.patch(
                "core.autoprofile.build_profile"
            ) as builder, mock.patch(
                "core.runner.run_arm", return_value=record
            ) as run_mock:
                code, output = self._capture(
                    [
                        "run",
                        "--arm",
                        "C",
                        "--workspace",
                        str(DEMO_REPO),
                        "--profile",
                        "auto",
                        "--out",
                        str(self.tmp / "existing.json"),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("使用已有档位", output)
        builder.assert_not_called()
        self.assertEqual(run_mock.call_args.kwargs["profile_dir"], profile_dir)


class RunnerAutoprofileWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-run-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_profile_paths_point_at_vault_and_table(self) -> None:
        paths = profile_paths(self.tmp / "profile")

        self.assertEqual(paths["vault_dir"], self.tmp / "profile" / "vault")
        self.assertEqual(
            paths["kb_path"], self.tmp / "profile" / "dispatch_table.json"
        )

    def test_run_arm_loads_vault_from_profile_dir(self) -> None:
        profile_dir = self.tmp / "profile"
        (profile_dir / "vault").mkdir(parents=True)
        (profile_dir / "dispatch_table.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "specialists": [],
                    "node_overrides": {},
                    "module_defaults": {},
                    "fallback": "generalist",
                }
            ),
            encoding="utf-8",
        )
        fake_acceptor = mock.Mock()
        fake_acceptor.check.return_value = Verdict(status="PASS")
        with mock.patch(
            "core.runner.FrozenAcceptor", return_value=fake_acceptor
        ) as acceptor_cls:
            record = run_arm(
                "C",
                DEMO_REPO,
                work_root=self.tmp / "work",
                profile_dir=profile_dir,
            )

        self.assertEqual(record.outcome, "PASS")
        acceptor_cls.assert_called_once_with(profile_dir / "vault")


class DispatcherAutoprofileTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="autoprofile-dispatcher-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_platform_workspace_uses_its_existing_profile(self) -> None:
        from core import dispatcher

        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        with mock.patch.object(
            dispatcher, "RUNS_DIR", self.tmp / "runs"
        ), mock.patch.object(
            dispatcher, "run_arm", return_value=record
        ) as run_mock:
            result = dispatcher.run_repair(
                workspace=str(DEMO_REPO), actors="scripted"
            )

        self.assertIn("outcome=PASS", result)
        self.assertEqual(
            run_mock.call_args.kwargs["profile_dir"],
            Path(__file__).resolve().parents[1] / "profiles" / "demo_repo",
        )
        self.assertNotIn("vault_dir", run_mock.call_args.kwargs)
        self.assertNotIn("kb_path", run_mock.call_args.kwargs)

    def test_platform_repo2_uses_its_existing_profile(self) -> None:
        from core import dispatcher

        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        with mock.patch.object(
            dispatcher, "RUNS_DIR", self.tmp / "runs"
        ), mock.patch.object(
            dispatcher, "run_arm", return_value=record
        ) as run_mock:
            result = dispatcher.run_repair(
                workspace=str(DEMO_REPO2), actors="scripted"
            )

        self.assertIn("outcome=PASS", result)
        self.assertEqual(
            run_mock.call_args.kwargs["profile_dir"],
            Path(__file__).resolve().parents[1] / "profiles" / "demo_repo2",
        )
        self.assertNotIn("vault_dir", run_mock.call_args.kwargs)
        self.assertNotIn("kb_path", run_mock.call_args.kwargs)

    def test_non_builtin_workspace_builds_profile_and_runs(self) -> None:
        from core import dispatcher

        workspace = self.tmp / "external_repo"
        shutil.copytree(
            DEMO_REPO,
            workspace,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        with mock.patch.object(
            dispatcher, "RUNS_DIR", self.tmp / "runs"
        ), mock.patch.object(
            dispatcher, "run_arm", return_value=record
        ) as run_mock:
            result = dispatcher.run_repair(
                workspace=str(workspace), actors="scripted"
            )

        profile_dir = (
            workspace
            / ".drbug"
            / "profiles"
            / default_profile_name(workspace)
        )
        self.assertIn("outcome=PASS", result)
        self.assertTrue((profile_dir / "vault" / "manifest.json").is_file())
        self.assertTrue((profile_dir / "dispatch_table.json").is_file())
        self.assertEqual(
            run_mock.call_args.kwargs["profile_dir"], profile_dir.resolve()
        )
        self.assertNotIn("vault_dir", run_mock.call_args.kwargs)

    def test_paid_gate_still_rejects_before_profiling(self) -> None:
        from core import dispatcher

        workspace = self.tmp / "external_repo"
        shutil.copytree(
            DEMO_REPO,
            workspace,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
        )
        with mock.patch.object(
            dispatcher, "PROFILES_ROOT", self.tmp / "profiles"
        ), mock.patch("core.autoprofile.build_profile") as builder:
            result = dispatcher.run_repair(
                workspace=str(workspace),
                actors="agentscope",
                confirm_paid=False,
            )

        self.assertIn("confirm_paid=True", result)
        builder.assert_not_called()
        self.assertFalse((self.tmp / "profiles").exists())


if __name__ == "__main__":
    unittest.main()
