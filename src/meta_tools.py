"""Meta-tool definitions and dispatch for token-efficient gateway mode.

Instead of exposing all upstream tools (43+) to the LLM, the meta-tool mode
exposes only 4 tools:

- discover: Find available tools by category or keyword
- describe: Get full parameter schema for a specific tool
- execute: Call any upstream tool by name with arguments

This reduces tool-definition tokens from ~6,500 to ~800 while retaining
full access to all upstream capabilities via the execute dispatcher.
"""

import logging
from typing import Any, Optional

from .registry import ToolRegistry

logger = logging.getLogger(__name__)

# ─── Meta-Tool Schemas ───────────────────────────────────────────────────────
# These are the only tool definitions sent to the LLM in meta mode.

META_TOOLS: list[dict[str, Any]] = [
    {
        "name": "mcp_gateway_discover",
        "description": (
            "Discover available tools on the gateway. "
            "Returns tool names and short descriptions, optionally filtered by "
            "backend name or keyword search. Use this to find what tools exist "
            "before calling them with mcp_gateway_execute."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend": {
                    "type": "string",
                    "description": (
                        "Filter by backend name (e.g. 'ncm-fleet', "
                        "'ncm-monitoring', 'ncm-cloud-services', 'puppeteer')"
                    ),
                },
                "query": {
                    "type": "string",
                    "description": (
                        "Keyword search across tool names and descriptions "
                        "(case-insensitive substring match)"
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "mcp_gateway_describe",
        "description": (
            "Get the full parameter schema for a specific tool. "
            "Use this when you need to know the exact arguments a tool accepts "
            "before calling it with mcp_gateway_execute."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "The exact tool name returned by mcp_gateway_discover",
                },
            },
            "required": ["tool_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "mcp_gateway_execute",
        "description": (
            "Execute any tool on the gateway by name. "
            "Pass the tool name and its arguments. Use mcp_gateway_discover "
            "to find tool names and mcp_gateway_describe to get parameter schemas."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_name": {
                    "type": "string",
                    "description": "The tool to execute (from mcp_gateway_discover)",
                },
                "arguments": {
                    "type": "object",
                    "description": "Arguments to pass to the tool",
                    "additionalProperties": True,
                },
            },
            "required": ["tool_name"],
            "additionalProperties": False,
        },
    },
]


# ─── Meta-Tool Dispatcher ────────────────────────────────────────────────────


class MetaToolDispatcher:
    """Handles meta-tool calls by dispatching to the underlying registry."""

    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        """Return the meta-tool schemas for tools/list responses."""
        return META_TOOLS

    def is_meta_tool(self, name: str) -> bool:
        """Check if a tool name is a meta-tool."""
        return name in (
            "mcp_gateway_discover",
            "mcp_gateway_describe",
            "mcp_gateway_execute",
        )

    async def handle_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a meta-tool call to the appropriate handler."""
        handlers = {
            "mcp_gateway_discover": self._handle_discover,
            "mcp_gateway_describe": self._handle_describe,
            "mcp_gateway_execute": self._handle_execute,
        }

        handler = handlers.get(tool_name)
        if not handler:
            raise ValueError(f"Unknown meta-tool: {tool_name}")

        return await handler(arguments)

    # ─── Handlers ─────────────────────────────────────────────────────────

    async def _handle_discover(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """List available tools with optional filtering.

        Returns compact name + description pairs to minimize response tokens.
        """
        backend_filter = arguments.get("backend")
        query_filter = arguments.get("query", "").lower()

        results = []
        for backend_state in self._registry._backends.values():
            # Filter by backend name if specified
            if backend_filter and backend_state.config.name != backend_filter:
                continue

            for entry in backend_state.tools.values():
                # Filter by keyword search
                if query_filter:
                    searchable = f"{entry.name} {entry.description}".lower()
                    if query_filter not in searchable:
                        continue

                results.append({
                    "name": entry.name,
                    "description": entry.description,
                    "backend": entry.backend_name,
                })

        # Also include available backends summary when no filters applied
        metadata: dict[str, Any] = {"tool_count": len(results)}
        if not backend_filter and not query_filter:
            metadata["backends"] = [
                {
                    "name": s.config.name,
                    "description": s.config.description,
                    "tool_count": len(s.tools),
                    "connected": s.connected,
                }
                for s in self._registry._backends.values()
            ]

        return {
            "content": [
                {
                    "type": "text",
                    "text": _format_discover_response(results, metadata),
                }
            ]
        }

    async def _handle_describe(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return the full input schema for a specific tool."""
        tool_name = arguments.get("tool_name")
        if not tool_name:
            return _error_content("Missing required argument: tool_name")

        # Get full schema (triggers lazy loading if needed)
        schema = await self._registry.get_tool_schema(tool_name)
        if schema is None:
            # Check if the tool exists at all
            if tool_name not in self._registry._tool_index:
                available = list(self._registry._tool_index.keys())
                return _error_content(
                    f"Unknown tool: '{tool_name}'. "
                    f"Use mcp_gateway_discover to find available tools. "
                    f"Total available: {len(available)}"
                )
            # Tool exists but schema couldn't be loaded
            return _error_content(
                f"Tool '{tool_name}' exists but schema could not be loaded. "
                f"You can still try mcp_gateway_execute with best-guess arguments."
            )

        # Find the tool entry for description
        backend_name = self._registry._tool_index[tool_name]
        state = self._registry._backends[backend_name]
        entry = state.tools.get(tool_name)
        description = entry.description if entry else ""

        return {
            "content": [
                {
                    "type": "text",
                    "text": _format_describe_response(tool_name, description, schema),
                }
            ]
        }

    async def _handle_execute(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute an upstream tool by name, routing through the registry."""
        tool_name = arguments.get("tool_name")
        if not tool_name:
            return _error_content("Missing required argument: tool_name")

        tool_arguments = arguments.get("arguments", {})

        try:
            result = await self._registry.call_tool(tool_name, tool_arguments)
            return result
        except ValueError as e:
            return _error_content(str(e))
        except ConnectionError as e:
            return _error_content(f"Backend connection error: {e}")
        except Exception as e:
            logger.exception(f"Error executing {tool_name}")
            return _error_content(f"Execution failed: {e}")


# ─── Response Formatting ─────────────────────────────────────────────────────


def _format_discover_response(
    tools: list[dict[str, Any]], metadata: dict[str, Any]
) -> str:
    """Format the discover response as a concise readable string."""
    lines = []

    if "backends" in metadata:
        lines.append("Available backends:")
        for b in metadata["backends"]:
            status = "connected" if b["connected"] else "disconnected"
            lines.append(f"  - {b['name']} ({status}, {b['tool_count']} tools): {b['description']}")
        lines.append("")

    lines.append(f"Tools ({metadata['tool_count']} total):")
    for tool in tools:
        lines.append(f"  - {tool['name']}: {tool['description']}")

    if not tools:
        lines.append("  (no tools match the filter)")

    return "\n".join(lines)


def _format_describe_response(
    name: str, description: str, schema: dict[str, Any]
) -> str:
    """Format a tool description with its full schema."""
    import json

    lines = [
        f"Tool: {name}",
        f"Description: {description}",
        "",
        "Input Schema:",
        json.dumps(schema, indent=2),
    ]
    return "\n".join(lines)


def _error_content(message: str) -> dict[str, Any]:
    """Create an MCP error content response."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
    }
