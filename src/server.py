"""MCP Gateway Server - the main entry point for the proxy."""

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import BackendServer, GatewayConfig, ToolFilter, load_config
from .meta_tools import MetaToolDispatcher
from .persistence import StateManager
from .registry import ToolRegistry

logger = logging.getLogger(__name__)


# ─── Auth Middleware ─────────────────────────────────────────────────────────


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces API key authentication.

    Paths exempt from auth: /health, /static/*, /
    The API key can be sent as:
    - Header: X-API-Key: <key>
    - Header: Authorization: Bearer <key>
    - Query param: ?api_key=<key>
    """

    def __init__(self, app, api_key: str):
        super().__init__(app)
        self._api_key = api_key
        self._exempt_prefixes = ["/health", "/static/", "/ws"]
        self._exempt_exact = ["/"]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth for exempt paths
        if path in self._exempt_exact:
            return await call_next(request)
        for prefix in self._exempt_prefixes:
            if path.startswith(prefix):
                return await call_next(request)

        # Check API key
        key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or request.query_params.get("api_key")
        )

        if key != self._api_key:
            return JSONResponse(
                {"error": "Unauthorized. Provide a valid API key."},
                status_code=401,
            )

        return await call_next(request)


# ─── Gateway Server ──────────────────────────────────────────────────────────


class MCPGatewayServer:
    """MCP-compliant gateway server that proxies tool calls to backend servers."""

    def __init__(self, config: GatewayConfig):
        self._config = config
        self._registry = ToolRegistry(config)
        self._state_manager = StateManager(config.state_file)
        self._meta_dispatcher = MetaToolDispatcher(self._registry)
        self._request_id = 0

    async def start(self) -> None:
        """Initialize the registry, load persisted state, connect to backends."""
        # Load backends from config file first
        await self._registry.initialize()

        # Then load any additional backends from persisted state
        persisted = self._state_manager.load()
        for backend_cfg in persisted:
            # Don't duplicate backends already loaded from config
            if backend_cfg.name not in self._registry._backends:
                await self._registry.add_backend(backend_cfg)

        self._save_state()

        logger.info(
            f"Gateway ready ({self._config.mode} mode): "
            f"{self._registry.tool_count} tools from "
            f"{self._registry.backend_count} backends"
        )
        if self._config.mode == "meta":
            logger.info(
                "Meta-tool mode active: exposing 3 meta-tools "
                "(discover, describe, execute) instead of all upstream tools"
            )

    async def stop(self) -> None:
        """Shut down the gateway and persist state."""
        self._save_state()
        await self._registry.shutdown()
        logger.info("Gateway shut down")

    def _save_state(self) -> None:
        """Persist current backend configurations."""
        configs = [s.config for s in self._registry._backends.values()]
        self._state_manager.save(configs)

    # ─── MCP Protocol Endpoint ────────────────────────────────────────────

    async def handle_mcp(self, request: Request) -> JSONResponse:
        """Handle JSON-RPC MCP requests."""
        try:
            body = await request.json()
        except Exception:
            return self._error_response(None, -32700, "Parse error")

        method = body.get("method")
        request_id = body.get("id")
        params = body.get("params", {})

        logger.debug(f"Received: {method} (id={request_id})")

        handler = self._get_handler(method)
        if not handler:
            return self._error_response(request_id, -32601, f"Unknown method: {method}")

        try:
            result = await handler(params)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            })
        except Exception as e:
            logger.exception(f"Error handling {method}")
            return self._error_response(request_id, -32000, str(e))

    def _get_handler(self, method: str):
        handlers = {
            "initialize": self._handle_initialize,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "ping": self._handle_ping,
        }
        return handlers.get(method)

    async def _handle_initialize(self, params: dict) -> dict[str, Any]:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "mcp-gateway", "version": "2.0.0"},
        }

    async def _handle_tools_list(self, params: dict) -> dict[str, Any]:
        # In meta mode, expose only the meta-tools (discover, describe, execute)
        if self._config.mode == "meta":
            return {"tools": self._meta_dispatcher.get_tool_definitions()}

        # Proxy mode: expose all upstream tools directly
        await self._registry.refresh_if_stale()
        include_schemas = not self._config.lazy_schema_loading
        tools = await self._registry.list_tools(include_schemas=include_schemas)
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict) -> dict[str, Any]:
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not tool_name:
            raise ValueError("Missing 'name' in tools/call params")

        # In meta mode, route meta-tool calls to the dispatcher
        if self._config.mode == "meta" and self._meta_dispatcher.is_meta_tool(tool_name):
            return await self._meta_dispatcher.handle_call(tool_name, arguments)

        result = await self._registry.call_tool(tool_name, arguments)
        return result

    async def _handle_ping(self, params: dict) -> dict[str, Any]:
        return {}

    def _error_response(self, request_id, code: int, message: str) -> JSONResponse:
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        })

    # ─── Admin API ───────────────────────────────────────────────────────

    async def handle_health(self, request: Request) -> JSONResponse:
        status = self._registry.get_backend_status()
        all_healthy = all(status.values()) if status else False
        return JSONResponse(
            {
                "status": "healthy" if all_healthy else "degraded",
                "mode": self._config.mode,
                "backends": status,
                "tools": self._registry.tool_count,
                "exposed_tools": (
                    len(self._meta_dispatcher.get_tool_definitions())
                    if self._config.mode == "meta"
                    else self._registry.tool_count
                ),
            },
            status_code=200 if all_healthy else 503,
        )

    async def handle_list_backends(self, request: Request) -> JSONResponse:
        backends = self._registry.list_backends()
        return JSONResponse({"backends": backends})

    async def handle_add_backend(self, request: Request) -> JSONResponse:
        """POST /admin/backends - Register a new backend server at runtime."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        name = body.get("name")
        if not name:
            return JSONResponse(
                {"error": "Missing required field: 'name'"}, status_code=400
            )

        url = body.get("url")
        command = body.get("command")
        if not url and not command:
            return JSONResponse(
                {"error": "Must provide either 'url' (HTTP/SSE) or 'command' (stdio)"},
                status_code=400,
            )

        transport = body.get("transport", "http")
        if url and transport not in ("http", "sse"):
            transport = "http"
        elif command:
            transport = "stdio"

        # Parse tool filter
        tool_filter = None
        tools_raw = body.get("tools")
        if tools_raw:
            tool_filter = ToolFilter(
                include=tools_raw.get("include", []),
                exclude=tools_raw.get("exclude", []),
            )

        try:
            backend_cfg = BackendServer(
                name=name,
                url=url,
                command=command,
                args=body.get("args", []),
                env=body.get("env", {}),
                transport=transport,
                lazy=body.get("lazy", True),
                description=body.get("description", ""),
                headers=body.get("headers", {}),
                tools=tool_filter,
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        result = await self._registry.add_backend(backend_cfg)
        self._save_state()

        status_code = 200 if result["status"] == "connected" else 502
        return JSONResponse(result, status_code=status_code)

    async def handle_remove_backend(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        removed = await self._registry.remove_backend(name)
        if removed:
            self._save_state()
            return JSONResponse({"removed": name})
        return JSONResponse({"error": f"Backend '{name}' not found"}, status_code=404)

    async def handle_refresh_backends(self, request: Request) -> JSONResponse:
        for state in self._registry._backends.values():
            if state.connected:
                await self._registry._discover_tools(state)
        return JSONResponse({
            "refreshed": True,
            "tools": self._registry.tool_count,
            "backends": self._registry.backend_count,
        })

    # ─── Metrics & Logs ──────────────────────────────────────────────────

    async def handle_metrics(self, request: Request) -> JSONResponse:
        """GET /admin/metrics - Return call metrics for all backends."""
        return JSONResponse(self._registry.get_metrics())

    async def handle_call_log(self, request: Request) -> JSONResponse:
        """GET /admin/logs - Return recent tool call log."""
        limit = int(request.query_params.get("limit", "50"))
        return JSONResponse({"logs": self._registry.get_call_log(limit)})

    # ─── Tools List (Admin) ──────────────────────────────────────────────

    async def handle_list_tools(self, request: Request) -> JSONResponse:
        """GET /admin/tools - List all registered tools with backend info."""
        tools = await self._registry.list_tools(include_schemas=False)
        return JSONResponse({"tools": tools})

    # ─── Backup & Restore ────────────────────────────────────────────────

    async def handle_backup(self, request: Request) -> JSONResponse:
        """GET /admin/backup - Download a full state backup."""
        backup = self._state_manager.create_backup()
        return JSONResponse(backup)

    async def handle_restore(self, request: Request) -> JSONResponse:
        """POST /admin/restore - Restore state from a backup."""
        try:
            backup = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if "backends" not in backup:
            return JSONResponse(
                {"error": "Invalid backup format: missing 'backends' key"},
                status_code=400,
            )

        # Remove all current backends
        for name in list(self._registry._backends.keys()):
            await self._registry.remove_backend(name)

        # Restore from backup
        backends = self._state_manager.restore_backup(backup)
        results = []
        for cfg in backends:
            result = await self._registry.add_backend(cfg)
            results.append(result)

        return JSONResponse({
            "restored": True,
            "backends": len(results),
            "results": results,
        })

    # ─── WebSocket Live Status ───────────────────────────────────────────

    async def handle_ws_status(self, websocket: WebSocket) -> None:
        """WebSocket endpoint for live backend status updates."""
        await websocket.accept()
        queue = self._registry.subscribe_status()

        try:
            # Send initial state
            await websocket.send_json({
                "type": "init",
                "backends": self._registry.get_backend_status(),
                "tools": self._registry.tool_count,
                "timestamp": time.time(),
            })

            # Stream status changes
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug(f"WebSocket closed: {e}")
        finally:
            self._registry.unsubscribe_status(queue)


# ─── App Factory ─────────────────────────────────────────────────────────────


def create_app(config: GatewayConfig | None = None) -> Starlette:
    """Create the ASGI application."""
    if config is None:
        config = load_config()

    gateway = MCPGatewayServer(config)

    # Resolve static files directory
    static_dir = Path(__file__).parent.parent / "static"

    async def serve_index(request: Request):
        index_path = static_dir / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return JSONResponse({"error": "Web UI not found"}, status_code=404)

    @asynccontextmanager
    async def lifespan(app):
        await gateway.start()
        yield
        await gateway.stop()

    routes = [
        # Web UI
        Route("/", serve_index, methods=["GET"]),
        # MCP protocol
        Route("/mcp", gateway.handle_mcp, methods=["POST"]),
        # Health (no auth required)
        Route("/health", gateway.handle_health, methods=["GET"]),
        # Admin API
        Route("/admin/backends", gateway.handle_list_backends, methods=["GET"]),
        Route("/admin/backends", gateway.handle_add_backend, methods=["POST"]),
        Route("/admin/backends/{name}", gateway.handle_remove_backend, methods=["DELETE"]),
        Route("/admin/refresh", gateway.handle_refresh_backends, methods=["POST"]),
        Route("/admin/metrics", gateway.handle_metrics, methods=["GET"]),
        Route("/admin/logs", gateway.handle_call_log, methods=["GET"]),
        Route("/admin/tools", gateway.handle_list_tools, methods=["GET"]),
        Route("/admin/backup", gateway.handle_backup, methods=["GET"]),
        Route("/admin/restore", gateway.handle_restore, methods=["POST"]),
        # WebSocket
        WebSocketRoute("/ws/status", gateway.handle_ws_status),
        # Static assets
        Mount("/static", StaticFiles(directory=str(static_dir)), name="static"),
    ]

    # Apply auth middleware if API key is configured
    middleware = []
    if config.api_key:
        middleware.append(Middleware(APIKeyMiddleware, api_key=config.api_key))

    app = Starlette(routes=routes, lifespan=lifespan, middleware=middleware)
    return app
