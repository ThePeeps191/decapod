"""Minimal Model Context Protocol client (stdio transport, tools only).

Each configured server is spawned as a subprocess speaking newline-delimited JSON-RPC 2.0.
Its tools are imported as ``mcp.<server>.<tool>`` with an operator-assigned risk tier and
the scope ``mcp:<server>``. Tool descriptions come from the server and are treated as
untrusted text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from typing import Any

from pydantic import ConfigDict

from decapod import __version__
from decapod.config.settings import MCPServerSettings
from decapod.domain import RiskTier, SideEffect, ToolResult
from decapod.tools.base import Tool, ToolArgs, ToolContext
from decapod.util.text import one_line, truncate

log = logging.getLogger(__name__)
PROTOCOL_VERSION = "2025-06-18"
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


class MCPError(RuntimeError):
    pass


class MCPServer:
    def __init__(self, config: MCPServerSettings) -> None:
        self.config = config
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.tools: list[dict[str, Any]] = []
        self.server_info: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self.config.name

    async def start(self) -> None:
        env = {**os.environ, **self.config.env}
        kwargs: dict[str, Any] = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True
        self.proc = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            limit=16 * 1024 * 1024,
            **kwargs,
        )
        self._reader = asyncio.create_task(self._read_loop(), name=f"mcp-{self.name}")
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "decapod", "version": __version__},
            },
        )
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        await self.notify("notifications/initialized", {})
        self.tools = await self.list_tools()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise MCPError("malformed tools/call result")
        return result

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError(f"MCP server {self.name} is not running")
        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        try:
            return await asyncio.wait_for(future, timeout=self.config.timeout.total_seconds())
        except TimeoutError:
            raise MCPError(f"MCP server {self.name} timed out on {method}") from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, message: dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        err = message["error"] or {}
                        future.set_exception(MCPError(f"{err.get('code')}: {err.get('message')}"))
                    else:
                        future.set_result(message.get("result"))
                elif "id" in message and "method" in message:
                    # Server-initiated request (sampling, roots, ping): we support only ping.
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "method not supported"}
                    await self._write(reply)
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MCPError(f"MCP server {self.name} exited"))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()


class MCPArgs(ToolArgs):
    model_config = ConfigDict(extra="allow")


def _tier_side_effect(tier: RiskTier) -> SideEffect:
    return {
        RiskTier.INTERNAL: SideEffect.NONE,
        RiskTier.READ: SideEffect.EXTERNAL_READ,
        RiskTier.LOCAL_WRITE: SideEffect.LOCAL,
        RiskTier.EXTERNAL_WRITE: SideEffect.EXTERNAL_WRITE,
        RiskTier.IRREVERSIBLE: SideEffect.IRREVERSIBLE,
    }[tier]


def make_mcp_tool(server: MCPServer, spec: dict[str, Any]) -> Tool:
    remote_name = str(spec.get("name", ""))
    safe = _NAME_RE.sub("_", remote_name)[:64] or "tool"
    tier = server.config.tool_tiers.get(remote_name, server.config.default_tier)
    schema = spec.get("inputSchema") or {"type": "object"}
    required = [str(r) for r in schema.get("required", [])]
    properties: dict[str, Any] = schema.get("properties", {}) or {}
    desc = one_line(str(spec.get("description", "")), 300)

    class MCPTool(Tool):
        name = f"mcp.{server.name}.{safe}"
        description = f"[from MCP server '{server.name}'; untrusted description] {desc}"
        Args = MCPArgs
        required_scopes = (f"mcp:{server.name}",)

        def signature(self) -> str:
            parts = []
            for key, prop in list(properties.items())[:20]:
                optional = "" if key in required else "?"
                parts.append(f"{key}{optional}: {prop.get('type', 'any')}")
            return ", ".join(parts)

        def precheck(self, args: ToolArgs, ctx: ToolContext) -> str | None:
            data = args.model_dump()
            missing = [r for r in required if r not in data]
            if missing:
                return f"missing required argument(s): {', '.join(missing)}"
            return None

        async def run(self, args: ToolArgs, ctx: ToolContext) -> ToolResult:
            try:
                result = await server.call_tool(remote_name, args.model_dump())
            except MCPError as exc:
                return ToolResult.failure(str(exc))
            texts = []
            for item in result.get("content", []) or []:
                if item.get("type") == "text":
                    texts.append(str(item.get("text", "")))
                else:
                    texts.append(f"[{item.get('type', 'content')} omitted]")
            output = f"[Untrusted output from MCP server '{server.name}']\n" + truncate(
                "\n".join(texts), 20_000
            )
            if result.get("isError"):
                return ToolResult(ok=False, output=output, error="MCP tool reported an error")
            return ToolResult.success(output)

    MCPTool.tier = tier
    MCPTool.side_effect = _tier_side_effect(tier)
    MCPTool.outbound = tier >= RiskTier.EXTERNAL_WRITE
    MCPTool.__name__ = f"MCPTool_{server.name}_{safe}"
    return MCPTool()


class MCPManager:
    def __init__(self, configs: list[MCPServerSettings]) -> None:
        self.configs = [c for c in configs if c.enabled]
        self.servers: list[MCPServer] = []
        self.errors: dict[str, str] = {}

    async def start(self) -> list[Tool]:
        tools: list[Tool] = []
        for config in self.configs:
            server = MCPServer(config)
            try:
                await server.start()
            except (OSError, MCPError, TimeoutError) as exc:
                self.errors[config.name] = str(exc)
                log.warning("MCP server %s failed to start: %s", config.name, exc)
                await server.close()
                continue
            self.servers.append(server)
            seen: set[str] = set()
            for spec in server.tools:
                tool = make_mcp_tool(server, spec)
                base, n = tool.name, 2
                while tool.name in seen:  # e.g. "get.item" and "get_item" both sanitize alike
                    type(tool).name = f"{base}_{n}"
                    n += 1
                seen.add(tool.name)
                tools.append(tool)
        return tools

    async def close(self) -> None:
        for server in self.servers:
            await server.close()
        self.servers.clear()

    def kill_all(self) -> None:
        for server in self.servers:
            if server.proc is not None and server.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    server.proc.kill()
