"""PolicyEngine 行为测试（PERM1A，PERM1B1 补 ctx 回归）。

引擎接口以 core/policy.py 为准。复合命令走 evaluate_command()。
PERM1B1 起必须能把 ctx 传进去：此前海象赋值被短路后 del 会抛
UnboundLocalError。
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from core.policy import (
    PolicyConfigError,
    PolicyContext,
    PolicyEngine,
    Rule,
    dump_policies,
    load_policies,
    project_policy_path,
)

ALLOW_ALL = Rule("*", "*", "*", "allow")


class _WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.global_policy = self.ws / "global-policies.toml"
        self.engine = PolicyEngine(
            workspace=self.ws,
            global_policy_path=self.global_policy,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _allow_everything(self) -> None:
        dump_policies(self.global_policy, [ALLOW_ALL])
        dump_policies(project_policy_path(self.ws), [ALLOW_ALL])
        self.engine.add_session_rule(ALLOW_ALL)

    def _context(
        self,
        *,
        role: str = "secretary",
        actor: str = "secretary",
        vaults: tuple[Path, ...] = (),
    ) -> PolicyContext:
        return PolicyContext(
            mode="ask",
            surface="runtime" if role == "worker" else "interactive",
            workspace=self.ws,
            vault_paths=vaults,
            role=role,
            is_ai=True,
            actor=actor,
        )


class Tier1DenyTests(_WorkspaceCase):
    """最宽松 toml（再加一条会话级全放行）盖不住 Tier 1。"""

    def setUp(self) -> None:
        super().setUp()
        self._allow_everything()
        self.vault = self.ws / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("dossier", encoding="utf-8")
        self.ctx = self._context(vaults=(self.vault,))

    def test_vault_write_still_denied(self) -> None:
        decision = self.engine.decide(
            "secretary",
            "write_file",
            str(self.vault / "note.md"),
            self.ctx,
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)
        self.assertIn("卷宗", decision.reason)

    def test_ssh_read_still_denied(self) -> None:
        decision = self.engine.decide(
            "secretary",
            "read_file",
            "~/.ssh/id_ed25519",
            self.ctx,
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)
        self.assertIn("密钥", decision.reason)

    def test_worker_test_file_write_still_denied(self) -> None:
        worker = self._context(role="worker", actor="coder", vaults=(self.vault,))
        for action, target in (
            ("write_file", self.ws / "tests" / "test_sample.py"),
            ("edit_file", self.ws / "test_sample.py"),
        ):
            with self.subTest(action=action, target=target.name):
                decision = self.engine.decide("coder", action, str(target), worker)
                self.assertEqual(decision.verdict, "deny")
                self.assertEqual(decision.tier, 1)
                self.assertIn("测试", decision.reason)

    def test_destructive_rm_still_denied(self) -> None:
        for command in ("rm -rf ./tmp_test", "rm -rf .git"):
            with self.subTest(command=command):
                decision = self.engine.evaluate_command(command)
                self.assertEqual(decision.verdict, "deny")
                self.assertEqual(decision.tier, 1)
                self.assertTrue(decision.never_permanent)
                self.assertIn("高危", decision.reason)


class CompoundCommandTests(_WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self._allow_everything()

    def test_pytest_and_rm_denied_on_rm_segment(self) -> None:
        command = "pytest && rm -rf ./tmp_test"
        decision = self.engine.evaluate_command(command)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)
        self.assertTrue(decision.never_permanent)
        denied = [segment for segment in decision.segments if segment.verdict == "deny"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].text, "rm -rf ./tmp_test")
        self.assertEqual(denied[0].tier, 1)
        allowed = [segment for segment in decision.segments if segment.verdict == "allow"]
        self.assertTrue(any(segment.text == "pytest" for segment in allowed))

    def test_git_status_and_pytest_not_denied(self) -> None:
        command = "git status && pytest -q"
        decision = self.engine.evaluate_command(command)
        self.assertEqual(decision.verdict, "allow")
        self.assertGreaterEqual(len(decision.segments), 2)
        self.assertTrue(all(segment.verdict != "deny" for segment in decision.segments))
        self.assertEqual(
            [segment.text for segment in decision.segments],
            ["git status", "pytest -q"],
        )


class PolicyFileTests(_WorkspaceCase):
    def test_never_permanent_decision_with_note_raises(self) -> None:
        path = self.ws / "never.toml"
        path.write_text(
            "\n".join(
                [
                    "[[rule]]",
                    'actor = "*"',
                    'action = "*"',
                    'glob = "*"',
                    'decision = "never_permanent"',
                    'note = "备注"',
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with self.assertRaises(PolicyConfigError):
            load_policies(path)

    def test_corrupt_toml_raises(self) -> None:
        path = self.ws / "broken.toml"
        path.write_text("[[rule]\nthis is not toml\n", encoding="utf-8")
        with self.assertRaises(PolicyConfigError):
            load_policies(path)

    def test_corrupt_policy_decide_returns_deny(self) -> None:
        path = self.ws / "broken.toml"
        path.write_text("[[rule]\nthis is not toml\n", encoding="utf-8")
        engine = PolicyEngine(workspace=self.ws, global_policy_path=path)
        readme = self.ws / "readme.txt"
        readme.write_text("hello", encoding="utf-8")
        ctx = self._context()
        with self.assertRaises(PolicyConfigError):
            engine.evaluate("secretary", "read_file", str(readme), ctx)
        decision = engine.decide("secretary", "read_file", str(readme), ctx)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 3)
        self.assertIn("损坏", decision.reason)


class AuditTests(_WorkspaceCase):
    def test_allow_and_deny_both_logged(self) -> None:
        readme = self.ws / "readme.txt"
        readme.write_text("hello", encoding="utf-8")
        ctx = self._context()
        allowed = self.engine.decide("secretary", "read_file", str(readme), ctx)
        denied = self.engine.decide(
            "secretary", "read_file", "~/.ssh/id_ed25519", ctx
        )
        self.assertEqual(allowed.verdict, "allow")
        self.assertEqual(denied.verdict, "deny")

        log = self.ws / ".drbug" / "audit.log"
        self.assertTrue(log.is_file())
        self.assertEqual(log.parent, self.ws / ".drbug")
        records = [
            json.loads(line)
            for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(records), 2)
        self.assertEqual([item["verdict"] for item in records], ["allow", "deny"])
        for record, action_target in zip(
            records,
            (("read_file", str(readme)), ("read_file", "~/.ssh/id_ed25519")),
        ):
            self.assertEqual(
                set(record),
                {"ts", "actor", "action", "target", "verdict", "tier", "reason"},
            )
            self.assertEqual(record["actor"], "secretary")
            self.assertEqual(record["action"], action_target[0])
            self.assertEqual(record["target"], action_target[1])
            self.assertIsInstance(record["tier"], int)
            self.assertTrue(record["reason"])
            parsed = datetime.fromisoformat(record["ts"])
            self.assertIsNotNone(parsed.tzinfo)


class DefaultDenyTests(_WorkspaceCase):
    def test_unknown_action_denied(self) -> None:
        decision = self.engine.decide(
            "secretary", "teleport", str(self.ws / "readme.txt"), self._context()
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 4)
        self.assertIn("未知动作", decision.reason)

    def test_bad_action_parameter_denied(self) -> None:
        decision = self.engine.decide(
            "secretary", None, str(self.ws / "readme.txt"), self._context()
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertIn("未知动作", decision.reason)


def _run_policy(*argv: str) -> tuple[int, str]:
    from core.cli import main

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(["policy", "check", *argv])
    return code, buffer.getvalue()


class PolicyCheckCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = Path(".drbug") / "audit.log"
        self._existed = self.audit.exists()
        self._before = self.audit.read_bytes() if self._existed else None

    def tearDown(self) -> None:
        if self._existed:
            self.assertEqual(self.audit.read_bytes(), self._before)
        else:
            self.assertFalse(self.audit.exists())

    def test_read_ssh_prints_deny(self) -> None:
        code, text = _run_policy("read", "~/.ssh/id_ed25519")
        self.assertEqual(code, 0)
        self.assertIn("verdict: deny", text)
        self.assertIn("tier: 1", text)

    def test_read_ssh_with_actor_prints_deny(self) -> None:
        code, text = _run_policy(
            "read", "~/.ssh/id_ed25519", "--actor", "secretary"
        )
        self.assertEqual(code, 0)
        self.assertIn("verdict: deny", text)
        self.assertIn("tier: 1", text)

    def test_write_demo_vault_prints_deny(self) -> None:
        from core.policy import discover_vaults

        vaults = discover_vaults(Path.cwd())
        self.assertTrue(vaults)
        code, text = _run_policy("write", str(vaults[0]))
        self.assertEqual(code, 0)
        self.assertIn("verdict: deny", text)
        self.assertIn("tier: 1", text)


class EvaluateCommandContextTests(_WorkspaceCase):
    """传入 ctx 不得抛 UnboundLocalError。PERM1B1 第 0 步回归。"""

    def setUp(self) -> None:
        super().setUp()
        self._allow_everything()
        self.ctx = self._context(role="worker", actor="worker")

    def test_decide_destructive_compound_with_ctx_denies(self) -> None:
        decision = self.engine.decide(
            "worker",
            "shell_exec",
            "pytest && rm -rf ./tmp_test",
            self.ctx,
        )
        self.assertEqual(decision.verdict, "deny")

    def test_evaluate_command_benign_compound_with_ctx_allows(self) -> None:
        decision = self.engine.evaluate_command("git status && pytest -q", self.ctx)
        self.assertEqual(decision.verdict, "allow")


class AppendGlobalRuleTests(unittest.TestCase):
    def test_round_trip_through_load_policies(self) -> None:
        from core.policy import Rule, append_global_rule, load_policies

        with tempfile.TemporaryDirectory(prefix="perm2a-") as tmp:
            path = Path(tmp) / "policies.toml"
            rule = Rule(
                actor="secretary",
                action="cross_dir_read",
                glob="/tmp/outside.txt",
                decision="allow",
            )
            append_global_rule(rule, path=path)
            loaded = load_policies(path)
            self.assertEqual(loaded, [rule])

    def test_append_preserves_existing_rules(self) -> None:
        from core.policy import Rule, append_global_rule, load_policies

        with tempfile.TemporaryDirectory(prefix="perm2a-") as tmp:
            path = Path(tmp) / "policies.toml"
            first = Rule("secretary", "read_file", "/a", "ask")
            second = Rule("secretary", "cross_dir_read", "/b", "allow")
            append_global_rule(first, path=path)
            append_global_rule(second, path=path)
            self.assertEqual(load_policies(path), [first, second])

    def test_append_refuses_corrupt_file(self) -> None:
        from core.policy import (
            PolicyConfigError,
            Rule,
            append_global_rule,
        )

        with tempfile.TemporaryDirectory(prefix="perm2a-") as tmp:
            path = Path(tmp) / "policies.toml"
            path.write_text("this is = = not toml [[[\n", encoding="utf-8")
            before = path.read_text(encoding="utf-8")
            with self.assertRaises(PolicyConfigError):
                append_global_rule(
                    Rule("secretary", "cross_dir_read", "/b", "allow"),
                    path=path,
                )
            self.assertEqual(path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
