"""CFM1：确认弹窗排队（撞号崩溃 + 答复槽位互覆）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage

from core import dispatcher
from core.tui import CifixChatApp, ConfirmModal
from tests.test_tui import (
    _RESPONSE_TIME,
    FinishedReason,
    ScriptedToolModel,
    _submit_text,
    _tool_result_texts,
    _wait_until,
)


class TwoCallModel(ScriptedToolModel):
    """一步发多个工具调用（同轮多个确认），其余同 ScriptedToolModel。"""

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        if self.steps and self.steps[0]["type"] == "tools":
            index = len(self.seen_messages)
            self.seen_messages.append(list(messages))
            step = self.steps.pop(0)
            blocks = [
                ToolCallBlock(
                    id=f"cfm1-tool-{index:04d}-{n}",
                    name=call["name"],
                    input=json.dumps(call["input"], ensure_ascii=False),
                )
                for n, call in enumerate(step["calls"])
            ]
            return ChatResponse(
                content=blocks,
                is_last=True,
                id=f"cfm1-response-{index:04d}",
                created_at=_RESPONSE_TIME,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
                finished_reason=FinishedReason.COMPLETED,
            )
        return await super()._call_api(model_name, messages, tools, tool_choice, **kwargs)


class _Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="cfm1-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.root = root
        self.workspace = root / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"cfm1-fixture\"\n", encoding="utf-8"
        )
        self.out_a = root / "outside-a.txt"
        self.out_b = root / "outside-b.txt"
        self.out_a.write_text("payload-a-7311\n", encoding="utf-8")
        self.out_b.write_text("payload-b-7322\n", encoding="utf-8")

    def _read(self, path: Path) -> dict:
        return {"name": "Read", "input": {"file_path": str(path)}}


class SameRoundTwoConfirmsTest(_Base):
    async def test_two_confirms_same_round_presented_in_turn(self) -> None:
        model = TwoCallModel(
            [
                {"type": "tools", "calls": [self._read(self.out_a), self._read(self.out_b)]},
                {"type": "text", "text": "两个都处理完了"},
            ]
        )
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读项目外两个文件")
            await _wait_until(
                pilot,
                lambda: isinstance(app.screen, ConfirmModal)
                and str(self.out_a) in "\n".join(app.screen._lines),
                "第一个确认",
            )
            stacked = [m for m in app.screen_stack if isinstance(m, ConfirmModal)]
            self.assertEqual(len(stacked), 1)
            first_seq = app.screen.seq
            await pilot.press("y")
            await _wait_until(
                pilot,
                lambda: isinstance(app.screen, ConfirmModal)
                and str(self.out_b) in "\n".join(app.screen._lines),
                "第二个确认",
            )
            stacked = [m for m in app.screen_stack if isinstance(m, ConfirmModal)]
            self.assertEqual(len(stacked), 1)
            self.assertGreater(app.screen.seq, first_seq)
            await pilot.press("n")
            await _wait_until(pilot, lambda: not app._agent_busy, "回复落定")
            results = "\n".join(
                text
                for messages in model.seen_messages
                for text in _tool_result_texts(messages)
            )
            self.assertIn("payload-a-7311", results)
            self.assertNotIn("payload-b-7322", results)
            # history 是 (kind, text, tone) 三元组；崩溃原文会落在这里
            transcript = "\n".join(str(record[1]) for record in app.history)
            self.assertTrue(transcript)
            self.assertNotIn("暂时不可用", transcript)
            self.assertNotIn("DuplicateIds", transcript)


class ConcurrentEntriesTest(_Base):
    async def test_two_entries_queue_and_keep_own_answers(self) -> None:
        app = CifixChatApp(
            model=ScriptedToolModel([]), workspace_root=self.workspace
        )
        answers: dict[str, object] = {}

        async def ask(label: str, lines) -> None:
            answers[label] = await app._present_confirm(lines)

        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # 两路不同形态的入口同时到达：弹窗式（如恢复替换）与输入框式（如删人）
            app.run_worker(ask("modal", ["恢复替换确认？"]), exit_on_error=False)
            app.run_worker(ask("input", None), exit_on_error=False)
            await _wait_until(
                pilot, lambda: isinstance(app.screen, ConfirmModal), "先到的弹窗"
            )
            first_future = app._confirm_future
            await pilot.pause(0.1)
            # 后到的在排队：没有顶掉先到者的答复通道
            self.assertIs(app._confirm_future, first_future)
            self.assertEqual(answers, {})
            await pilot.press("y")
            await _wait_until(pilot, lambda: "modal" in answers, "先到者拿到答复")
            await _wait_until(
                pilot,
                lambda: app._confirm_future is not None
                and app._confirm_future is not first_future
                and not isinstance(app.screen, ConfirmModal),
                "后到者开始呈现",
            )
            self.assertNotIn("input", answers)
            app.submit_text("N")
            await _wait_until(pilot, lambda: "input" in answers, "后到者拿到答复")
        self.assertEqual(answers["modal"], ("Y", ""))
        self.assertEqual(answers["input"], "N")

    async def test_modal_answer_routes_to_its_own_request(self) -> None:
        app = CifixChatApp(
            model=ScriptedToolModel([]), workspace_root=self.workspace
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            loop = __import__("asyncio").get_running_loop()
            mine = loop.create_future()
            other = loop.create_future()
            modal = ConfirmModal(app, lines=["x"], preview_lines=None, future=mine)
            app._confirm_future = other  # 槽位指向别人也不串扰
            app.push_screen(modal)
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            self.assertEqual(mine.result(), ("N", ""))
            self.assertFalse(other.done())
            app._confirm_future = None

    async def test_modal_seq_increments_and_id_keeps_registry_name(self) -> None:
        a = ConfirmModal(object(), lines=["a"], preview_lines=None)
        b = ConfirmModal(object(), lines=["b"], preview_lines=None)
        self.assertEqual(b.seq, a.seq + 1)
        self.assertEqual(a.id, "confirm-modal")

if __name__ == "__main__":
    unittest.main()
