# -*- coding: utf-8 -*-
"""Backlog 6b: secretary behavior-check freeze (impl + dispatcher tool)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptor.acceptor import FrozenAcceptor
from core import dispatcher
from core.repro import freeze_behavior_check

SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
    "from app.values import VALUE\n"
    "raise SystemExit(0 if VALUE == 42 else 1)\n"
)


class BehaviorSecretaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="behav-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()
        self.repo = root / "repo"
        (self.repo / "app").mkdir(parents=True)
        (self.repo / "checks").mkdir()
        (self.repo / "app" / "values.py").write_text(
            "VALUE = 42\n", encoding="utf-8"
        )
        (self.repo / "checks" / "smoke.py").write_text(
            SCRIPT, encoding="utf-8"
        )
        self.profiles = root / "profiles"

    def _break_repo(self) -> None:
        (self.repo / "app" / "values.py").write_text(
            "VALUE = 7\n", encoding="utf-8"
        )

    def test_impl_freeze_and_verdict_flips(self) -> None:
        result = freeze_behavior_check(
            self.repo, "checks/smoke.py", profiles_root=self.profiles
        )
        self.assertEqual(result.status, "FROZEN")
        self.assertTrue(result.sha256)
        self.assertTrue((result.profile_dir / "profile.json").is_file())
        self.assertTrue(
            (result.profile_dir / "dispatch_table.json").is_file()
        )
        acceptor = FrozenAcceptor(result.vault_dir)
        self.assertEqual(acceptor.check(self.repo).status, "PASS")
        self._break_repo()
        self.assertEqual(acceptor.check(self.repo).status, "FAIL")

    def test_impl_refuses_existing_profile(self) -> None:
        first = freeze_behavior_check(
            self.repo, "checks/smoke.py", profiles_root=self.profiles
        )
        self.assertEqual(first.status, "FROZEN")
        again = freeze_behavior_check(
            self.repo, "checks/smoke.py", profiles_root=self.profiles
        )
        self.assertEqual(again.status, "REFUSED")

    def test_impl_refuses_escape_and_secret(self) -> None:
        escape = freeze_behavior_check(
            self.repo, "../outside.py", profiles_root=self.profiles
        )
        self.assertEqual(escape.status, "REFUSED")
        (self.repo / ".env").write_text("X=1\n", encoding="utf-8")
        secret = freeze_behavior_check(
            self.repo, ".env", profiles_root=self.profiles
        )
        self.assertEqual(secret.status, "REFUSED")
        missing = freeze_behavior_check(
            self.repo, "checks/nope.py", profiles_root=self.profiles
        )
        self.assertEqual(missing.status, "REFUSED")

    def test_tool_confirm_gate_then_accept_flips(self) -> None:
        refused = dispatcher.freeze_behavior_check(
            workspace=str(self.repo),
            script="checks/smoke.py",
            confirm=False,
        )
        self.assertIn("拒绝", refused)
        frozen = dispatcher.freeze_behavior_check(
            workspace=str(self.repo),
            script="checks/smoke.py",
            confirm=True,
        )
        self.assertIn("行为检查已定下来", frozen)
        verdict = dispatcher.accept_check(workspace=str(self.repo))
        self.assertIn("PASS", verdict)
        self._break_repo()
        verdict = dispatcher.accept_check(workspace=str(self.repo))
        self.assertIn("FAIL", verdict)


if __name__ == "__main__":
    unittest.main()
