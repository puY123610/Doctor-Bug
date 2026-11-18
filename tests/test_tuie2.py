"""TUIE2：/help 弹窗化（命令表 + 快捷键表同屏、单一来源）+ 错误详情展开。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Static

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, error_log, keybinds
from core.error_log import MAX_FRAMES, MAX_RECORDS, NO_EXCEPTION, REDACTED
from core.tui import (
    COMMANDS,
    COMPLETION_COMMANDS,
    CifixChatApp,
    InfoViewModal,
    help_view_text,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
FAKE_KEY = "sk-TUIE2-fake-secret-abcdef123"


def _app() -> CifixChatApp:
    return CifixChatApp(
        model=FakeChatModel(fallback_reply="收到"), workspace_root=PLATFORM_ROOT
    )


def _body(app) -> str:
    return str(app.screen.query_one("#info-body", Static).render())


def _lines(app) -> list[str]:
    return [str(text) for _, text, _ in app.history]


async def _submit(app, pilot, text: str) -> None:
    prompt = app.screen.query_one("PromptInput")
    prompt.focus()
    prompt.text = text
    await pilot.pause()
    await pilot.press("enter")
    for _ in range(20):
        await pilot.pause()
        if isinstance(app.screen, InfoViewModal):
            return


def _deep_raise(depth: int, message: str) -> None:
    if depth <= 0:
        raise RuntimeError(message)
    _deep_raise(depth - 1, message)


class HelpViewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def test_help_text_is_single_source(self) -> None:
        import core.tui as tui

        self.assertFalse(hasattr(tui, "HELP_TEXT"))  # 文本版已退役，不留双份
        text = help_view_text()
        for command, description in COMMANDS:
            self.assertIn(f"  {command}", text)
            self.assertIn(description, text)
        # 补全候选与命令表同源（/exit 是唯一多出的别名）
        self.assertEqual(
            [c for c, _ in COMPLETION_COMMANDS if c != "/exit"],
            [c for c, _ in COMMANDS],
        )
        for key, description, _scope in keybinds.describe():
            self.assertIn(description, text)
            self.assertIn(key or "未绑定", text)

    def test_shortcut_table_follows_user_override(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"open_commands": "ctrl+k"}
        )
        self.assertEqual(diagnostics, ())
        with mock.patch.object(keybinds, "_ACTIVE", (entries, diagnostics)):
            text = help_view_text()
        line = next(l for l in text.splitlines() if "命令面板" in l and "（全局）" in l)
        self.assertIn("ctrl+k", line)
        self.assertNotIn("ctrl+p", line)

    async def test_help_opens_from_home_and_closes(self) -> None:
        app = _app()
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            self.assertEqual(app.screen.id, "home")
            await _submit(app, pilot, "/help")
            self.assertEqual(app.screen.id, keybinds.SCREEN_HELP_VIEW)
            self.assertEqual(_body(app), help_view_text())
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, InfoViewModal)

    async def test_help_close_key_from_registry(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"help_view_close": "q"}
        )
        with mock.patch.object(keybinds, "_ACTIVE", (entries, diagnostics)):
            app = _app()
            async with app.run_test(size=(140, 60)) as pilot:
                await pilot.pause()
                await _submit(app, pilot, "/help")
                await pilot.press("escape")
                await pilot.pause()
                self.assertIsInstance(app.screen, InfoViewModal)
                await pilot.press("q")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, InfoViewModal)


class ErrorLogUnitTest(unittest.TestCase):
    def test_ring_buffer_keeps_last_20(self) -> None:
        log = error_log.ErrorLog()
        for index in range(MAX_RECORDS + 5):
            log.record(f"错误 {index}")
        self.assertEqual(len(log), MAX_RECORDS)
        self.assertEqual(log.records()[0].message, "错误 5")
        self.assertEqual(log.latest().message, f"错误 {MAX_RECORDS + 4}")

    def test_exception_frames_and_no_exception(self) -> None:
        log = error_log.ErrorLog()
        try:
            _deep_raise(12, "深层失败")
        except RuntimeError as exc:
            record = log.record("执行失败", exc=exc, source="/check")
        self.assertEqual(record.exc_type, "RuntimeError")
        self.assertEqual(len(record.frames), MAX_FRAMES)
        self.assertTrue(all("_deep_raise" in frame for frame in record.frames))
        detail = error_log.format_record(record)
        self.assertIn("堆栈（最后 8 帧）", detail)
        plain = log.record("没有异常的红字")
        self.assertEqual(plain.exc_type, NO_EXCEPTION)
        self.assertEqual(plain.frames, ())
        plain_detail = error_log.format_record(plain)
        self.assertNotIn("堆栈", plain_detail)
        self.assertIn("触发来源：未记录", plain_detail)

    def test_redaction_of_key_and_home_path(self) -> None:
        home = str(Path.home())
        log = error_log.ErrorLog()
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": FAKE_KEY}):
            try:
                raise ValueError(
                    f"调用失败 key={FAKE_KEY} 文件 {home}/secret/config.json"
                )
            except ValueError as exc:
                record = log.record(f"失败：{exc}", exc=exc)
        detail = error_log.format_record(record)
        self.assertNotIn(FAKE_KEY, detail)
        self.assertNotIn(FAKE_KEY[:12], detail)
        self.assertNotIn(home + "/", detail)
        self.assertIn(REDACTED, detail)
        for frame in record.frames:
            self.assertFalse(frame.startswith("/"), frame)


class ErrorViewAppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def test_no_error_shows_hint_without_modal(self) -> None:
        app = _app()
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+r")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, InfoViewModal)
            self.assertIn("暂无可展开的错误", _lines(app))
            self.assertEqual(len(app.error_log), 0)

    async def test_error_log_line_recorded_and_expandable(self) -> None:
        app = _app()
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            try:
                _deep_raise(3, f"连不上 {FAKE_KEY}")
            except RuntimeError:
                app._log(f"体检失败: 连不上 {FAKE_KEY}", tone="error")
            app._log("普通一行", tone="plain")
            self.assertEqual(len(app.error_log), 1)
            # 历史区仍一行，原样不变长
            self.assertEqual(
                sum(1 for line in _lines(app) if line.startswith("体检失败")), 1
            )
            await pilot.press("ctrl+r")
            await pilot.pause()
            self.assertEqual(app.screen.id, keybinds.SCREEN_ERROR_VIEW)
            body = _body(app)
            self.assertIn("异常类型：RuntimeError", body)
            self.assertIn("堆栈（最后", body)
            self.assertNotIn(FAKE_KEY, body)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, InfoViewModal)

            app._log("模式值不认识", tone="error")
            await pilot.press("ctrl+r")
            await pilot.pause()
            body = _body(app)
            self.assertIn(f"异常类型：{NO_EXCEPTION}", body)
            self.assertNotIn("堆栈", body)

    async def test_slash_command_is_recorded_as_source(self) -> None:
        app = _app()
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause()
            with mock.patch.object(
                app, "_cmd_market", side_effect=lambda text: app._log("坏了", tone="error")
            ):
                await _submit(app, pilot, "/market")
            self.assertEqual(app.error_log.latest().source, "/market")

    async def test_error_view_key_from_registry(self) -> None:
        entries, diagnostics = keybinds.load_registry(overrides={"error_view": "ctrl+g"})
        self.assertEqual(diagnostics, ())
        with mock.patch.object(keybinds, "_ACTIVE", (entries, diagnostics)):
            import importlib

            # 页面 BINDINGS 在类定义时生成；这里核对注册表构造出的绑定
            # TUIC1 起页面作用域还有 sidebar_toggle / thought_toggle；
            # 这里只核对 error_view 那一条随覆盖键变化。
            bindings = [
                b
                for b in keybinds.bindings_for(keybinds.SCOPE_PAGE)
                if b.action == "open_error_view"
            ]
            self.assertEqual([b.key for b in bindings], ["ctrl+g"])
        from core.tui import HomeScreen, SessionScreen

        for screen in (HomeScreen, SessionScreen):
            self.assertEqual(
                [
                    b.key
                    for b in screen.BINDINGS
                    if b.action == "open_error_view"
                ],
                [keybinds.key_of("error_view")],
            )


if __name__ == "__main__":
    unittest.main()
