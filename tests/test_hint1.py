"""HINT1：输入框提示按会话状态确定性生成，提示的都是真能用的动作。

每个状态都用真实条件摆出来（空目录 / 有项目特征 / 写档案文件 / 写运行记录 /
真结果包回调 / 真确认通道），断言读输入框实际渲染出的 placeholder。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from agentscope_ext.fake_model import FakeChatModel
from core.autoprofile import default_profile_name
from core.tui import (
    COMMANDS,
    HINT_ACTIONS,
    HINT_CONFIRM,
    HINT_IDLE,
    HINT_NO_PROFILE,
    HINT_NO_RUN,
    HINT_RESULT,
    HINT_TEXTS,
    HINT_UNBOUND,
    PLACEHOLDER_EXAMPLES,
    CifixChatApp,
    PromptInput,
)
from tests.test_talk1 import JARGON, ROLE_LABEL

COMMAND_NAMES = {name for name, _ in COMMANDS}


def _placeholder(app: CifixChatApp) -> str:
    return str(app.screen.query_one(PromptInput).placeholder)


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _project(self) -> Path:
        project = self.tmp / "acme"
        project.mkdir()
        (project / "requirements.txt").write_text("x\n", encoding="utf-8")
        return project

    def _make_profile(self, project: Path) -> None:
        table = (
            project / ".drbug" / "profiles" / default_profile_name(project)
            / "dispatch_table.json"
        )
        table.parent.mkdir(parents=True)
        table.write_text("{}", encoding="utf-8")

    def _make_run(self, project: Path) -> None:
        runs = project / ".drbug" / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "tui_run_1.json").write_text("{}", encoding="utf-8")

    def _app(self, root: Path) -> CifixChatApp:
        return CifixChatApp(model=FakeChatModel(fallback_reply="x"), workspace_root=root)


class EachStateTest(_Case):
    # 1. 每个状态下，输入框里渲染出的就是该状态那条提示
    async def test_1_each_state_renders_its_hint(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        app = self._app(empty)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_UNBOUND)
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_UNBOUND])

        project = self._project()
        app = self._app(project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_NO_PROFILE)
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_NO_PROFILE])

            self._make_profile(project)
            app.refresh_placeholder()
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_NO_RUN)
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_NO_RUN])

            self._make_run(project)
            app.refresh_placeholder()
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_IDLE)
            idle = _placeholder(app)
            self.assertTrue(idle.startswith("问点什么，或让我办事…"))
            self.assertTrue(any(e in idle for e in PLACEHOLDER_EXAMPLES))

            bundle = self.tmp / "bundle"
            bundle.mkdir()
            app._on_bundle_written(bundle)  # 真结果包回调
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_RESULT)
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_RESULT])

            # 真确认通道：等确认期间提示是确认那条，答完回到原状态
            waiting = asyncio.ensure_future(app._present_confirm())
            await pilot.pause()
            self.assertEqual(app.hint_state(), HINT_CONFIRM)
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_CONFIRM])
            app._resolve_confirm("n")
            await waiting
            await pilot.pause()
            self.assertEqual(_placeholder(app), HINT_TEXTS[HINT_RESULT])


class SwitchTest(_Case):
    # 2. 同一会话状态变了，下一次渲染的提示跟着变，且各对各的状态
    async def test_2_hint_follows_state_change(self):
        project = self._project()
        app = self._app(project)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            before = _placeholder(app)
            self._make_profile(project)
            app._rotate_placeholder()  # 原有的定时刷新入口
            await pilot.pause()
            after = _placeholder(app)
        self.assertNotEqual(before, after)
        self.assertEqual(before, HINT_TEXTS[HINT_NO_PROFILE])
        self.assertEqual(after, HINT_TEXTS[HINT_NO_RUN])


class ActionsAreRealTest(unittest.TestCase):
    # 3. 每条提示点名的动作都在真实命令表里（Y/N 是确认通道的真实答复）
    def test_3_named_actions_exist(self):
        self.assertEqual(set(HINT_ACTIONS), set(HINT_TEXTS) | {HINT_IDLE})
        for state, text in HINT_TEXTS.items():
            with self.subTest(state=state):
                actions = HINT_ACTIONS[state]
                self.assertTrue(actions)
                for action in actions:
                    self.assertIn(action, text)
                    if action.startswith("/"):
                        self.assertIn(action, COMMAND_NAMES)
                    else:
                        self.assertIn(action, ("Y", "N"))
                for named in re.findall(r"/[a-z]+", text):
                    self.assertIn(named, COMMAND_NAMES)
                    self.assertIn(named, actions)

    def test_3b_confirm_answers_are_the_real_ones(self):
        import inspect

        source = inspect.getsource(CifixChatApp._spinner_text)
        self.assertIn("打 Y 放行、N 拒绝", source)


class PlainLanguageTest(unittest.TestCase):
    # 4. 提示无黑话、无括号标签（TALK1 词表）
    def test_4_no_jargon_no_role_labels(self):
        texts = list(HINT_TEXTS.values()) + [
            f'问点什么，或让我办事… "{e}"' for e in PLACEHOLDER_EXAMPLES
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual([w for w in JARGON if w in text], [])
                self.assertEqual(ROLE_LABEL.findall(text), [])
                self.assertNotIn("（", text)


if __name__ == "__main__":
    unittest.main()
