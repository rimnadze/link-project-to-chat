from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from typing import Any

import pytest

from link_project_to_chat.bot import ProjectBot, _topo_sort
from link_project_to_chat.plugin import BotCommand, Plugin, PluginContext
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.stream import TextDelta, ToolUse
from link_project_to_chat.transport.base import (
    ButtonClick,
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)


def _ctx() -> PluginContext:
    return PluginContext(bot_name="b", project_path=Path("/tmp"))


def _msg(text: str = "hi") -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg_ref = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg_ref,
    )


def _click(value: str = "rec_open") -> ButtonClick:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg_ref = MessageRef(transport_id="fake", native_id="100", chat=chat)
    return ButtonClick(chat=chat, message=msg_ref, sender=sender, value=value)


class _RecordingPlugin(Plugin):
    name = "rec"

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.events: list[tuple[str, Any]] = []
        self.consume_message = False
        self.consume_button = False
        self.context_text: str | None = None
        self.start_raises = False

    async def start(self) -> None:
        if self.start_raises:
            raise RuntimeError("boom")
        self.events.append(("start", None))

    async def stop(self) -> None:
        self.events.append(("stop", None))

    async def on_message(self, msg):
        self.events.append(("on_message", msg.text))
        return self.consume_message

    async def on_button(self, click):
        self.events.append(("on_button", click.value))
        return self.consume_button

    async def on_task_complete(self, task) -> None:
        self.events.append(("on_task_complete", task.id))

    async def on_tool_use(self, tool, path) -> None:
        self.events.append(("on_tool_use", (tool, path)))

    def get_context(self):
        return self.context_text


def _make_bot(plugins=None, backend_name="claude"):
    from link_project_to_chat.config import Config

    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.path = Path("/tmp/p")
    bot._allowed_users = []  # role enforcement off
    bot._plugins = plugins or []
    bot._plugin_configs = []
    bot._plugin_command_handlers = {}
    bot._shared_ctx = None
    bot._backend_name = backend_name
    # _init_plugins reads these; collision tests don't go through __init__,
    # so set safe defaults. _get_user_role is added in Task 5 — stub it as
    # a no-op so PluginContext can stash the reference at registration time.
    bot.bot_username = ""
    bot._get_user_role = lambda _identity: None
    # _init_plugins resolves PluginContext.data_dir via
    # resolve_project_meta_dir(self._config.meta_dir, self.name) — supply a
    # Config whose meta_dir points at a tmp subdir.
    # NOTE: Config()'s `meta_dir` default_factory returns the module-level
    # `DEFAULT_META_DIR` constant, which is computed at import time using
    # the REAL `Path.home()` — BEFORE conftest's autouse HOME-isolation
    # fixture runs (see conftest.py lines 9-12). So leaving the default
    # would make _init_plugins create `~/.link-project-to-chat/meta/p/`
    # in the developer's real home. Pin to a tmp subdir to avoid the leak.
    cfg = Config()
    cfg.meta_dir = Path(tempfile.mkdtemp(prefix="lptc-test-meta-"))
    bot._config = cfg
    return bot


def test_topo_sort_orders_by_depends_on():
    a = _RecordingPlugin(_ctx(), {})
    a.name = "a"
    a.depends_on = ["b"]
    b = _RecordingPlugin(_ctx(), {})
    b.name = "b"
    assert [p.name for p in _topo_sort([a, b])] == ["b", "a"]


def test_topo_sort_missing_dep_still_returns_plugin():
    p = _RecordingPlugin(_ctx(), {})
    p.name = "p"
    p.depends_on = ["unknown"]
    assert [x.name for x in _topo_sort([p])] == ["p"]


@pytest.mark.asyncio
async def test_on_tool_use_fires_for_each_plugin():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))
    assert ("on_tool_use", ("Write", "/x")) in p1.events
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_tool_use_continues_after_plugin_raises():
    p1 = _RecordingPlugin(_ctx(), {})
    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p1.on_tool_use = boom  # type: ignore[assignment]
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._dispatch_plugin_tool_use(ToolUse(tool="Write", path="/x"))
    assert ("on_tool_use", ("Write", "/x")) in p2.events


