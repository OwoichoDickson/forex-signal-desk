"""Async client for the existing TradingView MCP server (stdio), with rate limiting and retries."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .logging_setup import log

logger = logging.getLogger("fxsignals.mcp")


class ToolError(RuntimeError):
    pass


class TradingViewMCP:
    """Spawns ``node <server_path>`` (the TradingView MCP server) and calls its tools."""

    def __init__(self, cfg: dict):
        m = cfg["mcp"]
        self.command = m.get("command", "node")
        self.server_path = os.path.expanduser(m["server_path"])
        self.timeout = float(m.get("call_timeout_s", 45))
        self.min_interval = 1.0 / float(m.get("max_calls_per_second", 4))
        self.retries = int(m.get("retries", 3))
        self.backoff = float(m.get("backoff_base_s", 1.0))
        self._stack: Optional[AsyncExitStack] = None
        self._session: Optional[ClientSession] = None
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self.calls = 0
        self.tools: dict[str, dict] = {}

    async def __aenter__(self) -> "TradingViewMCP":
        await self._connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _connect(self) -> None:
        if not os.path.exists(self.server_path):
            raise ToolError(f"TradingView MCP server not found at {self.server_path}")
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=self.command, args=[self.server_path])
        errlog = open(os.devnull, "w")
        self._stack.callback(errlog.close)
        read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        listed = await self._session.list_tools()
        self.tools = {t.name: (t.input_schema if hasattr(t, "input_schema") else t.inputSchema) for t in listed.tools}
        log(logger, logging.INFO, "connected to TradingView MCP", tools=len(self.tools))

    async def close(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        self._stack, self._session = None, None

    def has(self, tool: str) -> bool:
        return tool in self.tools

    async def call(self, tool: str, args: Optional[dict[str, Any]] = None, retries: Optional[int] = None) -> dict:
        """Call a tool and return its parsed JSON payload. Retries with exponential backoff."""
        if self.tools and tool not in self.tools:
            raise ToolError(f"Tool '{tool}' is not provided by the TradingView MCP server")
        attempts = (self.retries if retries is None else retries) + 1
        last_err: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                async with self._lock:
                    wait = self.min_interval - (time.monotonic() - self._last_call)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last_call = time.monotonic()
                    self.calls += 1
                    if self._session is None:
                        await self._connect()
                    res = await asyncio.wait_for(self._session.call_tool(tool, args or {}), timeout=self.timeout)
                text = "".join(getattr(c, "text", "") for c in (res.content or []))
                is_error = getattr(res, "is_error", None)
                if is_error is None:
                    is_error = getattr(res, "isError", False)
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"success": not is_error, "raw": text}
                if is_error or payload.get("success") is False:
                    raise ToolError(f"{tool} failed: {payload.get('error') or text[:300]}")
                return payload
            except (ToolError, asyncio.TimeoutError, OSError, Exception) as e:  # noqa: BLE001
                last_err = e
                transient = not (isinstance(e, ToolError) and "is not provided" in str(e))
                log(logger, logging.WARNING, "tool call failed", tool=tool, attempt=attempt + 1, error=str(e)[:300])
                if not transient or attempt == attempts - 1:
                    break
                if not isinstance(e, ToolError):  # transport-level failure: restart the server process
                    await self.close()
                await asyncio.sleep(self.backoff * (2 ** attempt))
        raise ToolError(str(last_err))
