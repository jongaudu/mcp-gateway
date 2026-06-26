"""Configuration loading and validation for the MCP Gateway."""

import os
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


@dataclass
class ToolFilter:
    """Tool filtering rules for a backend."""

    include: list[str] = field(default_factory=list)  # Only expose these tools
    exclude: list[str] = field(default_factory=list)  # Expose all except these

    def is_allowed(self, tool_name: str) -> bool:
        """Check if a tool should be exposed through the gateway."""
        if self.include:
            return tool_name in self.include
        if self.exclude:
            return tool_name not in self.exclude
        return True


@dataclass
class BackendServer:
    """Configuration for a single backend MCP server."""

    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "http"  # "http", "sse", or "stdio"
    lazy: bool = True  # Whether to lazy-load tool schemas
    description: str = ""
    headers: dict[str, str] = field(default_factory=dict)  # Auth headers for backend
    tools: Optional[ToolFilter] = None  # Tool filtering rules

    def __post_init__(self):
        if self.url:
            # Auto-detect transport from URL pattern if still at default
            if self.transport == "http" and "/sse" in self.url:
                self.transport = "sse"
            # Ensure transport is valid for URL-based backends
            if self.transport not in ("http", "sse"):
                self.transport = "http"
        elif self.command:
            self.transport = "stdio"
        else:
            raise ValueError(
                f"Backend '{self.name}' must have either 'url' or 'command'"
            )

    def to_dict(self) -> dict:
        """Serialize to a dict for persistence."""
        d = {
            "name": self.name,
            "transport": self.transport,
            "lazy": self.lazy,
            "description": self.description,
        }
        if self.url:
            d["url"] = self.url
        if self.command:
            d["command"] = self.command
        if self.args:
            d["args"] = self.args
        if self.env:
            d["env"] = self.env
        if self.headers:
            d["headers"] = self.headers
        if self.tools:
            tools_dict = {}
            if self.tools.include:
                tools_dict["include"] = self.tools.include
            if self.tools.exclude:
                tools_dict["exclude"] = self.tools.exclude
            if tools_dict:
                d["tools"] = tools_dict
        return d


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "info"
    backends: list[BackendServer] = field(default_factory=list)
    tool_cache_ttl: int = 300  # seconds before re-fetching tool lists
    lazy_schema_loading: bool = True  # Global toggle for lazy loading
    api_key: Optional[str] = None  # Required API key for gateway access
    state_file: str = "state.json"  # Persistent state file path
    auto_reconnect: bool = True  # Auto-reconnect disconnected backends
    reconnect_interval: int = 30  # Seconds between reconnect attempts


# Regex to match ${VAR_NAME} or ${VAR_NAME:-default_value}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-(.*?))?\}")


def _interpolate_env(value: str) -> str:
    """Replace ${VAR} and ${VAR:-default} with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        default = match.group(2)
        return os.environ.get(var_name, default if default is not None else "")
    return _ENV_VAR_PATTERN.sub(replacer, value)


def _interpolate_recursive(obj):
    """Recursively interpolate environment variables in config values."""
    if isinstance(obj, str):
        return _interpolate_env(obj)
    elif isinstance(obj, dict):
        return {k: _interpolate_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_interpolate_recursive(item) for item in obj]
    return obj


def load_config(config_path: Optional[str] = None) -> GatewayConfig:
    """Load gateway configuration from YAML file.

    Resolution order:
    1. Explicit path argument
    2. MCP_GATEWAY_CONFIG environment variable
    3. ./config.yaml (relative to CWD)
    4. /etc/mcp-gateway/config.yaml

    Supports environment variable interpolation in values:
        url: "${MY_SERVER_URL:-http://localhost:3000/mcp}"
    """
    paths_to_try = []

    if config_path:
        paths_to_try.append(Path(config_path))

    env_path = os.environ.get("MCP_GATEWAY_CONFIG")
    if env_path:
        paths_to_try.append(Path(env_path))

    paths_to_try.extend([
        Path("config.yaml"),
        Path("/etc/mcp-gateway/config.yaml"),
    ])

    for path in paths_to_try:
        if path.exists():
            return _parse_config(path)

    # No config file found — return empty config (backends added via API)
    return GatewayConfig()


def _parse_backend(b: dict) -> BackendServer:
    """Parse a single backend entry from config."""
    tools_raw = b.get("tools")
    tool_filter = None
    if tools_raw:
        tool_filter = ToolFilter(
            include=tools_raw.get("include", []),
            exclude=tools_raw.get("exclude", []),
        )

    return BackendServer(
        name=b["name"],
        url=b.get("url"),
        command=b.get("command"),
        args=b.get("args", []),
        env=b.get("env", {}),
        transport=b.get("transport", "http"),
        lazy=b.get("lazy", True),
        description=b.get("description", ""),
        headers=b.get("headers", {}),
        tools=tool_filter,
    )


def _parse_config(path: Path) -> GatewayConfig:
    """Parse a YAML config file into a GatewayConfig."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    # Interpolate environment variables throughout
    raw = _interpolate_recursive(raw)

    gateway_section = raw.get("gateway", {})
    backends_raw = raw.get("backends", [])

    backends = [_parse_backend(b) for b in backends_raw]

    return GatewayConfig(
        host=gateway_section.get("host", "0.0.0.0"),
        port=int(gateway_section.get("port", 8080)),
        log_level=gateway_section.get("log_level", "info"),
        backends=backends,
        tool_cache_ttl=int(gateway_section.get("tool_cache_ttl", 300)),
        lazy_schema_loading=gateway_section.get("lazy_schema_loading", True),
        api_key=gateway_section.get("api_key"),
        state_file=gateway_section.get("state_file", "state.json"),
        auto_reconnect=gateway_section.get("auto_reconnect", True),
        reconnect_interval=int(gateway_section.get("reconnect_interval", 30)),
    )
