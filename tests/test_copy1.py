"""COPY1：回复区拖选松手自动复制（pbcopy 为主、OSC52 补充）+ /copy 原文兜底。

全部在真实 CifixChatApp 上用 Pilot 真拖（mouse_down → hover → mouse_up）
真点；系统剪贴板用 PATH 里的假 pbcopy 截获：脚本把 stdin 原样写进文件，
另记下它看到的 LANG/LC_ALL，断言收到的字节逐字正确。
"""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.cells import cell_len

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher
from core.tui import CifixChatApp, PromptInput, RichLog

REPLY = (
    "修复完成，改动如下：\n\n"
    "- 订单模块：折扣计算改为先四舍五入再求和\n"
    "- 库存模块：补上负数校验\n\n"
    "```python\nprice = round(total * rate, 2)\n```\n\n"
    "**结论**：全部检查通过，可以合并。"
)

FAKE_PBCOPY = """#!/bin/sh
cat > "$PBCOPY_OUT"
printf '%s|%s' "$LANG" "$LC_ALL" > "$PBCOPY_OUT.env"
"""

FAILING_PBCOPY = """#!/bin/sh
cat > /dev/null
exit 3
"""


def _lines(app) -> list[str]:
    return [str(text) for _, text, _ in app.history]


class _CopyCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.out = self.root / "clip.txt"
        self._install(FAKE_PBCOPY)
        patcher = mock.patch.dict(
            os.environ,
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PBCOPY_OUT": str(self.out),
                "LANG": "C",
                "LC_ALL": "C",
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _install(self, script: str) -> None:
        path = self.bin / "pbcopy"
        path.write_text(script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _pbcopy_got(self) -> str | None:
        if not self.out.exists():
            return None
        return self.out.read_bytes().decode("utf-8")

    def _app(self, reply: str = REPLY) -> CifixChatApp:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply=reply), workspace_root=self.ws
        )
        app._active_workspace = self.ws
        return app

    def _spy_notify(self, app) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        original = app.notify

        def spy(message, *args, **kwargs):
            seen.append((str(message), kwargs.get("severity", "information")))
            return original(message, *args, **kwargs)

        app.notify = spy
        return seen

    async def _ask(self, app, pilot, text: str = "帮我看看") -> RichLog:
        await pilot.pause()
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = text
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        for _ in range(50):
            await pilot.pause()
            if any(kind == "assistant" for kind, _, _ in app.history):
                break
        log = app.screen.query_one("#log", RichLog)
        await pilot.pause()
        return log

    @staticmethod
    def _row_of(log: RichLog, needle: str) -> tuple[int, str]:
        for y, strip in enumerate(log.lines):
            if needle in strip.text:
                return y, strip.text
        raise AssertionError(f"日志里找不到 {needle!r}")

    @staticmethod
    def _cell(log: RichLog, y: int, char_index: int) -> tuple[int, int]:
        """第 y 行第 char_index 个字在 #log 里的格子坐标（含内边距、滚动）。"""
        text = log.lines[y].text
        x = cell_len(text[:char_index])
        gutter = log.gutter
        return gutter.left + x, gutter.top + y - log.scroll_offset.y

    async def _drag(self, pilot, log, start: tuple[int, int], end: tuple[int, int]):
        await pilot.mouse_down(log, offset=start)
        await pilot.hover(log, offset=(start[0] + 1, start[1]))
        await pilot.hover(log, offset=end)
        await pilot.mouse_up(log, offset=end)
        for _ in range(5):
            await pilot.pause()


