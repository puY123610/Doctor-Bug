# -*- coding: utf-8 -*-
"""WIRE1：FakeChatModel 与 AgentScope Agent 的离线接线测试。

全程不实例化任何真实 provider 模型、不读密钥、不联网；
AgentScope 只提供基类与响应类型。
"""

from __future__ import annotations

import asyncio
import unittest

from agentscope.message import TextBlock, UserMsg
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage

from agentscope_ext import wiring_demo
from agentscope_ext.fake_model import FakeChatModel, FakeFormatter


def run_sync(coro):
    """在同步 unittest 里跑一段协程。"""
    return asyncio.run(coro)


class FakeChatModelContractTest(unittest.TestCase):
    """替身本身：类型、响应结构、脚本选择与调用记录。"""

    def test_is_chat_model_base_and_returns_chat_response(self) -> None:
        model = FakeChatModel(replies=["第一句"])
        self.assertIsInstance(model, ChatModelBase)
        self.assertEqual(FakeChatModel.__module__, "agentscope_ext.fake_model")
        self.assertIsInstance(model.formatter, FakeFormatter)
        self.assertEqual(model.formatter.supported_input_media_types, [])

        response = run_sync(model([UserMsg(name="u", content="你好")]))

        self.assertIsInstance(response, ChatResponse)
        self.assertTrue(response.is_last)
        self.assertEqual(len(response.content), 1)
        self.assertIsInstance(response.content[0], TextBlock)
        self.assertEqual(response.content[0].text, "第一句")
        self.assertIsInstance(response.usage, ChatUsage)
        self.assertEqual(response.usage.time, 0.0)
        # 固定 id / 时间戳，响应完全可复现
        self.assertEqual(response.id, "fake-response-0000")
        self.assertEqual(response.created_at, "1970-01-01T00:00:00.000000")
        self.assertEqual(response.metadata["matched_by"], "script:0")

    def test_script_advances_in_order_then_repeats_last(self) -> None:
        model = FakeChatModel(replies=("one", "two"))
        user = UserMsg(name="u", content="go")

        texts = [run_sync(model([user])).content[0].text for _ in range(3)]

        self.assertEqual(texts, ["one", "two", "two"])
        self.assertEqual(
            [call["matched_by"] for call in model.calls],
            ["script:0", "script:1", "script:1"],
        )
        self.assertEqual(model.call_count, 3)

    def test_keyword_mapping_selects_reply_by_input(self) -> None:
        model = FakeChatModel(
            keyword_replies={"库存": "inventory-fix", "鉴权": "auth-fix"},
        )

        inventory = run_sync(model([UserMsg(name="u", content="库存 双扣")]))
        auth = run_sync(model([UserMsg(name="u", content="退款 鉴权 缺失")]))

        self.assertEqual(inventory.content[0].text, "inventory-fix")
        self.assertEqual(auth.content[0].text, "auth-fix")
        self.assertEqual(
            [call["matched_by"] for call in model.calls],
            ["keyword:库存", "keyword:鉴权"],
        )

    def test_keyword_takes_precedence_without_consuming_script(self) -> None:
        model = FakeChatModel(
            replies=("scripted",),
            keyword_replies={"特殊": "keyworded"},
        )

        keyword_reply = run_sync(model([UserMsg(name="u", content="特殊")]))
        script_reply = run_sync(model([UserMsg(name="u", content="普通")]))

        self.assertEqual(keyword_reply.content[0].text, "keyworded")
        self.assertEqual(script_reply.content[0].text, "scripted")

    def test_call_records_input_summary(self) -> None:
        model = FakeChatModel(replies=["ok"])
        run_sync(
            model(
                [UserMsg(name="dispatcher", content="修复 计费 税基")],
                tools=None,
            ),
        )

        call = model.calls[0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(call["messages"][0]["role"], "user")
        self.assertEqual(call["messages"][0]["name"], "dispatcher")
        self.assertEqual(call["messages"][0]["text"], "修复 计费 税基")
        self.assertEqual(call["input_text"], "修复 计费 税基")
        self.assertEqual(call["tools"], [])
        self.assertEqual(call["reply"], "ok")

    def test_empty_script_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            FakeChatModel()

    def test_reset_clears_calls_and_rewinds_script(self) -> None:
        model = FakeChatModel(replies=("one", "two"))
        user = UserMsg(name="u", content="go")
        run_sync(model([user]))
        run_sync(model([user]))

        model.reset()

        self.assertEqual(model.call_count, 0)
        self.assertEqual(run_sync(model([user])).content[0].text, "one")


class AgentWiringTest(unittest.TestCase):
    """Agent 接线：接受替身、回复等于脚本、调用记录正确。"""

    def test_agent_accepts_fake_model(self) -> None:
        model = FakeChatModel(replies=[wiring_demo.SCRIPTED_REPLY])

        agent, returned_model = wiring_demo.build_agent(model)

        self.assertIs(returned_model, model)
        self.assertIs(agent.model, model)
        self.assertEqual(agent.name, wiring_demo.AGENT_NAME)

    def test_demo_reply_equals_scripted_text(self) -> None:
        result = run_sync(wiring_demo.run_demo())

        self.assertEqual(result.reply_text, wiring_demo.SCRIPTED_REPLY)
        self.assertEqual(result.model_calls, 1)

    def test_demo_call_count_and_input_summary(self) -> None:
        result = run_sync(wiring_demo.run_demo())

        self.assertEqual(result.model.call_count, 1)
        call = result.model.calls[0]
        self.assertEqual(call["index"], 0)
        self.assertEqual(
            [item["role"] for item in call["messages"]],
            ["system", "user", "assistant"],
        )
        self.assertEqual(call["messages"][0]["text"], wiring_demo.SYSTEM_PROMPT)
        self.assertEqual(
            call["messages"][1],
            {
                "role": "user",
                "name": "dispatcher",
                "text": wiring_demo.USER_MESSAGE,
                "block_types": ["TextBlock"],
            },
        )
        # 第三块是 AgentScope 注入的运行时提示（HintBlock，含当前时间），
        # 不计入纯文本摘要。
        self.assertEqual(call["messages"][2]["block_types"], ["HintBlock"])
        self.assertEqual(call["messages"][2]["text"], "")
        self.assertIn("test_tax_on_discounted_total", call["input_text"])
        self.assertIn(wiring_demo.AGENT_NAME, call["input_text"])
        self.assertEqual(call["matched_by"], "script:0")
        self.assertEqual(call["reply"], wiring_demo.SCRIPTED_REPLY)
        self.assertEqual(call["tools"], [])
        self.assertEqual(call["tool_choice"], "None")

    def test_two_runs_are_deterministic(self) -> None:
        first = run_sync(wiring_demo.run_demo())
        second = run_sync(wiring_demo.run_demo())

        self.assertEqual(first.reply_text, second.reply_text)
        self.assertEqual(
            first.model.calls[0]["reply"],
            second.model.calls[0]["reply"],
        )
        self.assertEqual(
            first.model.calls[0]["matched_by"],
            second.model.calls[0]["matched_by"],
        )

    def test_no_real_provider_instance_is_used(self) -> None:
        result = run_sync(wiring_demo.run_demo())

        # 只用本仓库扩展模块里的替身；AgentScope 侧只出现基类/响应类型。
        self.assertEqual(
            type(result.model).__module__,
            "agentscope_ext.fake_model",
        )
        self.assertEqual(type(result.model).__name__, "FakeChatModel")
        self.assertIsInstance(result.model, ChatModelBase)
        self.assertNotIn("DeepSeek", type(result.model).__name__)


if __name__ == "__main__":
    unittest.main()
