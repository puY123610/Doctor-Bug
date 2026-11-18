"""TALK1：主治医生话术改版——说人话、角色名后不加括号标签、黑话换大白话。

断言读真实文本：主治医生系统提示词按 _dispatcher_prompt 真实渲染；四段会被
原样转给老板的工具返回（复现对照、定下来的回执、修复前后对照、名册）走
dispatcher / repro / roster 的真实拼字代码，只把底层实现换成替身结果。
"""

from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentscope.message import Msg, TextBlock

from core import dispatcher
from core.dispatcher import build_dispatcher
from core.repro import ReproComparison, comparison_lines
from core.roster import format_roster_listing, format_team_line
from core.tui import COMMANDS, POSITIONING

# 黑话词表：对外文案（提示词正文、四段工具返回、TUI 说明）里一律不许出现。
JARGON = (
    "冻结", "判卷", "辖区", "派单", "llm", "LLM",
    "策略臂", "证据改派", "金库", "执行后端",
)
GLOSSARY_HEAD = "【用词对照】"
INTERNAL_HEAD = "【内部设置】"
ROLE_NAMES = ("主治医生", "分诊员", "模块专家", "通用修理工")
ROLE_LABEL = re.compile(r"(主治医生|分诊员|模块专家|通用修理工|领域专家|通用修复员)\s*[（(]")


def _hits(text: str) -> list[str]:
    return [word for word in JARGON if word in text]


def _prompt(**kwargs) -> str:
    return dispatcher._dispatcher_prompt(**kwargs)


def _body(prompt: str) -> str:
    return prompt.split(GLOSSARY_HEAD, 1)[0]


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.prompts = {
            "bound": _prompt(default_actors="agentscope", workspace="/tmp/ws"),
            "unbound": _prompt(default_actors="llm", workspace=None),
        }

    # 1. 提示词与角色描述里没有括号标签
    def test_1_no_bracket_labels_after_role_names(self):
        for name, prompt in self.prompts.items():
            with self.subTest(prompt=name):
                self.assertNotIn("（只查不改）", prompt)
                self.assertEqual(ROLE_LABEL.findall(prompt), [])
                self.assertIn("角色名后面都不要加括号补充说明", prompt)

    # 2. 正文零黑话；对照表有标题行、覆盖工具返回里会出现的黑话词
    def test_2_body_has_no_jargon_and_glossary_covers_it(self):
        for name, prompt in self.prompts.items():
            with self.subTest(prompt=name):
                self.assertEqual(prompt.count(GLOSSARY_HEAD), 1)
                body = _body(prompt)
                self.assertEqual(_hits(body), [])
                glossary = prompt.split(GLOSSARY_HEAD, 1)[1].split(INTERNAL_HEAD, 1)[0]
                for word in ("冻结", "判卷", "冻结判卷", "冻结验收", "辖区",
                             "派单", "证据改派", "策略臂", "验收金库", "TAMPERED", "llm"):
                    self.assertIn(word, glossary, word)
                self.assertIn("负责的文件", glossary)
                self.assertIn("先说结论，再说怎么做", body)
                self.assertIn("「我」「你」「咱们」", body)

    def test_2b_tool_returns_jargon_is_covered_by_glossary(self):
        """accept_check 等不在四段里的工具返回仍可能带黑话：对照表必须都覆盖。"""
        glossary = self.prompts["bound"].split(GLOSSARY_HEAD, 1)[1]
        sources = (
            dispatcher.accept_check.__doc__ or "",
            dispatcher.run_repair.__doc__ or "",
            dispatcher.kb_explain.__doc__ or "",
        )
        for text in sources:
            for word in _hits(text):
                self.assertIn(word, glossary, word)

    # 4. 角色名本身都还在
    def test_4_role_names_still_present(self):
        for name, prompt in self.prompts.items():
            with self.subTest(prompt=name):
                for role in ROLE_NAMES:
                    self.assertIn(role, _body(prompt))
                self.assertIn("Doctor Bug", prompt)

    def test_tui_copy_is_plain(self):
        self.assertEqual(_hits(POSITIONING), [])
        for command, help_text in COMMANDS:
            self.assertEqual(_hits(help_text), [], command)


