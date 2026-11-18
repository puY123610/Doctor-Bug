# -*- coding: utf-8 -*-
"""测试夹具：最小 MCP stdio 服务器（echo + add 两个工具）。

本机 mcp SDK 是构造器回调式 API（on_list_tools / on_call_tool），
不是旧版的装饰器式——按已装 SDK 的签名写。
"""

import asyncio

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

TOOLS = [
    Tool(
        name="echo",
        description="把 text 原样返回",
        inputSchema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    ),
    Tool(
        name="add",
        description="返回 a + b",
        inputSchema={
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    ),
]


async def _on_list_tools(ctx, params) -> ListToolsResult:
    return ListToolsResult(tools=TOOLS)


async def _on_call_tool(
    ctx, params: CallToolRequestParams
) -> CallToolResult:
    arguments = params.arguments or {}
    if params.name == "echo":
        return CallToolResult(
            content=[
                TextContent(type="text", text=str(arguments["text"]))
            ]
        )
    if params.name == "add":
        total = arguments["a"] + arguments["b"]
        return CallToolResult(
            content=[TextContent(type="text", text=str(total))]
        )
    raise ValueError(f"unknown tool: {params.name}")


app = Server(
    "echo-server",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main() -> None:
    async with stdio_server() as (read, write):
        await app.run(
            read, write, app.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
