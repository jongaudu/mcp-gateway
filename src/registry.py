"""Tool registry that maps tool names to backend servers and manages lazy loading."""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import BackendServer, GatewayConfig
from .transport import BackendTransport, create_transport

logger = logging.getLogger(__name__)


@dataclass
class ToolEntry:
    """A registered tool with its metadata and routing info."""

    name: str
    description: str
    backend_name: str
    input_schema: Optional[dict[str, Any]] = None
    schema_loaded: bool = False
    last_fetched: float = 0.0


@dataclass
class BackendMetrics:
    """Runtime metrics for a backend."""

    total_calls: int = 0
    total_errors: int = 0
    total_latency_ms: float = 0.0
    last_call_time: float = 0.0
    last_error: Optional[str] = None
    last_error_time: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_latency_ms / self.total_calls


@dataclass
class BackendState:
    """Runtime state for a connected backend server."""

    config: BackendServer
    transport: BackendTransport
    tools: dict[str, ToolEntry] = field(default_factory=dict)
    connected: bool = False
    last_discovery: float = 0.0
    metrics: BackendMetrics = field(default_factory=BackendMetrics)
    reconnect_attempts: int = 0
    last_reconnect_attempt: float = 0.0


@dataclass
class CallLogEntry:
    """A single tool call log entry."""

    timestamp: float
    tool_name: str
    backend_name: str
    latency_ms: float
    success: bool
    error: Optional[str] = None


