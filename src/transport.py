"""Transport layer for communicating with backend MCP servers.

Supports three MCP transport protocols:
- HTTP (Streamable HTTP): JSON-RPC over POST requests
- SSE (Server-Sent Events): Persistent GET stream + POST for requests
- Stdio: JSON-RPC over subprocess stdin/stdout
"""

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from .config import BackendServer

logger = logging.getLogger(__name__)


class BackendTransport(ABC):
    """Abstract base class for backend MCP server communication."""

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the backend."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Close connection to the backend."""
        ...

    @abstractmethod
    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch available tools from the backend."""
        ...

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool on the backend."""
        ...


class HttpTransport(BackendTransport):
    """Transport for Streamable HTTP MCP servers (JSON-RPC over POST).

    This is the simplest protocol: send a JSON-RPC request as POST body,
    receive a JSON-RPC response in the response body.
    """

    def __init__(self, config: BackendServer):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._base_url = config.url.rstrip("/") if config.url else ""
        self._request_id = 0
        self._headers = dict(config.headers) if config.headers else {}
        self._session_id: Optional[str] = None

    async def connect(self) -> None:
        # MCP Streamable HTTP spec requires Accept header for content negotiation
        default_headers = {
            "Accept": "application/json, text/event-stream",
        }
        default_headers.update(self._headers)
        self._client = httpx.AsyncClient(timeout=60.0, headers=default_headers)
        logger.debug(f"HTTP transport connecting to {self._base_url}")

        # Send initialize handshake and capture session ID
        try:
            await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"},
            })
            # Send initialized notification after successful handshake
            await self._send_notification("notifications/initialized", {})
        except Exception as e:
            logger.warning(f"HTTP initialize handshake failed (non-fatal): {e}")

    async def disconnect(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

    async def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no id, no response expected)."""
        if not self._client:
            raise ConnectionError("Not connected")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = await self._client.post(
            self._base_url, json=payload, headers=headers
        )
        # Notifications may return 200/202/204 — all acceptable
        if response.status_code >= 400:
            logger.debug(f"Notification {method} returned {response.status_code}")

    async def _send_request(self, method: str, params: dict) -> dict[str, Any]:
        """Send a JSON-RPC request via HTTP POST.

        Handles MCP Streamable HTTP session management:
        - Captures Mcp-Session-Id from responses
        - Sends session ID on subsequent requests
        - Handles SSE stream responses (server may respond with text/event-stream)
        - Handles empty response bodies (202/200 with no content)
        """
        if not self._client:
            raise ConnectionError("Not connected")

        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        # Include session ID if we have one from a previous response
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        response = await self._client.post(
            self._base_url, json=payload, headers=headers
        )
        response.raise_for_status()

        # Capture session ID from response headers (per Streamable HTTP spec)
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id

        content_type = response.headers.get("content-type", "")

        # Server responds with SSE stream — parse events to find the JSON-RPC response
        if "text/event-stream" in content_type:
            return self._parse_sse_response(response.text)

        # Handle empty body (some servers return 200/202 with no content for initialize)
        body = response.text.strip()
        if not body:
            return {}

        result = response.json()

        if "error" in result:
            raise RuntimeError(f"Backend error: {result['error']}")

        return result.get("result", {})

    def _parse_sse_response(self, text: str) -> dict[str, Any]:
        """Parse an SSE response body to extract the JSON-RPC result.

        SSE format:
            event: message
            data: {"jsonrpc": "2.0", "id": 1, "result": {...}}
        """
        data_lines = []
        for line in text.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line == "" and data_lines:
                # End of an event — try to parse accumulated data
                raw = "\n".join(data_lines)
                data_lines = []
                try:
                    message = json.loads(raw)
                    if "error" in message:
                        raise RuntimeError(f"Backend error: {message['error']}")
                    if "result" in message:
                        return message["result"]
                except json.JSONDecodeError:
                    continue

        # Try any remaining data lines (if stream didn't end with blank line)
        if data_lines:
            raw = "\n".join(data_lines)
            try:
                message = json.loads(raw)
                if "error" in message:
                    raise RuntimeError(f"Backend error: {message['error']}")
                if "result" in message:
                    return message["result"]
            except json.JSONDecodeError:
                pass

        return {}


class SseTransport(BackendTransport):
    """Transport for SSE-based MCP servers.

    The SSE protocol works as follows:
    1. Client opens a GET connection to the SSE endpoint
    2. Server sends an 'endpoint' event with the session POST URL
    3. Client sends JSON-RPC requests via POST to that session URL
    4. Server sends responses as SSE 'message' events on the GET stream

    This transport handles the full lifecycle including reconnection.
    """

    def __init__(self, config: BackendServer):
        self._config = config
        self._base_url = config.url.rstrip("/") if config.url else ""
        self._client: httpx.AsyncClient | None = None
        self._session_url: Optional[str] = None
        self._request_id = 0
        self._pending_responses: dict[int, asyncio.Future] = {}
        self._sse_task: Optional[asyncio.Task] = None
        self._connected_event = asyncio.Event()
        self._headers = dict(config.headers) if config.headers else {}

    async def connect(self) -> None:
        """Open SSE stream and wait for the endpoint event."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers=self._headers,
        )
        logger.debug(f"SSE transport connecting to {self._base_url}")

        # Start the SSE listener in background
        self._sse_task = asyncio.create_task(self._listen_sse())

        # Wait for the endpoint event (with timeout)
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"SSE connection timeout: no endpoint event received from {self._base_url}"
            )

        # Send initialize
        try:
            await self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"},
            })
        except Exception as e:
            logger.warning(f"SSE initialize handshake failed (non-fatal): {e}")

    async def disconnect(self) -> None:
        if self._sse_task and not self._sse_task.done():
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session_url = None
        self._connected_event.clear()
        # Cancel any pending futures
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })

    async def _send_request(self, method: str, params: dict) -> dict[str, Any]:
        """Send a JSON-RPC request via POST to the session endpoint."""
        if not self._client or not self._session_url:
            raise ConnectionError("SSE session not established")

        self._request_id += 1
        request_id = self._request_id

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }

        # Create a future to wait for the response via SSE stream
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending_responses[request_id] = future

        try:
            # POST the request to the session URL
            response = await self._client.post(self._session_url, json=payload)
            response.raise_for_status()

            # Some SSE servers return the result directly in POST response
            # if content-type is application/json
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                body = response.json()
                if "result" in body or "error" in body:
                    self._pending_responses.pop(request_id, None)
                    if not future.done():
                        future.cancel()
                    if "error" in body:
                        raise RuntimeError(f"Backend error: {body['error']}")
                    return body.get("result", {})

            # Otherwise wait for the response via SSE stream
            result = await asyncio.wait_for(future, timeout=30.0)
            return result

        except asyncio.TimeoutError:
            self._pending_responses.pop(request_id, None)
            raise RuntimeError(f"Timeout waiting for SSE response to {method}")
        except Exception:
            self._pending_responses.pop(request_id, None)
            raise

    async def _listen_sse(self) -> None:
        """Listen to the SSE stream for endpoint and message events."""
        try:
            async with self._client.stream("GET", self._base_url, headers={
                "Accept": "text/event-stream",
                "Cache-Control": "no-cache",
            }) as response:
                response.raise_for_status()

                event_type = ""
                data_buffer = ""

                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buffer += line[5:].strip()
                    elif line == "":
                        # Empty line = end of event
                        if event_type and data_buffer:
                            await self._handle_sse_event(event_type, data_buffer)
                        event_type = ""
                        data_buffer = ""

        except asyncio.CancelledError:
            logger.debug("SSE listener cancelled")
        except Exception as e:
            logger.error(f"SSE stream error: {e}")
            # Signal connection lost
            self._connected_event.clear()

    async def _handle_sse_event(self, event_type: str, data: str) -> None:
        """Process an SSE event."""
        if event_type == "endpoint":
            # The server sends the session URL we should POST to
            session_path = data.strip()
            # Build absolute URL from relative path
            if session_path.startswith("http"):
                self._session_url = session_path
            else:
                # Relative to base URL origin
                from urllib.parse import urljoin
                base_origin = self._base_url.rsplit("/", 1)[0] if "/" in self._base_url[8:] else self._base_url
                self._session_url = urljoin(base_origin + "/", session_path.lstrip("/"))

            logger.info(f"SSE session established: {self._session_url}")
            self._connected_event.set()

        elif event_type == "message":
            # JSON-RPC response
            try:
                message = json.loads(data)
                request_id = message.get("id")
                if request_id and request_id in self._pending_responses:
                    future = self._pending_responses.pop(request_id)
                    if not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError(f"Backend error: {message['error']}")
                            )
                        else:
                            future.set_result(message.get("result", {}))
            except json.JSONDecodeError as e:
                logger.warning(f"Invalid JSON in SSE message: {e}")

        else:
            logger.debug(f"Unknown SSE event type: {event_type}")


