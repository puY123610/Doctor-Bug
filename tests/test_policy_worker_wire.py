"""工人面 PolicyEngine 接线（PERM1B1）。全离线，不调用模型。

辖区内的读/写仍走原来的辖区结论。Tier 1（卷宗写入、密钥路径、
测试文件写入）在辖区放行之前被拒，目标文件逐字节不动。引擎给出
ask 时，工人面没有确认通道，落成 deny。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from agentscope.permission import PermissionBehavior

from core.agentscope_actors import (
    _SCOPE_ACTOR,
    _SCOPE_DENIALS,
    build_state,
    build_toolkit,
)
from core.policy import Decision


class WorkerPolicyWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.src = self.ws / "pkg" / "mod.py"
        self.src.parent.mkdir(parents=True)
        self.src.write_bytes(b"x = 1\n")
        self.other = self.ws / "pkg" / "other.py"
        self.other.write_bytes(b"y = 2\n")
        self.ssh = self.ws / ".ssh" / "id_rsa"
        self.ssh.parent.mkdir()
        self.ssh.write_bytes(b"fixture-not-a-real-key\n")
        self.test_file = self.ws / "tests" / "test_sample.py"
        self.test_file.parent.mkdir()
        self.test_file.write_bytes(b"def test_sample():\n    pass\n")
        self.note = (
            self.ws / ".drbug" / "profiles" / "demo" / "vault" / "note.md"
        )
        self.note.parent.mkdir(parents=True)
        self.note.write_bytes(b"dossier-bytes\n")
        # Tier 1 路径也放进名册，证明拒绝来自引擎而不是辖区清单。
        self.toolkit = build_toolkit(
            self.ws,
            (
                "pkg/mod.py",
                ".ssh/id_rsa",
                "tests/test_sample.py",
                ".drbug/profiles/demo/vault/note.md",
            ),
        )
        self.permission = build_state(self.ws).permission_context
        self.denials: list[dict] = []
        self._denial_token = _SCOPE_DENIALS.set(self.denials)
        self._actor_token = _SCOPE_ACTOR.set("coder")

    def tearDown(self) -> None:
        _SCOPE_ACTOR.reset(self._actor_token)
        _SCOPE_DENIALS.reset(self._denial_token)
        self._tmp.cleanup()

    def _tool(self, name: str, toolkit=None):
        box = self.toolkit if toolkit is None else toolkit
        return asyncio.run(box.get_tool(name))

    def _check(self, tool, path: str | Path):
        return asyncio.run(
            tool.check_permissions(
                {"file_path": str(path)},
                self.permission,
            )
        )

    def test_in_scope_read_and_write_still_pass(self) -> None:
        before = self.src.read_bytes()
        read = self._check(self._tool("Read"), self.src)
        write = self._check(self._tool("Write"), self.src)
        edit = self._check(self._tool("Edit"), self.src)
        self.assertEqual(read.behavior, PermissionBehavior.PASSTHROUGH)
        self.assertEqual(write.behavior, PermissionBehavior.ALLOW)
        self.assertEqual(edit.behavior, PermissionBehavior.ALLOW)
        self.assertEqual(self.denials, [])
        self.assertEqual(self.src.read_bytes(), before)

    def test_roster_denial_outside_scope_is_unchanged(self) -> None:
        before = self.other.read_bytes()
        decision = self._check(self._tool("Write"), self.other)
        self.assertEqual(decision.behavior, PermissionBehavior.DENY)
        self.assertEqual(
            self.denials,
            [
                {
                    "actor_id": "coder",
                    "tool": "Write",
                    "path": str(self.other),
                    "reason": "path outside the roster files scope",
                }
            ],
        )
        self.assertIn("roster scope", decision.message)
        self.assertEqual(self.other.read_bytes(), before)

    def test_tier1_targets_denied_and_bytes_unchanged(self) -> None:
        cases = (
            ("Write", self.note, "卷宗"),
            ("Edit", self.note, "卷宗"),
            ("Read", self.ssh, "密钥"),
            ("Write", self.ssh, "密钥"),
            ("Edit", self.ssh, "密钥"),
            ("Write", self.test_file, "测试"),
            ("Edit", self.test_file, "测试"),
            ("Write", "~/.ssh/id_rsa", "密钥"),
            ("Read", "~/.ssh/id_ed25519", "密钥"),
        )
        for tool_name, path, marker in cases:
            with self.subTest(tool=tool_name, path=str(path)):
                self.denials.clear()
                before = (
                    Path(path).read_bytes()
                    if not str(path).startswith("~") and Path(path).is_file()
                    else None
                )
                decision = self._check(self._tool(tool_name), path)
                self.assertEqual(decision.behavior, PermissionBehavior.DENY)
                self.assertIn(marker, decision.decision_reason)
                self.assertEqual(len(self.denials), 1)
                self.assertEqual(self.denials[0]["reason"], decision.decision_reason)
                self.assertEqual(self.denials[0]["tool"], tool_name)
                self.assertEqual(self.denials[0]["actor_id"], "coder")
                if before is not None:
                    self.assertEqual(Path(path).read_bytes(), before)

    def test_ask_on_worker_face_is_deny_and_file_untouched(self) -> None:
        before = self.src.read_bytes()
        write = self._tool("Write")

        def fake_decide(actor, action, target, ctx=None):
            return Decision(
                "ask",
                4,
                "需要确认后才能写",
                normalized_target=str(target),
            )

        write._policy_engine.decide = fake_decide
        decision = self._check(write, self.src)
        self.assertEqual(decision.behavior, PermissionBehavior.DENY)
        self.assertEqual(decision.decision_reason, "需要确认后才能写")
        self.assertEqual(self.src.read_bytes(), before)
        self.assertEqual(self.denials[-1]["reason"], "需要确认后才能写")
        self.assertEqual(self.denials[-1]["path"], str(self.src))
        self.assertNotIn("roster", decision.message)

    def test_unscoped_write_toolkit_is_not_the_roster_gate(self) -> None:
        """allowed_files 缺省时 Write/Edit 仍是框架原件，scripted 路径不经这里。"""

        plain = build_toolkit(self.ws)
        self.assertEqual(type(self._tool("Write", plain)).__name__, "Write")
        self.assertEqual(type(self._tool("Edit", plain)).__name__, "Edit")
        read = self._tool("Read", plain)
        self.assertEqual(type(read).__name__, "_PolicyRead")
        before = self.ssh.read_bytes()
        decision = self._check(read, self.ssh)
        self.assertEqual(decision.behavior, PermissionBehavior.DENY)
        self.assertIn("密钥", decision.decision_reason)
        self.assertEqual(self.ssh.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
