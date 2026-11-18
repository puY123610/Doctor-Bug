"""TUIC2：diff 补丁块（只展示）+ 消息跳转（会话内序号）。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import OptionList, RichLog, Static

from agentscope_ext.fake_model import FakeChatModel
from core import diff_view, dispatcher, keybinds, results
from core.diff_view import (
    BINARY_TEXT,
    EMPTY_TEXT,
    KEEP_LINES,
    PARSE_FAILED_TEXT,
    render,
)
from core.tui import (
    COMMANDS,
    CifixChatApp,
    PromptInput,
    SessionScreen,
    TimelineModal,
    TIMELINE_EMPTY_TEXT,
    timeline_row,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]

PATCH = (
    "--- a/core/x.py\n"
    "+++ b/core/x.py\n"
    "@@ -1,3 +1,3 @@\n"
    " keep\n"
    "-old line\n"
    "+new line\n"
    " tail\n"
)


def _styles(text, needle: str) -> set[str]:
    plain = text.plain
    start = plain.index(needle)
    end = start + len(needle)
    return {
        str(span.style)
        for span in text.spans
        if span.start <= start and span.end >= end
    }


class DiffRenderTest(unittest.TestCase):
    def test_colors_header_bold_hunk_gray(self) -> None:
        out = render(PATCH, color=True)
        self.assertIn("bold", _styles(out, "--- a/core/x.py"))
        self.assertIn("bold", _styles(out, "+++ b/core/x.py"))
        self.assertIn(diff_view.COLOR_HUNK, _styles(out, "@@ -1,3 +1,3 @@"))
        self.assertIn(f"on {diff_view.COLOR_ADD_BG}", _styles(out, "+new line"))
        self.assertIn(f"on {diff_view.COLOR_DEL_BG}", _styles(out, "-old line"))
        self.assertEqual(_styles(out, " keep"), set())
        self.assertNotIn("\x1b", out.plain)

    def test_rename_shows_old_to_new(self) -> None:
        git = (
            "diff --git a/old.py b/new.py\n"
            "similarity index 90%\n"
            "rename from old.py\n"
            "rename to new.py\n"
        )
        self.assertIn("old.py → new.py", render(git).plain)
        ab = "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertIn("old.py → new.py", render(ab).plain)
        added = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+a\n"
        self.assertNotIn("→", render(added).plain)

    def test_binary_single_line(self) -> None:
        patch = (
            "diff --git a/img.png b/img.png\n"
            "GIT binary patch\n"
            "literal 10\n"
            "zcmZ?wbhEHbRA3NcU|?WS|NjL\n"
            "\n"
        )
        plain = render(patch).plain
        self.assertEqual(plain.count(BINARY_TEXT), 1)
        self.assertNotIn("zcmZ", plain)
        simple = "Binary files a/x.bin and b/x.bin differ\n"
        self.assertEqual(render(simple).plain.count(BINARY_TEXT), 1)

    def test_oversized_truncates_to_500_lines(self) -> None:
        body = "".join(f"+line {i}\n" for i in range(2600))
        patch = f"--- a/big.py\n+++ b/big.py\n@@ -0,0 +1,2600 @@\n{body}"
        total = len(patch.splitlines())
        plain = render(patch).plain
        lines = plain.splitlines()
        # 标题 + 前 500 行 + 截断提示
        self.assertEqual(len(lines), 1 + KEEP_LINES + 1)
        self.assertEqual(lines[-1], f"已截断，共 {total} 行")
        # 只按字节超限也截断
        fat = "--- a/f.py\n+++ b/f.py\n@@ -0,0 +1,3 @@\n" + "+" + "x" * 210_000 + "\n"
        self.assertIn("已截断，共", render(fat).plain)

    def test_parse_failure_falls_back_to_plain(self) -> None:
        for bad in ("这不是 diff\n随便写的", "--- a/x\n+++ b/x\n@@ broken @@\n+a\n"):
            out = render(bad)
            self.assertIn(PARSE_FAILED_TEXT, out.plain)
            for raw in bad.splitlines():
                self.assertIn(raw, out.plain)

    def test_no_color_uses_prefix_only(self) -> None:
        out = render(PATCH, color=False)
        self.assertEqual({str(s.style) for s in out.spans}, {"bold"})
        self.assertIn("+new line", out.plain)
        self.assertIn("-old line", out.plain)


class _AppBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher.set_bundle_listener, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="tuic2-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )

    def bundle(self, patch: str = PATCH) -> Path:
        path = self.tmp / f"b{len(list(self.tmp.iterdir()))}"
        path.mkdir()
        (path / "patch.diff").write_text(patch, encoding="utf-8")
        (path / "record.json").write_text("{}", encoding="utf-8")
        return path

    async def submit(self, app, pilot, text: str) -> None:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = text
        await pilot.pause()
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()


def _log_text(app) -> str:
    for screen in app.screen_stack:
        if isinstance(screen, SessionScreen):
            log = screen.query_one("#log", RichLog)
            return "\n".join(strip.text for strip in log.lines)
    return ""


class DiffBlockAppTest(_AppBase):
    async def test_diff_command_without_run(self) -> None:
        self.assertIn("/diff", dict(COMMANDS))
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.submit(app, pilot, "/diff")
            self.assertEqual(app.history[-1][1], EMPTY_TEXT)
            self.assertFalse(any(k == "diff" for k, _, _ in app.history))

    async def test_run_completion_appends_diff_after_result(self) -> None:
        app = self.app()
        bundle = self.bundle()
        before = (bundle / "patch.diff").read_bytes()
        async with app.run_test(size=(120, 40)) as pilot:
            await self.submit(app, pilot, "/status")
            app.pop_screen()
            await pilot.pause()

            def work() -> str:
                dispatcher._notify_bundle(bundle)
                return "修复运行完成: outcome=PASS"

            with mock.patch.object(results, "apply_bundle") as apply:
                app._start_local_worker("修复运行", work)
                await app.workers.wait_for_complete()
                for _ in range(6):
                    await pilot.pause()
                kinds = [k for k, _, _ in app.history]
                self.assertEqual(kinds[-2:], ["plain", "diff"])
                self.assertEqual(app.history[-2][1], "修复运行完成: outcome=PASS")
                self.assertEqual(app.history[-1][1], PATCH)
                self.assertNotIn("\x1b", app.history[-1][1])
                self.assertIn("+new line", _log_text(app))
                # /diff 重显最近一轮
                await self.submit(app, pilot, "/diff")
                self.assertEqual(
                    [k for k, _, _ in app.history].count("diff"), 2
                )
                apply.assert_not_called()
        self.assertEqual((bundle / "patch.diff").read_bytes(), before)

    async def test_no_color_env(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)):
            with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
                self.assertFalse(app.diff_color_enabled())
            with mock.patch.dict(os.environ, {"NO_COLOR": ""}):
                self.assertTrue(app.diff_color_enabled())


class TimelineTest(_AppBase):
    async def test_seq_monotonic_and_stable_after_replay(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            for word in ("/status", "/help"):
                await self.submit(app, pilot, word)
                while not isinstance(app.screen, SessionScreen):
                    app.pop_screen()
                    await pilot.pause()
            app._log("第三条")
            await pilot.pause()
            seqs = [r.seq for r in app.history]
            self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
            before_lines = dict(app.record_lines)
            app.screen._reflow_log()
            await pilot.pause()
            self.assertEqual([r.seq for r in app.history], seqs)
            self.assertEqual(set(app.record_lines), set(before_lines))

    async def test_modal_rows_jump_and_close(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 20)) as pilot:
            await self.submit(app, pilot, "/status")
            while not isinstance(app.screen, SessionScreen):
                app.pop_screen()
                await pilot.pause()
            for i in range(40):
                app._log(f"第 {i} 条消息，" + "很长的摘要文字" * 10)
            for _ in range(4):
                await pilot.pause()
            log = app.screen.query_one("#log", RichLog)
            await pilot.press(keybinds.key_of("timeline"))
            for _ in range(4):
                await pilot.pause()
            self.assertIsInstance(app.screen, TimelineModal)
            ol = app.screen.query_one("#timeline-list", OptionList)
            self.assertEqual(ol.option_count, len(app.history))
            row = str(ol.get_option_at_index(1).prompt)
            target = app.history[1]
            self.assertEqual(row, timeline_row(target))
            self.assertRegex(row, r"^#2  系统  第 0 条消息，.{0,30}  \d\d:\d\d:\d\d$")
            summary = row.split("  ")[2]
            self.assertLessEqual(len(summary), 30)
            history_before = [(tuple(r), r.seq) for r in app.history]
            ol.highlighted = 1
            await pilot.press("enter")
            for _ in range(6):
                await pilot.pause()
            self.assertIsInstance(app.screen, SessionScreen)
            log = app.screen.query_one("#log", RichLog)
            self.assertEqual(int(log.scroll_y), app.record_lines[target.seq])
            self.assertGreater(app.record_lines[target.seq], 0)
            self.assertEqual(
                [(tuple(r), r.seq) for r in app.history], history_before
            )
            await pilot.press(keybinds.key_of("timeline"))
            await pilot.pause()
            await pilot.press(keybinds.key_of("timeline_close"))
            await pilot.pause()
            self.assertIsInstance(app.screen, SessionScreen)

    async def test_home_screen_does_not_open_timeline(self) -> None:
        # TUIC2 评审打回：首页没有消息流，按跳转键不应弹出无处可跳的列表。
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertNotIsInstance(app.screen, SessionScreen)
            screen_before = app.screen
            await pilot.press(keybinds.key_of("timeline"))
            for _ in range(3):
                await pilot.pause()
            self.assertNotIsInstance(app.screen, TimelineModal)
            self.assertIs(app.screen, screen_before)
            app.open_timeline()
            await pilot.pause()
            self.assertNotIsInstance(app.screen, TimelineModal)

    async def test_empty_session_shows_placeholder(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.history), 0)
            # 首页不再开跳转弹窗，这里直接推弹窗来核空列表占位。
            app.push_screen(TimelineModal(app))
            await pilot.pause()
            self.assertIsInstance(app.screen, TimelineModal)
            empty = app.screen.query_one("#timeline-empty", Static)
            self.assertEqual(str(empty.render()), TIMELINE_EMPTY_TEXT)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, TimelineModal)


if __name__ == "__main__":
    unittest.main()