class ToolRegistry:
    """Central registry managing tool discovery, lazy schema loading, and routing."""

    def __init__(self, config: GatewayConfig):
        self._config = config
        self._backends: dict[str, BackendState] = {}
        self._tool_index: dict[str, str] = {}  # tool_name -> backend_name
        self._lock = asyncio.Lock()
        self._call_log: list[CallLogEntry] = []
        self._max_log_entries = 500
        self._reconnect_task: Optional[asyncio.Task] = None
        self._status_listeners: list[asyncio.Queue] = []

    async def initialize(self) -> None:
        """Connect to all backends and perform initial tool discovery."""
        for backend_cfg in self._config.backends:
            await self.add_backend(backend_cfg)

        # Start auto-reconnect background task
        if self._config.auto_reconnect:
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def add_backend(self, backend_cfg: BackendServer) -> dict[str, Any]:
        """Add and connect a new backend at runtime."""
        if backend_cfg.name in self._backends:
            await self.remove_backend(backend_cfg.name)

        transport = create_transport(backend_cfg)
        state = BackendState(config=backend_cfg, transport=transport)
        self._backends[backend_cfg.name] = state

        try:
            await transport.connect()
            state.connected = True
            state.reconnect_attempts = 0
            logger.info(f"Connected to backend: {backend_cfg.name}")
            await self._discover_tools(state)
            await self._notify_status_change(backend_cfg.name, True)
            return {
                "name": backend_cfg.name,
                "status": "connected",
                "tools_discovered": len(state.tools),
            }
        except Exception as e:
            logger.error(f"Failed to connect to {backend_cfg.name}: {e}")
            await self._notify_status_change(backend_cfg.name, False)
            return {
                "name": backend_cfg.name,
                "status": "error",
                "error": str(e),
            }

    async def remove_backend(self, name: str) -> bool:
        """Remove a backend and unregister all its tools."""
        state = self._backends.pop(name, None)
        if not state:
            return False

        tools_to_remove = [
            tool_name for tool_name, backend_name in self._tool_index.items()
            if backend_name == name
        ]
        for tool_name in tools_to_remove:
            del self._tool_index[tool_name]

        try:
            await state.transport.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting {name}: {e}")

        await self._notify_status_change(name, None)  # None = removed
        logger.info(f"Removed backend: {name} ({len(tools_to_remove)} tools unregistered)")
        return True

    async def shutdown(self) -> None:
        """Disconnect from all backends and stop background tasks."""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        for state in self._backends.values():
            try:
                await state.transport.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting {state.config.name}: {e}")

    # ─── Auto-Reconnect ──────────────────────────────────────────────────

    async def _reconnect_loop(self) -> None:
        """Background task that retries disconnected backends."""
        try:
            while True:
                await asyncio.sleep(self._config.reconnect_interval)
                await self._attempt_reconnections()
        except asyncio.CancelledError:
            pass

    async def _attempt_reconnections(self) -> None:
        """Try to reconnect all disconnected backends."""
        for name, state in list(self._backends.items()):
            if state.connected:
                continue

            # Exponential backoff: 30s, 60s, 120s, 240s... capped at 5min
            backoff = min(
                self._config.reconnect_interval * (2 ** state.reconnect_attempts),
                300,
            )
            if time.time() - state.last_reconnect_attempt < backoff:
                continue

            state.last_reconnect_attempt = time.time()
            state.reconnect_attempts += 1
            logger.info(
                f"Reconnecting to {name} (attempt {state.reconnect_attempts})..."
            )

            try:
                await state.transport.disconnect()
                transport = create_transport(state.config)
                state.transport = transport
                await transport.connect()
                state.connected = True
                state.reconnect_attempts = 0
                await self._discover_tools(state)
                await self._notify_status_change(name, True)
                logger.info(f"Reconnected to {name}")
            except Exception as e:
                logger.warning(f"Reconnect failed for {name}: {e}")

    # ─── Tool Discovery with Filtering ───────────────────────────────────

    async def _discover_tools(self, state: BackendState) -> None:
        """Discover available tools from a backend, applying filters."""
        try:
            tools_response = await state.transport.list_tools()
            state.last_discovery = time.time()

            # Clear existing tools for this backend from index
            old_tools = list(state.tools.keys())
            for tool_name in old_tools:
                self._tool_index.pop(tool_name, None)
            state.tools.clear()

            for tool_data in tools_response:
                name = tool_data["name"]
                description = tool_data.get("description", "")

                # Apply tool filter
                if state.config.tools and not state.config.tools.is_allowed(name):
                    continue

                if self._config.lazy_schema_loading and state.config.lazy:
                    entry = ToolEntry(
                        name=name,
                        description=description,
                        backend_name=state.config.name,
                        schema_loaded=False,
                    )
                else:
                    entry = ToolEntry(
                        name=name,
                        description=description,
                        backend_name=state.config.name,
                        input_schema=tool_data.get("inputSchema"),
                        schema_loaded=True,
                        last_fetched=time.time(),
                    )

                state.tools[name] = entry
                self._tool_index[name] = state.config.name

            logger.info(
                f"Discovered {len(state.tools)} tools from {state.config.name}"
            )
        except Exception as e:
            logger.error(f"Tool discovery failed for {state.config.name}: {e}")

    # ─── Tool Listing & Schema Loading ───────────────────────────────────

    def list_backends(self) -> list[dict[str, Any]]:
        """Return info about all registered backends."""
        results = []
        for name, state in self._backends.items():
            info = {
                "name": name,
                "transport": state.config.transport,
                "url": state.config.url,
                "command": state.config.command,
                "description": state.config.description,
                "connected": state.connected,
                "tools": len(state.tools),
                "lazy": state.config.lazy,
                "headers": state.config.headers,
                "metrics": {
                    "total_calls": state.metrics.total_calls,
                    "total_errors": state.metrics.total_errors,
                    "avg_latency_ms": round(state.metrics.avg_latency_ms, 1),
                },
            }
            if state.config.tools:
                info["tool_filter"] = {
                    "include": state.config.tools.include,
                    "exclude": state.config.tools.exclude,
                }
            results.append(info)
        return results

    async def list_tools(self, include_schemas: bool = False) -> list[dict[str, Any]]:
        """Return all registered tools.

        The MCP spec requires inputSchema on every tool. When lazy loading is
        enabled and include_schemas is False, we still provide a minimal valid
        schema so clients pass validation.
        """
        tools = []
        _empty_schema: dict[str, Any] = {"type": "object", "properties": {}}
        for backend_state in self._backends.values():
            for entry in backend_state.tools.values():
                tool_info: dict[str, Any] = {
                    "name": entry.name,
                    "description": entry.description,
                    "backend": entry.backend_name,
                }
                if include_schemas and entry.schema_loaded:
                    tool_info["inputSchema"] = entry.input_schema or _empty_schema
                elif include_schemas and not entry.schema_loaded:
                    schema = await self._load_schema(entry)
                    tool_info["inputSchema"] = schema or _empty_schema
                else:
                    # Always include inputSchema to satisfy MCP spec validation
                    tool_info["inputSchema"] = entry.input_schema or _empty_schema
                tools.append(tool_info)
        return tools

    async def get_tool_schema(self, tool_name: str) -> Optional[dict[str, Any]]:
        """Get the full schema for a specific tool, loading lazily if needed."""
        backend_name = self._tool_index.get(tool_name)
        if not backend_name:
            return None

        state = self._backends[backend_name]
        entry = state.tools.get(tool_name)
        if not entry:
            return None

        if not entry.schema_loaded:
            await self._load_schema(entry)

        return entry.input_schema

    async def _load_schema(self, entry: ToolEntry) -> Optional[dict[str, Any]]:
        """Lazily load the full input schema for a tool."""
        async with self._lock:
            if entry.schema_loaded:
                return entry.input_schema

            state = self._backends[entry.backend_name]
            try:
                tools_response = await state.transport.list_tools()
                for tool_data in tools_response:
                    if tool_data["name"] == entry.name:
                        entry.input_schema = tool_data.get("inputSchema")
                        entry.schema_loaded = True
                        entry.last_fetched = time.time()
                        return entry.input_schema
            except Exception as e:
                logger.error(f"Failed to load schema for {entry.name}: {e}")

            return None

    # ─── Tool Invocation with Metrics ────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Route a tool call to the appropriate backend server."""
        backend_name = self._tool_index.get(tool_name)
        if not backend_name:
            raise ValueError(f"Unknown tool: {tool_name}")

        state = self._backends[backend_name]
        if not state.connected:
            raise ConnectionError(f"Backend '{backend_name}' is not connected")

        start_time = time.time()
        error_msg = None
        try:
            result = await state.transport.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            error_msg = str(e)
            state.metrics.total_errors += 1
            state.metrics.last_error = error_msg
            state.metrics.last_error_time = time.time()
            raise
        finally:
            latency_ms = (time.time() - start_time) * 1000
            state.metrics.total_calls += 1
            state.metrics.total_latency_ms += latency_ms
            state.metrics.last_call_time = time.time()

            # Log the call
            log_entry = CallLogEntry(
                timestamp=time.time(),
                tool_name=tool_name,
                backend_name=backend_name,
                latency_ms=round(latency_ms, 1),
                success=error_msg is None,
                error=error_msg,
            )
            self._call_log.append(log_entry)
            if len(self._call_log) > self._max_log_entries:
                self._call_log = self._call_log[-self._max_log_entries:]

            logger.info(
                f"{'OK' if not error_msg else 'ERR'} {tool_name} -> {backend_name} "
                f"({latency_ms:.0f}ms)"
            )

    # ─── Metrics & Logging ───────────────────────────────────────────────

    def get_call_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent call log entries."""
        entries = self._call_log[-limit:]
        return [
            {
                "timestamp": e.timestamp,
                "tool": e.tool_name,
                "backend": e.backend_name,
                "latency_ms": e.latency_ms,
                "success": e.success,
                "error": e.error,
            }
            for e in reversed(entries)
        ]

    def get_metrics(self) -> dict[str, Any]:
        """Return aggregated metrics for all backends."""
        total_calls = sum(s.metrics.total_calls for s in self._backends.values())
        total_errors = sum(s.metrics.total_errors for s in self._backends.values())
        backends_metrics = {}
        for name, state in self._backends.items():
            backends_metrics[name] = {
                "calls": state.metrics.total_calls,
                "errors": state.metrics.total_errors,
                "avg_latency_ms": round(state.metrics.avg_latency_ms, 1),
                "last_call": state.metrics.last_call_time,
                "last_error": state.metrics.last_error,
            }
        return {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": round(total_errors / max(total_calls, 1) * 100, 1),
            "backends": backends_metrics,
        }

    # ─── WebSocket Status Notifications ──────────────────────────────────

    def subscribe_status(self) -> asyncio.Queue:
        """Subscribe to backend status change events."""
        queue: asyncio.Queue = asyncio.Queue()
        self._status_listeners.append(queue)
        return queue

    def unsubscribe_status(self, queue: asyncio.Queue) -> None:
        """Unsubscribe from status events."""
        if queue in self._status_listeners:
            self._status_listeners.remove(queue)

    async def _notify_status_change(
        self, backend_name: str, connected: Optional[bool]
    ) -> None:
        """Notify all subscribers of a backend status change."""
        event = {
            "type": "status_change",
            "backend": backend_name,
            "connected": connected,  # None means removed
            "timestamp": time.time(),
        }
        for queue in self._status_listeners:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # ─── Refresh & Properties ────────────────────────────────────────────

    async def refresh_if_stale(self) -> None:
        """Re-discover tools from backends if cache TTL has expired."""
        now = time.time()
        for state in self._backends.values():
            if state.connected and now - state.last_discovery > self._config.tool_cache_ttl:
                await self._discover_tools(state)

    @property
    def tool_count(self) -> int:
        return len(self._tool_index)

    @property
    def backend_count(self) -> int:
        return len(self._backends)

    def get_backend_status(self) -> dict[str, bool]:
        return {name: s.connected for name, s in self._backends.items()}
