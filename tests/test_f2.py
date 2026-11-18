"""F2：存档入口（自动存档 + /save + 恢复前确认）。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import session_autosave, session_store as ss
from core.tui import COMMANDS, ConfirmModal, SessionScreen, help_view_text
from tests.test_tuif1 import _Base, _history_text, _rec


class TitleRuleTest(unittest.TestCase):
    def test_first_user_message_30_chars(self) -> None:
        history = [
            _rec("user", "/sessions"),
            _rec("event", "x"),
            _rec("user", "帮我看看登录模块为什么在凌晨两点之后总是返回四零一错误码而且只在生产环境出现"),
        ]
        title = session_autosave.default_title(history)
        self.assertEqual(len(title), 30)
        self.assertTrue(title.startswith("帮我看看登录模块"))

    def test_untitled_with_time(self) -> None:
        title = session_autosave.default_title([_rec("user", "/help")], now=0)
        self.assertTrue(title.startswith("未命名会话 "))
        self.assertRegex(title, r"^未命名会话 \d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_tool_state_mapping(self) -> None:
        self.assertEqual(session_autosave.tool_state("success"), ss.TOOL_SUCCEEDED)
        self.assertEqual(session_autosave.tool_state("error"), ss.TOOL_FAILED)
        self.assertEqual(session_autosave.tool_state("interrupted"), ss.TOOL_CANCELLED)
        self.assertEqual(session_autosave.tool_state("denied"), ss.TOOL_CANCELLED)
        self.assertEqual(session_autosave.tool_state("running"), ss.TOOL_RUNNING)
        self.assertEqual(session_autosave.tool_state("???"), ss.TOOL_UNKNOWN)


class CommandTest(unittest.TestCase):
    def test_save_registered_and_word_allowed(self) -> None:
        commands = dict(COMMANDS)
        self.assertIn("/save", commands)
        self.assertIn("会话列表", commands["/sessions"])
        self.assertIn("/save", help_view_text())


class _F2Base(_Base):
    async def say(self, app, pilot, text: str) -> None:
        app.submit_text(text)
        await app.workers.wait_for_complete()
        await self.settle(pilot)

    def entries(self):
        return self.store.list_sessions().entries


class AutosaveTest(_F2Base):
    async def test_turn_settles_then_archive_round_trips(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self.say(app, pilot, "帮我看看登录 bug")
            entries = self.entries()
            self.assertEqual(len(entries), 1)
            loaded = self.store.load(entries[0].session_id)
            saved = [ss.record_to_dict(r) for r in loaded.session.history]
            live = [ss.record_to_dict(r) for r in app.history]
            self.assertEqual(saved, live)
            self.assertEqual(loaded.session.title, "帮我看看登录 bug")
            self.assertIsNotNone(loaded.session.agent_state)
            self.assertEqual(loaded.session.workspace, str(self.ws))
            self.assertFalse(app._has_unsaved_content())

    async def test_repeat_updates_same_archive(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self.say(app, pilot, "第一句")
            first = self.entries()[0]
            await self.say(app, pilot, "第二句")
            entries = self.entries()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].session_id, first.session_id)
            self.assertGreater(entries[0].message_count, first.message_count)
            self.assertEqual(entries[0].created_at, first.created_at)
        dirs = [p for p in self.store.root.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1)

    async def test_failure_one_line_and_chat_goes_on(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            with mock.patch.object(ss.SessionStore, "save", side_effect=OSError("磁盘满")):
                await self.say(app, pilot, "第一句")
                await self.say(app, pilot, "第二句")
            lines = [r[1] for r in app.history]
            fails = [t for t in lines if t.startswith("自动存档失败")]
            self.assertEqual(len(fails), 2)  # 每轮只一行
            self.assertIn("磁盘满", fails[0])
            self.assertEqual(sum(1 for r in app.history if r[0] == "assistant"), 2)
            self.assertFalse(app._agent_busy)
            self.assertTrue(app._has_unsaved_content())

    async def test_tool_runs_written_as_six_states(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._track_tool_start(SimpleNamespace(tool_call_id="t1", tool_call_name="shell"))
            app._track_tool_start(SimpleNamespace(tool_call_id="t2", tool_call_name="read"))
            app._track_tool_end(SimpleNamespace(tool_call_id="t1", state="success"))
            await self.say(app, pilot, "跑一下")
            runs = {r.call_id: r for r in self.store.load(self.entries()[0].session_id).tool_runs}
        self.assertEqual(runs["t1"].state, ss.TOOL_SUCCEEDED)
        self.assertEqual(runs["t2"].state, ss.TOOL_RUNNING)
        self.assertIsNotNone(runs["t1"].ended_at)

    async def test_thinking_text_not_written(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._append_record("user", "问一句")
            app._event("思考 · 1.0s", "muted")
            app._thought_texts[id(app.history[-1])] = "机密思考全文XYZ"
            app._append_record("assistant", "答")
            app._cmd_save()
            await self.settle(pilot)
        sid = self.entries()[0].session_id
        raw = (self.store.root / sid / ss.SESSION_FILE).read_text(encoding="utf-8")
        self.assertNotIn("机密思考全文XYZ", raw)
        self.assertIn("思考 · 1.0s", raw)

    async def test_unbound_skips_silently(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._active_workspace = None
            await self.say(app, pilot, "随便聊")
            self.assertNotIn("自动存档失败", _history_text(app))
        self.assertFalse(self.store.root.exists())


class SaveCommandTest(_F2Base):
    async def test_save_writes_and_reports_truthfully(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._append_record("user", "存一下这个")
            app._append_record("assistant", "好")
            app._cmd_save()
            await self.settle(pilot)
            entry = self.entries()[0]
            line = [r[1] for r in app.history if r[1].startswith("已存档到")][-1]
            self.assertIn(str(self.store.root / entry.session_id), line)
            self.assertIn(f"{entry.message_count} 条记录", line)
            self.assertIn("消息 2 条", line)
            self.assertIn("标题「存一下这个」", line)

    async def test_empty_session_writes_nothing(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self.say(app, pilot, "/save")
            self.assertIn("没有可存档的内容", _history_text(app))
        self.assertFalse(self.store.root.exists())

    async def test_renamed_title_survives_autosave(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self.say(app, pilot, "原始问题")
            sid = self.entries()[0].session_id
            app._cmd_sessions(f"/sessions rename {sid} 我改的标题")
            await self.settle(pilot)
            await self.say(app, pilot, "再追问一句")
            app._cmd_save()
            await self.settle(pilot)
        self.assertEqual(self.store.load(sid).session.title, "我改的标题")
        self.assertEqual(self.entries()[0].title, "我改的标题")


class RestoreConfirmTest(_F2Base):
    async def _dirty(self, app) -> None:
        app._append_record("user", "还没存的问题")
        app._append_record("assistant", "还没存的回答")

    async def test_unsaved_asks_and_refuse_keeps_current(self) -> None:
        self.store.save(self.session(), self.runs())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self._dirty(app)
            before = [ss.record_to_dict(r) for r in app.history]
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, ConfirmModal)
            body = "\n".join(app.screen._lines)
            for part in ("2 条消息尚未存档", "修登录 bug", "s-1", "整体替换"):
                self.assertIn(part, body)
            await pilot.press("n")
            await self.settle(pilot)
            self.assertNotIsInstance(app.screen, ConfirmModal)
            self.assertIn("已取消恢复，当前对话未动", _history_text(app))
            texts = [r[1] for r in app.history]
            self.assertIn("还没存的问题", texts)
            self.assertNotIn("问题在 auth.py", texts)
            self.assertEqual([ss.record_to_dict(r) for r in app.history][: len(before)], before)
            self.assertIsNone(app._session_id)

    async def test_unsaved_confirm_replaces(self) -> None:
        self.store.save(self.session(), self.runs())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self._dirty(app)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            await pilot.press("y")
            await self.settle(pilot)
            texts = [r[1] for r in app.history]
            self.assertIn("问题在 auth.py", texts)
            self.assertNotIn("还没存的问题", texts)
            self.assertEqual(app._session_id, "s-1")
            self.assertFalse(app._has_unsaved_content())

    async def test_empty_restores_without_asking(self) -> None:
        self.store.save(self.session(), self.runs())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertNotIsInstance(app.screen, ConfirmModal)
            self.assertIn("问题在 auth.py", [r[1] for r in app.history])

    async def test_saved_restores_without_asking(self) -> None:
        self.store.save(self.session(), self.runs())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self._dirty(app)
            app._cmd_save()
            await self.settle(pilot)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertNotIsInstance(app.screen, ConfirmModal)
            self.assertEqual(app._session_id, "s-1")
            self.assertIn("问题在 auth.py", [r[1] for r in app.history])


if __name__ == "__main__":
    unittest.main()
