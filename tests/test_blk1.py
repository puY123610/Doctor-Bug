"""BLK1：用户消息底色加强 + 思考块摘要 / 按块展开合成。

断言都读真实渲染输出：会话页 RichLog 的行（重放走真实 render_record），
以及 _render_user 返回的 Text 的 style span。期望色值从常量导入比对。
B2：按块切换键 thought_block_toggle 走真实按键（keybinds.key_of），
定位口径为 TUIC2 跳转落点（_timeline_picked 记下的 seq）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from rich.cells import cell_len
from rich.color import Color
from rich.style import Style
from textual.widgets import RichLog

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, keybinds
from core.tui import (
    COLOR_BACKGROUND_RAISED,
    COLOR_BACKGROUND_USER,
    THOUGHT_NOT_ARCHIVED_TEXT,
    CifixChatApp,
    SessionScreen,
)
from tests.test_tui import _submit_text, _wait_until


def _luma(hex_color: str) -> float:
    r, g, b = Color.parse(hex_color).get_truecolor()
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class UserBackgroundTest(unittest.TestCase):
    def test_render_user_uses_new_background_brighter_than_raised(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="x"))
        rendered = app._render_user("你好\n第二行", 30)
        backgrounds = {
            Style.parse(str(span.style)).bgcolor
            for span in rendered.spans
            if Style.parse(str(span.style)).bgcolor is not None
        }
        self.assertEqual(backgrounds, {Color.parse(COLOR_BACKGROUND_USER)})
        self.assertGreater(_luma(COLOR_BACKGROUND_USER), _luma(COLOR_BACKGROUND_RAISED))
        # 竖条与全宽铺满不变：每行以 ┃ 开头、整行宽度等于给定宽度
        for row in rendered.plain.split("\n"):
            self.assertTrue(row.startswith("┃"))
            self.assertEqual(cell_len(row), 30)


class _ThoughtBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="blk1-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve() / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"blk1\"\n", encoding="utf-8"
        )

    async def _session(self, pilot, app) -> None:
        await pilot.pause()
        await _submit_text(app, pilot, "hi")
        await _wait_until(
            pilot,
            lambda: isinstance(app.screen, SessionScreen) and not app._agent_busy,
            "进入会话页",
        )

    def _thought(self, app, line: str, full: str | None, restored: bool = False):
        app._append_record("event", line, "muted")
        record = app.history[-1]
        if full is not None:
            app._thought_texts[id(record)] = full
        if restored:
            record.restored = True
        return record

    async def _rendered(self, pilot, app) -> tuple[list[str], int]:
        screen = app.screen
        log = screen.query_one("#log", RichLog)
        log.clear()
        screen._replay_history()
        await pilot.pause()
        return [strip.text for strip in log.lines], app._record_width(log)


class ThoughtSummaryTest(_ThoughtBase):
    async def test_collapsed_line_has_first_line_summary_within_width(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 1.2s", "\n\n  先看   失败用例  \n第二行机密全文QQ")
            lines, width = await self._rendered(pilot, app)
        target = [l for l in lines if "思考 · 1.2s" in l]
        self.assertEqual(len(target), 1)
        self.assertIn("思考 · 1.2s · 先看 失败用例", target[0])
        self.assertLessEqual(cell_len(target[0].rstrip()), width)
        self.assertFalse(any("第二行机密全文QQ" in l for l in lines))

    async def test_long_first_line_truncated_with_ellipsis(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(60, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 3.0s", "很长的首行" * 40)
            lines, width = await self._rendered(pilot, app)
        target = [l for l in lines if "思考 · 3.0s" in l]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0].rstrip().endswith("…"))
        self.assertLessEqual(cell_len(target[0].rstrip()), width)

    async def test_restored_record_without_full_text_has_no_summary(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 2.0s", None, restored=True)
            lines, _ = await self._rendered(pilot, app)
        target = [l for l in lines if "思考 · 2.0s" in l]
        self.assertEqual([l.rstrip() for l in target], ["● 思考 · 2.0s"])


class ThoughtExpandCompositionTest(_ThoughtBase):
    async def test_block_set_expands_only_that_block(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            first = self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            self._thought(app, "思考 · 2.0s", "乙摘要\n乙全文BBB")
            self.assertFalse(app.thoughts_expanded)
            app._thought_blocks_expanded.add(id(first))
            lines, _ = await self._rendered(pilot, app)
        text = "\n".join(lines)
        self.assertIn("甲全文AAA", text)
        self.assertNotIn("乙全文BBB", text)
        self.assertIn("思考 · 2.0s · 乙摘要", text)

    async def test_global_toggle_still_expands_all(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            self._thought(app, "思考 · 2.0s", "乙摘要\n乙全文BBB")
            app.thoughts_expanded = True
            lines, _ = await self._rendered(pilot, app)
        text = "\n".join(lines)
        self.assertIn("甲全文AAA", text)
        self.assertIn("乙全文BBB", text)

    async def test_restored_block_expanded_shows_not_archived(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            record = self._thought(app, "思考 · 2.0s", None, restored=True)
            app._thought_blocks_expanded.add(id(record))
            lines, _ = await self._rendered(pilot, app)
        self.assertIn(THOUGHT_NOT_ARCHIVED_TEXT, "\n".join(lines))


class ThoughtBlockToggleKeyTest(_ThoughtBase):
    async def _press(self, pilot, app) -> list[str]:
        await pilot.press(keybinds.key_of("thought_block_toggle"))
        await pilot.pause()
        log = app.screen.query_one("#log", RichLog)
        return [strip.text for strip in log.lines]

    async def test_after_jump_key_expands_only_landing_block(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            first = self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            self._thought(app, "思考 · 2.0s", "乙摘要\n乙全文BBB")
            app._timeline_picked(first.seq)
            lines = await self._press(pilot, app)
        text = "\n".join(lines)
        self.assertIn("甲全文AAA", text)
        self.assertNotIn("乙全文BBB", text)
        self.assertFalse(app.thoughts_expanded)

    async def test_never_jumped_key_expands_latest_block(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            self._thought(app, "思考 · 2.0s", "乙摘要\n乙全文BBB")
            self.assertIsNone(app._jumped_seq)
            lines = await self._press(pilot, app)
            text = "\n".join(lines)
            self.assertIn("乙全文BBB", text)
            self.assertNotIn("甲全文AAA", text)
            lines = await self._press(pilot, app)  # 再按一次收起
        self.assertNotIn("乙全文BBB", "\n".join(lines))

    async def test_landing_on_non_thought_moves_to_next_block(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            app._append_record("user", "中间一条用户消息")
            middle = app.history[-1]
            self._thought(app, "思考 · 2.0s", "乙摘要\n乙全文BBB")
            app._timeline_picked(middle.seq)
            text = "\n".join(await self._press(pilot, app))
        self.assertIn("乙全文BBB", text)
        self.assertNotIn("甲全文AAA", text)

    async def test_stale_landing_does_not_crash(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            self._thought(app, "思考 · 1.0s", "甲摘要\n甲全文AAA")
            app._jumped_seq = 987654  # 落点记录已不在
            text = "\n".join(await self._press(pilot, app))
        self.assertIn("甲全文AAA", text)
        self.assertFalse(any("暂时不可用" in r[1] for r in app.history))

    async def test_no_thought_blocks_gives_hint(self) -> None:
        app = CifixChatApp(model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace)
        async with app.run_test(size=(100, 40)) as pilot:
            await self._session(pilot, app)
            await self._press(pilot, app)
        self.assertIn("当前没有可展开的思考块", [r[1] for r in app.history])


if __name__ == "__main__":
    unittest.main()
