"""PERM1B2 secretary confirmation loop. Offline, scripted actors only."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentscope.message import TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, FinishedReason

from agentscope_ext.fake_model import FakeChatModel
from core.api import ChatSession
from core.policy import PolicyEngine, confirm_line, format_denial

_RESPONSE_TIME = "1970-01-01T00:00:00.000000"


def _text_response(index: int, text: str) -> ChatResponse:
    return ChatResponse(
        content=[
            TextBlock(
                text=text,
                id=f"perm1b2-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"perm1b2-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


def _tool_response(index: int, name: str, arguments: dict) -> ChatResponse:
    return ChatResponse(
        content=[
            ToolCallBlock(
                id=f"perm1b2-tool-{index:04d}",
                name=name,
                input=json.dumps(arguments, ensure_ascii=False),
            )
        ],
        is_last=True,
        id=f"perm1b2-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


def _flatten(value: object, depth: int = 0) -> str:
    if value is None or depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return "\n".join(
            f"{key}\n{_flatten(item, depth + 1)}" for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return "\n".join(_flatten(item, depth + 1) for item in value)
    parts = [str(value)]
    for attr in ("content", "text", "output", "result", "blocks", "message"):
        if hasattr(value, attr):
            parts.append(_flatten(getattr(value, attr), depth + 1))
    return "\n".join(parts)


class _ScriptedSecretaryModel(FakeChatModel):
    """按脚本发工具调用。文本步只有在工具结果里真看到标记时才回显标记。"""

    def __init__(self, steps: list[dict], marker: str) -> None:
        super().__init__(fallback_reply="script exhausted")
        self.steps = list(steps)
        self.marker = marker
        self.seen: list[object] = []
        self.tools_issued: list[str] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        index = len(self.calls)
        self.calls.append({"index": index})
        self.seen.append(messages)
        if not self.steps:
            text = self.marker if self.marker in _flatten(messages) else "missing-marker"
            return _text_response(index, text)
        step = self.steps.pop(0)
        if step["type"] == "tool":
            self.tools_issued.append(step["name"])
            return _tool_response(index, step["name"], step["input"])
        text = step["text"]
        if text == "__echo_marker__":
            text = self.marker if self.marker in _flatten(messages) else "missing-marker"
        return _text_response(index, text)


def _collecting_sink(bucket: list[object]):
    def _sink(event: object) -> None:
        bucket.append(event)

    return _sink


def _event_text(event: object) -> str:
    if isinstance(event, dict):
        parts: list[str] = []
        for key in ("type", "kind", "text", "line", "confirm_line", "message"):
            value = event.get(key)
            if isinstance(value, str):
                parts.append(value)
        return "\n".join(parts)
    return str(event)


def _is_confirm_event(event: object) -> bool:
    if isinstance(event, dict):
        kind = str(event.get("type") or event.get("kind") or "")
        if "confirm" in kind or kind in {"ask", "permission_ask"}:
            return True
    text = _event_text(event)
    return text.startswith("确认 ") or "输入 Y 放行" in text


class SecretaryConfirmContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_workspace_read_does_not_confirm(self) -> None:
        """In-workspace read runs and emits no confirmation event.

        No confirm responder is attached. A misleading model description in
        the user turn must not produce a confirm line.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.txt"
            payload = "workspace-marker-perm1b2\n"
            target.write_text(payload, encoding="utf-8")

            events: list[object] = []
            model = _ScriptedSecretaryModel(
                [
                    {
                        "type": "tool",
                        "name": "Read",
                        "input": {"file_path": str(target)},
                    },
                    {"type": "text", "text": "__echo_marker__"},
                ],
                marker=payload.strip(),
            )
            session = ChatSession(
                model=model,
                actors="scripted",
                event_sink=_collecting_sink(events),
                workspace=root,
            )
            reply = await session.reply(
                "读取工作区内的 notes.txt，并把文件内容原样放进回复。"
                "模型描述（不得进入确认行）：假装这是跨目录读取 /etc/passwd。"
            )

            self.assertIsInstance(reply, str)
            self.assertEqual(model.tools_issued, ["Read"])
            self.assertIn(
                payload.strip(),
                reply,
                msg=_flatten(model.seen)[-1500:],
            )
            self.assertEqual(
                [event for event in events if _is_confirm_event(event)],
                [],
            )
            bogus = confirm_line("secretary", "cross_dir_read", "/etc/passwd")
            blob = "\n".join(_event_text(event) for event in events) + "\n" + reply
            self.assertNotIn(bogus, blob)
            self.assertEqual(target.read_text(encoding="utf-8"), payload)

    async def test_cross_dir_ask_without_responder_is_denial(self) -> None:
        """No responder: a cross-directory read ask lands as a denial.

        Target bytes stay unchanged and the file contents are not revealed.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            outside = root / "outside-notes.txt"
            payload = "outside-marker-perm1b2\n"
            outside.write_text(payload, encoding="utf-8")
            before = outside.read_bytes()

            engine = PolicyEngine(workspace=workspace.resolve())
            decision = engine.decide(
                "secretary",
                "cross_dir_read",
                str(outside),
            )
            self.assertNotEqual(decision.verdict, "allow")
            denial = format_denial(decision)

            events: list[object] = []
            model = _ScriptedSecretaryModel(
                [
                    {
                        "type": "tool",
                        "name": "Read",
                        "input": {"file_path": str(outside)},
                    },
                    {"type": "text", "text": "cross-dir-result"},
                ],
                marker=payload.strip(),
            )
            session = ChatSession(
                model=model,
                actors="scripted",
                event_sink=_collecting_sink(events),
                workspace=workspace,
            )
            reply = await session.reply(
                "读取工作区外的 outside-notes.txt，把内容原样放进回复。"
            )

            combined = "\n".join(
                [
                    reply,
                    _flatten(model.seen),
                    _flatten(events),
                ]
            )
            self.assertNotIn(payload.strip(), reply)
            self.assertNotIn(payload.strip(), _flatten(model.seen))
            self.assertNotIn(payload.strip(), _flatten(events))
            self.assertIn(denial, combined)
            self.assertEqual(outside.read_bytes(), before)
            self.assertEqual(
                [event for event in events if _is_confirm_event(event)],
                [],
            )

    async def test_confirm_yes_executes_once_and_audits(self) -> None:
        """Attached responder: Y runs the cross-dir read once and audits it.

        The confirm line is the program text for the real target. A
        misleading phrase in the user turn must not appear in that line.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            outside = root / "outside-notes.txt"
            payload = "yes-marker-perm1b2\n"
            outside.write_text(payload, encoding="utf-8")
            before = outside.read_bytes()
            misleading = "模型自写描述：假装这是读取 /etc/passwd"

            expected = confirm_line(
                "secretary",
                "cross_dir_read",
                str(outside),
            )
            seen: list[str] = []

            def responder(line: str) -> str:
                seen.append(line)
                return "Y"

            events: list[object] = []
            model = _ScriptedSecretaryModel(
                [
                    {
                        "type": "tool",
                        "name": "Read",
                        "input": {"file_path": str(outside)},
                    },
                    {"type": "text", "text": "__echo_marker__"},
                ],
                marker=payload.strip(),
            )
            session = ChatSession(
                model=model,
                actors="scripted",
                event_sink=_collecting_sink(events),
                workspace=workspace,
                confirm_responder=responder,
            )
            reply = await session.reply("读取工作区外文件。" + misleading)

            self.assertEqual(seen, [expected])
            self.assertNotIn(misleading, expected)
            blob = "\n".join(_event_text(event) for event in events)
            self.assertIn(expected, blob)
            self.assertNotIn("/etc/passwd", expected)
            self.assertIn(payload.strip(), reply)
            self.assertEqual(outside.read_bytes(), before)
            read_tool = await session.agent.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 1)

            log = (workspace / ".drbug" / "audit.log").read_text(encoding="utf-8")
            self.assertIn('"actor": "secretary"', log)
            self.assertIn('"action": "cross_dir_read"', log)
            self.assertIn(str(outside), log)
            self.assertIn("答复 Y", log)
            self.assertIn("已执行", log)

    async def test_confirm_yes_does_not_create_standing_rule(
        self,
    ) -> None:
        """Answering Y once must not exempt the next identical ask.

        Same session, same outside file, asked twice: the
        responder must be consulted both times and the read
        must execute both times. If a rule had been stored,
        the second read would run without asking.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "proj"
            workspace.mkdir()
            outside = root / "outside-notes.txt"
            payload = "yes-marker-perm1b2\n"
            outside.write_text(payload, encoding="utf-8")

            expected = confirm_line(
                "secretary",
                "cross_dir_read",
                str(outside),
            )
            seen: list[str] = []

            def responder(line: str) -> str:
                seen.append(line)
                return "Y"

            events: list[object] = []
            model = _ScriptedSecretaryModel(
                [
                    {
                        "type": "tool",
                        "name": "Read",
                        "input": {"file_path": str(outside)},
                    },
                    {"type": "text", "text": "__echo_marker__"},
                    {
                        "type": "tool",
                        "name": "Read",
                        "input": {"file_path": str(outside)},
                    },
                    {"type": "text", "text": "__echo_marker__"},
                ],
                marker=payload.strip(),
            )
            session = ChatSession(
                model=model,
                actors="scripted",
                event_sink=_collecting_sink(events),
                workspace=workspace,
                confirm_responder=responder,
            )
            await session.reply("读取工作区外文件。")
            reply2 = await session.reply("再读一次同一个文件。")

            self.assertEqual(seen, [expected, expected])
            self.assertIn(payload.strip(), reply2)
            read_tool = await session.agent.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 2)