@pytest.mark.asyncio
async def test_on_task_complete_fires_for_done_and_failed_not_cancelled():
    p = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p])
    for status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
        task = Task.__new__(Task)
        task.id = 1
        task.status = status
        await bot._dispatch_plugin_task_complete(task)
    fired = [e for e in p.events if e[0] == "on_task_complete"]
    assert len(fired) == 2


@pytest.mark.asyncio
async def test_on_message_consumes_when_any_plugin_returns_true():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    p2.consume_message = True
    bot = _make_bot([p1, p2])
    consumed = await bot._dispatch_plugin_on_message(_msg("hello"))
    assert consumed is True
    assert ("on_message", "hello") in p1.events
    assert ("on_message", "hello") in p2.events


@pytest.mark.asyncio
async def test_on_message_no_consume_when_plugin_raises():
    p = _RecordingPlugin(_ctx(), {})
    async def boom(*a, **kw):
        raise RuntimeError("boom")
    p.on_message = boom  # type: ignore[assignment]
    bot = _make_bot([p])
    consumed = await bot._dispatch_plugin_on_message(_msg("hi"))
    assert consumed is False


@pytest.mark.asyncio
async def test_on_button_consumes_when_any_plugin_returns_true():
    p1 = _RecordingPlugin(_ctx(), {})
    p2 = _RecordingPlugin(_ctx(), {})
    p2.consume_button = True
    bot = _make_bot([p1, p2])
    consumed = await bot._dispatch_plugin_button(_click("rec_open"))
    assert consumed is True
    assert ("on_button", "rec_open") in p1.events


def test_plugin_context_prepend_with_claude_backend():
    p1 = _RecordingPlugin(_ctx(), {})
    p1.context_text = "FIRST"
    p2 = _RecordingPlugin(_ctx(), {})
    p2.context_text = "SECOND"
    p3 = _RecordingPlugin(_ctx(), {})
    p3.context_text = None
    bot = _make_bot([p1, p2, p3], backend_name="claude")
    out = bot._plugin_context_prepend("USER")
    assert out.startswith("FIRST\n\nSECOND")
    assert "\n\n---\n\nUSER" in out


def test_plugin_context_prepend_skips_on_codex_backend():
    p = _RecordingPlugin(_ctx(), {})
    p.context_text = "WOULD-BE-PREPEND"
    bot = _make_bot([p], backend_name="codex")
    assert bot._plugin_context_prepend("USER") == "USER"


def test_plugin_context_prepend_empty_when_no_contexts():
    p = _RecordingPlugin(_ctx(), {})
    p.context_text = None
    bot = _make_bot([p])
    assert bot._plugin_context_prepend("USER") == "USER"


@pytest.mark.asyncio
async def test_shutdown_calls_stop_in_reverse_order():
    order: list[str] = []

    class P(_RecordingPlugin):
        async def stop(self):
            order.append(self.name)

    p1 = P(_ctx(), {})
    p1.name = "first"
    p2 = P(_ctx(), {})
    p2.name = "second"
    bot = _make_bot([p1, p2])
    await bot._shutdown_plugins()
    assert order == ["second", "first"]


@pytest.mark.asyncio
async def test_shutdown_continues_when_stop_raises():
    p1 = _RecordingPlugin(_ctx(), {})
    async def boom():
        raise RuntimeError("boom")
    p1.stop = boom  # type: ignore[assignment]
    p2 = _RecordingPlugin(_ctx(), {})
    bot = _make_bot([p1, p2])
    await bot._shutdown_plugins()
    assert ("stop", None) in p2.events


# --- Plugin command collision policy ---
# Step 6's _init_plugins blocks two collision categories:
#   1. Plugin command shadowing a CORE_COMMAND_NAMES entry (e.g., /help).
#   2. Plugin command already claimed by an earlier plugin (last-load
#      silently winning is wrong — both should keep their other commands
#      but the duplicate gets dropped with a WARNING).
# Without these tests, the collision logic regresses silently — it's
# only observable via a WARNING log line today.


class _RecordingCommandPlugin(_RecordingPlugin):
    """Plugin variant that exposes a configurable commands() list.

    `_commands_to_register` is a list[BotCommand]; commands() returns it.
    """

    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self._commands_to_register: list[BotCommand] = []

    def commands(self):
        return list(self._commands_to_register)


