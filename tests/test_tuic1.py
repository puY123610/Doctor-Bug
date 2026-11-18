"""TUIC1：会话页侧边栏 + 思考块展开/折叠统一（只改展示不改数据）。"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from textual.widgets import RichLog, Static

from agentscope_ext.fake_model import (
    ChunkedFakeChatModel,
    FakeChatModel,
    ThinkingChunkedFakeChatModel,
)
from core import dispatcher, keybinds, recents, usage
from core.model_state import ModelTriple
from core.tui import (
    NO_CHANGES_TEXT,
    OUT_OF_WORKSPACE_TEXT,
    CifixChatApp,
    PromptInput,
    SessionScreen,
    SessionSidebar,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]


def _rec(call_id, inp, out, cost, source="provider") -> usage.UsageRecord:
    return usage.UsageRecord(
        call_id=call_id, ts=time.time(), provider="deepseek", model="m",
        effort=None, input_tokens=inp, output_tokens=out,
        cache_read_tokens=None, cache_write_tokens=None, source=source,
        price_in_snapshot=None, price_out_snapshot=None, cost=cost,
    )


async def _settle(pilot, n: int = 4) -> None:
    for _ in range(n):
        await pilot.pause()


async def _to_session(app, pilot, text: str = "/status") -> None:
    prompt = app.screen.query_one(PromptInput)
    prompt.focus()
    prompt.text = text
    await pilot.pause()
    # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
    if prompt.box is not None and prompt.box.suggestions_visible:
        await pilot.press("enter")
        await pilot.pause()
    await pilot.press("enter")
    await _settle(pilot)
    while not isinstance(app.screen, SessionScreen):
        app.pop_screen()
        await _settle(pilot)


async def _wait_until(pilot, predicate, label: str, attempts: int = 300) -> None:
    for _ in range(attempts):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError(f"等待超时：{label}")


def _log_text(app) -> str:
    log = app.screen.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _sidebar(app) -> SessionSidebar:
    return app.screen.query_one("#sidebar", SessionSidebar)


def _sidebar_text(app) -> str:
    return str(_sidebar(app).render())


def _thinking_rows(app) -> list[str]:
    return [
        text for kind, text, _ in app.history
        if kind == "event" and (text == "思考" or text.startswith("思考 ·"))
    ]


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        usage.reset_current()
        self.addCleanup(usage.reset_current)
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher.set_bundle_listener, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="tuic1-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def app(self, model=None) -> CifixChatApp:
        return CifixChatApp(
            model=model or FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )

    def bundle(self, changed: list[str] | None, *, review: bool = True) -> Path:
        path = self.tmp / f"b{len(list(self.tmp.iterdir()))}"
        path.mkdir()
        data: dict = {"outcome": "PASS", "rounds": []}
        if review:
            attempts = [] if changed is None else [
                {"status": "通过", "changed_files": changed}
            ]
            data["review"] = {"status": "通过", "attempts": attempts}
        (path / "record.json").write_text(json.dumps(data), encoding="utf-8")
        return path


class SidebarToggleTest(_Base):
    async def test_default_closed_and_toggle_twice_restores(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await _to_session(app, pilot)
            self.assertFalse(app.sidebar_open)
            self.assertEqual(str(_sidebar(app).styles.display), "none")
            await pilot.press(keybinds.key_of("sidebar_toggle"))
            await _settle(pilot)
            self.assertTrue(app.sidebar_open)
            self.assertEqual(str(_sidebar(app).styles.display), "block")
            self.assertEqual(_sidebar(app).outer_size.width, 32)
            await pilot.press(keybinds.key_of("sidebar_toggle"))
            await _settle(pilot)
            self.assertFalse(app.sidebar_open)
            self.assertEqual(str(_sidebar(app).styles.display), "none")

    async def test_80_columns_no_overlap_and_transcript_at_least_40(self) -> None:
        for width in (80, 60):
            app = self.app()
            async with app.run_test(size=(width, 24)) as pilot:
                await _to_session(app, pilot)
                await pilot.press("ctrl+b")
                await _settle(pilot)
                transcript = app.screen.query_one("#transcript")
                sidebar = _sidebar(app)
                self.assertGreaterEqual(transcript.size.width, 40)
                self.assertLessEqual(
                    transcript.region.right, sidebar.region.x or width
                )
                self.assertLessEqual(
                    transcript.outer_size.width + sidebar.outer_size.width, width
                )


class SidebarContentTest(_Base):
    async def _open(self, app, pilot) -> None:
        await _to_session(app, pilot)
        await pilot.press("ctrl+b")
        await _settle(pilot)

    async def test_workspace_and_model_match_backend(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open(app, pilot)
            text = _sidebar_text(app)
            active = app._resolved_active_path()
            expected_ws = "未绑定" if active is None else recents.shorten_home(active)
            self.assertIn(f"工作区\n{expected_ws}", text)
            triple = (
                f"{app._provider_label()} · {app._model_label()} · "
                f"思考 {app._effort_label()}"
            )
            self.assertIn(triple, text)
            self.assertNotIn("（本次会话覆盖）", text)

    async def test_model_switch_refreshes_sidebar(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open(app, pilot)
            app._model_state.set_override(
                ModelTriple("deepseek", "deepseek-reasoner", "high")
            )
            app._refresh_status_lines()  # 现有模型切换刷新路径
            await _settle(pilot)
            text = _sidebar_text(app)
            self.assertIn("deepseek-reasoner", text)
            self.assertIn("（本次会话覆盖）", text)

    async def test_usage_totals_unknown_and_estimate(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open(app, pilot)
            self.assertIn("调用 0 次", _sidebar_text(app))
            usage.record_usage(_rec("a", None, None, None, source="estimate"))
            app._refresh_sidebar()
            await _settle(pilot)
            text = _sidebar_text(app)
            self.assertIn("调用 1 次", text)
            self.assertIn("token 合计 未知", text)
            self.assertIn("成本 未知", text)
            self.assertNotIn("token 合计 0", text)
            self.assertIn("估算", text)
            usage.record_usage(_rec("b", 100, 20, 0.5))
            app._refresh_sidebar()
            await _settle(pilot)
            text = _sidebar_text(app)
            totals = usage.current_accumulator().totals()
            self.assertEqual(totals["calls"], 2)
            self.assertIn("调用 2 次", text)
            self.assertIn("token 合计 120（1 次未知）", text)
            self.assertIn("成本 $0.5000（1 次未知）", text)
            self.assertIn("其中 1 次为估算", text)

    async def test_changed_files_rules(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._open(app, pilot)
            # 没有本会话运行：暂无改动
            self.assertEqual(app.sidebar_changed_lines(), [NO_CHANGES_TEXT])
            # 运行落盘走调度员回调 → 本会话归属
            dispatcher._notify_bundle(self.bundle(["core/a.py", "tests/t.py"]))
            await _settle(pilot)
            self.assertEqual(
                app.sidebar_changed_lines(), ["core/a.py", "tests/t.py"]
            )
            self.assertIn("core/a.py", _sidebar_text(app))
            # 空 / 无 review / 读不到：暂无改动
            app._on_bundle_written(self.bundle([]))
            self.assertEqual(app.sidebar_changed_lines(), [NO_CHANGES_TEXT])
            app._on_bundle_written(self.bundle(None, review=False))
            self.assertEqual(app.sidebar_changed_lines(), [NO_CHANGES_TEXT])
            app._on_bundle_written(self.tmp / "missing")
            self.assertEqual(app.sidebar_changed_lines(), [NO_CHANGES_TEXT])
            # 越界：整行写工作区变更
            app._on_bundle_written(self.bundle(["ok.py", "../outside.py"]))
            self.assertEqual(app.sidebar_changed_lines(), [OUT_OF_WORKSPACE_TEXT])
            app._on_bundle_written(self.bundle(["/etc/hosts"]))
            self.assertEqual(app.sidebar_changed_lines(), [OUT_OF_WORKSPACE_TEXT])
            # 超过 5 条：列 5 条 + 共 N 个文件
            many = [f"f{i}.py" for i in range(7)]
            app._on_bundle_written(self.bundle(many))
            self.assertEqual(
                app.sidebar_changed_lines(), many[:5] + ["共 7 个文件"]
            )


class ThoughtToggleTest(_Base):
    async def _reply(self, app, pilot, text: str) -> None:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = text
        await pilot.pause()
        # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
        if prompt.box is not None and prompt.box.suggestions_visible:
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await app.workers.wait_for_complete()
        await _settle(pilot)

    async def test_no_thinking_no_row(self) -> None:
        app = self.app(ChunkedFakeChatModel(["只有正文。"], output_tokens=1))
        async with app.run_test(size=(120, 40)) as pilot:
            await self._reply(app, pilot, "你好")
            self.assertEqual(_thinking_rows(app), [])
            await pilot.press(keybinds.key_of("thought_toggle"))
            await _settle(pilot, 12)
            self.assertEqual(_thinking_rows(app), [])

    async def test_expand_then_collapse_keeps_history(self) -> None:
        thinking = ["先核对失败用例，", "再看派单。"]
        full_thinking = "".join(thinking)
        app = self.app(
            ThinkingChunkedFakeChatModel(thinking, ["结论"], output_tokens=2)
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await self._reply(app, pilot, "看看")
            rows = _thinking_rows(app)
            self.assertEqual(len(rows), 1)
            self.assertRegex(rows[0], r"^思考 · \d+\.\d+s$")
            # BLK1：收起态折叠行带首行摘要；判「未展开」= 没有单独一行全文。
            def standalone() -> int:
                return [
                    line.strip() for line in _log_text(app).splitlines()
                ].count(full_thinking)

            self.assertEqual(standalone(), 0)
            snapshot = [(tuple(r), r.actual) for r in app.history]

            await pilot.press(keybinds.key_of("thought_toggle"))
            await _wait_until(pilot, lambda: standalone() == 1, "展开全文")
            self.assertTrue(app.thoughts_expanded)
            self.assertEqual(_log_text(app).count(full_thinking), 1)

            await pilot.press(keybinds.key_of("thought_toggle"))
            await _wait_until(pilot, lambda: standalone() == 0, "收起")
            self.assertIn(rows[0], _log_text(app))
            self.assertEqual(
                [(tuple(r), r.actual) for r in app.history], snapshot
            )

    async def test_streaming_block_not_expanded(self) -> None:
        thinking = ["正在想的内容。"]
        gates = [asyncio.Event() for _ in range(2)]

        async def gate(index: int) -> None:
            await gates[index].wait()

        app = self.app(
            ThinkingChunkedFakeChatModel(
                thinking, ["答"], gate=gate, output_tokens=1
            )
        )
        async with app.run_test(size=(120, 40)) as pilot:
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            prompt.text = "想"
            await pilot.pause()
            await pilot.press("enter")
            gates[0].set()
            await _wait_until(
                pilot,
                lambda: isinstance(app.screen, SessionScreen)
                and thinking[0]
                in str(app.screen.query_one("#stream-text", Static).content),
                "流式思考可见",
            )
            app.screen.query_one("#log", RichLog).focus()
            await pilot.press(keybinds.key_of("thought_toggle"))
            await _settle(pilot, 12)
            self.assertTrue(app.thoughts_expanded)
            self.assertNotIn(thinking[0], _log_text(app))
            self.assertEqual(_thinking_rows(app), [])
            self.assertEqual(app._thought_texts, {})
            gates[1].set()
            await app.workers.wait_for_complete()
            await _settle(pilot)
            self.assertEqual(len(_thinking_rows(app)), 1)


class RegistryTest(unittest.TestCase):
    def test_new_entries_in_registry_page_scope(self) -> None:
        ids = {e.id: e for e in keybinds.DEFAULTS}
        self.assertEqual(ids["sidebar_toggle"].scope, keybinds.SCOPE_PAGE)
        self.assertEqual(ids["thought_toggle"].scope, keybinds.SCOPE_PAGE)
        self.assertEqual(
            SessionScreen.BINDINGS, keybinds.bindings_for(keybinds.SCOPE_PAGE)
        )


if __name__ == "__main__":
    unittest.main()
