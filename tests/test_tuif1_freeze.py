"""TUIF1：对话路径工具调用不再冻结事件循环 + scripted 默认说法清理。

文件名避开既有 test_tuif1.py（那是 F1 会话管理的测试）。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import unittest
from unittest import mock

from agentscope.tool import FunctionTool

from core import dispatcher

SLOW_S = 0.4
BEAT_S = 0.01


def slow_sync_tool(value: str = "x") -> str:
    """假的慢同步工具：内部 time.sleep，模拟判卷子进程/整树哈希。

    Args:
        value: 原样回显的值。
    """
    time.sleep(SLOW_S)
    return f"done:{value}:{threading.current_thread() is threading.main_thread()}"


async def _call_with_heartbeat(tool: FunctionTool) -> tuple[int, str]:
    beats = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal beats
        while not stop.is_set():
            beats += 1
            await asyncio.sleep(BEAT_S)

    hb = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)  # 让心跳先跑起来
    before = beats
    chunk = await tool.call(value="ok")
    during = beats - before
    stop.set()
    await hb
    text = "".join(getattr(b, "text", "") for b in chunk.content)
    return during, text


class HeartbeatRegressionTest(unittest.TestCase):
    def test_slow_tool_via_platform_path_keeps_loop_alive(self) -> None:
        # 与真实平台工具相同的包装路径：_allowed_tool -> FunctionTool.call
        tool = dispatcher._allowed_tool(slow_sync_tool, is_read_only=True)
        during, text = asyncio.run(_call_with_heartbeat(tool))
        self.assertGreater(during, 0)
        self.assertEqual(text, "done:ok:False")  # 结果正确，且不在主线程跑

    def test_control_unwrapped_sync_tool_freezes_loop(self) -> None:
        # 对照：未卸载的同步工具执行期间心跳一次都不前进（证明测试有效）
        tool = FunctionTool(func=slow_sync_tool, is_read_only=True)
        during, text = asyncio.run(_call_with_heartbeat(tool))
        self.assertEqual(during, 0)
        self.assertEqual(text, "done:ok:True")

    def test_every_platform_tool_is_offloaded_with_same_signature(self) -> None:
        for tool in dispatcher.build_tools():
            with self.subTest(tool=tool.name):
                func = tool._func
                self.assertTrue(inspect.iscoroutinefunction(func))
                original = getattr(dispatcher, tool.name)
                self.assertIs(func.__wrapped__, original)
                self.assertEqual(
                    inspect.signature(func), inspect.signature(original)
                )

    def test_platform_tool_runs_off_loop_thread(self) -> None:
        seen: list[bool] = []

        def fake_check() -> str:
            seen.append(threading.current_thread() is threading.main_thread())
            time.sleep(SLOW_S)
            return "体检完成"

        with mock.patch.object(dispatcher, "platform_check", fake_check):
            tool = dispatcher._allowed_tool(
                dispatcher.platform_check, is_read_only=True
            )
            chunk = asyncio.run(tool.call())
        self.assertEqual(seen, [False])
        self.assertEqual(chunk.content[0].text, "体检完成")


class DefaultActorsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dispatcher.default_actors()
        self.addCleanup(dispatcher._set_default_actors, self._saved)

    def test_prompt_never_claims_scripted_default(self) -> None:
        self.assertNotIn("默认执行后端 scripted", dispatcher.DISPATCHER_SYSTEM_PROMPT)
        prompt = dispatcher._dispatcher_prompt("agentscope", "/tmp/ws")
        self.assertIn("默认执行后端 agentscope", prompt)
        self.assertNotIn("默认执行后端 scripted", prompt)
        self.assertNotIn("{default_actors}", prompt)
        doc = dispatcher.run_repair.__doc__ or ""
        self.assertNotIn("scripted（默认）", doc)

    def test_dispatcher_default_matches_tui_run_backend(self) -> None:
        from core import tui

        app_backend = tui._ACTOR_BACKENDS[0]
        self.assertEqual(dispatcher.default_actors(), app_backend)
        self.assertIsNone(
            inspect.signature(dispatcher.run_repair).parameters["actors"].default
        )

    def test_omitted_actors_uses_session_default_and_hits_paid_gate(self) -> None:
        dispatcher._set_default_actors("agentscope")
        with mock.patch.object(
            dispatcher, "_workspace_for_tool", return_value=("/tmp/ws", None)
        ), mock.patch.object(dispatcher, "run_arm") as run_arm:
            text = dispatcher.run_repair()
        run_arm.assert_not_called()
        self.assertIn("拒绝", text)
        self.assertIn("actors=agentscope", text)

    def test_set_dispatcher_default_actors_updates_tool_default(self) -> None:
        agent = mock.Mock()
        agent._drbug_workspace = "/tmp/ws"
        agent._drbug_vision = False
        dispatcher.set_dispatcher_default_actors(agent, "llm")
        self.assertEqual(dispatcher.default_actors(), "llm")
        self.assertIn("默认执行后端 llm", agent._system_prompt)


if __name__ == "__main__":
    unittest.main()
