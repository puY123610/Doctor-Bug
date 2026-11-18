"""GEN1 offline tests: src-layout acceptor, repo2 vault and roster wiring.

No real model/API call is made.  The tool-call probe reuses the WIRE3
``ScriptedToolModel`` test double.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acceptor.acceptor import FrozenAcceptor
from acceptor.evidence import evidence_from_failed
from acceptor.frozen import freeze, normalise_package
from core.agentscope_actors import (
    build_roster_prompt,
    roster_from_table,
)
from core.agentscope_actors import agentscope_act
from core.runner import RunRecord, run_arm
from kb.lookup import DispatchKB
from tests.test_agentscope_actors import ScriptedToolModel

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO2 = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo2"
VAULT2 = PLATFORM_ROOT / "tests" / "fixtures" / "acceptor_vault2"
KB2 = PLATFORM_ROOT / "kb" / "dispatch_table_repo2.json"
KB1 = PLATFORM_ROOT / "kb" / "dispatch_table.json"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")

REPO2_FAILED = [
    "tests/test_bookings.py::test_book_and_cancel_releases_slot",
    "tests/test_bookings.py::test_rooms_do_not_share_availability",
    "tests/test_bookings.py::test_back_to_back_booking_in_same_room_is_allowed",
    "tests/test_bookings.py::test_cancelled_booking_no_longer_blocks_availability",
    "tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict",
    "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases",
]


def _repo2_roster():
    table = json.loads(KB2.read_text(encoding="utf-8"))
    return roster_from_table(table)


class AcceptorPackageTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen1-acceptor-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def _make_src_repo(self) -> Path:
        repo = self.tmp / "src_repo"
        package = repo / "src" / "room"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "rules.py").write_text(
            "ALLOWED = True\n", encoding="utf-8"
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_rules.py").write_text(
            "from room.rules import ALLOWED\n\n\n"
            "def test_allowed():\n"
            "    assert ALLOWED\n",
            encoding="utf-8",
        )
        (repo / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\npythonpath = src\n",
            encoding="utf-8",
        )
        return repo

    def _make_backoffice_repo(self) -> Path:
        repo = self.tmp / "legacy_repo"
        backoffice = repo / "backoffice"
        backoffice.mkdir(parents=True)
        (backoffice / "__init__.py").write_text("", encoding="utf-8")
        (backoffice / "rules.py").write_text(
            "ALLOWED = True\n", encoding="utf-8"
        )
        (repo / "tests").mkdir()
        (repo / "tests" / "test_rules.py").write_text(
            "from backoffice.rules import ALLOWED\n\n\n"
            "def test_allowed():\n"
            "    assert ALLOWED\n",
            encoding="utf-8",
        )
        (repo / "pytest.ini").write_text(
            "[pytest]\ntestpaths = tests\n", encoding="utf-8"
        )
        return repo

    def test_src_layout_package_is_frozen_and_judged(self) -> None:
        repo = self._make_src_repo()
        vault = self.tmp / "vault-src"
        manifest = freeze(repo, vault, package="src/room")

        self.assertEqual(manifest["package"], "src/room")
        self.assertIn("tests/test_rules.py", manifest["files"])
        acceptor = FrozenAcceptor(vault)
        self.assertEqual(acceptor.package, "src/room")
        self.assertEqual(acceptor.check(repo).status, "PASS")

        (repo / "src" / "room" / "rules.py").write_text(
            "ALLOWED = False\n", encoding="utf-8"
        )
        failed = acceptor.check(repo)
        self.assertEqual(failed.status, "FAIL")
        self.assertEqual(
            failed.failed_tests, ["tests/test_rules.py::test_allowed"]
        )

        test_file = repo / "tests" / "test_rules.py"
        test_file.write_text(
            test_file.read_text(encoding="utf-8") + "\n# tampered\n",
            encoding="utf-8",
        )
        self.assertEqual(acceptor.check(repo).status, "TAMPERED")

    def test_old_manifest_without_package_defaults_to_backoffice(self) -> None:
        repo = self._make_backoffice_repo()
        vault = self.tmp / "vault-demo"
        manifest = freeze(repo, vault)
        self.assertEqual(manifest["package"], "backoffice")

        on_disk = json.loads(
            (vault / "manifest.json").read_text(encoding="utf-8")
        )
        on_disk.pop("package")
        (vault / "manifest.json").write_text(
            json.dumps(on_disk, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        acceptor = FrozenAcceptor(vault)
        self.assertEqual(acceptor.package, "backoffice")
        self.assertEqual(acceptor.check(repo).status, "PASS")

    def test_package_escape_is_rejected(self) -> None:
        repo = self._make_src_repo()
        with self.assertRaises(ValueError):
            freeze(repo, self.tmp / "vault-bad", package="../escape")
        with self.assertRaises(ValueError):
            normalise_package("/absolute/package")

    def test_repo2_delivery_state_fails_exact_set(self) -> None:
        verdict = FrozenAcceptor(VAULT2).check(DEMO_REPO2)

        self.assertEqual(verdict.status, "FAIL")
        self.assertEqual(verdict.failed_tests, REPO2_FAILED)
        self.assertEqual(verdict.passed_count, 12)
        self.assertEqual(verdict.failed_count, 6)
        self.assertEqual(verdict.tampered_files, [])


class Repo2RosterAndLookupTest(unittest.TestCase):
    def test_lookup_assigns_every_repo2_failure(self) -> None:
        table = json.loads(KB2.read_text(encoding="utf-8"))
        plan = DispatchKB(table).lookup(evidence_from_failed(REPO2_FAILED))

        self.assertEqual(plan.unmapped, [])
        self.assertEqual(
            set(plan.assignments),
            {"booking-specialist", "scheduling-specialist"},
        )
        self.assertEqual(
            set(
                node
                for nodes in plan.assignments.values()
                for node in nodes
            ),
            set(REPO2_FAILED),
        )
        self.assertIn(
            "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases",
            plan.assignments["booking-specialist"],
        )
        self.assertIn(
            "tests/test_end_to_end.py::test_cross_room_back_to_back_then_cancel_releases",
            plan.assignments["scheduling-specialist"],
        )

    def test_repo2_roster_files_and_prompt(self) -> None:
        roster = _repo2_roster()

        self.assertEqual(roster.package, "src/studyroom")
        self.assertEqual(
            roster.allowed_files("scheduling-specialist"),
            ("src/studyroom/slots.py", "src/studyroom/conflicts.py"),
        )
        self.assertEqual(len(roster.allowed_files("generalist")), 4)

        prompt = build_roster_prompt(
            "scheduling-specialist",
            ["tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict"],
            REPO2_FAILED,
            roster,
        )
        self.assertIn("时段调度专家", prompt)
        self.assertIn("src/studyroom/slots.py", prompt)
        self.assertIn("src/studyroom/conflicts.py", prompt)
        self.assertIn("只允许修改", prompt)
        self.assertIn("背靠背预约被拒", prompt)

    def test_table_without_roster_is_rejected(self) -> None:
        table = json.loads(KB1.read_text(encoding="utf-8"))
        roster = roster_from_table(table)
        self.assertEqual(roster.package, "backoffice")
        self.assertIn("generalist", roster.specialists)
        bare = {k: v for k, v in table.items() if k != "roster"}
        with self.assertRaises(ValueError):
            roster_from_table(bare)


class ScopedRosterActorTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen1-scope-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.workspace = self.tmp / "workspace"
        self.package = self.workspace / "src" / "studyroom"
        self.package.mkdir(parents=True)
        for name in ("slots", "conflicts", "bookings", "members"):
            (self.package / f"{name}.py").write_text(
                "VALUE = 1\n", encoding="utf-8"
            )
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        self.roster = _repo2_roster()

    def _result(self, target: Path, new_value: str):
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Read",
                    "input": {"file_path": str(target)},
                },
                {
                    "type": "tool",
                    "name": "Edit",
                    "input": {
                        "file_path": str(target),
                        "old_string": "VALUE = 1",
                        "new_string": new_value,
                    },
                },
                {"type": "text", "text": "done"},
            ]
        )
        return agentscope_act(
            "scheduling-specialist",
            self.workspace,
            ["tests/test_conflicts.py::test_back_to_back_slots_do_not_conflict"],
            REPO2_FAILED,
            model=model,
            timeout_s=30,
            roster=self.roster,
        )

    def test_in_scope_edit_is_applied(self) -> None:
        target = self.package / "slots.py"

        result = self._result(target, "VALUE = 2")

        self.assertTrue(result.applied)
        self.assertIn("src/studyroom/slots.py", result.note)
        self.assertIn("VALUE = 2", target.read_text(encoding="utf-8"))

    def test_out_of_scope_edit_is_denied_and_file_unchanged(self) -> None:
        target = self.package / "members.py"
        original = target.read_text(encoding="utf-8")

        result = self._result(target, "VALUE = 2")

        self.assertFalse(result.applied)
        self.assertIn("changed=none", result.note)
        self.assertEqual(target.read_text(encoding="utf-8"), original)


class ProfileWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gen1-profile-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_core_cli_explicit_vault_and_kb_pass_through(self) -> None:
        from core.__main__ import main

        record = RunRecord(arm="C", kb_enabled=True, outcome="PASS")
        out = self.tmp / "repo2.json"
        argv = [
            "run",
            "--arm",
            "C",
            "--workspace",
            str(DEMO_REPO2),
            "--out",
            str(out),
            "--vault",
            str(VAULT2),
            "--kb",
            str(KB2),
        ]
        with mock.patch(
            "core.__main__.run_arm", return_value=record
        ) as run_mock:
            self.assertEqual(main(argv), 0)

        self.assertEqual(run_mock.call_args.kwargs["vault_dir"], VAULT2)
        self.assertEqual(run_mock.call_args.kwargs["kb_path"], KB2)

    def test_run_arm_passes_repo2_roster_to_native_backend(self) -> None:
        fix = mock.Mock()
        fix.applied = False
        fix.note = "mock"
        fix.actor_id = "scheduling-specialist"
        with mock.patch(
            "core.agentscope_actors.agentscope_act", return_value=fix
        ) as backend:
            record = run_arm(
                "C",
                DEMO_REPO2,
                max_rounds=1,
                work_root=self.tmp / "work",
                actor_backend="agentscope",
                vault_dir=VAULT2,
                kb_path=KB2,
            )

        self.assertEqual(record.outcome, "CAPPED")
        self.assertEqual(backend.call_count, 2)
        for call in backend.call_args_list:
            self.assertEqual(call.kwargs["roster"].package, "src/studyroom")


if __name__ == "__main__":
    unittest.main()
