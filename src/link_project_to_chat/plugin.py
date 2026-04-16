"""
Plugin base classes and PluginContext.

Plugins are installed as Python packages exposing the entry point:
    [project.entry-points."lptc.plugins"]
    my-plugin = "my_package:MyPlugin"

They are declared per-project in config.json:
    "plugins": [
        {"name": "in-app-web-server"},
        {"name": "diff-reviewer", "option": "value"}
    ]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class BotCommand:
    command: str                                     # e.g. "diff"
    description: str
    handler: Callable[..., Awaitable[Any]]


@dataclass
class PluginContext:
    """Shared context for all plugins in a project. One instance per bot."""
    bot_name: str
    project_path: Path
    bot_token: str | None = None
    trusted_user_id: int | None = None

    web_port: int | None = None                      # local port of the embedded web server
    public_url: str | None = None                    # public HTTPS URL; set by in-app-web-server

    # Set by in-app-web-server. Called by other plugins to register web handlers.
    # owner: plugin identifier (e.g. "diff"), page: handler name (e.g. "main")
    # URL will be: {public_url}/{owner}/{page}
    register_in_app_web_handler: Callable[[str, str, Callable[..., Awaitable[Any]]], None] | None = field(default=None, repr=False)

    _send: Callable[..., Awaitable[Any]] | None = field(default=None, repr=False)

    async def send_message(self, chat_id: int, text: str, **kwargs) -> Any:
        """Send a Telegram message from a plugin without importing bot internals."""
        if self._send:
            return await self._send(chat_id, text, **kwargs)


class Plugin:
    """Base class for all plugins. Subclass and override what you need."""

    #: Unique name matching the entry-point key and config.json "name" field.
    name: str = ""

    def __init__(self, context: PluginContext, config: dict) -> None:
        self._ctx = context
        self._config = config

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Called when the bot starts. Perform setup here."""

    async def stop(self) -> None:
        """Called when the bot stops. Clean up resources here."""

    # ── task hooks ───────────────────────────────────────────────────────────

    async def on_task_complete(self, task) -> None:
        """Called after a task finishes (DONE or FAILED). Not called for CANCELLED."""

    async def on_tool_use(self, tool: str, path: str | None) -> None:
        """Called when Claude uses a tool during a task (e.g. Write, Edit)."""

    # ── registration ─────────────────────────────────────────────────────────

    def commands(self) -> list[BotCommand]:
        """Additional Telegram bot commands this plugin registers."""
        return []

    def callbacks(self) -> dict[str, Callable[..., Awaitable[Any]]]:
        """Callback query handlers keyed by callback_data prefix."""
        return {}


def load_plugin(name: str, context: PluginContext, config: dict) -> Plugin | None:
    """
    Instantiate a plugin by name using the 'lptc.plugins' entry point group.
    Returns None if the plugin is not installed.
    """
    from importlib.metadata import entry_points

    eps = entry_points(group="lptc.plugins")
    for ep in eps:
        if ep.name == name:
            cls = ep.load()
            return cls(context, config)

    logger.error("plugin %r not found — is it installed?", name)
    return None
