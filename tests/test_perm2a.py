# -*- coding: utf-8 -*-
"""PERM2a: modal lines are program-generated; A/P rule effects."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.policy import PolicyEngine
from core.tui import _confirm_modal_lines, _confirm_rule_preview


class ModalLinesTests(unittest.TestCase):
    def test_lines_carry_program_facts_only(self) -> None:
        lines = _confirm_modal_lines(
            "/outside/secret.txt",
            question="秘书想读项目外文件 /outside/secret.txt，是否允许？",
            pending_reason="工作区外读取需要确认",
        )
        text = "\n".join(lines)
        self.assertIn("发起者：主治医生", text)
        self.assertIn("目标：/outside/secret.txt", text)
        self.assertIn("在当前项目之外", text)
        self.assertIn("P 永久放行（先预览规则）", text)

    def test_p_banned_lines_hide_p_and_explain(self) -> None:
        lines = _confirm_modal_lines(
            "/outside/x",
            question="q",
            p_allowed=False,
            p_ban_reason="敏感路径或凭据文件不许永久放行",
        )
        text = "\n".join(lines)
        self.assertNotIn("P 永久放行（先预览规则）", text)
        self.assertIn("永久放行不可用：敏感路径或凭据文件不许永久放行", text)

    def test_preview_shows_exact_rule_text(self) -> None:
        from core.policy import Rule, format_rule_toml

        rule = Rule("secretary", "cross_dir_read", "/outside/x", "allow")
        preview = "\n".join(_confirm_rule_preview(format_rule_toml(rule)))
        self.assertIn("[[rule]]", preview)
        self.assertIn('glob = "/outside/x"', preview)
        self.assertIn("再按一次 P", preview)


class RuleEffectTests(unittest.TestCase):
    def test_session_rule_stops_second_ask(self) -> None:
        from core.dispatcher import _SecretaryRead
        from core.policy import Rule

        with tempfile.TemporaryDirectory(prefix="perm2a-") as tmp:
            root = Path(tmp)
            workspace = root / "ws"
            workspace.mkdir()
            outside = root / "outside.txt"
            outside.write_text("x", encoding="utf-8")
            tool = _SecretaryRead(workspace, has_confirm_responder=True)
            first = tool._decision(
                {"file_path": str(outside)}, audit=False
            )
            self.assertEqual(first.verdict, "ask")
            glob = str(outside.resolve())
            tool._engine.add_session_rule(
                Rule("secretary", "cross_dir_read", glob, "allow")
            )
            second = tool._decision(
                {"file_path": str(outside)}, audit=False
            )
            self.assertEqual(second.verdict, "allow")

    def test_global_rule_applies_to_fresh_engine(self) -> None:
        from core.policy import Rule, append_global_rule

        with tempfile.TemporaryDirectory(prefix="perm2a-") as tmp:
            root = Path(tmp)
            policy_file = root / "policies.toml"
            append_global_rule(
                Rule(
                    "secretary",
                    "cross_dir_read",
                    str((root / "outside.txt").resolve()),
                    "allow",
                ),
                path=policy_file,
            )
            engine = PolicyEngine(
                workspace=root, global_policy_path=policy_file
            )
            decision = engine.evaluate(
                "secretary", "cross_dir_read", str(root / "outside.txt")
            )
            self.assertEqual(decision.verdict, "allow")


if __name__ == "__main__":
    unittest.main()
