"""MCP 工具插口：客户端接入 + 逐次调用门禁。

老板在 ``~/.config/drbug/mcp.json`` 登记 stdio 型 MCP
服务器（格式见 load_mcp_servers）。工人的工具包会多出
这些工具，但每个工具都被 MCPGateTool 包住：调用前先经
PolicyEngine 以 ``mcp_call`` 动作裁决，目标写成
``服务器名:工具名``。工人面默认拒绝，只有策略规则明确
放行才真调；裁决经 decide() 落审计。连接失败不拖垮
工具包：失败服务器记进 notices，其余照常。
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig
from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool._base import ToolBase
from agentscope.tool._response import ToolChunk

from .policy import PolicyContext, PolicyEngine, format_denial

DEFAULT_MCP_CONFIG_PATH = (
    Path.home() / ".config" / "drbug" / "mcp.json"
)


def load_mcp_servers(
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """读 MCP 服务器登记表；文件不存在返回空列表。

    格式：{"servers": [{"name": "echo", "command": "python3",
    "args": ["server.py"], "env": {"K": "V"}}]}——args/env
    可省略。格式错误抛 ValueError 并说清哪一条不对。
    """

    config_path = Path(path) if path else DEFAULT_MCP_CONFIG_PATH
    if not config_path.is_file():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"MCP 配置无法读取：{config_path}：{exc}"
        ) from exc
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        raise ValueError(
            f"MCP 配置格式不对：{config_path} 顶层需要 "
            '{"servers": [...]}'
        )
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(servers):
        if not isinstance(item, dict):
            raise ValueError(f"MCP 配置第 {index + 1} 条不是对象")
        name = item.get("name")
        command = item.get("command")
        url = item.get("url")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"MCP 配置第 {index + 1} 条缺 name"
            )
        if command is not None and url is not None:
            raise ValueError(
                f"MCP 服务器 {name} 的 command 与 url 只能选一个"
            )
        if url is not None:
            if not isinstance(url, str) or not url.startswith(
                ("http://", "https://")
            ):
                raise ValueError(
                    f"MCP 服务器 {name} 的 url 须是 http(s) 地址"
                )
            if name in seen:
                raise ValueError(f"MCP 服务器名重复：{name}")
            seen.add(name)
            headers = item.get("headers") or {}
            if not isinstance(headers, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in headers.items()
            ):
                raise ValueError(
                    f"MCP 服务器 {name} 的 headers 须是字符串字典"
                )
            specs.append(
                {"name": name, "url": url, "headers": dict(headers)}
            )
            continue
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"MCP 服务器 {name} 缺 command"
            )
        if name in seen:
            raise ValueError(f"MCP 服务器名重复：{name}")
        seen.add(name)
        args = item.get("args") or []
        env = item.get("env") or {}
        if not isinstance(args, list) or not all(
            isinstance(a, str) for a in args
        ):
            raise ValueError(
                f"MCP 服务器 {name} 的 args 须是字符串列表"
            )
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in env.items()
        ):
            raise ValueError(
                f"MCP 服务器 {name} 的 env 须是字符串字典"
            )
        specs.append(
            {
                "name": name,
                "command": command,
                "args": list(args),
                "env": dict(env),
            }
        )
    return specs


class MCPGateTool(ToolBase):
    """包住一个框架 MCP 工具：每次调用先过门禁。

    check_permissions 与 call 各裁一次（与 ShellTool 同形）：
    绕过权限检查的直调也必须过裁决。裁决走 decide()，
    允许与拒绝都进审计。
    """

    # ToolBase 只声明不给默认值的类属性，子类必须自带
    # （SHELL1-fix2 的教训）。外接工具副作用未知：不并发、
    # 不冒充只读。
    is_concurrency_safe: bool = False
    is_read_only: bool = False
    is_mcp: bool = False

    def __init__(
        self,
        inner: ToolBase,
        *,
        server: str,
        workspace: str | Path,
        actor: str = "worker",
        on_denial=None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._server = server
        self._workspace = Path(workspace)
        self._actor = actor
        self._on_denial = on_denial
        # 框架给 MCP 工具的名字已带 mcp__<服务器>__ 前缀、
        # 全局唯一，照原样用；策略目标用未加前缀的原名。
        self.name = inner.name
        prefix = f"mcp__{server}__"
        self._raw_name = (
            inner.name[len(prefix):]
            if inner.name.startswith(prefix)
            else inner.name
        )
        self.description = (
            f"[MCP 外接工具 · 服务器 {server}] {inner.description}"
        )
        self.input_schema = inner.input_schema

    @property
    def target(self) -> str:
        return f"{self._server}:{self._raw_name}"

    def _verdict(self):
        engine = PolicyEngine(workspace=self._workspace)
        is_secretary = self._actor == "secretary"
        ctx = PolicyContext(
            mode="ask",
            surface="interactive" if is_secretary else "runtime",
            workspace=self._workspace,
            role="secretary" if is_secretary else "worker",
            is_ai=True,
            actor=self._actor,
        )
        return engine.decide(
            self._actor, "mcp_call", self.target, ctx=ctx
        )

    async def check_permissions(self, tool_input, context):
        decision = self._verdict()
        if decision.verdict == "allow":
            return await super().check_permissions(
                tool_input, context
            )
        if self._on_denial is not None:
            self._on_denial(self.target, decision.reason)
        return PermissionDecision(
            behavior=PermissionBehavior.DENY,
            message=format_denial(decision),
            decision_reason=decision.reason,
        )

    async def call(self, *args, **kwargs):
        # 协议形态照 ShellTool.call：async generator，
        # ToolChunk 包 TextBlock，带 state 与 is_last。
        # 框架工具一律 **kwargs 调用；直接调用方偶尔把整个入参 dict
        # 当位置参数传进来，这里归一化成 kwargs，免得内层工具报错。
        if args:
            if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
                kwargs = dict(args[0])
            else:
                raise TypeError(
                    "MCPGateTool.call 只接受关键字参数"
                    "（或单个 dict 位置参数）"
                )
        decision = self._verdict()
        if decision.verdict != "allow":
            if self._on_denial is not None:
                self._on_denial(self.target, decision.reason)
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "MCP 调用被拒绝："
                            + format_denial(decision)
                        )
                    )
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        result = self._inner.call(**kwargs)
        if inspect.isasyncgen(result):
            async for chunk in result:
                yield chunk
        elif hasattr(result, "__await__"):
            chunk = await result
            yield chunk
        else:
            yield result


class MCPToolSet:
    """一组 MCP 服务器连接的生命周期：open 拿工具、close 收尾。"""

    def __init__(
        self,
        *,
        workspace: str | Path,
        actor: str = "worker",
        config_path: str | Path | None = None,
        on_denial=None,
    ) -> None:
        self._workspace = Path(workspace)
        self._actor = actor
        self._config_path = config_path
        self._on_denial = on_denial
        self._clients: list[MCPClient] = []
        self.notices: list[str] = []

    async def open(self) -> list[ToolBase]:
        tools: list[ToolBase] = []
        for spec in load_mcp_servers(self._config_path):
            if spec.get("url"):
                mcp_config = HttpMCPConfig(
                    url=spec["url"],
                    headers=spec["headers"] or None,
                )
            else:
                mcp_config = StdioMCPConfig(
                    command=spec["command"],
                    args=spec["args"] or None,
                    env=spec["env"] or None,
                )
            client = MCPClient(
                name=spec["name"],
                is_stateful=True,
                mcp_config=mcp_config,
            )
            try:
                await client.connect()
                raw_tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 - 记 notice 不拖垮
                self.notices.append(
                    f"MCP 服务器 {spec['name']} 连接失败："
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
            self._clients.append(client)
            for inner in raw_tools:
                tools.append(
                    MCPGateTool(
                        inner,
                        server=spec["name"],
                        workspace=self._workspace,
                        actor=self._actor,
                        on_denial=self._on_denial,
                    )
                )
        return tools

    async def close(self) -> None:
        for client in self._clients:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
