"""SLASH1：斜杠补全浮层补缺——回车填入、鼠标可点、带参数命令填入带尾随空格；
等待确认时浮层禁用（确认优先）。

全部在真实 CifixChatApp 上用 Pilot 按真键/真点击；候选来自真实
_completion_candidates / COMPLETION_COMMANDS，提交与否看真实 Submitted 消息、
input_history 与会话记录。新符号（ARG_COMMANDS / completion_fill）在用到的
测试里才取，旧代码上各条按行为各自变红而不是整文件导入失败。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher
from core import tui as tui_mod
from core.tui import (
    COMMANDS,
    COMPLETION_COMMANDS,
    CifixChatApp,
    PromptBox,
    PromptInput,
    _completion_candidates,
)


def _lines(app: CifixChatApp) -> list[str]:
    return [str(text) for _, text, _ in app.history]


class _SlashCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name).resolve() / "ws"
        (self.ws / "src").mkdir(parents=True)
        (self.ws / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.ws / "README.md").write_text("# 说明\n", encoding="utf-8")

    def _app(self) -> CifixChatApp:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"), workspace_root=self.ws
        )
        app._active_workspace = self.ws
        return app

    async def _prompt(self, app, pilot) -> PromptInput:
        await pilot.pause()
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        return prompt

    async def _type(self, pilot, prompt: PromptInput, text: str) -> None:
        prompt.text = text
        prompt.cursor_location = (0, len(text))
        await pilot.pause()

    def _spy_submitted(self) -> list[str]:
        sent: list[str] = []
        original = PromptInput.post_message

        def spy(widget, message):
            if isinstance(message, PromptInput.Submitted):
                sent.append(message.value)
            return original(widget, message)

        patcher = mock.patch.object(PromptInput, "post_message", spy)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent


class MenuTest(_SlashCase):
    # 1. 打「/」出菜单、输「/ru」过滤
    async def test_1_slash_opens_and_filters(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            box = prompt.box
            await self._type(pilot, prompt, "/")
            self.assertTrue(box.suggestions_visible)
            self.assertEqual(box._suggestion_items, list(COMPLETION_COMMANDS))
            await self._type(pilot, prompt, "/ru")
            self.assertEqual(box._suggestion_items, _completion_candidates("/ru"))
            self.assertEqual(
                [c for c, _ in box._suggestion_items], ["/run", "/runs"]
            )
            shown = str(box._suggest.content)
            self.assertIn("/runs", shown)
            self.assertNotIn("/check", shown)

    # 2. 浮层开时 enter 填入选中命令且未提交
    async def test_2_enter_fills_without_submitting(self) -> None:
        sent = self._spy_submitted()
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            box = prompt.box
            await self._type(pilot, prompt, "/ru")
            await pilot.press("down")  # 选中第二条 /runs
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(prompt.text, "/runs ")
            self.assertFalse(box.suggestions_visible)
            self.assertEqual(sent, [])
            self.assertEqual(app.input_history, [])
            self.assertEqual(_lines(app), [])

    # 3. 浮层关时 enter 照常提交（含填入后再按一次 enter）
    async def test_3_enter_submits_when_menu_closed(self) -> None:
        sent = self._spy_submitted()
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "你好")
            self.assertFalse(prompt.box.suggestions_visible)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(sent, ["你好"])
            prompt = await self._prompt(app, pilot)  # 首次提交后已切到会话页
            await self._type(pilot, prompt, "/dif")
            await pilot.press("enter")  # 填入 /diff，浮层关
            await pilot.pause()
            self.assertEqual(prompt.text, "/diff")
            self.assertFalse(prompt.box.suggestions_visible)
            self.assertEqual(sent, ["你好"])
            await pilot.press("enter")  # 再按才提交
            await pilot.pause()
            self.assertEqual(sent, ["你好", "/diff"])
            self.assertEqual(app.input_history, ["你好", "/diff"])
            self.assertEqual(prompt.text, "")

    # 4. ↑↓ 移动选中、Esc 关闭、浮层开时 ↑↓ 不触发历史召回
    async def test_4_arrows_escape_and_no_history_recall(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            app.remember_input("旧消息")
            box = prompt.box
            await self._type(pilot, prompt, "/")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(box._suggestion_index, 1)
            await pilot.press("up", "up")
            await pilot.pause()
            self.assertEqual(box._suggestion_index, len(COMPLETION_COMMANDS) - 1)
            self.assertEqual(prompt.text, "/")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(box.suggestions_visible)
            self.assertEqual(prompt.text, "/")
            await self._type(pilot, prompt, "")
            await pilot.press("up")  # 浮层关：↑ 照旧召回历史
            await pilot.pause()
            self.assertEqual(prompt.text, "旧消息")


class ClickTest(_SlashCase):
    # 5. 鼠标单击某行填入该行命令（不是默认第一条）
    async def test_5_click_fills_clicked_row(self) -> None:
        sent = self._spy_submitted()
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            box = prompt.box
            await self._type(pilot, prompt, "/")
            self.assertEqual(box._suggestion_index, 0)
            # 浮层 max-height 12，只点可见行：/cd 在第 7 行（下标 6）。
            row = [c for c, _ in COMPLETION_COMMANDS].index("/cd")
            self.assertLess(row, box._suggest.size.height)
            await pilot.click(box._suggest, offset=(6, row))
            await pilot.pause()
            self.assertEqual(prompt.text, "/cd ")
            self.assertNotEqual(prompt.text, COMPLETION_COMMANDS[0][0])
            self.assertFalse(box.suggestions_visible)
            self.assertEqual(sent, [])
            self.assertIs(app.focused, prompt)

    async def test_5b_click_in_mention_mode_selects_file(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "看 @")
            box = prompt.box
            self.assertTrue(box.mention_mode)
            rels = [i[0] for i in box._suggestion_items]
            row = rels.index("src/app.py")
            await pilot.click(box._suggest, offset=(4, row))
            await pilot.pause()
            self.assertEqual(prompt.text, "看 @src/app.py ")
            self.assertEqual(app._mention_attachment.rel, "src/app.py")


class TrailingSpaceTest(_SlashCase):
    # 6. 带参数命令填入带尾随空格、无参数不带
    def test_6a_arg_list_is_explicit_and_sourced(self) -> None:
        arg_commands = tui_mod.ARG_COMMANDS
        fill = tui_mod.completion_fill
        names = {c for c, _ in COMPLETION_COMMANDS}
        self.assertTrue(arg_commands <= names)
        self.assertEqual(
            arg_commands,
            {
                "/run", "/accept", "/profile", "/repro", "/cd", "/workspace",
                "/team", "/mode", "/permissions", "/pending", "/runs",
                "/sessions", "/market",
            },
        )
        for name in names:
            expected = f"{name} " if name in arg_commands else name
            self.assertEqual(fill(name), expected, name)
        self.assertEqual(tuple(c for c, _ in COMMANDS)[:2], ("/check", "/run"))

    async def test_6b_enter_fill_spacing_by_key(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            for typed, expected in (
                ("/cd", "/cd "),
                ("/acc", "/accept "),
                ("/perm", "/permissions "),
                ("/dif", "/diff"),
                ("/hel", "/help"),
                ("/sav", "/save"),
            ):
                await self._type(pilot, prompt, typed)
                self.assertTrue(prompt.box.suggestions_visible, typed)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(prompt.text, expected, typed)
                self.assertFalse(prompt.box.suggestions_visible, typed)


class KeepTest(_SlashCase):
    # 7. Tab 填入保持、@ 补全 enter 填入保持
    async def test_7_tab_fill_and_mention_enter_kept(self) -> None:
        sent = self._spy_submitted()
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            await self._type(pilot, prompt, "/mar")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.text, "/market ")
            await self._type(pilot, prompt, "/sta")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.text, "/status")
            await self._type(pilot, prompt, "看看 @app")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(prompt.text, "看看 @src/app.py ")
            self.assertEqual(app._mention_attachment.rel, "src/app.py")
            self.assertEqual(sent, [])


class ConfirmTest(_SlashCase):
    # 核实：等待确认（输入框答复形态）时浮层禁用，enter 交出的就是答复
    async def test_confirm_disables_menu_and_enter_answers(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = await self._prompt(app, pilot)
            box = prompt.box
            await self._type(pilot, prompt, "/ru")
            self.assertTrue(box.suggestions_visible)
            task = asyncio.ensure_future(app._present_confirm())
            for _ in range(50):
                await pilot.pause()
                if app._awaiting_confirm:
                    break
            self.assertTrue(app._awaiting_confirm)
            await pilot.pause()
            self.assertFalse(box.suggestions_visible)  # 进入确认即收起
            await self._type(pilot, prompt, "/")
            self.assertFalse(box.suggestions_visible)  # 确认期不再弹
            await self._type(pilot, prompt, "n")
            await pilot.press("enter")
            answer = await asyncio.wait_for(task, 5)
            self.assertEqual(answer, "n")
            await pilot.pause()
            self.assertFalse(app._awaiting_confirm)
            await self._type(pilot, prompt, "/")
            self.assertTrue(box.suggestions_visible)  # 答完恢复


if __name__ == "__main__":
    unittest.main()
