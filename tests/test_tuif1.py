"""F1：会话管理 /sessions（列表 / 恢复 / 重命名 / 删除）。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope.message import Msg, TextBlock
from agentscope.permission import PermissionMode
from textual.widgets import OptionList, RichLog

from core import dispatcher, session_admin, usage
from core import session_store as ss
from core.agentscope_actors import build_state
from core.model_state import ModelTriple
from core.tui import (
    COMMANDS,
    THOUGHT_NOT_ARCHIVED_TEXT,
    CifixChatApp,
    ConfirmModal,
    HistoryRecord,
    SessionScreen,
    SessionsModal,
    WorkspaceTrustScreen,
)
from core.usage import UsageRecord
from tests.test_tui import FakeChatModel

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def _rec(kind, text, tone="plain", actual=None, seq=None, ts=None):
    r = HistoryRecord(kind, text, tone, actual=actual)
    r.seq = seq
    r.ts = ts
    return r


def _tree(root: Path, skip: str | None = None) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if skip and skip in p.parts:
            continue
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _log_text(app) -> str:
    for screen in app.screen_stack:
        if isinstance(screen, SessionScreen):
            log = screen.query_one("#log", RichLog)
            return "\n".join(strip.text for strip in log.lines)
    return ""


def _history_text(app) -> str:
    return "\n".join(r[1] for r in app.history)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.addCleanup(usage.reset_current)
        self.tmp = Path(tempfile.mkdtemp(prefix="tuif1-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "proj"
        self.ws.mkdir()
        (self.ws / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.store = ss.SessionStore.for_workspace(self.ws)

    def state(self, ws: Path | None = None):
        state = build_state(ws or self.ws)
        state.context.append(
            Msg(name="用户", role="user", content=[TextBlock(text="帮我看看登录 bug")])
        )
        state.context.append(
            Msg(name="主治医生", role="assistant", content=[TextBlock(text="问题在 auth.py")])
        )
        state.summary = "修登录"
        return state

    def session(self, sid="s-1", title="修登录 bug", ws: Path | None = None, override=True):
        ws = ws or self.ws
        history = [
            _rec("user", "帮我看看登录 bug", seq=1, ts=10.5),
            _rec("event", "思考 · 1.2s", "muted", seq=2, ts=11.0),
            _rec("assistant", "问题在 auth.py", actual=("openai", "gpt-x", None), seq=3, ts=11.25),
            _rec("diff", PATCH, seq=4, ts=12.0),
        ]
        return ss.SessionData(
            session_id=sid,
            title=title,
            workspace=str(ws),
            agent_state=self.state(ws),
            history=history,
            model_override=ModelTriple("openai", "gpt-x", "high") if override else None,
            usage=[
                UsageRecord("c1", 1.0, "openai", "gpt-x", "high", 100, 20, None, None,
                            "provider", 1.0, 2.0, 0.00014),
                UsageRecord("c2", 2.0, "openai", "gpt-x", None, None, 7, None, None,
                            "estimate", None, None, None),
            ],
        )

    def runs(self):
        return [
            ss.ToolRun(f"call-{i}", "shell", ss.summarize_args({"cmd": "pytest -q"}, self.ws),
                       state=state)
            for i, state in enumerate(ss.TOOL_STATES)
        ]

    def app(self, gates: bool = False) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=self.ws,
            interactive_gates=gates,
        )

    async def start(self, app, pilot) -> None:
        await pilot.pause()
        app._active_workspace = self.ws
        if not isinstance(app.screen, SessionScreen):
            app.page = "session"
            app.switch_screen(SessionScreen(app))
        for _ in range(3):
            await pilot.pause()

    async def settle(self, pilot, n: int = 4) -> None:
        for _ in range(n):
            await pilot.pause()


class CommandRegistrationTest(unittest.TestCase):
    def test_sessions_in_commands(self) -> None:
        names = [c for c, _ in COMMANDS]
        self.assertIn("/sessions", names)
        desc = dict(COMMANDS)["/sessions"]
        # F2：会话列表已实现，「会话列表」按 mcp/侧边栏/diff 先例放行。
        for banned in ("会话历史", "历史会话"):
            self.assertNotIn(banned, desc)

    def test_gitignore_has_drbug(self) -> None:
        lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".drbug/", [ln.strip() for ln in lines])


class ListTest(_Base):
    def _four(self) -> None:
        self.store.save(self.session("s-ok", "正常会话"))
        self.store.save(self.session("s-bad", "会坏的"))
        self.store.save(self.session("s-ver", "版本不符的"))
        gone = self.tmp / "gone"
        gone.mkdir()
        self.store.save(self.session("s-gone", "工作区没了", ws=gone))
        shutil.rmtree(gone)
        p = self.store.root / "s-bad" / "session.json"
        p.write_text(p.read_text("utf-8").replace("会坏的", "已改的", 1), encoding="utf-8")
        p = self.store.root / "s-ver" / "session.json"
        doc = json.loads(p.read_text("utf-8"))
        doc["format_version"] = 2
        p.write_text(json.dumps(doc), encoding="utf-8")

    def test_four_kinds_of_rows(self) -> None:
        self._four()
        items = {i.session_id: i for i in session_admin.list_sessions(self.store)}
        self.assertEqual(set(items), {"s-ok", "s-bad", "s-ver", "s-gone"})
        self.assertEqual(items["s-ok"].status, session_admin.STATUS_OK)
        self.assertEqual(items["s-bad"].status, "已损坏")
        self.assertEqual(items["s-ver"].status, "已损坏")
        self.assertEqual(items["s-ver"].reason, "版本不符")
        self.assertEqual(items["s-gone"].status, "工作区不存在")
        ok_row = session_admin.format_row(items["s-ok"])
        for part in ("s-ok", "正常会话", "4 条", PATCH.splitlines()[0]):
            self.assertIn(part, ok_row)
        self.assertIn("[已损坏]", session_admin.format_row(items["s-bad"]))
        self.assertIn("[工作区不存在]", session_admin.format_row(items["s-gone"]))

    async def test_command_lists_and_modal(self) -> None:
        self._four()
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, SessionsModal)
            ol = app.screen.query_one("#sessions-list", OptionList)
            self.assertEqual(ol.option_count, 4)
            await pilot.press("escape")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, SessionScreen)
            text = _history_text(app)
            for part in ("s-ok", "[已损坏]", "[工作区不存在]"):
                self.assertIn(part, text)

    async def test_empty_and_unbound(self) -> None:
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions")
            await self.settle(pilot)
            self.assertIn("本工作区还没有存档的会话", _history_text(app))
            app._active_workspace = None
            app._cmd_sessions("/sessions")
            app._cmd_sessions("/sessions bogus x")
            await self.settle(pilot)
            self.assertIn("未绑定工作区", _history_text(app))


class RestoreTest(_Base):
    async def test_roundtrip_override_usage_and_modal_enter(self) -> None:
        original = self.session()
        self.store.save(original, self.runs())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            with mock.patch.object(app, "_profile_for_triple", return_value=None), \
                 mock.patch.object(app, "_rebuild_doctor", return_value=True):
                app._cmd_sessions("/sessions")
                await self.settle(pilot)
                self.assertIsInstance(app.screen, SessionsModal)
                await pilot.press("enter")
                await self.settle(pilot, 6)
            self.assertIsInstance(app.screen, SessionScreen)
            restored = [r for r in app.history if getattr(r, "restored", False)]
            self.assertEqual(len(restored), len(original.history))
            for got, want in zip(restored, original.history):
                self.assertEqual(tuple(got), tuple(want))
                self.assertEqual(got.actual, want.actual)
                self.assertEqual(got.seq, want.seq)
                self.assertEqual(got.ts, want.ts)
            self.assertGreater(app._next_seq, 4)
            self.assertEqual(app.history[-1].seq, app._next_seq - 1)
            self.assertEqual(app._model_state.override, ModelTriple("openai", "gpt-x", "high"))
            self.assertEqual(usage.current_accumulator().records(), original.usage)
            totals = usage.current_accumulator().totals()
            self.assertEqual(totals["input_tokens"], {"sum": 100, "missing": 1})
            self.assertEqual(totals["estimated_calls"], 1)
            state = app._restored_agent_state
            self.assertEqual(
                [m.content for m in state.context],
                [m.content for m in original.agent_state.context],
            )
            self.assertEqual(state.summary, "修登录")
            self.assertEqual(state.session_id, original.agent_state.session_id)
            self.assertIn("已恢复会话「修登录 bug」", _history_text(app))
            self.assertIn(PATCH, _history_text(app))

    async def test_running_unknown_never_rerun(self) -> None:
        self.store.save(self.session(override=False), self.runs())
        from agentscope.tool import Toolkit
        from core import shell_tool

        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            with mock.patch.object(shell_tool, "_execute") as execute, \
                 mock.patch.object(shell_tool, "run_shell") as run_shell, \
                 mock.patch.object(shell_tool.ShellTool, "call") as tool_call, \
                 mock.patch.object(Toolkit, "call_tool") as toolkit_call:
                app._cmd_sessions("/sessions restore s-1")
                await self.settle(pilot, 6)
            for m in (execute, run_shell, tool_call, toolkit_call):
                m.assert_not_called()
            text = _history_text(app)
            self.assertIn("call-1）存档时状态为 running，不会自动重跑", text)
            self.assertIn("call-5）存档时状态为 unknown，不会自动重跑", text)
            self.assertNotIn("call-2）存档时状态", text)

    async def test_thinking_only_folded_line_after_restore(self) -> None:
        self.store.save(self.session(override=False))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot, 6)
            self.assertEqual(app._thought_texts, {})
            app.screen.action_toggle_thoughts()
            await self.settle(pilot, 6)
            text = _log_text(app)
            self.assertIn("思考 · 1.2s", text)
            self.assertIn(THOUGHT_NOT_ARCHIVED_TEXT, text)

    async def test_missing_workspace_refused(self) -> None:
        gone = self.tmp / "gone"
        gone.mkdir()
        self.store.save(self.session(ws=gone))
        shutil.rmtree(gone)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            before = list(app.history)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertEqual(app.history[: len(before)], before)
            self.assertIn("工作区不存在", _history_text(app))
            self.assertIn("不会猜测新路径", _history_text(app))
            self.assertEqual(app._resolved_active_path(), self.ws)
            self.assertFalse(any(getattr(r, "restored", False) for r in app.history))

    async def test_corrupt_refused(self) -> None:
        self.store.save(self.session())
        (self.store.root / "s-1" / "session.json").write_text("{坏", encoding="utf-8")
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions restore s-1")
            app._cmd_sessions("/sessions restore nope")
            await self.settle(pilot)
            text = _history_text(app)
            self.assertIn("已损坏", text)
            self.assertIn("找不到会话：nope", text)


class NoEscalationTest(_Base):
    async def test_trust_asked_again_and_cancel_changes_nothing(self) -> None:
        self.store.save(self.session(override=False))
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._interactive_gates = True
            app._session_trusted.add(str(self.ws))  # 本次会话已同意过
            before = list(app.history)
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)
            app.screen.dismiss(False)
            await self.settle(pilot)
            self.assertEqual(app.history[: len(before)], before)
            self.assertFalse(any(getattr(r, "restored", False) for r in app.history))
            self.assertIn("已取消恢复", _history_text(app))
            app._cmd_sessions("/sessions restore s-1")
            await self.settle(pilot)
            self.assertIsInstance(app.screen, WorkspaceTrustScreen)
            app.screen.dismiss(True)
            await self.settle(pilot, 6)
            self.assertTrue(any(getattr(r, "restored", False) for r in app.history))

    def test_old_grants_not_revived(self) -> None:
        saved = self.session(override=False)
        state = saved.agent_state
        state.permission_context.mode = PermissionMode.BYPASS
        extra = self.tmp / "elsewhere"
        extra.mkdir()
        wd = dict(state.permission_context.working_directories)
        first = next(iter(wd.values()))
        wd[str(extra)] = first.model_copy(update={"path": str(extra), "source": "old-grant"})
        state.permission_context.working_directories = wd
        self.store.save(saved)
        loaded = session_admin.prepare_restore(self.store, "s-1")
        fresh = build_state(self.ws)
        restored = loaded.session.agent_state
        self.assertEqual(restored.permission_context, fresh.permission_context)
        self.assertNotIn(str(extra), restored.permission_context.working_directories)
        self.assertEqual(restored.tool_context, fresh.tool_context)
        # 回复上下文按新会话重建：除新生成的随机 id 外与新建默认值一致
        strip = lambda ctx: {k: v for k, v in ctx.model_dump().items() if not k.endswith("_id")}
        self.assertEqual(strip(restored.reply_context), strip(fresh.reply_context))
        self.assertNotEqual(
            restored.reply_context.model_dump(), saved.agent_state.reply_context.model_dump()
        )
        self.assertEqual(len(restored.context), 2)


class RenameTest(_Base):
    def test_rename_only_title(self) -> None:
        self.store.save(self.session())
        path = self.store.root / "s-1" / "session.json"
        before = json.loads(path.read_text("utf-8"))["payload"]
        entry = session_admin.rename(self.store, "s-1", "  新标题  ")
        after = json.loads(path.read_text("utf-8"))["payload"]
        self.assertEqual(entry.session_id, "s-1")
        self.assertEqual(after["title"], "新标题")
        self.assertEqual(after["id"], "s-1")
        for key in set(before) - {"title"}:
            self.assertEqual(after[key], before[key], key)
        listed = self.store.list_sessions()
        self.assertEqual(listed.manifest_status, "ok")
        self.assertEqual([e.title for e in listed.entries], ["新标题"])
        self.assertEqual(listed.entries[0].updated_at, before["updated_at"])

    def test_empty_and_too_long_rejected(self) -> None:
        self.store.save(self.session())
        snapshot = _tree(self.store.root)
        for bad in ("", "   ", "x" * 101, "两行\n标题"):
            with self.assertRaises(ValueError):
                session_admin.rename(self.store, "s-1", bad)
        self.assertEqual(_tree(self.store.root), snapshot)
        session_admin.rename(self.store, "s-1", "y" * 100)
        with self.assertRaises(ValueError):
            session_admin.rename(self.store, "nope", "标题")

    async def test_rename_command(self) -> None:
        self.store.save(self.session())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions rename s-1 改过的 标题")
            app._cmd_sessions("/sessions rename s-1 " + "z" * 101)
            await self.settle(pilot)
            text = _history_text(app)
            self.assertIn("已重命名会话 s-1：改过的 标题", text)
            self.assertIn("标题超长：101 字，最多 100 字", text)
        self.assertEqual(self.store.load("s-1").session.title, "改过的 标题")


class DeleteTest(_Base):
    async def _ask(self, app, pilot) -> ConfirmModal:
        app._cmd_sessions("/sessions delete s-1")
        await self.settle(pilot)
        self.assertIsInstance(app.screen, ConfirmModal)
        return app.screen

    async def test_confirm_text_impact_then_refuse_changes_nothing(self) -> None:
        self.store.save(self.session(), self.runs())
        self.store.save(self.session("s-2", "另一个"))
        snapshot = _tree(self.store.root)
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            modal = await self._ask(app, pilot)
            body = "\n".join(modal._lines)
            for part in ("修登录 bug", "s-1", "4 条消息", "不可恢复"):
                self.assertIn(part, body)
            texts = [r[1] for r in app.history]
            impact_at = next(i for i, t in enumerate(texts) if t.startswith("删除会话会移除"))
            confirm_at = next(i for i, t in enumerate(texts) if t.startswith("确认删除会话"))
            self.assertLess(impact_at, confirm_at)
            self.assertEqual(
                texts[confirm_at],
                session_admin.delete_confirm_line(session_admin.find(self.store, "s-1")),
            )
            await pilot.press("n")
            await self.settle(pilot)
            self.assertNotIsInstance(app.screen, ConfirmModal)
            self.assertIn("已取消删除，存档一字未动", _history_text(app))
        self.assertEqual(_tree(self.store.root), snapshot)

    async def test_confirm_deletes_three_parts_only(self) -> None:
        self.store.save(self.session(), self.runs())
        self.store.save(self.session("s-2", "另一个"))
        ws_before = _tree(self.ws, skip=".drbug")
        other_before = _tree(self.store.root / "s-2")
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            await self._ask(app, pilot)
            await pilot.press("y")
            await self.settle(pilot)
            self.assertIn("已删除会话 s-1", _history_text(app))
        self.assertFalse((self.store.root / "s-1").exists())
        listed = self.store.list_sessions()
        self.assertEqual(listed.manifest_status, "ok")
        self.assertEqual([e.session_id for e in listed.entries], ["s-2"])
        manifest = json.loads(self.store.manifest_path.read_text("utf-8"))
        self.assertEqual([e["id"] for e in manifest["payload"]["sessions"]], ["s-2"])
        self.assertEqual(_tree(self.store.root / "s-2"), other_before)
        self.assertEqual(_tree(self.ws, skip=".drbug"), ws_before)

    async def test_typed_answer_fallback_and_missing(self) -> None:
        self.store.save(self.session())
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await self.start(app, pilot)
            app._cmd_sessions("/sessions delete nope")
            await self.settle(pilot)
            self.assertIn("找不到会话：nope", _history_text(app))
            await self._ask(app, pilot)
            app._resolve_confirm("N")
            await self.settle(pilot)
            self.assertTrue((self.store.root / "s-1" / "session.json").exists())
            self.assertFalse(app._awaiting_confirm)
            self.assertIsNone(app._confirm_future)

    def test_delete_keeps_foreign_files_in_dir(self) -> None:
        self.store.save(self.session(), self.runs())
        stray = self.store.root / "s-1" / "notes.txt"
        stray.write_text("keep", encoding="utf-8")
        removed = session_admin.delete(self.store, "s-1")
        self.assertEqual(len(removed), 2)
        self.assertEqual(stray.read_text("utf-8"), "keep")
        self.assertEqual(self.store.list_sessions().entries, [])


if __name__ == "__main__":
    unittest.main()
