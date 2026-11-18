"""PST1：粘贴图片/文件变附件块、零确认、多图全发、坏情况兜底。

断言的数据来源全是真实属性：输入框 prompt.text、app.history、记录的
attachments、替身模型实际收到的消息（calls / seen_messages）、
push_screen 的真实调用记录。不读不存在的属性（CFM1 打回教训）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from textual.events import Paste

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, session_store
from core.tui import (
    PASTED_IMAGE_NOT_SENT,
    CifixChatApp,
    ConfirmModal,
    PromptInput,
)
from tests.test_tui import (
    ScriptedToolModel,
    _tool_result_texts,
    _user_data_block_counts,
    _wait_until,
)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="pst1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"pst1-fixture\"\n", encoding="utf-8"
        )
        self.outside = root / "Desktop"
        self.outside.mkdir()

    def _png(self, name: str) -> Path:
        path = self.outside / name
        path.write_bytes(b"\x89PNG\r\n\x1a\noffline")
        return path

    def _app(self, model) -> CifixChatApp:
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        self.pushed: list = []
        original = app.push_screen

        def recording_push(screen, *args, **kwargs):
            self.pushed.append(screen)
            return original(screen, *args, **kwargs)

        app.push_screen = recording_push
        return app

    async def _paste(self, app, pilot, text: str) -> PromptInput:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        await pilot.pause()
        prompt.post_message(Paste(text))
        await pilot.pause()
        return prompt

    async def _send(self, app, pilot) -> None:
        app.screen.query_one(PromptInput).focus()
        await pilot.press("enter")
        await pilot.pause()

    def _confirm_modals(self) -> list:
        return [s for s in self.pushed if isinstance(s, ConfirmModal)]

    def _user_records(self, app) -> list:
        return [r for r in app.history if r[0] == "user"]

    def _events(self, app) -> list[str]:
        return [r[1] for r in app.history if r[0] == "event"]


class PastedImagesTest(_Base):
    async def test_two_images_become_blocks_and_all_reach_vision_model(self) -> None:
        a, b = self._png("截屏 A.png"), self._png("截屏 B.png")
        model = FakeChatModel(fallback_reply="两张都看到了", model_name="deepseek-flash")
        model.formatter.input_types = ["text/plain", "image/*"]
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, f"'{a}' '{b}'")
            self.assertEqual(prompt.text, "〖Image 1〗 〖Image 2〗")
            self.assertNotIn(str(self.outside), prompt.text)
            await self._send(app, pilot)
            await _wait_until(pilot, lambda: not app._agent_busy and model.calls, "回复落定")
            user = self._user_records(app)
            self.assertEqual(len(user), 1)
            self.assertEqual(user[0][1], "〖Image 1〗 〖Image 2〗")
            self.assertNotIn(str(self.outside), user[0][1])
            self.assertEqual(
                user[0].attachments,
                (("〖Image 1〗", str(a)), ("〖Image 2〗", str(b))),
            )
            self.assertIn(f"已附图：{a.name}、{b.name}", self._events(app))
            self.assertFalse(any("cross_dir_read" in e for e in self._events(app)))
        self.assertEqual(sum(_user_data_block_counts(model)), 2)
        self.assertEqual(self._confirm_modals(), [])

    async def test_non_vision_model_says_images_not_sent(self) -> None:
        a = self._png("blind.png")
        model = FakeChatModel(fallback_reply="只看到文字", model_name="deepseek-chat")
        model.formatter.input_types = ["text/plain", "image/*"]
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, str(a))
            prompt.insert(" 帮我看看")
            await self._send(app, pilot)
            await _wait_until(pilot, lambda: not app._agent_busy and model.calls, "回复落定")
            self.assertIn(PASTED_IMAGE_NOT_SENT, self._events(app))
            self.assertFalse(any(e.startswith("已附图") for e in self._events(app)))
        self.assertEqual(sum(_user_data_block_counts(model)), 0)
        self.assertIn("帮我看看", model.calls[0]["input_text"])
        self.assertEqual(self._confirm_modals(), [])


class PastedFileTest(_Base):
    async def test_file_block_content_reaches_model(self) -> None:
        notes = self.outside / "notes.txt"
        notes.write_text("pasted-file-payload-8841\n", encoding="utf-8")
        model = FakeChatModel(fallback_reply="读到了")
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, str(notes))
            self.assertEqual(prompt.text, "〖File 1〗")
            await self._send(app, pilot)
            await _wait_until(pilot, lambda: not app._agent_busy and model.calls, "回复落定")
            self.assertEqual(
                self._user_records(app)[0].attachments, (("〖File 1〗", str(notes)),)
            )
        self.assertIn("pasted-file-payload-8841", model.calls[0]["input_text"])
        self.assertNotIn(str(self.outside), model.calls[0]["input_text"])
        self.assertEqual(self._confirm_modals(), [])

    async def test_file_deleted_before_send_is_skipped_with_notice(self) -> None:
        keep = self.outside / "keep.txt"
        gone = self.outside / "gone.txt"
        keep.write_text("keep-payload-1201\n", encoding="utf-8")
        gone.write_text("gone-payload-1202\n", encoding="utf-8")
        model = FakeChatModel(fallback_reply="ok")
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, f"{keep} {gone}")
            self.assertEqual(prompt.text, "〖File 1〗 〖File 2〗")
            gone.unlink()
            await self._send(app, pilot)
            await _wait_until(pilot, lambda: not app._agent_busy and model.calls, "回复落定")
            self.assertIn("〖File 2〗 的文件已不存在，已跳过", self._events(app))
        sent = model.calls[0]["input_text"]
        self.assertIn("keep-payload-1201", sent)
        self.assertNotIn("gone-payload-1202", sent)

    async def test_damaged_block_sends_plain_text_without_crash(self) -> None:
        a = self._png("x.png")
        model = FakeChatModel(fallback_reply="ok", model_name="deepseek-flash")
        model.formatter.input_types = ["text/plain", "image/*"]
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, str(a))
            prompt.text = "〖Image 1 看这个"
            await self._send(app, pilot)
            await _wait_until(pilot, lambda: not app._agent_busy and model.calls, "回复落定")
            self.assertIsNone(self._user_records(app)[0].attachments)
            self.assertFalse(any("暂时不可用" in r[1] for r in app.history))
        self.assertIn("〖Image 1 看这个", model.calls[0]["input_text"])
        self.assertEqual(sum(_user_data_block_counts(model)), 0)

    async def test_mixed_text_paste_is_not_an_attachment(self) -> None:
        a = self._png("y.png")
        app = self._app(FakeChatModel(fallback_reply="ok"))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = await self._paste(app, pilot, f"看这个 {a}")
            self.assertNotIn("〖Image", prompt.text)


class SecretaryReadStillConfirmsTest(_Base):
    async def test_secretary_cross_dir_read_still_confirms_after_paste(self) -> None:
        a = self._png("shot.png")
        secret = self.outside / "other.txt"
        secret.write_text("secretary-payload-5531\n", encoding="utf-8")
        model = ScriptedToolModel(
            [
                {"type": "tool", "name": "Read", "input": {"file_path": str(secret)}},
                {"type": "text", "text": "完"},
            ]
        )
        app = self._app(model)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._paste(app, pilot, str(a))
            await self._send(app, pilot)
            await _wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmModal), "秘书自发读仍弹确认"
            )
            self.assertIn(str(secret), "\n".join(app.screen._lines))
            self.assertNotIn(str(a), "\n".join(app.screen._lines))
            await pilot.press("n")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
        self.assertEqual(len(self._confirm_modals()), 1)
        results = "\n".join(
            text for messages in model.seen_messages for text in _tool_result_texts(messages)
        )
        self.assertNotIn("secretary-payload-5531", results)


class ArchiveRoundTripTest(unittest.TestCase):
    def test_attachments_round_trip_and_absent_field_unchanged(self) -> None:
        from core.tui import HistoryRecord

        plain = HistoryRecord("user", "hi")
        self.assertNotIn("attachments", session_store.record_to_dict(plain))
        record = HistoryRecord("user", "〖Image 1〗")
        record.attachments = (("〖Image 1〗", "/Users/hostuser/Desktop/a.png"),)
        data = session_store.record_to_dict(record)
        self.assertEqual(data["attachments"], [["〖Image 1〗", "/Users/hostuser/Desktop/a.png"]])
        back = session_store.record_from_dict(data)
        self.assertEqual(back.attachments, record.attachments)
        self.assertIsNone(session_store.record_from_dict(session_store.record_to_dict(plain)).attachments)


if __name__ == "__main__":
    unittest.main()
