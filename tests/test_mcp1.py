# -*- coding: utf-8 -*-
"""MCP1: config loading, engine semantics, gated real calls."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.mcp_tools import MCPToolSet, load_mcp_servers
from core.policy import (
    PolicyContext,
    PolicyEngine,
    Rule,
    format_rule_toml,
    project_policy_path,
)

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "mcp_echo_server.py"
)


def _ctx(surface: str) -> PolicyContext:
    return PolicyContext(
        mode="ask",
        surface=surface,
        workspace=None,
        role="worker" if surface == "runtime" else "secretary",
        is_ai=True,
        actor="worker" if surface == "runtime" else "secretary",
    )


async def _collect_text(tool, **kwargs) -> str:
    parts: list[str] = []
    async for chunk in tool.call(**kwargs):
        for block in chunk.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
    return "".join(parts)


class ConfigLoadTests(unittest.TestCase):
    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp1-") as tmp:
            self.assertEqual(
                load_mcp_servers(Path(tmp) / "nope.json"), []
            )

    def test_valid_and_invalid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp1-") as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text(
                json.dumps(
                    {
                        "servers": [
                            {
                                "name": "echo-server",
                                "command": sys.executable,
                                "args": [str(FIXTURE)],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            specs = load_mcp_servers(path)
            self.assertEqual(len(specs), 1)
            self.assertEqual(specs[0]["name"], "echo-server")
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_mcp_servers(path)
            path.write_text(
                json.dumps({"servers": [{"name": "x"}]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                load_mcp_servers(path)


class EngineSemanticsTests(unittest.TestCase):
    def test_worker_default_deny_secretary_ask(self) -> None:
        engine = PolicyEngine(workspace=None)
        worker = engine.evaluate(
            "worker", "mcp_call", "echo-server:echo",
            ctx=_ctx("runtime"),
        )
        self.assertEqual(worker.verdict, "deny")
        self.assertIn("MCP", worker.reason)
        secretary = engine.evaluate(
            "secretary", "mcp_call", "echo-server:echo",
            ctx=_ctx("interactive"),
        )
        self.assertEqual(secretary.verdict, "ask")

    def test_allow_rule_opens_the_gate(self) -> None:
        engine = PolicyEngine(workspace=None)
        engine.add_session_rule(
            Rule(
                actor="worker",
                action="mcp_call",
                glob="echo-server:*",
                decision="allow",
            )
        )
        decision = engine.evaluate(
            "worker", "mcp_call", "echo-server:echo",
            ctx=_ctx("runtime"),
        )
        self.assertEqual(decision.verdict, "allow")


class GatedCallTests(unittest.TestCase):
    def _workspace(self, tmp: str, *, allow: bool) -> Path:
        workspace = Path(tmp)
        if allow:
            policy_file = project_policy_path(workspace)
            policy_file.parent.mkdir(parents=True, exist_ok=True)
            rule = Rule(
                actor="worker",
                action="mcp_call",
                glob="echo-server:*",
                decision="allow",
            )
            policy_file.write_text(
                format_rule_toml(rule), encoding="utf-8"
            )
        return workspace

    def _config(self, tmp: str) -> Path:
        path = Path(tmp) / "mcp.json"
        path.write_text(
            json.dumps(
                {
                    "servers": [
                        {
                            "name": "echo-server",
                            "command": sys.executable,
                            "args": [str(FIXTURE)],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_denied_without_rule_allowed_with_rule(self) -> None:
        async def scenario() -> tuple[str, str]:
            with tempfile.TemporaryDirectory(
                prefix="mcp1-deny-"
            ) as tmp:
                toolset = MCPToolSet(
                    workspace=self._workspace(tmp, allow=False),
                    config_path=self._config(tmp),
                )
                tools = await toolset.open()
                self.assertEqual(toolset.notices, [])
                names = {tool.name for tool in tools}
                self.assertIn("mcp__echo-server__echo", names)
                echo = next(
                    t for t in tools
                    if t.name == "mcp__echo-server__echo"
                )
                denied_text = await _collect_text(echo, text="hello")
                await toolset.close()
            with tempfile.TemporaryDirectory(
                prefix="mcp1-allow-"
            ) as tmp:
                toolset = MCPToolSet(
                    workspace=self._workspace(tmp, allow=True),
                    config_path=self._config(tmp),
                )
                tools = await toolset.open()
                echo = next(
                    t for t in tools
                    if t.name == "mcp__echo-server__echo"
                )
                allowed_text = await _collect_text(echo, text="hello")
                await toolset.close()
            return denied_text, allowed_text

        denied_text, allowed_text = asyncio.run(scenario())
        self.assertIn("被拒绝", denied_text)
        self.assertEqual(allowed_text, "hello")

    def test_broken_server_becomes_notice_not_crash(self) -> None:
        async def scenario() -> tuple[list, list]:
            with tempfile.TemporaryDirectory(
                prefix="mcp1-bad-"
            ) as tmp:
                path = Path(tmp) / "mcp.json"
                path.write_text(
                    json.dumps(
                        {
                            "servers": [
                                {
                                    "name": "ghost",
                                    "command": "/nonexistent/mcp-bin",
                                    "args": [],
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                toolset = MCPToolSet(
                    workspace=Path(tmp), config_path=path
                )
                tools = await toolset.open()
                await toolset.close()
                return tools, toolset.notices

        tools, notices = asyncio.run(scenario())
        self.assertEqual(tools, [])
        self.assertEqual(len(notices), 1)
        self.assertIn("ghost", notices[0])


if __name__ == "__main__":
    unittest.main()
