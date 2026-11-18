# -*- coding: utf-8 -*-
"""离线、确定性的 ``ChatModelBase`` 替身。

设计约束（WIRE1 任务）：
- 不发任何网络请求，不读取密钥，不实例化任何真实 provider 模型；
- 回复由构造参数给出的脚本决定：文本序列（按序取用）或
  关键词 -> 回复 的映射；
- 每次模型调用的输入摘要记录在 ``self.calls``，供测试断言；
- 返回的 ``ChatResponse`` 字段与 agentscope 2.0.11 实际定义一致。

AgentScope 2.0.11 的 ``ChatModelBase`` 不是 ``abc.ABC``，但
``ChatModelBase.__dict__`` 里 ``_call_api`` 带 ``__isabstractmethod__``；
子类只需实现 ``_call_api``（``__call__`` 负责重试与流式累积，直接复用基类）。
另经实测，``Agent.reply`` 在 ``_handle_incoming_messages`` 中无条件访问
``self.model.formatter.supported_input_media_types``，而基类并不提供
``formatter``，所以本替身额外挂一个最小的 ``FakeFormatter``。
"""

from __future__ import annotations

from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Mapping,
    Sequence,
)

from agentscope.credential import CredentialBase
from agentscope.formatter import FormatterBase
from agentscope.message import Msg, TextBlock, ThinkingBlock
from agentscope.model import ChatModelBase, ChatResponse, ChatUsage, FinishedReason
from agentscope.tool import ToolChoice

# 固定时间戳：让 ChatResponse / TextBlock 完全可复现（不依赖墙钟）。
_FAKE_CREATED_AT = "1970-01-01T00:00:00.000000"


def _message_text(msg: Msg) -> str:
    """尽量把一条消息的文本内容取出来，取不到时退回空串。"""
    try:
        text = msg.get_text_content()
    except Exception:  # pragma: no cover - 防御性兜底
        return ""
    return text or ""


def _summarize_messages(messages: Sequence[Msg]) -> list[dict[str, Any]]:
    """把消息列表压成可断言的摘要（role / name / text / block_types）。

    ``text`` 只取 ``TextBlock`` 文本；AgentScope 注入的运行时提示是
    ``HintBlock``（含当前时间，逐次不同），因此单独用 ``block_types``
    暴露块类型而不混入文本。
    """
    return [
        {
            "role": getattr(msg, "role", ""),
            "name": getattr(msg, "name", ""),
            "text": _message_text(msg),
            "block_types": [
                type(block).__name__ for block in msg.get_content_blocks()
            ],
        }
        for msg in messages
    ]