def _bc(name: str) -> BotCommand:
    async def _h(_ci):  # pragma: no cover — never invoked in these tests
        return None
    return BotCommand(command=name, description="", handler=_h)


def _bc_with_description(name: str, description: str) -> BotCommand:
    async def _h(_ci):  # pragma: no cover — never invoked in these tests
        return None
    return BotCommand(command=name, description=description, handler=_h)


@pytest.mark.asyncio
async def test_plugin_cannot_shadow_core_command(monkeypatch, caplog):
    """A plugin registering /help (a CORE_COMMAND_NAMES entry) is dropped
    for that command. Other commands from the same plugin still register."""
    from link_project_to_chat.bot import ProjectBot

    p = _RecordingCommandPlugin(_ctx(), {})
    p.name = "rec"
    p._commands_to_register = [_bc("help"), _bc("rec_open")]

    registered: list[tuple[str, Any]] = []

    class _FakeTransport:
        TRANSPORT_ID = "fake"
        def on_command(self, name, handler):
            registered.append((name, handler))

    bot = _make_bot([])
    bot._transport = _FakeTransport()
    bot._plugin_configs = [{"name": "rec"}]
    bot._plugins = [p]  # bypass load_plugin; pre-seed the list.
    # Stub load_plugin so _init_plugins doesn't try entry-point discovery.
    import link_project_to_chat.bot as bot_mod
    monkeypatch.setattr(bot_mod, "load_plugin", lambda *a, **kw: p)
    # Stub _wrap_plugin_command (defined in Task 5) so this test passes
    # before Task 5 lands — it returns the raw handler in Task 2's window.
    bot._wrap_plugin_command = lambda plugin, bc: bc.handler

    with caplog.at_level("WARNING"):
        await bot._init_plugins()

    names = [n for n, _ in registered]
    assert "help" not in names, "core command must not be shadowed"
    assert "rec_open" in names, "non-core command from same plugin should still register"
    assert any("reserved core command" in r.message.lower() or "core command" in r.message.lower()
               for r in caplog.records)


@pytest.mark.asyncio
async def test_after_ready_refreshes_transport_command_menu_for_plugin_commands(monkeypatch):
    """Plugin slash commands should be pushed through the Transport layer.

    bot.py must not import Telegram types to refresh the platform command menu.
    """
    from link_project_to_chat.bot import COMMANDS

    p = _RecordingCommandPlugin(_ctx(), {})
    p.name = "rec"
    p._commands_to_register = [_bc_with_description("rec_open", "Open recorder")]

    class _MenuTransport:
        TRANSPORT_ID = "fake"

        def __init__(self):
            self.registered: list[str] = []
            self.command_menu: list[tuple[str, str]] | None = None

        def on_command(self, name, handler):
            self.registered.append(name)

        async def set_command_menu(self, commands):
            self.command_menu = list(commands)

    transport = _MenuTransport()
    bot = _make_bot([])
    bot._transport = transport
    bot.transport_kind = "telegram"
    bot._plugin_configs = [{"name": "rec"}]
    bot._plugins = []
    bot.team_name = ""
    bot.role = None
    bot._refresh_team_system_note = lambda: None
    bot._backfill_own_bot_username = lambda: None
    bot._auth_dirty = False
    bot._wrap_plugin_command = lambda plugin, bc: bc.handler

    import link_project_to_chat.bot as bot_mod
    monkeypatch.setattr(bot_mod, "load_plugin", lambda *a, **kw: p)

    identity = Identity(
        transport_id="fake", native_id="bot", display_name="Bot",
        handle="bot", is_bot=True,
    )

    await bot._after_ready(identity)

    assert "rec_open" in transport.registered
    assert transport.command_menu is not None
    assert ("rec_open", "Open recorder") in transport.command_menu
    assert all(command in transport.command_menu for command in COMMANDS)