class DragCopyTest(_CopyCase):
    # a. 真拖选跨行一段（含中文、列表、代码块）→ pbcopy 逐字收到，OSC52 同值
    async def test_a_drag_multiline_copies_rendered_text(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            y1, line1 = self._row_of(log, "订单模块")
            y2, line2 = self._row_of(log, "price = round")
            i = line1.index("订单模块")
            j = line2.index("round") + len("round") - 1
            await self._drag(
                pilot, log, self._cell(log, y1, i), self._cell(log, y2, j)
            )
            rows = [log.lines[y].text.rstrip() for y in range(y1, y2 + 1)]
            expected = "\n".join([rows[0][i:], *rows[1:-1], rows[-1][: j + 1]])
            self.assertIn("订单模块", expected)
            self.assertIn("库存模块", expected)
            self.assertTrue(expected.endswith("round"))
            self.assertEqual(self._pbcopy_got(), expected)
            self.assertEqual(app.clipboard, expected)
            self.assertEqual(
                (self.out.parent / (self.out.name + ".env")).read_text(),
                "en_US.UTF-8|en_US.UTF-8",
            )
            self.assertIn(("已复制", "information"), notes)
            # 选区保留不清
            self.assertTrue(app.screen.selections)
            self.assertEqual(app.screen.get_selected_text(), expected)

    # b. 中文选段：起止落在中文字之间，无错位、无半字
    async def test_b_cjk_span_has_no_half_characters(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            log = await self._ask(app, pilot)
            y, line = self._row_of(log, "折扣计算")
            i = line.index("折扣")
            j = line.index("四舍五入") + len("四舍五入") - 1
            await self._drag(pilot, log, self._cell(log, y, i), self._cell(log, y, j))
            self.assertEqual(self._pbcopy_got(), "折扣计算改为先四舍五入")
            # 指针落在中文字的右半格：框架 compositor 逐字累加格宽，到达指针
            # 所在格即停，右半格归到下一个字的起点——结果仍是整字、无半字。
            x, row = self._cell(log, y, i)
            await self._drag(pilot, log, (x + 1, row), (x + 5, row))
            got = self._pbcopy_got()
            self.assertEqual(got, "扣计算")
            self.assertIn(got, line)
            self.assertNotIn("\ufffd", got)

    # c. 原地单击：剪贴板不变、无提示
    async def test_c_click_does_not_copy(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            y, line = self._row_of(log, "订单模块")
            await pilot.click(log, offset=self._cell(log, y, line.index("订单")))
            for _ in range(5):
                await pilot.pause()
            self.assertIsNone(self._pbcopy_got())
            self.assertIsNone(app.clipboard or None)
            self.assertEqual(notes, [])

    async def test_pbcopy_failure_is_reported_not_faked(self) -> None:
        self._install(FAILING_PBCOPY)
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            log = await self._ask(app, pilot)
            y, line = self._row_of(log, "订单模块")
            i = line.index("订单模块")
            await self._drag(
                pilot, log, self._cell(log, y, i), self._cell(log, y, i + 3)
            )
            self.assertEqual(app.clipboard, "订单模块")  # OSC52 照发
            self.assertNotIn(("已复制", "information"), notes)
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0][1], "warning")
            self.assertIn("pbcopy 退出码 3", notes[0][0])

    async def test_pbcopy_missing_is_reported(self) -> None:
        (self.bin / "pbcopy").unlink()
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            with mock.patch("core.tui.shutil.which", return_value=None):
                log = await self._ask(app, pilot)
                y, line = self._row_of(log, "订单模块")
                i = line.index("订单模块")
                await self._drag(
                    pilot, log, self._cell(log, y, i), self._cell(log, y, i + 1)
                )
            self.assertEqual(app.clipboard, "订单")
            self.assertEqual([s for _, s in notes], ["warning"])
            self.assertIn("找不到 pbcopy", notes[0][0])


class SlashCopyTest(_CopyCase):
    # d. /copy：剪贴板逐字等于回复原文（含 Markdown 符号），不是渲染文本
    async def test_d_copy_command_copies_raw_reply(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            await self._ask(app, pilot)
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "/copy"
            await pilot.pause()
            if prompt.box is not None and prompt.box.suggestions_visible:
                await pilot.press("enter")  # SLASH1：先填入
                await pilot.pause()
            await pilot.press("enter")
            for _ in range(5):
                await pilot.pause()
            self.assertEqual(self._pbcopy_got(), REPLY)
            self.assertEqual(app.clipboard, REPLY)
            self.assertIn("**结论**", self._pbcopy_got())
            self.assertIn("```python", self._pbcopy_got())

    async def test_d_copy_without_reply_only_hints(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            notes = self._spy_notify(app)
            await pilot.pause()
            app.submit_text("/copy")
            for _ in range(5):
                await pilot.pause()
            self.assertIsNone(self._pbcopy_got())
            self.assertIsNone(app.clipboard or None)
            self.assertEqual(notes, [])
            self.assertIn("还没有主治医生的回复可复制", _lines(app))

    def test_d_copy_is_listed_once_from_commands(self) -> None:
        from core import keybinds
        from core.tui import COMMANDS, COMPLETION_COMMANDS, help_view_text

        self.assertEqual([c for c, _ in COMMANDS].count("/copy"), 1)
        self.assertIn("/copy", dict(COMPLETION_COMMANDS))
        self.assertIn("/copy", help_view_text())
        self.assertIsNone(keybinds.key_of("copy_last_reply"))
        self.assertEqual(
            [b for b in keybinds.bindings_for(keybinds.SCOPE_GLOBAL)
             if b.action == "copy_last_reply"],
            [],
        )


class ConfirmTest(_CopyCase):
    # e. 等待确认时拖选复制不影响答复
    async def test_e_drag_during_confirm_keeps_answer_channel(self) -> None:
        app = self._app()
        async with app.run_test(size=(100, 40)) as pilot:
            log = await self._ask(app, pilot)
            task = asyncio.ensure_future(app._present_confirm())
            for _ in range(50):
                await pilot.pause()
                if app._awaiting_confirm:
                    break
            self.assertTrue(app._awaiting_confirm)
            y, line = self._row_of(log, "库存模块")
            i = line.index("库存模块")
            await self._drag(
                pilot, log, self._cell(log, y, i), self._cell(log, y, i + 3)
            )
            self.assertEqual(self._pbcopy_got(), "库存模块")
            self.assertTrue(app._awaiting_confirm)
            self.assertFalse(task.done())
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "n"
            await pilot.pause()
            await pilot.press("enter")
            self.assertEqual(await asyncio.wait_for(task, 5), "n")


if __name__ == "__main__":
    unittest.main()
