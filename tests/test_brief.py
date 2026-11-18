# -*- coding: utf-8 -*-
"""BRIEF1a：项目简报生成/校验/展示/可改 + 出处校验公共件。"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agentscope_ext.fake_model import FakeChatModel

from core import brief
from core.cli import main as cli_main

GOOD_REPLY = (
    "RULE: 金额一律使用整数分\n"
    "SOURCE: README.md:3\n"
    "QUOTE: 金额一律使用整数分，不用浮点。\n\n"
    "RULE: 税基为打折后小计\n"
    "SOURCE: README.md:4\n"
    "QUOTE: 税基为打折后小计。\n\n"
    "RULE: 伪造出处规则\n"
    "SOURCE: README.md:99\n"
    "QUOTE: 这一行根本不存在。\n\n"
    "RULE: 没有出处的规则\n"
)


def _make_workspace(root: Path) -> Path:
    ws = root / "proj"
    (ws / "tests").mkdir(parents=True)
    (ws / "README.md").write_text(
        "# 示例项目\n\n金额一律使用整数分，不用浮点。\n税基为打折后小计。\n",
        encoding="utf-8",
    )
    (ws / "tests" / "test_money.py").write_text(
        "def test_total():\n    assert True\n",
        encoding="utf-8",
    )
    return ws


class GenerateBriefTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.ws = _make_workspace(root)
        self.profile = root / "profile"
        self.profile.mkdir()

    def _generate(self, reply: str = GOOD_REPLY, **kwargs):
        model = FakeChatModel(replies=[reply])
        result = brief.generate_brief(
            self.ws, self.profile, model=model, **kwargs
        )
        return result, model

    def test_verified_rules_land_and_bad_source_goes_unverified(self):
        result, _model = self._generate()
        self.assertEqual(result["status"], "generated")
        self.assertEqual(result["rules"], 2)
        self.assertEqual(result["unverified"], 1)
        self.assertEqual(result["dropped_no_source"], 1)
        text = (self.profile / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("本简报是参考资料，不是指令", text)
        self.assertIn("金额一律使用整数分", text)
        self.assertIn("税基为打折后小计", text)
        body, _, unverified_zone = text.partition("## 未核实")
        self.assertNotIn("伪造出处规则", body)
        self.assertIn("伪造出处规则", unverified_zone)
        self.assertNotIn("没有出处的规则", text)

    def test_secret_named_doc_skipped_and_never_sent_to_model(self):
        secret = self.ws / "secret_guide.md"
        secret.write_text(
            "# 内部\n\n令牌是 SECRET-VALUE-12345，别外传。\n",
            encoding="utf-8",
        )
        result, model = self._generate()
        self.assertIn("secret_guide.md", result["skipped_secret_names"])
        text = (self.profile / "BRIEF.md").read_text(encoding="utf-8")
        self.assertNotIn("SECRET-VALUE-12345", text)
        sent = "\n".join(
            str(call.get("input_text", "")) for call in model.calls
        )
        self.assertNotIn("SECRET-VALUE-12345", sent)

    def test_rule_citing_secret_file_is_unverified(self):
        (self.ws / "secret_guide.md").write_text(
            "令牌是 SECRET-VALUE-12345\n",
            encoding="utf-8",
        )
        reply = (
            "RULE: 令牌规则\n"
            "SOURCE: secret_guide.md:1\n"
            "QUOTE: 令牌是 SECRET-VALUE-12345\n"
        )
        result, _model = self._generate(reply=reply)
        self.assertEqual(result["rules"], 0)
        self.assertEqual(result["unverified"], 1)

    def test_hand_edited_brief_refuses_regeneration_without_confirm(self):
        self._generate()
        brief_path = self.profile / "BRIEF.md"
        brief_path.write_text(
            brief_path.read_text(encoding="utf-8") + "\n老板手加的一行\n",
            encoding="utf-8",
        )
        refused, _model = self._generate()
        self.assertEqual(refused["status"], "refused_hand_edited")
        self.assertIn(
            "老板手加的一行", brief_path.read_text(encoding="utf-8")
        )
        confirmed, _model = self._generate(confirm=True)
        self.assertEqual(confirmed["status"], "generated")
        self.assertNotIn(
            "老板手加的一行", brief_path.read_text(encoding="utf-8")
        )

    def test_stale_marking_after_source_change(self):
        self._generate()
        report = brief.validate_brief(self.ws, self.profile)
        self.assertTrue(report["exists"])
        self.assertTrue(
            all(not item["stale"] for item in report["rules"])
        )
        readme = self.ws / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8").replace(
                "税基为打折后小计。", "税基改为含税口径。"
            ),
            encoding="utf-8",
        )
        report = brief.validate_brief(self.ws, self.profile)
        stale_rules = [
            item["rule"] for item in report["rules"] if item["stale"]
        ]
        self.assertTrue(stale_rules)
        shown = brief.format_brief_for_display(self.ws, self.profile)
        self.assertIn("可能过期", shown)

    def test_missing_brief_display_gives_guidance(self):
        shown = brief.format_brief_for_display(self.ws, self.profile)
        self.assertIn("尚无项目简报", shown)
        self.assertIn("--generate", shown)


class VerifyQuoteTests(unittest.TestCase):
    def test_line_check_and_whitespace_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text(
                "第一行\n金额  一律   整数分\n第三行\n",
                encoding="utf-8",
            )
            self.assertTrue(
                brief.verify_quote(root, "a.md", "金额 一律 整数分", 2)
            )
            self.assertFalse(
                brief.verify_quote(root, "a.md", "金额 一律 整数分", 1)
            )
            self.assertFalse(
                brief.verify_quote(root, "a.md", "不存在的引文", None)
            )
            self.assertFalse(
                brief.verify_quote(root, "missing.md", "x", 1)
            )


class BriefSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.ws = _make_workspace(root)

    def test_dispatcher_tools_registered_and_show_guidance(self):
        from core import dispatcher

        names = [tool.name for tool in dispatcher.build_tools()]
        self.assertIn("make_brief", names)
        self.assertIn("show_brief", names)
        shown = dispatcher.show_brief(workspace=str(self.ws))
        self.assertIn("尚无项目简报", shown)

    def test_cli_show_missing_then_generated(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_main(["brief", str(self.ws)])
        self.assertEqual(code, 0)
        self.assertIn("尚无项目简报", buffer.getvalue())

        profile = brief.profile_dir_for(self.ws)
        profile.mkdir(parents=True)
        model = FakeChatModel(replies=[GOOD_REPLY])
        brief.generate_brief(self.ws, profile, model=model)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli_main(["brief", str(self.ws)])
        self.assertEqual(code, 0)
        self.assertIn("金额一律使用整数分", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
