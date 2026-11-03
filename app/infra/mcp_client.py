"""MCP（Model Context Protocol）客户端。

用于把外部 MCP Server 暴露的工具接入问答链路（典型场景：联网搜索）。

设计要点：
- 传输：stdio（拉起本地进程，JSON-RPC over stdin/stdout）与 Streamable HTTP 两种；
- 生命周期：每次操作建立独立会话，用完即关。MCP 会话绑定 asyncio task，
  跨请求复用会在并发请求间串话，短连接更稳；
- 成本控制：tools/list 结果做进程内 TTL 缓存，避免每次问答都拉起子进程；
- 安全边界：工具数量、单个结果长度、单次调用时长均有上限，异常一律降级为空结果，
  不允许外部服务故障拖垮对话主链路；
- 工具名加 "服务名__" 前缀，避免多个 Server 同名工具冲突（同时符合 OpenAI 函数名规则）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

from app.config.settings import McpServerConfig, get_settings
from app.core.logging import get_logger

logger = get_logger("app.mcp")

_NAME_SAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sdk_available() -> bool:
    """mcp SDK 是否可用：未安装时整条工具链路自动关闭，不影响对话。"""
    return importlib.util.find_spec("mcp") is not None


@dataclass(slots=True)
class McpTool:
    """一个可被大模型调用的工具。"""

    server: str
    name: str                 # MCP Server 内的原始工具名
    qualified: str            # 暴露给模型的函数名（服务名__工具名）
    description: str
    input_schema: dict

    def to_openai(self) -> dict:
        """转成 OpenAI function calling 的工具描述。"""
        return {
            "type": "function",
            "function": {
                "name": self.qualified,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass
class _ToolCache:
    tools: list[McpTool] = field(default_factory=list)
    expires_at: float = 0.0


def _qualified(server_name: str, tool_name: str) -> str:
    raw = f"{server_name}__{tool_name}"
    return _NAME_SAFE.sub("_", raw)[:64]


def _schema_of(tool) -> dict:
    """兼容 MCP SDK 1.x（inputSchema）与 2.x（input_schema）字段命名。"""
    return (getattr(tool, "inputSchema", None)
            or getattr(tool, "input_schema", None) or {})


class McpClient:
    """MCP 工具发现与调用；未配置/未安装 SDK 时所有方法返回空，不抛异常。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or get_settings().mcp
        self._cache: dict[str, _ToolCache] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.servers and sdk_available())

    async def list_tools(self) -> list[McpTool]:
        """汇总所有 Server 的工具；单个 Server 失败只影响它自己。"""
        if not self.enabled:
            return []
        tools: list[McpTool] = []
        for srv in self.cfg.servers[: self.cfg.max_servers]:
            tools.extend(await self._server_tools(srv))
        return tools[: self.cfg.max_tools]

    async def call(self, tool: McpTool, arguments: dict | None = None) -> str:
        """执行工具，返回文本结果（已截断）；任何失败返回可读的错误说明。"""
        srv = self._server(tool.server)
        if srv is None:
            return f"（工具 {tool.qualified} 所属 MCP 服务未配置）"
        try:
            async with self._session(srv) as session:
                result = await asyncio.wait_for(
                    session.call_tool(tool.name, arguments or {}),
                    timeout=self.cfg.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("mcp_call_timeout", server=srv.name, tool=tool.name)
            return f"（工具 {tool.qualified} 调用超时）"
        except Exception as e:                       # 外部服务异常不向上传播
            logger.warning("mcp_call_failed", server=srv.name, tool=tool.name,
                           error=str(e))
            return f"（工具 {tool.qualified} 调用失败：{e}）"
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            logger.warning("mcp_tool_error", server=srv.name, tool=tool.name)
        return self._result_text(result)

    # ------------------------------------------------------------------
    # 内部：工具发现 / 结果解析 / 会话
    # ------------------------------------------------------------------
    async def _server_tools(self, srv: McpServerConfig) -> list[McpTool]:
        now = time.monotonic()
        cached = self._cache.get(srv.name)
        if cached is not None and cached.expires_at > now:
            return cached.tools
        try:
            async with self._session(srv) as session:
                listed = await asyncio.wait_for(
                    session.list_tools(), timeout=self.cfg.timeout_seconds)
        except Exception as e:
            # 失败时沿用过期缓存：远端临时抖动不应让已发现的工具全部消失
            logger.warning("mcp_list_tools_failed", server=srv.name, error=str(e))
            return cached.tools if cached else []
        tools = [
            McpTool(server=srv.name, name=t.name,
                    qualified=_qualified(srv.name, t.name),
                    description=(t.description or "")[:400],
                    input_schema=_schema_of(t))
            for t in (listed.tools or [])
        ]
        self._cache[srv.name] = _ToolCache(tools, now + self.cfg.cache_ttl_seconds)
        logger.info("mcp_tools_loaded", server=srv.name, count=len(tools))
        return tools

    def _server(self, name: str) -> McpServerConfig | None:
        return next((s for s in self.cfg.servers if s.name == name), None)

    def _result_text(self, result) -> str:
        """拼接 MCP 结果中的文本块；非文本内容序列化为 JSON。"""
        parts: list[str] = []
        for block in (getattr(result, "content", None) or []):
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
                continue
            data = getattr(block, "data", None)
            if isinstance(data, (dict, list)):
                parts.append(json.dumps(data, ensure_ascii=False))
        text = "\n".join(parts).strip() or "（工具未返回内容）"
        limit = self.cfg.max_result_chars
        return text if len(text) <= limit else text[:limit] + "\n（结果过长，已截断）"

    @asynccontextmanager
    async def _session(self, srv: McpServerConfig) -> AsyncIterator:
        """建立一次 MCP 会话（initialize → yield → 关闭）。"""
        from mcp import ClientSession, StdioServerParameters

        timeout = self.cfg.timeout_seconds
        if srv.transport == "http":
            from mcp.client.streamable_http import streamable_http_client
            client = None
            if srv.headers:
                import httpx2
                client = httpx2.AsyncClient(headers=dict(srv.headers))
            try:
                async with streamable_http_client(srv.url, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1],
                                             read_timeout_seconds=timeout) as session:
                        await session.initialize()
                        yield session
            finally:
                if client is not None:
                    await client.aclose()
        else:
            if not srv.command:
                raise ValueError(f"MCP 服务 {srv.name} 未配置 command")
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(
                command=srv.command, args=list(srv.args or []),
                env={**os.environ, **(srv.env or {})})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
                    await session.initialize()
                    yield session


_client: McpClient | None = None


def get_mcp() -> McpClient:
    global _client
    if _client is None:
        _client = McpClient()
    return _client
