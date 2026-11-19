"""APPLY2-fix1：确认弹窗「拒绝理由」框抢焦点、回车即拒绝的坑。

全产品共用的 ConfirmModal 本体测试，走秘书跨目录读文件的真实确认
（与 test_tui.TuiConfirmModalTest 同一套夹具），用 Pilot 真点击、真按键
模拟焦点，不直调内部函数。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import dispatcher
from core.tui import CifixChatApp, ConfirmModal
from tests.test_tui import (
    ScriptedToolModel,
    _submit_text,
    _wait_until,
)


class ConfirmModalReasonFocusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="apply2-fix1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"fix1-fixture\"\n", encoding="utf-8"
        )
        self.outside = root / "outside-fix1.txt"
        self.outside.write_text("fix1-payload-7731\n", encoding="utf-8")

    def _app(self) -> tuple[CifixChatApp, ScriptedToolModel]:
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Read",
                    "input": {"file_path": str(self.outside)},
                },
                {"type": "text", "text": "好的"},
            ]
        )
        return CifixChatApp(model=model, workspace_root=self.workspace), model

    def _audit(self) -> str:
        log = self.workspace / ".drbug" / "audit.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    async def _open_modal(self, app, pilot) -> None:
        await pilot.pause()
        await _submit_text(app, pilot, "读一下项目外那个文件")
        await _wait_until(pilot, lambda: app._awaiting_confirm, "进入等待确认")
        self.assertIsInstance(app.screen, ConfirmModal)

    async def _focus_reason(self, app, pilot) -> None:
        await pilot.click("#confirm-reason")
        await pilot.pause()
        reason = app.screen.query_one("#confirm-reason")
        self.assertTrue(reason.has_focus, "点击后焦点应在拒绝理由框")

    async def _reads(self, app) -> int:
        tool = await app._dispatcher.toolkit.get_tool("Read")
        return tool.executions

    async def test_a_empty_reason_enter_does_not_deny(self) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            await self._focus_reason(app, pilot)
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmModal, "空框回车后弹窗应还开着")
            self.assertTrue(app._awaiting_confirm)
            self.assertNotIn("答复 N", self._audit())
            self.assertNotIn("deny", self._audit())
            # 收尾：Esc 拒绝路径仍通（框有焦点时也通）。
            await pilot.press("escape")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            self.assertEqual(await self._reads(app), 0)

    async def _yes_in_empty_box(self, key: str) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            await self._focus_reason(app, pilot)
            await pilot.press(key)
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            self.assertNotIsInstance(app.screen, ConfirmModal)
            self.assertEqual(await self._reads(app), 1)
            self.assertIn("allow", self._audit())

    async def test_b_focused_empty_box_y_allows(self) -> None:
        await self._yes_in_empty_box("y")

    async def test_b_focused_empty_box_upper_y_allows(self) -> None:
        await self._yes_in_empty_box("Y")

    async def test_c_typed_reason_enter_denies_with_reason(self) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            await self._focus_reason(app, pilot)
            # 理由里带 y、n 字母：框里有字时字母只打字，不是答复键。
            await pilot.press(*"not yet")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmModal)
            self.assertEqual(
                app.screen.query_one("#confirm-reason").value, "not yet"
            )
            await pilot.press("enter")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            self.assertEqual(await self._reads(app), 0)
            self.assertIn("deny", self._audit())
            # 理由按原语义交回秘书工具（同轮重试的拒绝文案与审计会带上它）。
            secretary = app._dispatcher._drbug_secretary_read
            self.assertEqual(secretary._denied_reason, "not yet")

    async def test_d_unfocused_enter_still_allows(self) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            self.assertFalse(app.screen.query_one("#confirm-reason").has_focus)
            await pilot.press("enter")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            self.assertEqual(await self._reads(app), 1)

    async def test_e_unfocused_n_still_denies(self) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            await pilot.press("n")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            self.assertEqual(await self._reads(app), 0)
            self.assertIn("deny", self._audit())

    async def test_f_key_hint_line_visible(self) -> None:
        app, _ = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open_modal(app, pilot)
            hint = str(app.screen.query_one("#confirm-keys").render())
            self.assertIn("按 Y 同意", hint)
            self.assertIn("按 N 或 Esc 拒绝", hint)
            self.assertIn("只在拒绝时填理由，可不填", hint)
            await pilot.press("escape")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")


if __name__ == "__main__":
    unittest.main()
