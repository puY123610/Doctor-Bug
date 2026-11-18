# -*- coding: utf-8 -*-
"""AgentScope 最小接线演示：FakeChatModel -> Agent -> reply，全程离线。

运行：
    .venv/bin/python -m agentscope_ext.wiring_demo

退出码 0 表示接线成功；打印内容包含预设回复文本与模型调用次数。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from agentscope.agent import Agent
from agentscope.message import Msg, UserMsg

from .fake_model import FakeChatModel

# 本项目角色：计费专家（对应 demo_repo 中 billing 模块的修复职责）。
AGENT_NAME = "billing-specialist"
SYSTEM_PROMPT = (
    "你是 billing-specialist（计费专家），负责修复演示仓库 backoffice/billing.py "
    "中由冻结验收器报告的缺陷。你的回答必须给出可核对的定位步骤与最小修复建议，"
    "不得臆造未验证的结论。"
)
# 演示输入：调度层把冻结验收器的失败证据交给该专家。
USER_MESSAGE = (
    "冻结验收器报告失败：tests/test_billing.py::test_tax_on_discounted_total。"
    "现象是折扣后总额的税额与预期不符。请给出你的定位步骤与最小修复建议。"
)
# 脚本预设回复（演示用，非真实模型推理结果）。
SCRIPTED_REPLY = (
    "定位步骤：1) 打开 backoffice/billing.py，确认税额是按折扣前金额还是折扣后金额计算；"
    "2) 对照 tests/test_billing.py::test_tax_on_discounted_total 的期望值，"
    "确认税基应为折后小计。最小修复建议：先算折扣后小计，再以该小计乘税率，"
    "最后输出总额；不要改动其他计费路径。"
)

DEFAULT_SCRIPT = (SCRIPTED_REPLY,)


@dataclass
class DemoResult:
    """一次演示运行的产物。"""

    reply_text: str
    reply_msg: Msg
    model: FakeChatModel

    @property
    def model_calls(self) -> int:
        return self.model.call_count


def build_agent(
    model: FakeChatModel | None = None,
) -> tuple[Agent, FakeChatModel]:
    """构造 Agent + FakeChatModel；未传模型时使用默认脚本。"""
    model = model or FakeChatModel(replies=DEFAULT_SCRIPT)
    agent = Agent(
        name=AGENT_NAME,
        system_prompt=SYSTEM_PROMPT,
        model=model,
    )
    return agent, model


async def run_demo(
    model: FakeChatModel | None = None,
    user_text: str = USER_MESSAGE,
) -> DemoResult:
    """向 Agent 发一条消息，返回回复文本与模型调用记录。"""
    agent, model = build_agent(model)
    reply_msg = await agent.reply(UserMsg(name="dispatcher", content=user_text))
    return DemoResult(
        reply_text=reply_msg.get_text_content() or "",
        reply_msg=reply_msg,
        model=model,
    )


def main() -> int:
    result = asyncio.run(run_demo())
    print(f"[agent] {AGENT_NAME}")
    print(f"[user] {USER_MESSAGE}")
    print(f"[reply] {result.reply_text}")
    print(f"[note] 以上回复为 FakeChatModel 的脚本预设，非真实模型推理")
    print(f"[model_calls] {result.model_calls}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