@pytest.mark.asyncio
async def test_plugin_command_inert_when_start_fails(monkeypatch):
    """If plugin.start() raises, _init_plugins removes the plugin from
    self._plugins. Commands registered before start ran stay wired on the
    transport (the Transport Protocol exposes no remove-handler), but
    invoking them must be a no-op via _wrap_plugin_command's active-plugin
    guard. Without this, a half-initialised plugin would still serve its
    commands and could trip over unset state.
    """
    from link_project_to_chat.transport.base import CommandInvocation

    inner_called: list[bool] = []

    async def _real_handler(_ci):  # the plugin's own handler
        inner_called.append(True)

    p = _RecordingCommandPlugin(_ctx(), {})
    p.name = "rec"
    p.start_raises = True
    p._commands_to_register = [
        BotCommand(command="rec_open", description="", handler=_real_handler)
    ]

    registered: list[tuple[str, Any]] = []

    class _FakeTransport:
        TRANSPORT_ID = "fake"
        def on_command(self, name, handler):
            registered.append((name, handler))

    bot = _make_bot([])
    bot._transport = _FakeTransport()
    bot._plugin_configs = [{"name": "rec"}]
    # NOTE: leave bot._plugins empty — _init_plugins appends from load_plugin.
    # Pre-seeding here would double-register the same plugin instance.
    bot._auth_dirty = False  # _persist_auth_if_dirty no-ops in the finally.
    # Force the auth/role checks in _wrap_plugin_command to pass so we
    # actually exercise the active-plugin guard. With empty _allowed_users,
    # _auth_identity fails closed and the handler is skipped for an
    # unrelated reason — masking whether the guard works.
    bot._auth_identity = lambda _identity: True
    bot._require_executor = lambda _identity: True
    import link_project_to_chat.bot as bot_mod
    monkeypatch.setattr(bot_mod, "load_plugin", lambda *a, **kw: p)
    # NOTE: DO NOT stub bot._wrap_plugin_command here — we need the real
    # one, including its `plugin_ref not in self._plugins` guard.

    await bot._init_plugins()

    # The failed plugin must be removed from dispatch.
    assert p not in bot._plugins, "plugin must be removed after start() failure"
    # The command was registered before start ran — transport still holds it.
    names = [n for n, _ in registered]
    assert "rec_open" in names, "command must have been registered pre-start"

    # Invoke the registered (wrapped) handler. The active-plugin guard
    # must short-circuit before the inner handler runs.
    wrapped = next(h for n, h in registered if n == "rec_open")
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="A", handle="alice", is_bot=False,
    )
    msg_ref = MessageRef(transport_id="fake", native_id="100", chat=chat)
    invocation = CommandInvocation(
        chat=chat, sender=sender, name="rec_open",
        args=[], raw_text="/rec_open", message=msg_ref,
    )
    await wrapped(invocation)
    assert inner_called == [], (
        "inner plugin handler must not run after start() failure"
    )


@pytest.mark.asyncio
async def test_plugin_command_collision_between_plugins(monkeypatch, caplog):
    """Two plugins both claim /share_cmd. First-load wins; the second
    plugin's /share_cmd is dropped, but its other commands still register."""
    from link_project_to_chat.bot import ProjectBot

    p1 = _RecordingCommandPlugin(_ctx(), {})
    p1.name = "first"
    p1._commands_to_register = [_bc("share_cmd")]

    p2 = _RecordingCommandPlugin(_ctx(), {})
    p2.name = "second"
    p2._commands_to_register = [_bc("share_cmd"), _bc("only_second")]

    registered: list[tuple[str, Any]] = []

    class _FakeTransport:
        TRANSPORT_ID = "fake"
        def on_command(self, name, handler):
            registered.append((name, handler))

    bot = _make_bot([])
    bot._transport = _FakeTransport()
    bot._plugin_configs = [{"name": "first"}, {"name": "second"}]
    bot._plugins = [p1, p2]
    import link_project_to_chat.bot as bot_mod
    plugin_by_name = {"first": p1, "second": p2}
    monkeypatch.setattr(bot_mod, "load_plugin",
                        lambda name, *a, **kw: plugin_by_name[name])
    bot._wrap_plugin_command = lambda plugin, bc: bc.handler

    with caplog.at_level("WARNING"):
        await bot._init_plugins()

    names = [n for n, _ in registered]
    # share_cmd registered exactly once (by p1 — first wins).
    assert names.count("share_cmd") == 1
    # p2's other command still registers.
    assert "only_second" in names
    assert any("already claimed" in r.message.lower() or "duplicate" in r.message.lower()
               for r in caplog.records)
