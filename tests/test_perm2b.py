# -*- coding: utf-8 -*-
"""PERM2b1: mode semantics, auto-review checklist, pending list."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.policy import (
    PolicyContext,
    PolicyEngine,
    Rule,
    load_policies,
    remove_policy_rule,
)


def _ctx(workspace: Path, mode: str) -> PolicyContext:
    return PolicyContext(
        mode=mode,
        surface="interactive",
        workspace=workspace,
        vault_paths=(),
        role="secretary",
        is_ai=True,
        actor="secretary",
    )


class ModeSemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm2b-")
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name).resolve() / "ws"
        self.ws.mkdir(parents=True)
        (self.ws / "code.py").write_text("x = 1\n", encoding="utf-8")
        self.engine = PolicyEngine(workspace=self.ws)

    def test_ask_mode_write_inside_asks(self) -> None:
        d = self.engine.evaluate(
            "secretary", "write_file", str(self.ws / "code.py"),
            _ctx(self.ws, "ask"),
        )
        self.assertEqual(d.verdict, "ask")

    def test_auto_edit_write_inside_allows(self) -> None:
        d = self.engine.evaluate(
            "secretary", "edit_file", str(self.ws / "code.py"),
            _ctx(self.ws, "auto-edit"),
        )
        self.assertEqual(d.verdict, "allow")

    def test_auto_edit_cross_dir_write_still_asks(self) -> None:
        d = self.engine.evaluate(
            "secretary", "cross_dir_write", "/tmp/elsewhere.py",
            _ctx(self.ws, "auto-edit"),
        )
        self.assertEqual(d.verdict, "ask")

    def test_runtime_surface_rejects_non_ask_mode(self) -> None:
        ctx = PolicyContext(
            mode="auto-edit",
            surface="runtime",
            workspace=self.ws,
            vault_paths=(),
            role="worker",
            is_ai=True,
            actor="worker",
        )
        d = self.engine.evaluate(
            "worker", "read_file", str(self.ws / "code.py"), ctx
        )
        self.assertEqual(d.verdict, "deny")

    def test_unknown_mode_denied(self) -> None:
        d = self.engine.evaluate(
            "secretary", "read_file", str(self.ws / "code.py"),
            _ctx(self.ws, "bogus"),
        )
        self.assertEqual(d.verdict, "deny")


class AutoReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm2b-ar-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir(parents=True)
        (self.ws / "code.py").write_text("x = 1\n", encoding="utf-8")
        self.policy_file = self.root / "policies.toml"
        from core.policy import append_global_rule

        append_global_rule(
            Rule(
                "secretary",
                "read_file",
                str(self.ws / "code.py"),
                "ask",
            ),
            path=self.policy_file,
        )
        self.engine = PolicyEngine(
            workspace=self.ws, global_policy_path=self.policy_file
        )

    def test_ask_rule_parks_in_ask_mode(self) -> None:
        d = self.engine.evaluate(
            "secretary", "read_file", str(self.ws / "code.py"),
            _ctx(self.ws, "ask"),
        )
        self.assertEqual(d.verdict, "ask")

    def test_ask_rule_auto_allows_in_auto_review(self) -> None:
        d = self.engine.evaluate(
            "secretary", "read_file", str(self.ws / "code.py"),
            _ctx(self.ws, "auto-review"),
        )
        self.assertEqual(d.verdict, "allow")
        self.assertIn("[自动审核放行]", d.reason)

    def test_outside_read_never_auto_allows(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("o\n", encoding="utf-8")
        d = self.engine.evaluate(
            "secretary", "cross_dir_read", str(outside),
            _ctx(self.ws, "auto-review"),
        )
        self.assertEqual(d.verdict, "ask")

    def test_secret_read_stays_denied_in_auto_review(self) -> None:
        d = self.engine.evaluate(
            "secretary",
            "read_file",
            str(Path.home() / ".ssh" / "id_rsa"),
            _ctx(self.ws, "auto-review"),
        )
        self.assertEqual(d.verdict, "deny")

    def test_write_ask_never_auto_allows(self) -> None:
        d = self.engine.evaluate(
            "secretary", "write_file", str(self.ws / "code.py"),
            _ctx(self.ws, "auto-review"),
        )
        self.assertEqual(d.verdict, "ask")


class RemoveHelpersTests(unittest.TestCase):
    def test_remove_session_rule(self) -> None:
        engine = PolicyEngine()
        rule = Rule("secretary", "read_file", "/a", "allow")
        engine.add_session_rule(rule)
        self.assertTrue(engine.remove_session_rule(rule))
        self.assertFalse(engine.remove_session_rule(rule))

    def test_remove_policy_rule_rewrites_file(self) -> None:
        from core.policy import append_global_rule

        with tempfile.TemporaryDirectory(prefix="perm2b-rm-") as tmp:
            path = Path(tmp) / "policies.toml"
            first = Rule("secretary", "read_file", "/a", "ask")
            second = Rule("secretary", "read_file", "/b", "allow")
            append_global_rule(first, path=path)
            append_global_rule(second, path=path)
            removed = remove_policy_rule(path, 0)
            self.assertEqual(removed, first)
            self.assertEqual(load_policies(path), [second])
            with self.assertRaises(IndexError):
                remove_policy_rule(path, 5)


class PendingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm2b-pd-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir(parents=True)
        self.outside = self.root / "outside.txt"
        self.outside.write_text("pending-payload\n", encoding="utf-8")

    def test_record_dedupes_and_lists(self) -> None:
        from core.pending import list_pending, record_pending

        first = record_pending(
            self.ws, actor="secretary",
            action="cross_dir_read", target=str(self.outside),
        )
        second = record_pending(
            self.ws, actor="secretary",
            action="cross_dir_read", target=str(self.outside),
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list_pending(self.ws)), 1)

    def test_execute_unchanged_reads_and_clears(self) -> None:
        from core.pending import (
            execute_pending,
            list_pending,
            record_pending,
        )

        entry = record_pending(
            self.ws, actor="secretary",
            action="cross_dir_read", target=str(self.outside),
        )
        result = execute_pending(self.ws, entry["id"])
        self.assertEqual(result["status"], "executed")
        self.assertIn("pending-payload", result["content"])
        self.assertEqual(list_pending(self.ws), [])

    def test_execute_changed_needs_acknowledgement(self) -> None:
        from core.pending import execute_pending, record_pending

        entry = record_pending(
            self.ws, actor="secretary",
            action="cross_dir_read", target=str(self.outside),
        )
        self.outside.write_text("tampered\n", encoding="utf-8")
        result = execute_pending(self.ws, entry["id"])
        self.assertEqual(result["status"], "changed")
        result2 = execute_pending(
            self.ws, entry["id"], acknowledge_change=True
        )
        self.assertEqual(result2["status"], "executed")
        self.assertIn("tampered", result2["content"])

    def test_execute_refused_when_policy_now_denies(self) -> None:
        from core.pending import execute_pending, record_pending
        from core.policy import append_global_rule

        entry = record_pending(
            self.ws, actor="secretary",
            action="cross_dir_read", target=str(self.outside),
        )
        append_global_rule(
            Rule(
                "secretary", "cross_dir_read",
                str(self.outside), "deny",
            ),
            path=self.ws / ".drbug" / "policies.toml",
        )
        result = execute_pending(self.ws, entry["id"])
        self.assertEqual(result["status"], "refused")


class DispatcherHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_unanswered_ask_is_recorded(self) -> None:
        from core.dispatcher import _SecretaryRead
        from core.pending import list_pending

        with tempfile.TemporaryDirectory(prefix="perm2b-hook-") as tmp:
            root = Path(tmp).resolve()
            ws = root / "ws"
            ws.mkdir()
            outside = root / "outside.txt"
            outside.write_text("x\n", encoding="utf-8")
            tool = _SecretaryRead(ws, has_confirm_responder=False)
            decision = await tool.check_permissions(
                {"file_path": str(outside)}, None
            )
            self.assertEqual(decision.behavior.value, "deny")
            entries = list_pending(ws)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["target"], str(outside))

    async def test_answered_ask_is_not_recorded(self) -> None:
        from core.dispatcher import _SecretaryRead
        from core.pending import list_pending

        with tempfile.TemporaryDirectory(prefix="perm2b-hook-") as tmp:
            root = Path(tmp).resolve()
            ws = root / "ws"
            ws.mkdir()
            outside = root / "outside.txt"
            outside.write_text("x\n", encoding="utf-8")
            tool = _SecretaryRead(ws, has_confirm_responder=True)
            decision = await tool.check_permissions(
                {"file_path": str(outside)}, None
            )
            self.assertEqual(decision.behavior.value, "ask")
            self.assertEqual(list_pending(ws), [])


if __name__ == "__main__":
    unittest.main()