class StdioTransport(BackendTransport):
    """Transport for stdio-based MCP servers (spawned as subprocesses).

    Communicates via JSON-RPC messages over stdin (requests) and
    stdout (responses), one JSON object per line.
    """

    def __init__(self, config: BackendServer):
        self._config = config
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0

    async def connect(self) -> None:
        """Spawn the backend process."""
        env = {**self._config.env} if self._config.env else None
        cmd = [self._config.command] + self._config.args

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        logger.debug(f"Stdio transport spawned: {' '.join(cmd)}")

        # Send initialize request
        await self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"},
        })

    async def disconnect(self) -> None:
        if self._process:
            self._process.terminate()
            await self._process.wait()
            self._process = None

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._send_request("tools/call", {
            "name": name,
            "arguments": arguments,
        })
        return result

    async def _send_request(self, method: str, params: dict) -> dict[str, Any]:
        """Send a JSON-RPC request over stdin and read response from stdout."""
        if not self._process or not self._process.stdin or not self._process.stdout:
            raise ConnectionError("Process not running")

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params,
        }

        message = json.dumps(request) + "\n"
        self._process.stdin.write(message.encode())
        await self._process.stdin.drain()

        # Read response line
        line = await self._process.stdout.readline()
        if not line:
            raise ConnectionError("Backend process closed stdout")

        response = json.loads(line.decode())

        if "error" in response:
            raise RuntimeError(f"Backend error: {response['error']}")

        return response.get("result", {})


def create_transport(config: BackendServer) -> BackendTransport:
    """Factory function to create the appropriate transport.

    Transport selection:
    - Explicit 'transport' field takes priority
    - Auto-detect from config: url with /sse -> SSE, url -> HTTP, command -> stdio
    """
    transport_type = config.transport

    if transport_type == "http":
        return HttpTransport(config)
    elif transport_type == "sse":
        return SseTransport(config)
    elif transport_type == "stdio":
        return StdioTransport(config)
    else:
        raise ValueError(f"Unknown transport type: {transport_type}")