class FourSegmentsTest(unittest.TestCase):
    """3. 秘书原样转给老板的四段文字：真实拼字输出零黑话、零括号标签。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ws = self.tmp / "repo"
        self.ws.mkdir()

    def _check(self, text: str):
        self.assertEqual(_hits(text), [], text)
        self.assertEqual(ROLE_LABEL.findall(text), [])

    def test_3a_repro_comparison(self):
        comparison = ReproComparison(
            symptom="用券后总价不对", observed="实际 110", expected="应是 100"
        )
        result = SimpleNamespace(
            repo_path=self.ws, status="REPRO_READY", reason="ok",
            test_path=self.ws / "t.py", attempts=[{"verdict": "assertion_failed"}],
            comparison=comparison, test_source="def test_x():\n    assert 1 == 2",
            failing_output="E assert 1 == 2",
        )
        with mock.patch("core.repro.propose_repro", return_value=result):
            text = dispatcher.propose_repro(workspace=str(self.ws), symptom="x")
        self.assertIn("症状复现对照", text)
        self.assertIn("还没定下来", text)
        self._check(text)
        self._check("\n".join(comparison_lines(comparison)))

    def test_3b_fixed_receipts(self):
        frozen = SimpleNamespace(
            status="FROZEN", reason="", repo_test_path=self.ws / "tests/t.py",
            sha256="abc", profile_dir=self.tmp / "p", origin="repro",
            receipt_path=None, dispatch_table_path=self.tmp / "p/d.json",
            vault_dir=self.tmp / "p/vault",
        )
        with mock.patch("core.repro.freeze_repro", return_value=frozen):
            text = dispatcher.freeze_repro(workspace=str(self.ws), confirm=True)
        self.assertIn("已定下来", text)
        self.assertIn("直接判不通过", text)
        self._check(text)
        behavior = SimpleNamespace(
            status="FROZEN", reason="", script="checks/smoke.py", sha256="abc",
            profile_dir=self.tmp / "p", vault_dir=self.tmp / "p/vault",
        )
        with mock.patch("core.repro.freeze_behavior_check", return_value=behavior):
            text = dispatcher.freeze_behavior_check(
                workspace=str(self.ws), script="checks/smoke.py", confirm=True
            )
        self.assertIn("行为检查已定下来", text)
        self._check(text)

    def test_3c_repair_before_after(self):
        from core import repro

        lines = [
            "-- 修复后复验对照（与生成复现时同一组输入） --",
        ]
        source = Path(repro.__file__).read_text(encoding="utf-8")
        start = source.index("def repair_comparison_lines")
        end = source.index("\ndef ", start + 10)
        self._check(source[start:end].split('"""', 2)[-1])
        self._check("\n".join(lines))

    def test_3d_roster_listing(self):
        rows = [
            {"name": "billing-specialist", "role": "specialist", "source": "auto",
             "source_label": "自动", "module": "billing",
             "scope_files": ["backoffice/billing.py"], "has_archive": True},
            {"name": "generalist", "role": "generalist", "source": "auto",
             "module": "", "scope_files": [], "has_archive": False},
        ]
        text = format_roster_listing(rows)
        self.assertIn("负责的文件 backoffice/billing.py", text)
        self.assertIn("没有负责的文件", text)
        self._check(text)
        self._check(format_roster_listing([]))
        self._check(format_team_line(rows))

    def test_3e_secretary_relays_roster_without_jargon(self):
        """模拟秘书回答：真 build_dispatcher 调 list_roster，把工具返回原样转述。"""
        from tests.test_roster import ScriptedToolModel, _tool_result_texts

        model = ScriptedToolModel(
            [
                {"type": "tool", "name": "list_roster",
                 "input": {"workspace": str(self.ws)}},
                {"type": "text", "text": "名册已转达"},
            ]
        )
        agent = build_dispatcher(model=model, workspace=str(self.ws))
        asyncio.run(
            agent.reply(
                Msg(name="用户", role="user", content=[TextBlock(text="介绍下团队")])
            )
        )
        relayed = _tool_result_texts(model.seen_messages[-1])
        self.assertTrue(relayed)
        for text in relayed:
            self._check(text)
        system = model.seen_messages[0][0]
        self.assertEqual(ROLE_LABEL.findall(str(system.content)), [])


if __name__ == "__main__":
    unittest.main()