class FakeFormatter(FormatterBase):
    """最小 formatter：只为满足 ``Agent`` 对 ``model.formatter`` 的访问。

    AgentScope 的 ``Agent`` 只用它读取 ``supported_input_media_types``
    （判断输入里的 DataBlock 是否受支持）；本平台的离线接线不传多模态数据，
    因此 ``format`` 只做文本透传，返回 OpenAI 风格的简化结构。
    """

    async def format(
        self,
        msgs: list[Msg],
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        return [
            {
                "role": msg.role,
                "name": msg.name,
                "content": _message_text(msg),
            }
            for msg in msgs
        ]


class FakeChatModel(ChatModelBase):
    """按脚本回复的离线 ChatModel。

    Args:
        replies: 文本回复序列，按调用顺序依次取用；序列用尽后重复最后一条。
        keyword_replies: ``关键词 -> 回复`` 映射，按插入顺序匹配；命中即返回。
            与 ``replies`` 同时提供时，关键词优先，且关键词调用不消耗序列。
        fallback_reply: 既无关键词命中、又无 ``replies`` 时的兜底回复。
        model_name: 记录在 ``self.model`` 中的模型名（默认 ``fake-chat-model``）。
        context_size: 上下文窗口大小（仅影响 Agent 的上下文压缩阈值判断）。

    三个回复来源至少要给一个，否则构造时直接报错（避免静默返回空回复）。

    Attributes:
        calls: 每次模型调用的记录，元素为 dict，键为
            ``index / messages / input_text / tools / tool_choice /
            reply / matched_by``。
    """

    def __init__(
        self,
        replies: Sequence[str] | None = None,
        keyword_replies: Mapping[str, str] | None = None,
        fallback_reply: str | None = None,
        model_name: str = "fake-chat-model",
        context_size: int = 32768,
    ) -> None:
        if not replies and not keyword_replies and fallback_reply is None:
            raise ValueError(
                "FakeChatModel 需要 replies、keyword_replies 或 "
                "fallback_reply 之一，不能为空脚本。",
            )

        super().__init__(
            credential=CredentialBase(name="fake-offline-credential"),
            model=model_name,
            parameters=self.Parameters(),
            stream=False,
            max_retries=0,
            retry_delay=0.0,
            context_size=context_size,
        )

        # Agent 在 _handle_incoming_messages 里会访问该属性（见模块 docstring）。
        self.formatter = FakeFormatter()

        self._replies: tuple[str, ...] = tuple(replies or ())
        self._keyword_replies: dict[str, str] = dict(keyword_replies or {})
        self._fallback_reply = fallback_reply
        self._script_cursor = 0
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 公开辅助
    # ------------------------------------------------------------------
    @property
    def call_count(self) -> int:
        """模型被调用的次数。"""
        return len(self.calls)

    def reset(self) -> None:
        """清空调用记录并把脚本游标拨回开头（便于测试复用同一实例）。"""
        self._script_cursor = 0
        self.calls.clear()

    # ------------------------------------------------------------------
    # ChatModelBase 要求的最小实现
    # ------------------------------------------------------------------
    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse:
        """返回一条脚本预定的完整 ``ChatResponse``（不流式）。"""
        reply, matched_by = self._select_reply(messages)
        index = len(self.calls)

        message_summaries = _summarize_messages(messages)
        input_text = "\n".join(
            item["text"] for item in message_summaries if item["text"]
        )
        input_tokens = await self.count_tokens(messages, tools)
        output_tokens = max(1, (len(reply.encode("utf-8")) + 3) // 4)

        # 先记账：记录本次调用的输入摘要与选中的回复。
        self.calls.append(
            {
                "index": index,
                "messages": message_summaries,
                "input_text": input_text,
                "tools": [tool["function"]["name"] for tool in (tools or [])],
                "tool_choice": repr(tool_choice),
                "reply": reply,
                "matched_by": matched_by,
            },
        )

        return ChatResponse(
            content=[
                TextBlock(
                    text=reply,
                    id=f"fake-text-{index:04d}",
                    created_at=_FAKE_CREATED_AT,
                    finished_at=_FAKE_CREATED_AT,
                ),
            ],
            is_last=True,
            id=f"fake-response-{index:04d}",
            created_at=_FAKE_CREATED_AT,
            usage=ChatUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                time=0.0,
            ),
            finished_reason=FinishedReason.COMPLETED,
            metadata={"fake": True, "matched_by": matched_by},
        )

    # ------------------------------------------------------------------
    # 脚本选择
    # ------------------------------------------------------------------
    def _select_reply(self, messages: Sequence[Msg]) -> tuple[str, str]:
        """按「关键词优先、其次序列、最后兜底」的规则选出一条回复。"""
        input_text = "\n".join(_message_text(msg) for msg in messages)

        for keyword, reply in self._keyword_replies.items():
            if keyword and keyword in input_text:
                return reply, f"keyword:{keyword}"

        if self._replies:
            position = min(self._script_cursor, len(self._replies) - 1)
            self._script_cursor += 1
            return self._replies[position], f"script:{position}"

        assert self._fallback_reply is not None  # 构造时已保证
        return self._fallback_reply, "fallback"


class ChunkedFakeChatModel(FakeChatModel):
    """分段流式的离线 ChatModel：专供「增量渲染」测试使用。

    与父类的区别只在 ``_call_api`` 的返回形态：本类返回异步生成器，
    按 ``chunks`` 逐段产出 ``is_last=False`` 的 ``ChatResponse``，
    最后再产出一条 ``is_last=True`` 的完整响应 —— 与真实流式 provider
    （OpenAI 兼容的 SSE 分块 + 末块 usage）同形。父类行为完全不变。

    Args:
        chunks: 分段文本；逐段拼接即完整回复（至少一段）。
        gate: 可选异步门控，``await gate(index)`` 在产出第 ``index`` 段
            之前调用。测试用 ``asyncio.Event`` 逐段放行，不靠 sleep 竞速。
        model_name: 记录在 ``self.model`` 的模型名。
        output_tokens: 末块上报的输出 token 数；``None`` 时按字节估算
            （与父类同一公式）。

    Attributes:
        calls: 与父类同形；另加 ``streamed=True`` 便于断言走了流式路径。
        emitted: 已产出的分段数（含门控阻塞前已放行的段）。
    """

    def __init__(
        self,
        chunks: Sequence[str],
        gate: Callable[[int], Awaitable[None]] | None = None,
        model_name: str = "fake-stream-model",
        output_tokens: int | None = None,
    ) -> None:
        if not chunks:
            raise ValueError("ChunkedFakeChatModel 需要至少一个文本分段。")

        reply = "".join(chunks)
        super().__init__(fallback_reply=reply, model_name=model_name)
        # 本类走分块生成器（与真实流式 provider 同形）；父类默认 stream=False。
        self.stream = True
        self.chunks: tuple[str, ...] = tuple(chunks)
        self.gate = gate
        self._output_tokens = output_tokens
        self.emitted = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        """返回分块异步生成器（形态与真实流式模型一致）。"""
        index = len(self.calls)
        reply = "".join(self.chunks)
        message_summaries = _summarize_messages(messages)
        input_text = "\n".join(
            item["text"] for item in message_summaries if item["text"]
        )
        input_tokens = await self.count_tokens(messages, tools)
        output_tokens = (
            self._output_tokens
            if self._output_tokens is not None
            else max(1, (len(reply.encode("utf-8")) + 3) // 4)
        )

        self.calls.append(
            {
                "index": index,
                "messages": message_summaries,
                "input_text": input_text,
                "tools": [tool["function"]["name"] for tool in (tools or [])],
                "tool_choice": repr(tool_choice),
                "reply": reply,
                "matched_by": "chunks",
                "streamed": True,
            },
        )

        async def stream() -> AsyncGenerator[ChatResponse, None]:
            for chunk_index, delta in enumerate(self.chunks):
                if self.gate is not None:
                    await self.gate(chunk_index)
                self.emitted += 1
                yield ChatResponse(
                    content=[
                        TextBlock(
                            text=delta,
                            id=f"fake-stream-text-{index:04d}",
                            created_at=_FAKE_CREATED_AT,
                        ),
                    ],
                    is_last=False,
                    id=f"fake-stream-response-{index:04d}",
                    created_at=_FAKE_CREATED_AT,
                )
            yield ChatResponse(
                content=[
                    TextBlock(
                        text=reply,
                        id=f"fake-stream-text-{index:04d}",
                        created_at=_FAKE_CREATED_AT,
                        finished_at=_FAKE_CREATED_AT,
                    ),
                ],
                is_last=True,
                id=f"fake-stream-response-{index:04d}",
                created_at=_FAKE_CREATED_AT,
                usage=ChatUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    time=0.0,
                ),
                finished_reason=FinishedReason.COMPLETED,
                metadata={"fake": True, "matched_by": "chunks"},
            )

        return stream()


class ThinkingChunkedFakeChatModel(FakeChatModel):
    """分段流式的离线 ChatModel：先产出思考分块，再产出正文分块。

    只供「思考可见」测试使用。``FakeChatModel`` 与 ``ChunkedFakeChatModel``
    的行为不经过本类，保持原样。思考文本只出现在 ``ThinkingBlock`` 里，
    正文 ``TextBlock`` 与同内容的非流式最终消息一致。

    Args:
        thinking_chunks: 思考分段（至少一段）。逐段拼接即完整思考。
        text_chunks: 正文分段（至少一段）。逐段拼接即完整回复。
        gate: 可选 ``await gate(index)``，下标先覆盖思考段、再覆盖正文段
            （``emit_text_deltas=False`` 时只有思考段）。
        emit_text_deltas: 为假时不发正文增量，正文只出现在最终 ``is_last``
            块里。用来测「没有正文增量就测不到思考时长」。
        model_name: 记录在 ``self.model`` 的模型名。
        output_tokens: 末块上报的输出 token 数；``None`` 时按正文字节估算。
    """

    def __init__(
        self,
        thinking_chunks: Sequence[str],
        text_chunks: Sequence[str],
        gate: Callable[[int], Awaitable[None]] | None = None,
        emit_text_deltas: bool = True,
        model_name: str = "fake-thinking-model",
        output_tokens: int | None = None,
    ) -> None:
        if not thinking_chunks:
            raise ValueError("ThinkingChunkedFakeChatModel 需要至少一段思考。")
        if not text_chunks:
            raise ValueError("ThinkingChunkedFakeChatModel 需要至少一段正文。")

        reply = "".join(text_chunks)
        super().__init__(fallback_reply=reply, model_name=model_name)
        self.stream = True
        self.thinking_chunks: tuple[str, ...] = tuple(thinking_chunks)
        self.text_chunks: tuple[str, ...] = tuple(text_chunks)
        self.gate = gate
        self.emit_text_deltas = emit_text_deltas
        self._output_tokens = output_tokens
        self.emitted = 0

    async def _call_api(
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        index = len(self.calls)
        reply = "".join(self.text_chunks)
        thinking = "".join(self.thinking_chunks)
        message_summaries = _summarize_messages(messages)
        input_text = "\n".join(
            item["text"] for item in message_summaries if item["text"]
        )
        input_tokens = await self.count_tokens(messages, tools)
        output_tokens = (
            self._output_tokens
            if self._output_tokens is not None
            else max(1, (len(reply.encode("utf-8")) + 3) // 4)
        )
        self.calls.append(
            {
                "index": index,
                "messages": message_summaries,
                "input_text": input_text,
                "tools": [tool["function"]["name"] for tool in (tools or [])],
                "tool_choice": repr(tool_choice),
                "reply": reply,
                "thinking": thinking,
                "matched_by": "thinking-chunks",
                "streamed": True,
            },
        )

        thinking_id = f"fake-thinking-{index:04d}"
        text_id = f"fake-thinking-text-{index:04d}"
        response_id = f"fake-thinking-response-{index:04d}"

        async def stream() -> AsyncGenerator[ChatResponse, None]:
            pieces: list[tuple[str, str]] = [
                ("thinking", delta) for delta in self.thinking_chunks
            ]
            if self.emit_text_deltas:
                pieces.extend(("text", delta) for delta in self.text_chunks)
            for chunk_index, (kind, delta) in enumerate(pieces):
                if self.gate is not None:
                    await self.gate(chunk_index)
                self.emitted += 1
                if kind == "thinking":
                    content = [
                        ThinkingBlock(
                            thinking=delta,
                            id=thinking_id,
                            created_at=_FAKE_CREATED_AT,
                        )
                    ]
                else:
                    content = [
                        TextBlock(
                            text=delta,
                            id=text_id,
                            created_at=_FAKE_CREATED_AT,
                        )
                    ]
                yield ChatResponse(
                    content=content,
                    is_last=False,
                    id=response_id,
                    created_at=_FAKE_CREATED_AT,
                )
            yield ChatResponse(
                content=[
                    ThinkingBlock(
                        thinking=thinking,
                        id=thinking_id,
                        created_at=_FAKE_CREATED_AT,
                        finished_at=_FAKE_CREATED_AT,
                    ),
                    TextBlock(
                        text=reply,
                        id=text_id,
                        created_at=_FAKE_CREATED_AT,
                        finished_at=_FAKE_CREATED_AT,
                    ),
                ],
                is_last=True,
                id=response_id,
                created_at=_FAKE_CREATED_AT,
                usage=ChatUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    time=0.0,
                ),
                finished_reason=FinishedReason.COMPLETED,
                metadata={"fake": True, "matched_by": "thinking-chunks"},
            )

        return stream()
