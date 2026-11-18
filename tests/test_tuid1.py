"""TUID1：输入框草稿暂存（ctrl+s）+ 粘贴长文折叠（paste_view ctrl+o）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual import events
from textual.widgets import TextArea

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, keybinds, prompt_extras
from core.tui import CifixChatApp, PasteViewModal, PromptInput, _paste_view_text

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
LONG = "\n".join(f"第 {i} 行内容" for i in range(1, 9))  # 8 行 > 5


def _app() -> CifixChatApp:
    model = FakeChatModel(fallback_reply="收到")
    app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
    app.fake_model_for_test = model  # 测试持有假模型，断言它收到的文本
    return app


def _lines(app: CifixChatApp) -> list[str]:
    return [str(text) for _, text, _ in app.history]


async def _paste(pilot, prompt: PromptInput, text: str) -> None:
    # 从 app 投递，与终端驱动一致（直接投给控件会冒泡后再被转发一次）。
    prompt.app.post_message(events.Paste(text))
    await pilot.pause()


class PasteTableUnitTest(unittest.TestCase):
    def test_fold_thresholds(self) -> None:
        self.assertFalse(prompt_extras.paste_should_fold("a\nb\nc\nd\ne"))
        self.assertTrue(prompt_extras.paste_should_fold("a\nb\nc\nd\ne\nf"))
        self.assertFalse(prompt_extras.paste_should_fold("x" * 400))
        self.assertTrue(prompt_extras.paste_should_fold("x" * 401))
        self.assertFalse(prompt_extras.paste_too_big("x" * 100 * 1024))
        self.assertTrue(prompt_extras.paste_too_big("x" * (100 * 1024 + 1)))

    def test_expand_single_pass_and_mangled(self) -> None:
        table = prompt_extras.PasteTable()
        # 全文里带有像占位符的字，也不会被二次展开。
        inner = "〖已粘贴 9 行 #1〗"
        block = table.add("a\n" * 8 + inner)
        self.assertEqual(block.placeholder, "〖已粘贴 9 行 #1〗")
        self.assertEqual(table.expand(f"x{block.placeholder}y"), f"x{block.text}y")
        broken = "〖已粘贴 8 行 #1〗"  # 行数被改：与原样不一致
        self.assertEqual(table.expand(broken), broken)


class DraftStashTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def test_stash_restore_and_empty_press(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            # 空且无暂存：无操作 + 落行。
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(prompt.text, "")
            self.assertIn("没有暂存的草稿", _lines(app))

            prompt.text = "写一半的\n第二行"
            prompt.cursor_location = (1, 2)
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(prompt.text, "")
            self.assertIsNotNone(app.draft_stash)
            self.assertIn("草稿已暂存", _lines(app))

            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(prompt.text, "写一半的\n第二行")
            self.assertEqual(tuple(prompt.cursor_location), (1, 2))
            self.assertIsNone(app.draft_stash)
            self.assertIn("草稿已取回", _lines(app))

    async def test_missing_attachment_dropped_on_restore(self) -> None:
        app = _app()
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "keep.png"
            gone = Path(tmp) / "gone.png"
            keep.write_bytes(b"png")
            gone.write_bytes(b"png")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                prompt = app.screen.query_one(PromptInput)
                prompt.focus()
                prompt.text = f"看图 {keep} 和 {gone} 说说"
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(
                    app.draft_stash.attachments, (str(keep), str(gone))
                )
                gone.unlink()
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertIn(str(keep), prompt.text)
                self.assertNotIn(str(gone), prompt.text)
                last = _lines(app)[-1]
                self.assertIn("草稿已取回", last)
                self.assertIn(f"附件 {gone} 已不存在，已剔除", last)

    async def test_stash_failure_keeps_input(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "别丢了我"
            with mock.patch.object(
                app, "_capture_draft", side_effect=RuntimeError("假异常")
            ):
                await pilot.press("ctrl+s")
                await pilot.pause()
            self.assertEqual(prompt.text, "别丢了我")
            self.assertIsNone(app.draft_stash)
            self.assertTrue(any("草稿暂存失败" in line for line in _lines(app)))


class PasteFoldTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def test_short_paste_not_folded(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await _paste(pilot, prompt, "短短一句")
            self.assertEqual(prompt.text, "短短一句")
            self.assertEqual(len(app.paste_blocks), 0)

    async def test_long_pastes_fold_into_independent_blocks(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await _paste(pilot, prompt, LONG)
            await _paste(pilot, prompt, "y" * 500)
            self.assertEqual(prompt.text, "〖已粘贴 8 行 #1〗〖已粘贴 1 行 #2〗")
            self.assertEqual(app.paste_blocks.get(1).text, LONG)
            self.assertEqual(app.paste_blocks.get(2).text, "y" * 500)

    async def test_send_expands_to_original(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "帮我看看："
            prompt.cursor_location = (0, 5)
            await _paste(pilot, prompt, LONG)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            sent = app.fake_model_for_test.calls[-1]["input_text"]
            self.assertIn("帮我看看：" + LONG, sent)
            self.assertNotIn("〖已粘贴", sent)
            self.assertEqual(len(app.paste_blocks), 0)  # 发送后清理

    async def test_mangled_placeholder_sent_as_text(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await _paste(pilot, prompt, LONG)
            prompt.text = prompt.text.replace("8 行", "8行")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            sent = app.fake_model_for_test.calls[-1]["input_text"]
            self.assertIn("〖已粘贴 8行 #1〗", sent)
            self.assertNotIn("第 3 行内容", sent)

    async def test_stashed_placeholder_survives_other_send(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await _paste(pilot, prompt, LONG)
            await pilot.press("ctrl+s")
            await pilot.pause()
            prompt.text = "另一条"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsNotNone(app.paste_blocks.get(1))
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn(LONG, app.fake_model_for_test.calls[-1]["input_text"])

    async def test_oversize_rejected_input_unchanged(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "原有内容"
            await _paste(pilot, prompt, "z" * (100 * 1024 + 1))
            self.assertEqual(prompt.text, "原有内容")
            self.assertEqual(len(app.paste_blocks), 0)
            self.assertIn("100 KB", _lines(app)[-1])

    async def test_paste_view_modal_matches_blocks(self) -> None:
        app = _app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PasteViewModal)
            self.assertIn("当前输入里没有粘贴块", _lines(app))

            await _paste(pilot, prompt, LONG)
            await _paste(pilot, prompt, "y" * 500)
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertIsInstance(app.screen, PasteViewModal)
            body = app.screen.query_one("#paste-body", TextArea)
            blocks = [app.paste_blocks.get(1), app.paste_blocks.get(2)]
            self.assertEqual(body.text, _paste_view_text(blocks))
            self.assertTrue(body.read_only)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PasteViewModal)


class Tuid1KeybindWiringTest(unittest.IsolatedAsyncioTestCase):
    """键位来自注册表：改注册表后旧键失效、新键生效（构造核对）。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def test_defaults_registered(self) -> None:
        self.assertEqual(keybinds.key_of("stash_draft", keybinds.DEFAULTS), "ctrl+s")
        self.assertEqual(keybinds.key_of("paste_view", keybinds.DEFAULTS), "ctrl+o")

    async def test_remapped_keys_drive_prompt(self) -> None:
        entries, diagnostics = keybinds.load_registry(
            overrides={"stash_draft": "ctrl+e", "paste_view": "ctrl+g"}
        )
        self.assertEqual(diagnostics, ())
        with mock.patch.object(keybinds, "_ACTIVE", (entries, diagnostics)):
            app = _app()
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                prompt = app.screen.query_one(PromptInput)
                prompt.focus()
                prompt.text = "草稿"
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(prompt.text, "草稿")  # 旧键不再触发
                await pilot.press("ctrl+e")
                await pilot.pause()
                self.assertEqual(prompt.text, "")
                self.assertIsNotNone(app.draft_stash)
                await pilot.press("ctrl+e")
                await pilot.pause()
                await _paste(pilot, prompt, LONG)
                await pilot.press("ctrl+o")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, PasteViewModal)
                await pilot.press("ctrl+g")
                await pilot.pause()
                self.assertIsInstance(app.screen, PasteViewModal)


if __name__ == "__main__":
    unittest.main()
