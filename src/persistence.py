"""Persistent state management for the MCP Gateway.

Saves and loads backend configurations to/from a JSON state file
so dynamically added backends survive restarts.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from .config import BackendServer, ToolFilter

logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent gateway state."""

    def __init__(self, state_file: str = "state.json"):
        self._path = Path(state_file)
        self._state: dict[str, Any] = {
            "version": 1,
            "backends": [],
            "last_saved": 0,
        }

    def load(self) -> list[BackendServer]:
        """Load backends from the state file.

        Returns a list of BackendServer configs that were persisted.
        """
        if not self._path.exists():
            logger.debug(f"No state file at {self._path}")
            return []

        try:
            with open(self._path, "r") as f:
                self._state = json.load(f)

            backends = []
            for b in self._state.get("backends", []):
                tool_filter = None
                if "tools" in b and b["tools"]:
                    tool_filter = ToolFilter(
                        include=b["tools"].get("include", []),
                        exclude=b["tools"].get("exclude", []),
                    )

                backends.append(BackendServer(
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
                ))

            logger.info(f"Loaded {len(backends)} backends from state file")
            return backends
        except Exception as e:
            logger.error(f"Failed to load state file: {e}")
            return []

    def save(self, backends: list[BackendServer]) -> None:
        """Save current backend configurations to the state file."""
        self._state = {
            "version": 1,
            "backends": [b.to_dict() for b in backends],
            "last_saved": time.time(),
        }

        try:
            with open(self._path, "w") as f:
                json.dump(self._state, f, indent=2)
            logger.debug(f"Saved {len(backends)} backends to state file")
        except Exception as e:
            logger.error(f"Failed to save state file: {e}")

    def create_backup(self) -> dict[str, Any]:
        """Create a full backup of the current state.

        Returns a dict that can be used to restore state later.
        """
        if self._path.exists():
            with open(self._path, "r") as f:
                return json.load(f)
        return self._state

    def restore_backup(self, backup: dict[str, Any]) -> list[BackendServer]:
        """Restore state from a backup dict.

        Writes the backup to the state file and returns the backends.
        """
        self._state = backup

        try:
            with open(self._path, "w") as f:
                json.dump(self._state, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write restored state: {e}")

        # Parse backends from backup
        backends = []
        for b in backup.get("backends", []):
            tool_filter = None
            if "tools" in b and b["tools"]:
                tool_filter = ToolFilter(
                    include=b["tools"].get("include", []),
                    exclude=b["tools"].get("exclude", []),
                )

            try:
                backends.append(BackendServer(
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
                ))
            except (ValueError, KeyError) as e:
                logger.warning(f"Skipping invalid backend in backup: {e}")

        logger.info(f"Restored {len(backends)} backends from backup")
        return backends
