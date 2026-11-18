"""BLK2：工具块（流消费层配对、收起/展开、脱敏、落点切换、存档兼容）。

断言读真实渲染输出：会话页 RichLog 的行（真实 render_record 重放）。
工具事件用 AgentScope 的真实事件类构造，经 app._track_tool_block 进入；
另有一条走 ScriptedToolModel 的真实 reply_stream 端到端。
假密钥只出现在输入里，断言只认打码后的形态。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentscope.event import (
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from textual.widgets import RichLog

from agentscope_ext.fake_model import FakeChatModel
from core import dispatcher, keybinds, session_store
from core.tui import (
    TOOL_EXCERPT_MAX_BYTES,
    TOOL_NOT_ARCHIVED_TEXT,
    CifixChatApp,
    HistoryRecord,
    SessionScreen,
    _tool_excerpt,
)
from tests.test_tui import ScriptedToolModel, _submit_text, _wait_until

FAKE_KEY = "sk-FAKEKEY0000111122223333"
REDACTED = "‹已脱敏›"


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="blk2-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve() / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"blk2\"\n", encoding="utf-8"
        )

    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="ok"), workspace_root=self.workspace
        )

    async def _session(self, pilot, app) -> None:
        await pilot.pause()
        await _submit_text(app, pilot, "hi")
        await _wait_until(
            pilot,
            lambda: isinstance(app.screen, SessionScreen) and not app._agent_busy,
            "进入会话页",
        )

    def _tool(self, app, cid, name, args, result=None, state="success",
              result_first=False, end=True):
        ev = dict(reply_id="r", tool_call_id=cid)
        calls = [
            ToolCallStartEvent(tool_call_name=name, **ev),
            ToolCallDeltaEvent(delta=json.dumps(args, ensure_ascii=False), **ev),
            ToolCallEndEvent(**ev),
        ]
        results = []
        if result is not None or end:
            results.append(ToolResultStartEvent(tool_call_name=name, **ev))
            if result is not None:
                results.append(ToolResultTextDeltaEvent(delta=result, **ev))
            if end:
                results.append(ToolResultEndEvent(state=state, **ev))
        order = results + calls if result_first else calls + results
        for event in order:
            app._track_tool_block(event)
        return [r for r in app.history if r[0] == "tool"][-1]

    async def _lines(self, pilot, app) -> list[str]:
        log = app.screen.query_one("#log", RichLog)
        log.clear()
        app.screen._replay_history()
        await pilot.pause()
        return [strip.text.rstrip() for strip in log.lines]

    async def _press(self, pilot, app) -> list[str]:
        await pilot.press(keybinds.key_of("tool_block_toggle"))
        await pilot.pause()
        return [s.text.rstrip() for s in app.screen.query_one("#log", RichLog).lines]


class CollapsedAndExpandedTest(_Base):
    async def test_collapsed_line_name_args_state_no_result(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._session(pilot, app)
            self._tool(app, "c1", "Read", {"file_path": str(self.workspace / "a.py")},
                       result="RESULT-BODY-7781")
            lines = await self._lines(pilot, app)
        rows = [l for l in lines if l.startswith("⚙ ")]
        self.assertEqual(rows, ["⚙ Read（file_path=a.py） · 成功"])
        self.assertFalse(any("RESULT-BODY-7781" in l for l in lines))

    async def test_expand_head_tail_and_omitted_count(self) -> None:
        body = "\n".join(f"row-{i:03d}" for i in range(1, 101))
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            record = self._tool(app, "c1", "Grep", {"pattern": "x"}, result=body)
            app._tool_blocks_expanded.add(id(record))
            text = "\n".join(await self._lines(pilot, app))
        for i in list(range(1, 41)) + list(range(81, 101)):
            self.assertIn(f"row-{i:03d}", text)
        for i in range(41, 81):
            self.assertNotIn(f"row-{i:03d}", text)
        self.assertIn("…中间省略 40 行…", text)
        self.assertIn('"pattern": "x"', text)

    async def test_over_8kb_is_cut_with_notice(self) -> None:
        body = "\n".join("Z" * 900 for _ in range(30))
        self.assertLessEqual(len(_tool_excerpt(body).encode("utf-8")), TOOL_EXCERPT_MAX_BYTES)
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            record = self._tool(app, "c1", "Read", {"file_path": "x"}, result=body)
            app._tool_blocks_expanded.add(id(record))
            lines = await self._lines(pilot, app)
        self.assertIn("…已截断（超过 8KB）…", "\n".join(lines))
        zs = sum(l.count("Z") for l in lines)
        self.assertLess(zs, len(body.replace("\n", "")))
        self.assertLessEqual(zs, TOOL_EXCERPT_MAX_BYTES)


class RedactionTest(_Base):
    async def test_args_and_result_scrubbed(self) -> None:
        home_file = str(Path.home() / "blk2-secret-dir" / "notes.txt")
        ws_file = str(self.workspace / "pkg" / "mod.py")
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            record = self._tool(
                app, "c1", "Read",
                {"file_path": ws_file, "note": f"token={FAKE_KEY}", "other": home_file},
                result=f"key {FAKE_KEY} at {ws_file} and {home_file}",
            )
            app._tool_blocks_expanded.add(id(record))
            text = "\n".join(await self._lines(pilot, app))
        self.assertNotIn(FAKE_KEY, text)
        self.assertIn(REDACTED, text)
        self.assertNotIn(str(self.workspace), text)
        self.assertIn("pkg/mod.py", text)
        self.assertNotIn(str(Path.home()) + "/", text)
        self.assertIn("~", text)


class PairingTest(_Base):
    async def test_result_first_no_result_and_failed(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            first = self._tool(app, "c1", "A", {"q": 1}, result="early", result_first=True)
            pending = self._tool(app, "c2", "B", {"q": 2}, result=None, end=False)
            failed = self._tool(app, "c3", "C", {"q": 3},
                                result=f"boom {FAKE_KEY}", state="error")
            for record in (first, pending, failed):
                app._tool_blocks_expanded.add(id(record))
            lines = await self._lines(pilot, app)
        text = "\n".join(lines)
        rows = [l for l in lines if l.startswith("⚙ ")]
        self.assertEqual([r for r in rows if r.startswith("⚙ A")], ["⚙ A · 成功"])
        self.assertTrue(any(r.startswith("⚙ B") and r.endswith("进行中") for r in rows))
        self.assertTrue(any(r.startswith("⚙ C") and r.endswith("失败") for r in rows))
        self.assertIn("early", text)
        self.assertIn('"q": 1', text)  # 后到的参数补进了同一块
        self.assertIn("结果：执行中…", text)
        self.assertIn("boom " + REDACTED, text)
        self.assertNotIn(FAKE_KEY, text)


class LandingKeyTest(_Base):
    async def test_jump_expands_only_landing_tool(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            first = self._tool(app, "c1", "A", {"q": 1}, result="AAA-BODY")
            self._tool(app, "c2", "B", {"q": 2}, result="BBB-BODY")
            app._timeline_picked(first.seq)
            text = "\n".join(await self._press(pilot, app))
        self.assertIn("AAA-BODY", text)
        self.assertNotIn("BBB-BODY", text)

    async def test_landing_on_non_tool_moves_to_next(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            self._tool(app, "c1", "A", {"q": 1}, result="AAA-BODY")
            app._append_record("user", "中间")
            middle = app.history[-1]
            self._tool(app, "c2", "B", {"q": 2}, result="BBB-BODY")
            app._timeline_picked(middle.seq)
            text = "\n".join(await self._press(pilot, app))
        self.assertIn("BBB-BODY", text)
        self.assertNotIn("AAA-BODY", text)

    async def test_no_landing_latest_then_collapse(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            self._tool(app, "c1", "A", {"q": 1}, result="AAA-BODY")
            self._tool(app, "c2", "B", {"q": 2}, result="BBB-BODY")
            text = "\n".join(await self._press(pilot, app))
            self.assertIn("BBB-BODY", text)
            self.assertNotIn("AAA-BODY", text)
            text = "\n".join(await self._press(pilot, app))
        self.assertNotIn("BBB-BODY", text)

    async def test_stale_landing_and_no_tools(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            await self._press(pilot, app)
            self.assertIn("当前没有可展开的工具块", [r[1] for r in app.history])
            self._tool(app, "c1", "A", {"q": 1}, result="AAA-BODY")
            app._jumped_seq = 987654
            text = "\n".join(await self._press(pilot, app))
        self.assertIn("AAA-BODY", text)

    async def test_thought_key_still_targets_thoughts(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 60)) as pilot:
            await self._session(pilot, app)
            app._append_record("event", "思考 · 1.0s", "muted")
            app._thought_texts[id(app.history[-1])] = "思摘要\n思全文TTT"
            self._tool(app, "c1", "A", {"q": 1}, result="AAA-BODY")
            await pilot.press(keybinds.key_of("thought_block_toggle"))
            await pilot.pause()
            text = "\n".join(s.text for s in app.screen.query_one("#log", RichLog).lines)
        self.assertIn("思全文TTT", text)
        self.assertNotIn("AAA-BODY", text)


class ArchiveTest(_Base):
    async def test_restored_tool_block_shows_not_archived(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._session(pilot, app)
            record = self._tool(app, "c1", "Read", {"file_path": "a"}, result="SECRET-BODY")
            data = session_store.record_to_dict(record)
            self.assertEqual(data["tool"], {"call_id": "c1", "name": "Read", "state": "succeeded"})
            self.assertNotIn("SECRET-BODY", json.dumps(data, ensure_ascii=False))
            back = session_store.record_from_dict(data)
            back.restored = True
            app.history.append(back)
            app._tool_blocks_expanded.add(id(back))
            lines = await self._lines(pilot, app)
        self.assertIn(TOOL_NOT_ARCHIVED_TEXT, "\n".join(lines))
        self.assertIn("⚙ Read（file_path=a） · 成功", lines)

    def test_old_archive_records_without_tool_field_load(self) -> None:
        old = {"kind": "event", "text": "调用工具 run_repair …", "tone": "accent",
               "actual": None, "seq": 3, "ts": 1.0}
        record = session_store.record_from_dict(old)
        self.assertIsNone(record.tool)
        self.assertNotIn("tool", session_store.record_to_dict(HistoryRecord("user", "hi")))


class EndToEndStreamTest(_Base):
    async def test_real_reply_stream_builds_scrubbed_block(self) -> None:
        data = self.workspace / "data.txt"
        data.write_text(f"line-A\nkey {FAKE_KEY}\nline-C\n", encoding="utf-8")
        model = ScriptedToolModel(
            [{"type": "tool", "name": "Read", "input": {"file_path": str(data)}},
             {"type": "text", "text": "读完"}]
        )
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            tools = [r for r in app.history if r[0] == "tool"]
            self.assertEqual(len(tools), 1)
            app._tool_blocks_expanded.add(id(tools[0]))
            text = "\n".join(await self._lines(pilot, app))
        self.assertIn("⚙ Read（file_path=data.txt） · 成功", text)
        self.assertIn("line-C", text)
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(str(self.workspace), text)


if __name__ == "__main__":
    unittest.main()
