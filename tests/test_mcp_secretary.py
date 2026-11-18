# -*- coding: utf-8 -*-
"""Backlog 8: HTTP MCP specs, secretary-surface gating, secretary attach."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import dispatcher
from core.mcp_tools import MCPToolSet, load_mcp_servers

PLATFORM = Path(__file__).resolve().parents[1]
ECHO_SERVER = PLATFORM / "tests" / "fixtures" / "mcp_echo_server.py"


def _write_config(root: Path, servers: list) -> Path:
    config = root / "mcp.json"
    config.write_text(
        json.dumps({"servers": servers}), encoding="utf-8"
    )
    return config


class HttpSpecTests(unittest.TestCase):
    def test_url_spec_loads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-http-") as tmp:
            config = _write_config(
                Path(tmp),
                [{"name": "remote", "url": "https://example.com/mcp"}],
            )
            specs = load_mcp_servers(config)
            self.assertEqual(specs[0]["url"], "https://example.com/mcp")
            self.assertEqual(specs[0]["headers"], {})

    def test_command_and_url_conflict_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-http-") as tmp:
            config = _write_config(
                Path(tmp),
                [
                    {
                        "name": "bad",
                        "command": "python3",
                        "url": "https://example.com/mcp",
                    }
                ],
            )
            with self.assertRaises(ValueError):
                load_mcp_servers(config)

    def test_bad_url_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-http-") as tmp:
            config = _write_config(
                Path(tmp), [{"name": "bad", "url": "ftp://x"}]
            )
            with self.assertRaises(ValueError):
                load_mcp_servers(config)

    def test_unreachable_http_server_becomes_notice(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-http-") as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                [{"name": "remote", "url": "http://127.0.0.1:9/mcp"}],
            )
            toolset = MCPToolSet(
                workspace=root, actor="secretary", config_path=config
            )
            tools = asyncio.run(toolset.open())
            self.assertEqual(tools, [])
            self.assertTrue(
                any("remote" in n for n in toolset.notices),
                toolset.notices,
            )
            asyncio.run(toolset.close())


class SecretaryAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_attach_adds_gated_tools_with_secretary_verdict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-sec-") as tmp:
            root = Path(tmp)
            config = _write_config(
                root,
                [
                    {
                        "name": "echo-server",
                        "command": sys.executable,
                        "args": [str(ECHO_SERVER)],
                    }
                ],
            )
            from agentscope_ext.fake_model import FakeChatModel

            agent = dispatcher.build_dispatcher(
                model=FakeChatModel(replies=["你好"]),
                workspace=root,
            )
            toolset = await dispatcher.attach_secretary_mcp(
                agent, root, config_path=config
            )
            self.assertIsNotNone(toolset)
            names = [
                tool.name
                for group in agent.toolkit.tool_groups
                for tool in group.tools
            ]
            self.assertIn("mcp__echo-server__echo", names)
            gated = await agent.toolkit.get_tool("mcp__echo-server__echo")
            verdict = gated._verdict()
            self.assertEqual(verdict.verdict, "ask")
            again = await dispatcher.attach_secretary_mcp(
                agent, root, config_path=config
            )
            self.assertIs(again, toolset)
            await toolset.close()

    async def test_attach_without_config_is_noop(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-sec-") as tmp:
            root = Path(tmp)
            from agentscope_ext.fake_model import FakeChatModel

            agent = dispatcher.build_dispatcher(
                model=FakeChatModel(replies=["你好"]),
                workspace=root,
            )
            result = await dispatcher.attach_secretary_mcp(
                agent, root, config_path=root / "absent.json"
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
