from __future__ import annotations

import asyncio
import logging
import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcriber import Synthesizer, Transcriber

from .config import (
    AllowedUser,
    Config,
    DEFAULT_CONFIG,
    RoomBinding,
    clear_session,
    load_config,
    load_session,
    load_teams,
    patch_backend_state,
    patch_project,
    patch_team,
    patch_team_bot_backend,
    patch_team_bot_backend_state,
    resolve_permissions,
    resolve_project_allowed_users,
    resolve_project_meta_dir,
    resolve_start_model,
    save_session,
)
from ._auth import AuthMixin
from .chat_history import ChatHistory
from .conversation_log import (
    ASSISTANT_ROLE,
    USER_ROLE,
    ConversationLog,
    default_db_path,
    format_history_block,
)
from .formatting import md_to_telegram, split_html, split_or_attach, strip_html
from .backends.claude import (
    PERMISSION_MODES,
    ClaudeBackend,
    ClaudeStreamError,
    is_usage_cap_error,
)
from .team_safety import TeamAuthority
from .transport import Button, Buttons, ChatKind, ChatRef, Identity, MessageRef
from .transport.streaming import StreamingMessage
from .stream import Question, StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType
from .plugin import BotCommand, Plugin, PluginContext, load_plugin

logger = logging.getLogger(__name__)

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List all tasks"),
    ("backend", "Show or switch backend"),
    ("model", "Set backend model"),
    ("effort", "Set backend reasoning effort"),
    ("thinking", "Toggle live thinking display (on/off)"),
    ("context", "Toggle per-chat conversation history (on/off/N)"),
    ("permissions", "Set permission mode"),
    ("compact", "Compact backend session"),
    ("status", "Bot status"),
    ("reset", "Clear backend session"),
    ("version", "Show version"),
    ("help", "Show available commands"),
    ("skills", "List skills or activate one"),
    ("stop_skill", "Deactivate current skill"),
    ("create_skill", "Create a new skill"),
    ("delete_skill", "Delete a skill"),
    ("persona", "Activate a persona (per-message)"),
    ("stop_persona", "Deactivate current persona"),
    ("create_persona", "Create a new persona"),
    ("delete_persona", "Delete a persona"),
    ("voice", "Show voice transcription status"),
    ("lang", "Switch voice message language"),
    ("halt", "Pause bot-to-bot iteration (group only)"),
    ("resume", "Resume bot-to-bot iteration (group only)"),
]

_CMD_HELP = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)


def _render_team_safety_block(
    authority: TeamAuthority,
    consecutive_turns: int,
    max_autonomous_turns: int,
) -> str:
    snap = authority.status_snapshot
    lines = [
        "Team safety: strict",
        f"Autonomous turns: {consecutive_turns} / {max_autonomous_turns}",
    ]
    if snap["active_grants"]:
        lines.append("Active grants:")
        for grant in snap["active_grants"]:
            scopes = ", ".join(grant["scopes"])
            lines.append(
                f"- {scopes} ({grant['age_seconds']}s ago, msg #{grant['user_message_id']})"
            )
    else:
        lines.append("Active grants: none")
    return "\n".join(lines)


def _parse_task_id(data: str) -> int:
    return int(data.split("_")[-1])


def _topo_sort(plugins: "list[Plugin]") -> "list[Plugin]":
    """Order plugins so each comes after the plugins it depends_on.

    Missing dependencies are logged but do not drop the plugin (best-effort).
    """
    by_name = {p.name: p for p in plugins}
    visited: set[str] = set()
    temp: set[str] = set()
    out: list[Plugin] = []

    def visit(p: Plugin) -> None:
        if p.name in visited:
            return
        if p.name in temp:
            logger.warning("plugin dependency cycle involving %s", p.name)
            return
        temp.add(p.name)
        for dep in p.depends_on:
            target = by_name.get(dep)
            if target is None:
                logger.warning(
                    "plugin %s depends_on %s which is not installed; ignoring",
                    p.name, dep,
                )
                continue
            visit(target)
        temp.discard(p.name)
        visited.add(p.name)
        out.append(p)

    for p in plugins:
        visit(p)
    return out


class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_username: str = "",
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        allowed_usernames: list[str] | None = None,
        transcriber: "Transcriber | None" = None,
        synthesizer: "Synthesizer | None" = None,
        active_persona: str | None = None,
        show_thinking: bool = False,
        team_name: str | None = None,
        group_chat_id: int | None = None,
        room: RoomBinding | None = None,
        role: str | None = None,
        peer_bot_username: str = "",
        config_path: Path | None = None,
        transport_kind: str = "telegram",
        web_port: int = 8080,
        backend_name: str = "claude",
        backend_state: dict[str, dict] | None = None,
        context_enabled: bool = True,
        context_history_limit: int = 10,
        conversation_log: ConversationLog | None = None,
        plugins: list[dict] | None = None,
        allowed_users: list | None = None,
        auth_source: str = "project",
        respond_in_groups: bool = False,
        config: Config | None = None,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self._config_path = config_path
        # Source of operator-tuned settings (currently used for ``meta_dir``;
        # other knobs still flow via individual ctor kwargs). When the caller
        # didn't supply a Config (e.g. legacy ``run_bot`` callers, tests that
        # only need the bot's transport surface) we fall back to a default
        # Config so ``self._config.meta_dir`` is always usable in
        # ``_init_plugins`` without a None-check.
        self._config: Config = config if config is not None else Config()
        self.transport_kind = transport_kind
        self.web_port = web_port
        # Legacy ``allowed_usernames`` is the only pre-v1.0 kwarg still accepted
        # on the signature; it is consumed by the synthesis block below to seed
        # ``_allowed_users``. The ``trusted_*`` kwargs and ``on_trust`` callback
        # are gone — the AllowedUser model carries per-user locked_identities.
        self._started_at = time.monotonic()
        self._app = None
        self._transport = None  # TelegramTransport — set in _build_app
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._live_text: dict[int, StreamingMessage] = {}
        self._live_thinking: dict[int, StreamingMessage] = {}
        # Tasks whose live message start() failed once — don't retry for the
        # rest of the turn; fall back to post-completion paths instead.
        self._live_text_failed: set[int] = set()
        self._live_thinking_failed: set[int] = set()
        self._thinking_buf: dict[int, str] = {}   # task_id → accumulated thinking (toggle-off path OR live fallback)
        self._thinking_store: dict[int, str] = {}  # task_id → thinking text
        self._init_auth()
        # Hot-reload state for AuthMixin._reload_allowed_users_if_stale —
        # _auth_identity polls these on each authentication so manager-bot
        # writes to allowed_users land in this process within 5 s without a
        # restart. Falls back to DEFAULT_CONFIG when the caller didn't pass an
        # explicit config_path (matches _effective_config_path()).
        self._project_config_path: Path = config_path or DEFAULT_CONFIG
        self._project_name: str = self.name
        self._last_allowed_users_reload: float = 0.0
        self._active_skill: str | None = None
        self._active_persona = active_persona
        # Pending skill/persona capture — set by /create_skill + scope-button click,
        # consumed by the next plain-text message. Initialized here so _on_text
        # can read them unconditionally without a getattr dance.
        self._pending_skill_name: str | None = None
        self._pending_skill_scope: str | None = None
        self._pending_persona_name: str | None = None
        self._pending_persona_scope: str | None = None
        self.show_thinking = show_thinking
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._voice_tasks: set[int] = set()
        from .backends.factory import create as _create_backend
        # Phase 2: legacy flat constructor args still take effect, layered onto
        # any backend_state[<backend_name>] supplied by the caller. Persisted
        # backend_state wins for keys it sets; legacy args fill the rest.
        persisted_state = dict((backend_state or {}).get(backend_name, {}))
        persisted_state.setdefault(
            "permissions",
            "dangerously-skip-permissions" if skip_permissions else permission_mode,
        )
        persisted_state.setdefault("allowed_tools", allowed_tools or [])
        persisted_state.setdefault("disallowed_tools", disallowed_tools or [])
        persisted_state.setdefault("show_thinking", show_thinking)
        _backend = _create_backend(backend_name, self.path, persisted_state)
        self._backend_name = backend_name
        self._backend_state = dict(backend_state or {})
        self._backend_state[backend_name] = persisted_state
        self._backend_switch_lock = asyncio.Lock()
        self.task_manager = TaskManager(
            project_path=self.path,
            backend=_backend,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            on_waiting_input=self._on_waiting_input,
        )
        # Per-room chatter history for "[Recent discussion]" injection on
        # each LLM call. Process-local; skipped for DMs (those use
        # conversation_log for prior-turn context).
        self._chat_history: ChatHistory = ChatHistory()
        # Per-chat conversation log. Bot-level (not backend-level) so a
        # ``/backend codex`` swap doesn't lose Claude-era context. When the
        # caller supplied ``config_path`` (typically tests with a tmp_path
        # config) the conversation DB lives next to it so tests don't write
        # under the user's home; production callers fall back to the
        # standard ``~/.link-project-to-chat/conversations/<name>.db``.
        self.context_enabled = context_enabled
        self.context_history_limit = context_history_limit
        if conversation_log is not None:
            self.conversation_log = conversation_log
        else:
            if self._config_path is not None:
                db_path = self._config_path.parent / "conversations" / f"{self.name}.db"
            else:
                db_path = default_db_path(self.name)
            self.conversation_log = ConversationLog(db_path)
        self.team_name = team_name
        self.group_mode = team_name is not None
        self.group_chat_id = group_chat_id
        # `_room` is the canonical transport-agnostic bound-room reference.
        # Populated either from the `room` kwarg (new path, any transport) or
        # synthesized from `group_chat_id` (legacy Telegram path) at the first
        # opportunity. `0` is the legacy "not yet captured" sentinel.
        if room is not None:
            self._room: RoomBinding | None = room
        elif group_chat_id is not None and group_chat_id != 0:
            self._room = RoomBinding(transport_id="telegram", native_id=str(group_chat_id))
        else:
            self._room = None
        self.role = role
        self.peer_bot_username = peer_bot_username
        self.bot_username: str = ""  # populated in _after_ready via transport.on_ready
        self._team_authority: TeamAuthority | None = None
        # Resolve cfg.safety_prompt → backend.safety_system_prompt once at
        # startup. The backend renders this on every prompt; see
        # _resolve_safety_prompt() for the three-state semantics.
        self._resolve_safety_prompt()
        # Tell Claude about its own + peer @handle so it uses the correct
        # usernames instead of placeholders ("@developer") or hallucinating a
        # pre-suffix-bump name it remembers from the persona. Called once here
        # so existing peer info is available, and again in _after_ready once
        # the transport's on_ready fires with the bot's identity.
        self._refresh_team_system_note()
        from .group_state import GroupStateRegistry
        self._group_state = GroupStateRegistry(max_bot_rounds=20)
        self._probe_tasks: set[asyncio.Task] = set()
        # Plugin framework state. Populated in _after_ready after transport is ready.
        self._plugin_configs: list[dict] = list(plugins or [])
        self._plugins: list[Plugin] = []
        self._plugin_command_handlers: dict[str, list] = {}
        self._shared_ctx: PluginContext | None = None
        # Sole auth + authority source. Empty list → fail-closed (no users authorized).
        # TRANSITION SHIM (Task 5): when callers pass legacy
        # ``allowed_username`` / ``allowed_usernames`` but not ``allowed_users``,
        # synthesize a list of executor AllowedUser entries from the legacy
        # kwargs. Lets the existing test suite (and a small handful of
        # external callers) keep working through Tasks 5–6 while the new
        # auth path becomes the only one. Step 11/12 strip the legacy kwargs.
        if allowed_users is not None:
            self._allowed_users: list = list(allowed_users)
        elif allowed_usernames:
            self._allowed_users = [
                AllowedUser(username=str(u).lstrip("@").lower(), role="executor")
                for u in allowed_usernames
            ]
        elif allowed_username:
            self._allowed_users = [
                AllowedUser(
                    username=str(allowed_username).lstrip("@").lower(),
                    role="executor",
                ),
            ]
        else:
            self._allowed_users = []
        # Track which scope this allow-list came from so _persist_auth_if_dirty
        # writes back to the right place. "project" for per-project lists,
        # "global" when run_bots used the Config.allowed_users fallback.
        self._auth_source: str = auth_source if auth_source in ("project", "global") else "project"
        # Solo-mode group-routing flag (spec respond_in_groups v1.1.0). When
        # True and chat.kind == ROOM, _on_text_from_transport requires the
        # message to be addressed-at-me (@mention or reply-to-bot, from a
        # human, not self). Default False — DM-only pre-v1.1.0 behavior.
        self._respond_in_groups: bool = bool(respond_in_groups)

    @property
    def _claude(self) -> ClaudeBackend:
        """Tier-2 accessor for Claude-specific behavior.

        Only valid while the
        configured backend is ClaudeBackend; asserts so other backends
        surface a clear error rather than silent attribute misses."""
        backend = self.task_manager.backend
        assert isinstance(backend, ClaudeBackend), "Tier-2 Claude-only access requires ClaudeBackend"
        return backend

    def _backend_supports_prompt_customization(self) -> bool:
        return isinstance(self.task_manager.backend, ClaudeBackend)

    def _backend_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_backend_switch_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._backend_switch_lock = lock
        return lock

    async def _history_block(self, chat: ChatRef) -> str:
        """Render the recent-history prepend for a turn in ``chat``.

        Returns an empty string when context history is disabled OR the log
        for this chat is empty, so callers can concatenate unconditionally.
        Tolerates a stub bot (constructed via ``__new__``) that didn't run
        ``__init__`` by treating missing attributes as "feature disabled".
        """
        if not getattr(self, "context_enabled", False):
            return ""
        log = getattr(self, "conversation_log", None)
        if log is None:
            return ""
        turns = await log.recent_async(
            chat, limit=getattr(self, "context_history_limit", 10)
        )
        return format_history_block(turns)

    async def _log_user_turn(self, chat: ChatRef, text: str) -> None:
        """Capture a conversational user turn for cross-backend continuity.

        Only conversational text — slash commands, button clicks, file
        uploads, and ``/run`` output stay out of the log.
        """
        if not text or not text.strip():
            return
        log = getattr(self, "conversation_log", None)
        if log is None:
            return
        # Writes continue while /context is off so re-enabling context has a
        # coherent recent-history window available.
        await log.append_async(
            chat,
            USER_ROLE,
            text,
            backend=self.task_manager.backend.name,
        )

    async def _log_assistant_turn(self, chat: ChatRef, text: str) -> None:
        if not text or not text.strip():
            return
        log = getattr(self, "conversation_log", None)
        if log is None:
            return
        await log.append_async(
            chat,
            ASSISTANT_ROLE,
            text,
            backend=self.task_manager.backend.name,
        )

    def _effective_config_path(self) -> Path:
        return self._config_path or DEFAULT_CONFIG

    def _is_wrong_room(self, chat: ChatRef) -> bool:
        """True if this chat is not the bound room. Caller silently ignores."""
        if self._room is None:
            return False
        return (
            chat.transport_id != self._room.transport_id
            or chat.native_id != self._room.native_id
        )

    def _capture_room(self, chat: ChatRef) -> None:
        """Bind this team to `chat` and persist. Mirrors a legacy
        Telegram-shaped `group_chat_id` int for one release for downgrade
        safety; non-Telegram transports persist only the new shape."""
        new_room = RoomBinding(transport_id=chat.transport_id, native_id=chat.native_id)
        fields: dict = {
            "room": {
                "transport_id": new_room.transport_id,
                "native_id": new_room.native_id,
            }
        }
        if new_room.transport_id == "telegram":
            try:
                fields["group_chat_id"] = int(new_room.native_id)
            except ValueError:
                pass
        assert self.team_name is not None
        patch_team(
            self.team_name,
            fields,
            self._effective_config_path(),
        )
        self._room = new_room
        if new_room.transport_id == "telegram":
            try:
                self.group_chat_id = int(new_room.native_id)
            except ValueError:
                pass

    async def _on_task_started(self, task: Task) -> None:
        # Only show typing indicator for Claude tasks, not /run commands
        if task.type == TaskType.COMMAND:
            return
        assert self._transport is not None
        self._typing_tasks[task.id] = asyncio.create_task(
            self._keep_typing(self._transport, task.chat)
        )
        # Team bots skip per-delta livestreaming (_on_stream_event returns
        # early), but we still need to emit *one* message early so the relay's
        # event-driven auto-delete (_delete_pending_for_peer) can drop the
        # forwarded trigger message before its 60-second fallback deletes it.
        # Sending the placeholder now — while the forward still exists —
        # means `reply_to` resolves; finalize() later *edits* this same
        # message, so reply_to is never re-validated against a vanished
        # target.
        if self.group_mode and task.id not in self._live_text:
            live = StreamingMessage(
                transport=self._transport,
                chat=task.chat,
                reply_to=task.message,
            )
            try:
                await live.start()
            except Exception:
                logger.exception(
                    "StreamingMessage.start failed for team placeholder (task #%d); "
                    "falling back to send-at-finalize",
                    task.id,
                )
                self._live_text_failed.add(task.id)
                return
            self._live_text[task.id] = live

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, TextDelta):
            # Team bots do not livestream to the group chat. Streaming edits
            # produce partial content that team_relay forwards before the
            # message is complete; disabling livestream here means task.result
            # is built from collected_text in task_manager and sent once at
            # finalize via _send_to_chat, eliminating the partial-relay race.
            if self.group_mode:
                return
            live = self._live_text.get(task.id)
            if live is None and task.id not in self._live_text_failed:
                assert self._transport is not None
                live = StreamingMessage(
                    self._transport,
                    task.chat,
                    reply_to=task.message,
                )
                try:
                    await live.start()
                except Exception:
                    logger.exception(
                        "StreamingMessage.start failed for live text (task #%d); "
                        "falling back to send-at-finalize",
                        task.id,
                    )
                    self._live_text_failed.add(task.id)
                    # task.result will be populated from collected_text in
                    # task_manager; _finalize_claude_task's "no live_text"
                    # branch will then _send_to_chat it in one shot.
                    return
                self._live_text[task.id] = live
            if live is not None:
                await live.append(event.text)
        elif isinstance(event, ThinkingDelta):
            if self.show_thinking and task.id not in self._live_thinking_failed and not self.group_mode:
                live = self._live_thinking.get(task.id)
                if live is None:
                    assert self._transport is not None
                    live = StreamingMessage(
                        self._transport,
                        task.chat,
                        reply_to=task.message,
                        prefix="💭 ",
                    )
                    try:
                        await live.start()
                    except Exception:
                        logger.exception(
                            "StreamingMessage.start failed for live thinking (task #%d); "
                            "falling back to post-completion Thinking button",
                            task.id,
                        )
                        self._live_thinking_failed.add(task.id)
                        # Fall through to the buffer-accumulation branch so
                        # _finalize_claude_task can still populate _thinking_store
                        # (which powers the /tasks → Thinking button).
                        live = None
                    else:
                        self._live_thinking[task.id] = live
                if live is not None:
                    await live.append(event.text)
                    return
            # Toggle-off path, group-mode path, OR toggle-on with failed live start.
            buf = self._thinking_buf.setdefault(task.id, "")
            sep = "\n\n" if buf else ""
            self._thinking_buf[task.id] = buf + sep + event.text
        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(task.chat, event.path, reply_to=task.message)
            await self._dispatch_plugin_tool_use(event)

    async def _send_html(
        self,
        chat: ChatRef,
        html: str,
        reply_to: MessageRef | None = None,
        reply_markup: Buttons | None = None,
    ) -> MessageRef | None:
        """Send HTML message(s), attaching buttons to the last chunk. Returns the last sent ref.

        For multi-chunk messages, each chunk replies to the previous chunk
        (linear chain). The first chunk replies to ``reply_to``. This gives
        every transport a native sense of "these messages belong together":
        Google Chat and Slack keep all chunks in the same thread; Telegram
        and Discord render a reply chain. If a chunk's send fails entirely,
        the next chunk falls back to the last successful parent rather than
        breaking the chain.
        """
        assert self._transport is not None
        chunks = split_html(html, limit=self._transport.max_text_length)
        last_ref: MessageRef | None = None
        chain_parent: MessageRef | None = reply_to
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            btns = reply_markup if is_last else None
            sent_ref: MessageRef | None = None
            try:
                sent_ref = await self._transport.send_text(
                    chat, chunk, html=True, reply_to=chain_parent, buttons=btns,
                )
            except Exception as exc:
                # The transport already retries on deleted-reply-target. Anything
                # reaching here is genuinely unexpected (parse error, malformed
                # HTML, network issue). Fall back to plain so the user gets *some*
                # output; log so the cause is recoverable.
                logger.warning("HTML send failed, falling back to plain: %s", exc, exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    try:
                        sent_ref = await self._transport.send_text(
                            chat,
                            plain[:self._transport.max_text_length] if len(plain) > self._transport.max_text_length else plain,
                            reply_to=chain_parent,
                            buttons=btns,
                        )
                    except Exception:
                        logger.error("Plain-text fallback also failed", exc_info=True)
            if sent_ref is not None:
                last_ref = sent_ref
                chain_parent = sent_ref  # next chunk replies to this one
            # if sent_ref is None (both send paths failed), keep chain_parent
            # so the NEXT chunk still attaches to the last successful message.
        return last_ref

    async def _send_to_chat(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        if self.group_mode:
            await self._send_to_chat_singleton(chat, text, reply_to=reply_to)
            return
        html = md_to_telegram(text or "[No output]").replace("\x00", "")
        await self._send_html(chat, html, reply_to)

    async def _send_to_chat_singleton(
        self, chat: ChatRef, text: str, *, reply_to: MessageRef | None = None,
    ) -> None:
        """Team-mode send: exactly one Telegram message + optional file overflow.

        Multi-message replies fragment bot-to-bot context: the relay's coalesce
        is fragile (3s window, requires same reply_to, only the first chunk
        carries the @peer mention), so late or out-of-order parts get silently
        dropped. Singleton sends keep each reply self-contained — content past
        the visible limit lives in the attachment, which the peer bot reads via
        ``incoming.files`` per its normal upload-handling path.
        """
        assert self._transport is not None
        body = text or "[No output]"
        visible, overflow = split_or_attach(body)
        html = md_to_telegram(visible).replace("\x00", "")
        sent_html = False
        try:
            await self._transport.send_text(chat, html, html=True, reply_to=reply_to)
            sent_html = True
        except Exception as exc:
            logger.warning(
                "Team singleton HTML send failed; falling back to plain: %s",
                exc, exc_info=True,
            )
        if not sent_html:
            plain = strip_html(html).replace("\x00", "")
            if plain.strip():
                try:
                    await self._transport.send_text(chat, plain, reply_to=reply_to)
                except Exception:
                    logger.error(
                        "Team singleton plain fallback failed", exc_info=True,
                    )
                    return
        if overflow is not None:
            await self._send_overflow_attachment(chat, overflow)

    async def _send_overflow_attachment(
        self, chat: ChatRef, overflow: str,
    ) -> None:
        """Persist `overflow` to a per-bot temp file and send it via the transport.

        The file is left on disk after upload — the peer bot's standard upload
        handler downloads its own copy from the platform, so cleanup of this
        local copy isn't on the response path. Files are tiny (text only) and
        live in the per-bot overflow dir, which can be cleaned by external
        rotation if it ever grows.
        """
        assert self._transport is not None
        overflow_dir = (
            Path(tempfile.gettempdir())
            / "link-project-to-chat"
            / self.name
            / "overflow"
        )
        overflow_dir.mkdir(parents=True, exist_ok=True)
        path = overflow_dir / f"reply-{uuid.uuid4().hex}.txt"
        path.write_text(overflow, encoding="utf-8")
        try:
            await self._transport.send_file(
                chat, path, caption="full reply text (truncated in chat above)",
            )
        except Exception:
            logger.exception("Team singleton overflow attachment failed")

    async def _send_raw(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        escaped = (text or "[No output]").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._send_html(chat, f"<pre>{escaped}</pre>", reply_to)

    async def _cancel_live_for(self, task_id: int, note: str = "(cancelled)") -> None:
        """Seal any live text/thinking messages for this task with a cancellation marker."""
        live_text = self._live_text.pop(task_id, None)
        self._live_text_failed.discard(task_id)
        if live_text is not None:
            try:
                await live_text.cancel(note)
            except Exception:
                logger.warning("Failed to cancel live text for task %d", task_id, exc_info=True)
        live_thinking = self._live_thinking.pop(task_id, None)
        self._live_thinking_failed.discard(task_id)
        if live_thinking is not None:
            try:
                await live_thinking.cancel(note)
            except Exception:
                logger.warning("Failed to cancel live thinking for task %d", task_id, exc_info=True)

    async def _finalize_claude_task(self, task: Task) -> str | None:
        live_text = self._live_text.pop(task.id, None)
        live_thinking = self._live_thinking.pop(task.id, None)
        self._live_text_failed.discard(task.id)
        self._live_thinking_failed.discard(task.id)
        thinking = self._thinking_buf.pop(task.id, None)
        is_voice = task.id in self._voice_tasks
        self._voice_tasks.discard(task.id)

        if task._compact:
            text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
            # Compact tasks don't stream, but clean up defensively.
            if live_text is not None:
                await live_text.cancel("(compacted)")
            if live_thinking is not None:
                await live_thinking.cancel("(compacted)")
            await self._send_to_chat(task.chat, text, reply_to=task.message)
            return None

        if task.status == TaskStatus.DONE:
            assistant_text: str | None = None
            if live_text is not None:
                # Don't pass task.result — it contains only the LAST assistant text block.
                # The StreamingMessage buffer already holds every streamed text delta (narration
                # + final answer); overwriting it would make intermediate narration vanish.
                # Fall back to task.result only when the buffer is empty (stream dropped).
                # Use full_text (not buffer) so rotations don't truncate the persisted turn.
                has_buffer = bool(live_text.full_text.strip())
                has_result = bool((task.result or "").strip())
                if not has_buffer and not has_result:
                    # Claude turn ended with only tool_use blocks (no text output). The
                    # "…" placeholder would otherwise linger forever — replace with a
                    # short notice so the user knows the turn finished.
                    await live_text.finalize(
                        "(done — no text response; check files/tools the bot touched)",
                        render=False,
                    )
                else:
                    fallback = task.result if not has_buffer else None
                    assistant_text = live_text.full_text if has_buffer else task.result
                    await live_text.finalize(fallback, render=True)
                await self._send_completion_notice(task)
            else:
                assistant_text = task.result
                await self._send_to_chat(task.chat, task.result, reply_to=task.message)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)
            elif thinking:
                self._thinking_store[task.id] = thinking
            if (
                is_voice
                and self._synthesizer
                and task.result
                and self._transport.TRANSPORT_ID != "google_chat"
            ):
                # Google Chat's REST media.upload requires user-OAuth scopes;
                # the default chat.bot service-account auth 403s. Skip the
                # TTS round-trip so the operator gets a clean text reply
                # instead of "[Google Chat file upload is not available]".
                await self._send_voice_response(task.chat, task.result, reply_to=task.message)
            return assistant_text
        else:
            error_text = f"Error: {task.error}"
            if is_usage_cap_error(task.error) and self.group_mode:
                if live_text is not None:
                    await live_text.finalize(error_text, render=False)
                if live_thinking is not None:
                    await live_thinking.finalize(render=False)
                self._group_state.halt(task.chat)
                await self._send_to_chat(
                    task.chat,
                    "Hit Max usage cap. Pausing until reset. Will retry every 30 min.",
                    reply_to=task.message,
                )
                self._schedule_cap_probe(task.chat)
                return None
            if live_text is not None:
                await live_text.finalize(error_text, render=False)
                await self._send_completion_notice(task, failed=True)
            else:
                await self._send_to_chat(task.chat, error_text, reply_to=task.message)
            if live_thinking is not None:
                await live_thinking.finalize(render=False)
            return None

    async def _send_completion_notice(self, task: Task, *, failed: bool = False) -> None:
        """Send a fresh completion ping after a live-edited agent response.

        Telegram edits keep the original timestamp and usually do not notify.
        This short message gives users a final notification and visible elapsed
        time without duplicating the full answer.
        """
        if self.group_mode:
            return
        assert self._transport is not None
        status = "Failed" if failed else "Done"
        elapsed = f" in {task.elapsed_human}" if task.elapsed_human else ""
        await self._transport.send_text(task.chat, f"{status}{elapsed}.", reply_to=task.message)

    def _schedule_cap_probe(self, chat: ChatRef, interval_s: int = 1800) -> None:
        """Probe the backend every `interval_s` seconds; on success, resume the group."""
        if not self.task_manager.backend.capabilities.supports_usage_cap_detection:
            return
        async def _probe() -> None:
            while self._group_state.get(chat).halted:
                await asyncio.sleep(interval_s)
                if not self._group_state.get(chat).halted:
                    return  # user manually resumed
                try:
                    status = await self.task_manager.backend.probe_health()
                except Exception:
                    logger.warning("cap probe failed", exc_info=True)
                    continue
                if status.ok and not status.usage_capped:
                    self._group_state.resume(chat)
                    await self._send_to_chat(chat, "Usage cap cleared. Resumed.")
                    return
        task = asyncio.create_task(_probe())
        self._probe_tasks.add(task)
        task.add_done_callback(self._probe_tasks.discard)

    async def _send_voice_response(
        self, chat: ChatRef, text: str, reply_to: MessageRef | None = None,
    ) -> None:
        assert self._transport is not None
        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "tts"
        voice_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        plain = strip_html(md_to_telegram(text))
        if len(plain) > self._transport.max_text_length:
            plain = plain[:self._transport.max_text_length - 3] + "..."
        out_path = voice_dir / f"tts_{uuid.uuid4().hex}.opus"

        try:
            await self._synthesizer.synthesize(plain, out_path)
            await self._transport.send_voice(chat, out_path, reply_to=reply_to)
        except Exception:
            logger.warning("TTS failed", exc_info=True)
        finally:
            if out_path.exists():
                try:
                    out_path.unlink()
                except OSError:
                    pass

    async def _finalize_command_task(self, task: Task) -> None:
        output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated, use /log)"
        elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
        if task.status == TaskStatus.DONE:
            await self._send_raw(task.chat, f"{output}\n[exit 0{elapsed}]")
        else:
            await self._send_raw(task.chat, f"[exit {task.exit_code}{elapsed}]\n\n{output}")

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.AGENT:
            task_backend = getattr(task, "_backend", None) or self.task_manager.backend
            if task_backend.session_id:
                try:
                    if self.team_name and self.role:
                        patch_team_bot_backend_state(
                            self.team_name,
                            self.role,
                            task_backend.name,
                            {"session_id": task_backend.session_id},
                            self._effective_config_path(),
                        )
                    else:
                        patch_backend_state(
                            self.name,
                            task_backend.name,
                            {"session_id": task_backend.session_id},
                            self._effective_config_path(),
                        )
                except Exception:
                    logger.exception(
                        "Failed to persist session_id for task #%d", task.id
                    )
            # Capture the assistant's final reply text into the per-chat
            # conversation log. Skip /compact tasks (they don't represent a
            # conversational reply) and skip when the turn produced no text.
            assistant_text = await self._finalize_claude_task(task)
            if not task._compact and task.status == TaskStatus.DONE and assistant_text:
                await self._log_assistant_turn(task.chat, assistant_text)
        else:
            await self._finalize_command_task(task)
        await self._dispatch_plugin_task_complete(task)

    def _question_buttons(self, task_id: int, q_idx: int, question: Question) -> Buttons:
        rows: list[list[Button]] = []
        for opt_idx, opt in enumerate(question.options):
            label = opt.label[:64] or f"Option {opt_idx + 1}"
            rows.append([
                Button(label=label, value=f"ask_{task_id}_{q_idx}_{opt_idx}")
            ])
        return Buttons(rows=rows)

    @staticmethod
    def _render_question_html(question) -> str:
        """Render an AskUserQuestion Question as Telegram-compatible HTML.

        Used by _on_waiting_input (initial send) and _on_button (ask-answer
        annotation after user picks an option).
        """
        def _esc(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        header = question.header.strip() if question.header else ""
        body = question.question.strip() or "(question)"
        lines: list[str] = []
        if header:
            lines.append(f"<b>{_esc(header)}</b>")
        lines.append(_esc(body))
        if question.multi_select:
            lines.append("<i>(Multi-select: tap an option or reply with comma-separated values.)</i>")
        else:
            lines.append("<i>(Tap an option or reply with free text.)</i>")
        for opt in question.options:
            if opt.description:
                lines.append(f"• <b>{_esc(opt.label)}</b> — {_esc(opt.description)}")
        return "\n".join(lines)

    async def _on_waiting_input(self, task: Task) -> None:
        """Render pending questions as Telegram messages with option buttons."""
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        # Finalize any live messages so the question buttons appear after the sealed stream.
        live_text = self._live_text.pop(task.id, None)
        self._live_text_failed.discard(task.id)
        if live_text is not None:
            await live_text.finalize(task.result or None, render=True)
        elif task.result and task.result.strip():
            await self._send_to_chat(task.chat, task.result, reply_to=task.message)
        live_thinking = self._live_thinking.pop(task.id, None)
        self._live_thinking_failed.discard(task.id)
        if live_thinking is not None:
            await live_thinking.finalize(render=False)

        for q_idx, question in enumerate(task.pending_questions):
            await self._send_html(
                task.chat,
                self._render_question_html(question),
                reply_to=task.message,
                reply_markup=self._question_buttons(task.id, q_idx, question),
            )

    async def _on_start(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        backend_name = self.task_manager.backend.name
        await self._transport.send_text(
            ci.chat,
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with {backend_name}.\n{_CMD_HELP}",
            reply_to=ci.message,
        )

    def _strip_self_mention(self, incoming: "IncomingMessage") -> "IncomingMessage":
        """Remove case-insensitive ``@<bot_username>`` from incoming.text.

        Word-bounded: only strips when the mention is bounded by non-word
        characters (or start/end of string). Handles like ``@MyBotIsCool`` or
        embedded sequences like ``user@MyBot.example`` are left intact.

        Returns a new ``IncomingMessage`` via ``dataclasses.replace`` since
        ``IncomingMessage`` is frozen. When ``self.bot_username`` is empty
        (typical before ``_after_ready`` fires), the helper is a no-op and
        returns the original incoming unchanged.

        Used by the ``respond_in_groups`` routing gate in
        ``_on_text_from_transport``; captions ride on ``incoming.text`` per
        ``TelegramTransport._dispatch_message`` so this helper also cleans
        captioned files / voice / photo messages.
        """
        if not self.bot_username:
            return incoming
        import dataclasses
        import re
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_@])@{re.escape(self.bot_username)}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        cleaned = pattern.sub("", incoming.text)
        if cleaned == incoming.text:
            return incoming
        return dataclasses.replace(incoming, text=cleaned)

    def _resolve_recent_discussion(self, incoming) -> str:
        """For ROOM-kind chats: serialize chatter since last LLM call,
        mark the boundary so subsequent calls don't re-include the same
        messages. DMs always return "".

        The mark is set on the last msg_id currently in the buffer when
        one exists — that way the mark is always findable by future
        ``since_last_llm`` calls, even if the incoming message wasn't
        recorded into chat_history (e.g., direct-unit-test callers).
        Falls back to the incoming msg_id when the buffer is empty.
        """
        if incoming.chat.kind != ChatKind.ROOM:
            return ""
        text = self._chat_history.since_last_llm(
            incoming.chat, incoming.message.native_id,
        )
        mark_id = self._chat_history.last_msg_id(incoming.chat) or incoming.message.native_id
        self._chat_history.mark_llm_call(incoming.chat, mark_id)
        return text

    async def _on_text_from_transport(self, incoming) -> None:
        """Unified entry point for inbound messages from the Transport.

        Branch order:
          0. Solo respond_in_groups gate (when chat.kind == ROOM and not team)
          1. Voice (audio mime) → _on_voice_from_transport
          2. Non-audio files → _on_file_from_transport
          3. Plain text → _on_text (legacy shim)
          4. Nothing actionable → generic unsupported reply
        """
        # 0. Solo project bot in a Telegram group (spec respond_in_groups
        # v1.1.0). Runs BEFORE voice/file/text routing so files and voice
        # in groups are gated identically. Team mode (group_mode) keeps its
        # own routing path below — this branch is mutually exclusive with it.
        # When the gate passes, fall through to the existing payload-type
        # dispatch below so @-mentioned voice / captioned-file / text get
        # the same treatment as in DM. Calling ``_on_text`` directly here
        # would silently discard ``incoming.files`` (photos, voice notes,
        # documents), since ``_on_text`` only reads ``incoming.text``.
        if not self.group_mode and incoming.chat.kind == ChatKind.ROOM:
            if not self._respond_in_groups:
                # Defensive bot-side filter: production PTB filter already
                # blocks group messages here, but if a transport ever bypasses
                # the filter (tests, alternate non-Telegram transports), drop
                # silently rather than treating it as a DM.
                return
            from .group_filters import is_from_self, is_directed_at_me
            if is_from_self(incoming, self.bot_username):
                return
            if incoming.sender.is_bot:
                return  # peer-bot defense: solo mode never accepts other bots
            if not is_directed_at_me(incoming, self.bot_username):
                return
            incoming = self._strip_self_mention(incoming)
            # Reply-context promotion: when the user @-mentioned the bot
            # in a reply to another message and wrote nothing else (so the
            # stripped text is empty), treat the replied-to content as the
            # user's intent and forward it as the prompt. Without this the
            # bot falls through to "Nothing actionable" and replies "This
            # message type is not supported" — confusing UX when the user
            # clearly meant "@bot, look at this message". Cleared
            # reply_to_text on the forwarded message so _build_user_prompt
            # doesn't double-prepend a "[Replying to: ...]" header
            # containing the same content we just promoted.
            if (
                not incoming.text.strip()
                and not incoming.files
                and not incoming.has_unsupported_media
                and incoming.reply_to_text
            ):
                import dataclasses
                incoming = dataclasses.replace(
                    incoming,
                    text=incoming.reply_to_text,
                    reply_to_text=None,
                )
            # Record cleaned text for group chat-history (idempotent: no-op
            # for DMs since the record() side gates on chat.kind == ROOM).
            # Placed after mention-strip + reply-promotion so the stored
            # text matches what the human typed and what the bot sees.
            self._chat_history.record(
                chat=incoming.chat,
                msg_id=incoming.message.native_id,
                sender=(
                    incoming.sender.handle
                    or incoming.sender.display_name
                    or "unknown"
                ),
                text=incoming.text,
                is_own_bot=False,  # incoming gate already filters self-messages
            )
            # Fall through to the existing payload-type dispatch below
            # (voice / file / text). Don't call _on_text directly here —
            # _on_text only handles text+prompt; it doesn't transcribe voice
            # or copy captioned files into the prompt. The fall-through gives
            # @-mentioned media the same treatment as DM media.

        # 1. Voice (audio mime).
        if incoming.files and any(
            (f.mime_type or "").startswith("audio/") for f in incoming.files
        ):
            await self._on_voice_from_transport(incoming)
            return

        # 2. Non-audio files.
        if incoming.files:
            await self._on_file_from_transport(incoming)
            return

        # 3. Text (or unsupported media that needs the specific rejection in
        # `_on_text`). Caption-less stickers / muted videos / locations have
        # `text==""` but `has_unsupported_media=True` — without the second
        # clause they'd fall through to the generic branch 4 reply.
        if incoming.text.strip() or incoming.has_unsupported_media:
            if self.group_mode:
                handled = await self._handle_group_text(incoming)
                if handled:
                    return
                # Bot-to-bot path bypasses auth; submit directly.
                if incoming.is_relayed_bot_to_bot or incoming.sender.is_bot:
                    await self._submit_group_message_to_claude(incoming)
                    return
                # Human message in group — fall through to full auth/rate-limit/
                # pending-skill/pending-persona flow below.
            await self._on_text(incoming)
            return

        # 4. Nothing actionable — unsupported.
        if not self._auth_identity(incoming.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(
            incoming.chat,
            "This message type is not supported. Please send text, a voice message, or a file.",
        )

    async def _handle_group_text(self, incoming) -> bool:
        """Route a group-mode text message via the Transport-native path.

        Returns True if handled (caller should return immediately).
        Returns False if the message should proceed to further processing
        (normal user flow OR bot-to-bot direct-to-Claude).
        """
        from .group_filters import is_from_self, is_directed_at_me, is_from_other_bot

        # Auto-capture: if no room is bound and sender is trusted, write it.
        if self._room is None:
            if self._auth_identity(incoming.sender) and self.team_name:
                self._capture_room(incoming.chat)
                # Fall through so this message still gets processed.
        elif self._is_wrong_room(incoming.chat):
            return True  # wrong group — silent ignore

        if is_from_self(incoming, self.bot_username):
            return True  # self-silence

        if not is_directed_at_me(incoming, self.bot_username):
            return True  # not addressed to this bot

        if incoming.is_relayed_bot_to_bot or is_from_other_bot(incoming, self.bot_username):
            # Bot-to-bot path — via relay (is_relayed_bot_to_bot) or native (non-Telegram transports).
            if self._group_state.get(incoming.chat).halted:
                return True
            self._group_state.note_bot_to_bot(incoming.chat)
            if self._group_state.get(incoming.chat).halted:
                assert self._transport is not None
                await self._transport.send_text(
                    incoming.chat,
                    f"Auto-paused after {self._group_state.max_bot_rounds} bot-to-bot rounds. "
                    "Send any message to resume.",
                )
                return True
            # Caller submits to Claude on False return via _submit_group_message_to_claude.
            return False

        # Human (trusted user) message — reset the round counter and clear any halt.
        self._group_state.resume(incoming.chat)
        return False

    async def _submit_group_message_to_claude(self, incoming) -> None:
        """Bypass auth + rate-limit and submit a bot-to-bot message to Claude.

        Called when _handle_group_text returned False AND the sender is a bot/relay
        (so the message has already been validated as peer-bot-to-this-bot and
        the round counter has been incremented).

        Human messages in groups go through the full auth/rate-limit path via
        the legacy _on_text shim — not through this method.
        """
        assert self._transport is not None
        async with self._backend_lock():
            prompt = await self._build_user_prompt(incoming.chat, incoming.text)
            await self._log_user_turn(incoming.chat, incoming.text)
            self.task_manager.submit_agent(
                chat=incoming.chat,
                message=incoming.message,
                prompt=prompt,
                recent_discussion=self._resolve_recent_discussion(incoming),
            )

    def _team_session_active(self) -> bool:
        """True when team mode + the active backend has a resumable session.

        While true, the agent's own session memory carries the persona and
        prior turns; re-prepending them to the user message every turn causes
        the agent to re-execute the persona's procedural directives from
        scratch (this is what produced the codex manager loop on 2026-04-27).
        Outside team mode, this gate is off and existing solo behavior holds.
        """
        if not self.group_mode:
            return False
        backend = self.task_manager.backend
        capabilities = getattr(backend, "capabilities", None)
        return bool(
            getattr(backend, "session_id", None)
            and getattr(capabilities, "supports_resume", False)
        )

    async def _build_user_prompt(
        self,
        chat: ChatRef,
        raw_text: str,
        *,
        reply_to_text: str | None = None,
    ) -> str:
        """Compose the prompt sent to the agent for a user/peer message.

        Always carries the actual user message and any "[Replying to: ...]"
        prefix. The persona block and recent-history block are prepended only
        when ``_team_session_active()`` is False — i.e. on a fresh team
        session, or in solo mode. See ``_team_session_active`` for rationale.
        """
        prompt = raw_text
        if reply_to_text:
            prompt = f"[Replying to: {reply_to_text}]\n\n{prompt}"
        if self._team_session_active():
            return prompt
        if self._active_persona:
            from .skills import load_persona, format_persona_prompt
            persona = load_persona(self._active_persona, self.path)
            if persona:
                prompt = format_persona_prompt(persona, prompt)
        prompt = await self._history_block(chat) + prompt
        prompt = self._plugin_context_prepend(prompt)
        return prompt

    async def _persist_auth_if_dirty(self) -> None:
        """Save config.json once if ``_auth_dirty`` was set by a first-contact lock.

        Called from message-handling tails (``_on_text``, ``_on_run``, etc.)
        after the message is processed. Cheap when nothing to do (single
        bool check).

        Three correctness properties:

        1. **Atomic read-modify-write.** The load → merge → save sequence
           holds the existing ``_config_lock`` (fcntl.flock POSIX,
           msvcrt.locking Windows) for the WHOLE duration, not just the
           write phase. Without this, two concurrent first-contacts would
           each load the pre-write state, each merge in their own change,
           each save — last writer wins and silently drops one lock. The
           lock is provided by ``locked_config_rmw(path)`` in ``config.py``.
        2. **Scope-aware.** Writes back to whichever scope the bot's
           ``_allowed_users`` came from. When the bot inherited the global
           allow-list via ``resolve_project_allowed_users`` fallback,
           ``self._auth_source == "global"`` and we update
           ``Config.allowed_users`` on disk — NOT the project's empty list
           (which would silently promote the global list to a project-
           scoped copy).
        3. **Per-user merge.** Find the AllowedUser by username, union its
           ``locked_identities`` with our in-memory copy, write. Does NOT
           replace the full list. Concurrent edits to OTHER users on disk
           are preserved.
        """
        if not self._auth_dirty:
            return
        from .config import locked_config_rmw, save_config_within_lock
        cfg_path = self._effective_config_path()
        try:
            with locked_config_rmw(cfg_path) as disk:
                # disk is a freshly-loaded Config with the file lock held.
                if self._auth_source == "project":
                    if self.name not in disk.projects:
                        logger.warning(
                            "Auth persist skipped: project %r missing from disk", self.name,
                        )
                        return
                    target = disk.projects[self.name].allowed_users
                else:  # "global"
                    target = disk.allowed_users

                # Per-user merge: union our in-memory locks with disk's.
                in_memory_by_user = {u.username: u for u in self._allowed_users}
                for au in target:
                    mem = in_memory_by_user.get(au.username)
                    if mem is None:
                        continue
                    merged = list(au.locked_identities)
                    for ident in mem.locked_identities:
                        if ident not in merged:
                            merged.append(ident)
                    au.locked_identities = merged

                # Inside the context manager — save_config_within_lock writes
                # without re-locking (the context manager already holds the
                # lock).
                save_config_within_lock(disk, cfg_path)

            self._auth_dirty = False
        except Exception:
            logger.exception("Failed to persist auth state; will retry on next message")

    async def _guard_executor(self, ci_or_msg) -> bool:
        """Return True if the user may run state-changing actions.

        Replies 'Read-only access' on the active transport when blocked.
        ``ci_or_msg`` accepts either a ``CommandInvocation`` or an
        ``IncomingMessage`` — both expose ``.sender``, ``.chat``, and
        ``.message``.

        IMPORTANT: persists ``_auth_dirty`` on BOTH the success AND the
        viewer-denied path. ``_require_executor`` may have appended a
        first-contact identity to the user's ``locked_identities`` even
        when the role check ultimately fails (e.g., the user is a viewer
        logging in from a new transport — they get authed, locked, then
        denied for the state-changing action). Skipping the save on the
        deny path would lose that lock.
        """
        sender = getattr(ci_or_msg, "sender", None)
        if sender is None:
            return False
        allowed = self._require_executor(sender)
        # Persist first-contact locks regardless of allow/deny outcome.
        await self._persist_auth_if_dirty()
        if allowed:
            return True
        assert self._transport is not None
        await self._transport.send_text(
            ci_or_msg.chat,
            "Read-only access — your role is viewer.",
            reply_to=getattr(ci_or_msg, "message", None),
        )
        return False

    async def _on_text(self, incoming) -> None:
        """Handle a plain-text message: auth, rate-limit, pending skill/persona
        capture, waiting-input routing, supersede, then Claude submission.

        All routing keys are read from `IncomingMessage` directly — no
        telegram/ctx knowledge required.
        """
        assert self._transport is not None
        # Empty text is only actionable when the dispatcher routed an
        # unsupported-media message here for the specific rejection.
        if not incoming.text.strip() and not incoming.has_unsupported_media:
            return
        if not self._auth_identity(incoming.sender):
            await self._transport.send_text(
                incoming.chat, "Unauthorized.", reply_to=incoming.message,
            )
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            await self._transport.send_text(
                incoming.chat, "Rate limited. Try again shortly.", reply_to=incoming.message,
            )
            return

        consumed = await self._dispatch_plugin_on_message(incoming)
        if consumed:
            return

        # Role gate: only executors may submit prompts / answer questions /
        # trigger backend turns. Read-only viewers should not be able to push
        # the agent forward.
        if not await self._guard_executor(incoming):
            return

        if incoming.has_unsupported_media:
            await self._transport.send_text(
                incoming.chat,
                "Unsupported media type. I can read text, photos, documents, voice notes, and audio.",
                reply_to=incoming.message,
            )
            return

        # Pending skill capture (set by /create_skill + scope-button click).
        pending_skill = self._pending_skill_name
        pending_scope = self._pending_skill_scope
        if pending_skill and pending_scope:
            from .skills import save_skill
            save_skill(pending_skill, incoming.text, self.path, scope=pending_scope)
            self._pending_skill_name = None
            self._pending_skill_scope = None
            icon = "🌐" if pending_scope == "global" else "📁"
            await self._transport.send_text(
                incoming.chat,
                f"{icon} Skill '{pending_skill}' created ({pending_scope}). "
                f"Use /use {pending_skill} to activate.",
                reply_to=incoming.message,
            )
            return

        # Pending persona capture (same shape as pending skill).
        pending_persona = self._pending_persona_name
        pending_persona_scope = self._pending_persona_scope
        if pending_persona and pending_persona_scope:
            from .skills import save_persona
            save_persona(pending_persona, incoming.text, self.path, scope=pending_persona_scope)
            self._pending_persona_name = None
            self._pending_persona_scope = None
            icon = "🌐" if pending_persona_scope == "global" else "📁"
            await self._transport.send_text(
                incoming.chat,
                f"{icon} Persona '{pending_persona}' created ({pending_persona_scope}). "
                f"Use /persona {pending_persona} to activate.",
                reply_to=incoming.message,
            )
            return

        message_ref = incoming.message

        # Active Claude turn waiting on a question? Route as the answer.
        waiting = self.task_manager.waiting_input_task(incoming.chat)
        if waiting:
            self.task_manager.submit_answer(waiting.id, incoming.text)
            return

        # Supersede any in-flight task tied to this message (edit-message case).
        for prev in self.task_manager.find_by_message(message_ref):
            self.task_manager.cancel(prev.id)
            await self._cancel_live_for(prev.id, "(superseded)")
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        async with self._backend_lock():
            prompt = await self._build_user_prompt(
                incoming.chat,
                incoming.text,
                reply_to_text=incoming.reply_to_text,
            )
            # Log the user entry AFTER building the prompt so the current message
            # does not appear in its own prepend block (only relevant when the
            # gate is open and history actually gets injected).
            await self._log_user_turn(incoming.chat, incoming.text)
            self.task_manager.submit_agent(
                chat=incoming.chat,
                message=message_ref,
                prompt=prompt,
                recent_discussion=self._resolve_recent_discussion(incoming),
            )

    async def _on_run(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if not await self._guard_executor(ci):
            return
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /run <command>", reply_to=ci.message)
            return
        command = " ".join(ci.args)
        self.task_manager.run_command(
            chat=ci.chat,
            message=ci.message,
            command=command,
        )

    _TASK_ICONS = {
        TaskStatus.WAITING: "~",
        TaskStatus.RUNNING: ">",
        TaskStatus.DONE: "+",
        TaskStatus.FAILED: "!",
        TaskStatus.CANCELLED: "x",
    }

    def _tasks_buttons(self, chat: ChatRef) -> Buttons | None:
        all_tasks = self.task_manager.list_tasks(chat=chat, limit=100)
        active = [t for t in all_tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
        finished = [t for t in all_tasks if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)][:5]
        tasks = active + finished
        if not tasks:
            return None
        rows: list[list[Button]] = []
        for t in tasks:
            icon = self._TASK_ICONS.get(t.status, "?")
            elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:40]
            rows.append([Button(label=f"{icon} #{t.id}{elapsed} {label}", value=f"task_info_{t.id}")])
        return Buttons(rows=rows)

    async def _render_tasks(self, chat: ChatRef, msg_ref: MessageRef) -> None:
        """Render the tasks list into an existing message (edit)."""
        buttons = self._tasks_buttons(chat)
        assert self._transport is not None
        await self._transport.edit_text(
            msg_ref,
            "Tasks:" if buttons else "No tasks.",
            buttons=buttons,
        )

    async def _on_tasks(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        buttons = self._tasks_buttons(ci.chat)
        assert self._transport is not None
        await self._transport.send_text(
            ci.chat,
            "Tasks:" if buttons else "No tasks.",
            buttons=buttons,
        )

    def _model_buttons(self) -> Buttons:
        backend = self.task_manager.backend
        current = backend.model
        rows: list[list[Button]] = []
        for model_id, label, *_ in backend.MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([Button(label=f"{prefix}{label}", value=f"model_set_{model_id}")])
        return Buttons(rows=rows)

    def _current_model(self) -> str:
        """Return the human-readable label for the active model.

        Matches against both the user-facing slug (exact) AND the
        wire-identifier prefixes the backend echoes back after a turn
        (e.g. Claude reports `claude-opus-4-7[1m]` for the `opus[1m]`
        slug). Wire-prefix order in MODEL_OPTIONS must list more-specific
        variants first so we don't accidentally match `opus` for an
        `opus[1m]` wire id.
        """
        backend = self.task_manager.backend
        raw = backend.model or ""
        for entry in backend.MODEL_OPTIONS:
            model_id, label, desc = entry[0], entry[1], entry[2]
            wire_aliases = entry[3] if len(entry) > 3 else ()
            if raw == model_id or any(raw.startswith(a) for a in wire_aliases):
                return f"{label} — {desc}" if desc else label
        return backend.model_display or raw or "default"

    async def _on_model(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        backend = self.task_manager.backend
        models = tuple(backend.capabilities.models)
        if not models:
            await self._transport.send_text(ci.chat, "This backend doesn't support /model.")
            return
        args = ci.args or []
        if args:
            requested = args[0]
            if requested in models:
                backend.model = requested
                backend.model_display = None
                self._patch_backend_config({"model": requested})
                await self._transport.send_text(ci.chat, f"Model: {self._current_model()}")
                return
            await self._transport.send_text(ci.chat, f"Usage: /model {'|'.join(models)}")
            return
        await self._transport.send_text(
            ci.chat,
            f"Select model\nCurrent: {self._current_model()}",
            buttons=self._model_buttons(),
        )

    def _effort_buttons(self) -> Buttons:
        levels = self.task_manager.backend.capabilities.effort_levels
        return Buttons(rows=[
            [Button(label=e, value=f"effort_set_{e}")]
            for e in levels
        ])

    def _current_effort(self) -> str:
        return self.task_manager.backend.effort or "medium"

    async def _on_effort(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        backend = self.task_manager.backend
        levels = tuple(backend.capabilities.effort_levels)
        if not backend.capabilities.supports_effort:
            await self._transport.send_text(ci.chat, "This backend doesn't support /effort.")
            return
        args = ci.args or []
        if args:
            requested = args[0]
            if requested in levels:
                backend.effort = requested
                self._patch_backend_config({"effort": requested})
                await self._transport.send_text(ci.chat, f"Effort: {self._current_effort()}")
                return
            await self._transport.send_text(ci.chat, f"Usage: /effort {'|'.join(levels)}")
            return
        await self._transport.send_text(
            ci.chat,
            f"Current: {self._current_effort()}",
            buttons=self._effort_buttons(),
        )

    def _thinking_buttons(self) -> Buttons:
        return Buttons(rows=[[
            Button(label="On", value="thinking_set_on"),
            Button(label="Off", value="thinking_set_off"),
        ]])

    def _current_thinking(self) -> str:
        return "on" if self.show_thinking else "off"

    async def _on_thinking(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self.task_manager.backend.capabilities.supports_thinking:
            await self._transport.send_text(ci.chat, "This backend doesn't support /thinking.")
            return
        args = ci.args or []
        if args:
            arg = args[0].lower()
            if arg in ("on", "off"):
                self.show_thinking = arg == "on"
                self._patch_backend_config({"show_thinking": self.show_thinking})
                await self._transport.send_text(
                    ci.chat, f"Live thinking: {self._current_thinking()}"
                )
                return
            await self._transport.send_text(ci.chat, "Usage: /thinking on|off")
            return
        await self._transport.send_text(
            ci.chat,
            f"Live thinking: {self._current_thinking()}",
            buttons=self._thinking_buttons(),
        )

    _CONTEXT_LIMIT_MIN = 1
    _CONTEXT_LIMIT_MAX = 50

    def _context_status_text(self) -> str:
        if self.context_enabled:
            return f"Context history: ON ({self.context_history_limit} turns)"
        return "Context history: OFF"

    def _persist_context_settings(
        self, *, enabled: bool, limit: int,
    ) -> None:
        """Persist context_enabled / context_history_limit to disk.

        Bot-level fields — backend-agnostic — so we route through the same
        ``_patch_config`` helper that updates active_persona / show_thinking.

        Skips writing values that match the dataclass defaults so the
        on-disk JSON stays clean. ``None`` values are intentional remove-key
        signals; the team-bot save path honors them through ``_patch_config``.
        """
        fields: dict = {}
        if enabled is not True:
            fields["context_enabled"] = enabled
        else:
            fields["context_enabled"] = None  # signal to remove the key
        if limit != 10:
            fields["context_history_limit"] = limit
        else:
            fields["context_history_limit"] = None
        self._patch_config(fields)

    async def _on_context(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        args = ci.args or []
        if not args:
            # Read-only — viewers may inspect the current state.
            await self._transport.send_text(ci.chat, self._context_status_text())
            return
        # Toggling / setting limit is state-changing — gate to executor.
        if not await self._guard_executor(ci):
            return
        arg = args[0].lower()
        if arg in ("on", "off"):
            self.context_enabled = arg == "on"
            self._persist_context_settings(
                enabled=self.context_enabled,
                limit=self.context_history_limit,
            )
            await self._transport.send_text(ci.chat, self._context_status_text())
            return
        try:
            n = int(arg)
        except ValueError:
            await self._transport.send_text(
                ci.chat,
                f"Usage: /context [on|off|<N>] where N is "
                f"{self._CONTEXT_LIMIT_MIN}–{self._CONTEXT_LIMIT_MAX}",
            )
            return
        if not self._CONTEXT_LIMIT_MIN <= n <= self._CONTEXT_LIMIT_MAX:
            await self._transport.send_text(
                ci.chat,
                f"N must be between {self._CONTEXT_LIMIT_MIN} and {self._CONTEXT_LIMIT_MAX}.",
            )
            return
        self.context_history_limit = n
        self.context_enabled = True
        self._persist_context_settings(
            enabled=self.context_enabled,
            limit=self.context_history_limit,
        )
        await self._transport.send_text(ci.chat, self._context_status_text())

    _PERMISSION_OPTIONS = (
        *PERMISSION_MODES,
        "dangerously-skip-permissions",
    )

    def _permissions_buttons(self) -> Buttons:
        return Buttons(rows=[
            [Button(label=o, value=f"permissions_set_{o}")]
            for o in self._PERMISSION_OPTIONS
        ])

    def _current_permission(self) -> str:
        return self.task_manager.backend.current_permission()

    async def _on_permissions(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self.task_manager.backend.capabilities.supports_permissions:
            await self._transport.send_text(ci.chat, "This backend doesn't support /permissions.")
            return
        await self._transport.send_text(
            ci.chat,
            f"Current: {self._current_permission()}",
            buttons=self._permissions_buttons(),
        )

    async def _on_compact(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self.task_manager.backend.capabilities.supports_compact:
            await self._transport.send_text(ci.chat, "This backend doesn't support /compact.")
            return
        if not self.task_manager.backend.session_id:
            await self._transport.send_text(ci.chat, "No active session.")
            return
        self.task_manager.submit_compact(
            chat=ci.chat,
            message=ci.message,
        )

    def _backend_buttons(self) -> Buttons:
        """One row per registered backend; active marked with ●."""
        from .backends.factory import available

        current = self.task_manager.backend.name
        rows = []
        for name in available():
            prefix = "● " if name == current else ""
            rows.append([Button(label=f"{prefix}{name}", value=f"backend_set_{name}")])
        return Buttons(rows=rows)

    async def _switch_backend(self, requested: str) -> str:
        """Activate-first backend swap. Returns the user-visible reply text.

        Per Phase 2 spec §4.5: build new → close_interactive(old) → swap →
        persist on success. Disk follows runtime so a crash mid-swap leaves
        the bot recoverable on the previous backend.

        Returns one of: "<x> is already active.", an unknown-backend error,
        a live-task rejection, or "Switched to <x>." on success.
        """
        from .backends.factory import available, create

        async with self._backend_lock():
            available_backends = available()
            current_name = self.task_manager.backend.name

            if requested == current_name:
                return f"{requested} is already active."
            if requested not in available_backends:
                return (
                    f"Unknown backend '{requested}'. "
                    f"Available: {', '.join(available_backends)}"
                )
            if self.task_manager.has_live_agent_tasks():
                return "Cancel running tasks before switching backend."

            new_backend = create(requested, self.path, self._backend_state_for(requested))
            self.task_manager.backend.close_interactive()
            self.task_manager._backend = new_backend
            self._backend_name = requested
            self._backend_state.setdefault(requested, {})
            self._refresh_team_system_note()
            # Re-apply safety guardrail to the new backend — class default is
            # None (safety off), so without this the operator's configured
            # cfg.safety_prompt silently drops on every /backend swap.
            self._resolve_safety_prompt()

            cfg = self._effective_config_path()
            if self.team_name and self.role:
                patch_team_bot_backend(self.team_name, self.role, requested, cfg)
            else:
                patch_project(self.name, {"backend": requested}, cfg)

            return f"Switched to {requested}."

    async def _on_backend(self, ci) -> None:
        """Show or switch the active backend.

        - No args: render a button picker (one row per registered backend).
        - `<name>` typed: same four-form switch logic via `_switch_backend`,
          text reply.
        """
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None

        if not ci.args:
            await self._transport.send_text(
                ci.chat,
                f"Active backend: {self.task_manager.backend.name}",
                buttons=self._backend_buttons(),
            )
            return

        msg = await self._switch_backend(ci.args[0].lower())
        await self._transport.send_text(ci.chat, msg)

    async def _on_version_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        from . import __version__
        assert self._transport is not None
        await self._transport.send_text(ci.chat, f"link-project-to-chat v{__version__}")

    async def _on_help_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._transport.send_text(ci.chat, _CMD_HELP)

    async def _on_reset(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        buttons = Buttons(rows=[[
            Button(label="Yes, reset", value="reset_confirm"),
            Button(label="Cancel", value="reset_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat,
            f"Are you sure? This will clear the {self.task_manager.backend.name} session.",
            buttons=buttons,
        )

    def _picker_buttons(self, items: dict, prefix: str) -> Buttons | None:
        if not items:
            return None
        btns = [
            Button(label=name, value=f"{prefix}_{name}")
            for name in sorted(items)
        ]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        return Buttons(rows=rows)

    async def _on_skills(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not self._backend_supports_prompt_customization():
            await self._transport.send_text(
                ci.chat, "This backend doesn't support skills yet (personas still work)."
            )
            return
        if ci.args:
            # Activation branch is state-changing — gate to executor.
            if not await self._guard_executor(ci):
                return
            name = ci.args[0].lower()
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                await self._transport.send_text(ci.chat, f"Skill '{name}' not found.")
                return
            self._claude.append_system_prompt = skill.content
            self._active_skill = name
            await self._transport.send_text(
                ci.chat, f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate."
            )
            return
        from .skills import load_skills
        skills = load_skills(self.path)
        if not skills:
            await self._transport.send_text(
                ci.chat, "No skills available.\nCreate one with /create_skill <name>"
            )
            return
        lines = ["Available skills:"]
        for name, skill in sorted(skills.items()):
            icon = "\U0001f4c1" if skill.source == "project" else "\U0001f310" if skill.source == "global" else "\U0001f916"
            lines.append(f"  {icon} {name} ({skill.source})")
        if self._active_skill:
            lines.append(f"\nActive: {self._active_skill}")
        buttons = self._picker_buttons(skills, "pick_skill")
        await self._transport.send_text(ci.chat, "\n".join(lines), buttons=buttons)

    async def _on_stop_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self._backend_supports_prompt_customization():
            await self._transport.send_text(
                ci.chat, "This backend doesn't support skills yet (personas still work)."
            )
            return
        if not self._active_skill:
            await self._transport.send_text(ci.chat, "No active skill.")
            return
        old = self._active_skill
        self._active_skill = None
        self._claude.append_system_prompt = None
        await self._transport.send_text(ci.chat, f"Skill '{old}' deactivated.")

    async def _on_create_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /create_skill <name>")
            return
        name = ci.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "skill")
        except ValueError as e:
            await self._transport.send_text(ci.chat, str(e))
            return
        # Stash pending name so the scope-click handler knows which skill to create.
        self._pending_skill_name = name
        buttons = Buttons(rows=[[
            Button(label="📁 Project", value=f"skill_scope_project_{name}"),
            Button(label="🌐 Global", value=f"skill_scope_global_{name}"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Where should skill '{name}' be saved?", buttons=buttons,
        )

    async def _on_delete_skill(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /delete_skill <name>")
            return
        name = ci.args[0].lower()
        from .skills import load_skill
        skill = load_skill(name, self.path)
        if not skill:
            await self._transport.send_text(ci.chat, f"Skill '{name}' not found.")
            return
        icon = "🌐" if skill.source == "global" else "📁"
        buttons = Buttons(rows=[[
            Button(label="Delete", value=f"skill_delete_confirm_{skill.source}_{name}"),
            Button(label="Cancel", value="skill_delete_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Delete {icon} {skill.source} skill '{name}'?", buttons=buttons,
        )

    # --- Persona handlers ---

    def _resolve_safety_prompt(self) -> None:
        """Resolve cfg.safety_prompt → backend.safety_system_prompt.

        None  → DEFAULT_SAFETY_SYSTEM_PROMPT (safety on, default text)
        ""    → "" (safety off — explicit operator opt-out)
        "..." → "..." (safety on, custom text)

        Called once from __init__ after the backend is constructed. Each
        backend renders the resolved field in its native style (Claude:
        --append-system-prompt parts list; Codex: <system-reminder> block).
        """
        from .backends.base import DEFAULT_SAFETY_SYSTEM_PROMPT
        project_cfg = self._config.projects.get(self.name) if self._config else None
        if project_cfg is None or project_cfg.safety_prompt is None:
            self.task_manager.backend.safety_system_prompt = DEFAULT_SAFETY_SYSTEM_PROMPT
        else:
            self.task_manager.backend.safety_system_prompt = project_cfg.safety_prompt

    def _refresh_team_system_note(self) -> None:
        """(Re)build the team system note that pins own + peer @handle.

        Called from __init__ (self handle still unknown — note carries peer only)
        and from _after_ready after get_me() fills self.bot_username (note now
        carries both). Without the self handle agents tend to invent one from
        the persona name — e.g. the pre-suffix-bump ``@..._dev_claude_bot``
        when the real handle is ``@..._dev_2_claude_bot``.
        """
        backend = self.task_manager.backend
        if not self.peer_bot_username:
            backend.team_system_note = None
            backend.team_authority = None
            return
        if self._team_authority is None:
            self._team_authority = TeamAuthority(team_name=self.team_name or self.name)
        backend.team_authority = self._team_authority
        peer_role = "developer" if self.role == "manager" else "manager"
        self_line = (
            f"Your own Telegram @handle in this group is @{self.bot_username}. "
            if self.bot_username
            else ""
        )
        backend.team_system_note = (
            f"You are the '{self.role}' role bot in a dual-agent team group. "
            f"{self_line}"
            f"Your team peer (role: {peer_role}) in this group is @{self.peer_bot_username}. "
            f"Use the exact @handles pinned above — never placeholders like "
            f"'@developer'/'@manager' and never a different suffix. "
            f"\n\nRecipient routing: the group has two audiences — the human user and "
            f"your peer — and you choose who hears each reply:\n"
            f"- To hand work to your peer, ask them a substantive question, or deliver "
            f"actionable feedback: BEGIN the reply with @{self.peer_bot_username} on "
            f"its own line, followed by a blank line, then the body. The group relay "
            f"forwards any reply that starts with this @mention to your peer.\n"
            f"- To respond to the user: reply normally WITHOUT @{self.peer_bot_username}. "
            f"The relay does not forward those, so they go only to the human.\n"
            f"- For pure acknowledgments ('ok', 'agreed', 'noted', 'standing by', '👍'): "
            f"do not reply at all. Silence is the correct answer when there is nothing "
            f"actionable to add — echoing an acknowledgment back to your peer just "
            f"creates a ping-pong loop. The relay drops ack-only messages as a safety "
            f"net, but you should not produce them in the first place."
        )

    def _backfill_own_bot_username(self, config_path: Path | None = None) -> None:
        """One-time migration: write self.bot_username into TeamConfig if empty.

        For teams created before bot_username was stored at /create_team time,
        this lets the next team-bot startup discover each bot's @handle and
        populate it in config.json so the peer role can read it.
        """
        cfg = config_path or self._effective_config_path()
        teams = load_teams(cfg)
        team = teams.get(self.team_name or "")
        if team is None:
            return
        bot = team.bots.get(self.role or "")
        if bot is None or bot.bot_username == self.bot_username:
            return
        self._patch_config({"bot_username": self.bot_username}, cfg)
        logger.info(
            "Backfilled bot_username=%s for team=%s role=%s in config",
            self.bot_username, self.team_name, self.role,
        )

    def _patch_backend_config(self, fields: dict) -> None:
        """Persist backend-state fields for this bot's active backend.

        Routes to ``patch_team_bot_backend_state`` for team bots or
        ``patch_backend_state`` for solo bots. Backend-state writes are
        scoped per-backend, so switching backends doesn't clobber a sibling
        backend's saved state.
        """
        cfg = self._effective_config_path()
        backend_name = self.task_manager.backend.name
        if self.team_name and self.role:
            patch_team_bot_backend_state(
                self.team_name, self.role, backend_name, fields, cfg,
            )
        else:
            patch_backend_state(self.name, backend_name, fields, cfg)

    def _backend_state_for(self, backend_name: str) -> dict:
        """Return a copy of the persisted backend_state[<backend_name>] for this bot."""
        return dict(self._backend_state.get(backend_name, {}))

    def _patch_config(self, fields: dict, config_path: Path | None = None) -> None:
        """Persist config fields for this bot, routing to team or project config.

        Team bots live under ``config.teams[team].bots[role]``; solo bots under
        ``config.projects[name]``. ``patch_project`` on a team bot would create
        a stray projects entry and never touch the real team config, so the
        team path re-serialises the full ``bots`` dict via ``patch_team``.
        """
        cfg = config_path or self._effective_config_path()
        if self.team_name:
            teams = load_teams(cfg)
            team = teams.get(self.team_name)
            if team is None:
                logger.warning("Team %r not in config; change %r not persisted.", self.team_name, fields)
                return
            bots_dict: dict[str, dict] = {}
            for role, bot in team.bots.items():
                entry: dict = {
                    "telegram_bot_token": bot.telegram_bot_token,
                    "backend": bot.backend,
                    "backend_state": dict(bot.backend_state),
                }
                if bot.active_persona is not None:
                    entry["active_persona"] = bot.active_persona
                if bot.autostart:
                    entry["autostart"] = True
                if bot.permissions is not None:
                    entry["permissions"] = bot.permissions
                if bot.bot_username:
                    entry["bot_username"] = bot.bot_username
                if bot.session_id is not None:
                    entry["session_id"] = bot.session_id
                if bot.model is not None:
                    entry["model"] = bot.model
                if bot.effort is not None:
                    entry["effort"] = bot.effort
                if bot.show_thinking:
                    entry["show_thinking"] = True
                if not bot.context_enabled:
                    entry["context_enabled"] = False
                if bot.context_history_limit != 10:
                    entry["context_history_limit"] = bot.context_history_limit

                if role == self.role:
                    for k, v in fields.items():
                        if v is None:
                            entry.pop(k, None)
                        else:
                            entry[k] = v
                bots_dict[role] = entry
            patch_team(self.team_name, {"bots": bots_dict}, cfg)
        else:
            patch_project(self.name, fields, cfg)

    def _persist_active_persona(self, name: str | None, config_path: Path | None = None) -> None:
        """Persist this bot's active_persona."""
        self._patch_config({"active_persona": name}, config_path)

    def _on_persona_change(self) -> None:
        """Clear the backend session so the new persona takes effect next turn.

        In team mode, ``_team_session_active`` gates persona/history injection
        on the backend NOT having a resumable session. Without this clearing
        step, a ``/persona`` swap mid-conversation would be invisible to the
        agent — it would keep acting under the persona that was injected on
        the original first turn until ``/reset``. Forcing session_id=None makes
        the next turn a fresh first-turn, where the new persona is injected
        and lands in the new session's memory.

        Solo mode skips this: the per-turn injection gate is off there, the
        new persona shows up on the very next turn anyway, and disturbing
        session continuity would needlessly drop the agent's working context.
        """
        if not self.group_mode:
            return
        backend = self.task_manager.backend
        if getattr(backend, "session_id", None) is None:
            return
        backend.session_id = None
        logger.info(
            "Cleared backend session_id on persona change (team=%s role=%s) "
            "so the new persona injects on the next turn",
            self.team_name, self.role,
        )

    async def _on_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        if not ci.args:
            if self._active_persona:
                await self._transport.send_text(
                    ci.chat,
                    f"Active persona: {self._active_persona}\nUse /stop_persona to deactivate.",
                )
                return
            from .skills import load_personas
            buttons = self._picker_buttons(load_personas(self.path), "pick_persona")
            if not buttons:
                await self._transport.send_text(
                    ci.chat, "No personas available.\nCreate one with /create_persona <name>"
                )
                return
            await self._transport.send_text(ci.chat, "Pick a persona to activate:", buttons=buttons)
            return
        # Activation branch is state-changing — gate to executor.
        if not await self._guard_executor(ci):
            return
        name = ci.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            await self._transport.send_text(ci.chat, f"Persona '{name}' not found.")
            return
        self._active_persona = name
        self._on_persona_change()
        self._persist_active_persona(name)
        await self._transport.send_text(
            ci.chat, f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate."
        )

    async def _on_stop_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self._active_persona:
            await self._transport.send_text(ci.chat, "No active persona.")
            return
        old = self._active_persona
        self._active_persona = None
        self._on_persona_change()
        self._persist_active_persona(None)
        await self._transport.send_text(ci.chat, f"Persona '{old}' deactivated.")

    async def _on_halt(self, ci) -> None:
        assert self._transport is not None
        if not self.group_mode:
            await self._transport.send_text(
                ci.chat, "/halt is only available in group mode.", reply_to=ci.message,
            )
            return
        if self._is_wrong_room(ci.chat):
            return  # silently ignore — wrong group
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if not await self._guard_executor(ci):
            return
        self._group_state.halt(ci.chat)
        await self._transport.send_text(
            ci.chat, "Halted. Use /resume to continue.", reply_to=ci.message,
        )

    async def _on_resume(self, ci) -> None:
        assert self._transport is not None
        if not self.group_mode:
            await self._transport.send_text(
                ci.chat, "/resume is only available in group mode.", reply_to=ci.message,
            )
            return
        if self._is_wrong_room(ci.chat):
            return  # silently ignore — wrong group
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if not await self._guard_executor(ci):
            return
        self._group_state.resume(ci.chat)
        await self._transport.send_text(ci.chat, "Resumed.", reply_to=ci.message)

    async def _on_create_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /create_persona <name>")
            return
        name = ci.args[0].lower()
        from .skills import _sanitize_name
        try:
            name = _sanitize_name(name, "persona")
        except ValueError as e:
            await self._transport.send_text(ci.chat, str(e))
            return
        self._pending_persona_name = name
        buttons = Buttons(rows=[[
            Button(label="📁 Project", value=f"persona_scope_project_{name}"),
            Button(label="🌐 Global", value=f"persona_scope_global_{name}"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Where should persona '{name}' be saved?", buttons=buttons,
        )

    async def _on_delete_persona(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not ci.args:
            await self._transport.send_text(ci.chat, "Usage: /delete_persona <name>")
            return
        name = ci.args[0].lower()
        from .skills import load_persona
        persona = load_persona(name, self.path)
        if not persona:
            await self._transport.send_text(ci.chat, f"Persona '{name}' not found.")
            return
        icon = "🌐" if persona.source == "global" else "📁"
        buttons = Buttons(rows=[[
            Button(label="Delete", value=f"persona_delete_confirm_{persona.source}_{name}"),
            Button(label="Cancel", value="persona_delete_cancel"),
        ]])
        await self._transport.send_text(
            ci.chat, f"Delete {icon} {persona.source} persona '{name}'?", buttons=buttons,
        )

    async def _on_voice_status(self, ci) -> None:
        assert self._transport is not None
        if not self._auth_identity(ci.sender):
            await self._transport.send_text(ci.chat, "Unauthorized.", reply_to=ci.message)
            return
        if self._transcriber:
            backend = type(self._transcriber).__name__
            await self._transport.send_text(
                ci.chat, f"Voice: enabled ({backend})", reply_to=ci.message,
            )
        else:
            await self._transport.send_text(
                ci.chat,
                "Voice: disabled\nConfigure with: link-project-to-chat setup",
                reply_to=ci.message,
            )

    LANGUAGES = [
        ("auto", "Auto-detect"),
        ("en", "English"),
        ("ka", "Georgian"),
        ("ru", "Russian"),
        ("de", "German"),
        ("fr", "French"),
        ("es", "Spanish"),
        ("tr", "Turkish"),
        ("uk", "Ukrainian"),
        ("ja", "Japanese"),
        ("zh", "Chinese"),
    ]

    async def _on_lang(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        if not await self._guard_executor(ci):
            return
        assert self._transport is not None
        if not self._transcriber:
            await self._transport.send_text(ci.chat, "Voice is not configured.")
            return
        if ci.args:
            code = ci.args[0].lower()
            if code == "auto":
                code = ""
            self._transcriber._language = code
            label = code or "auto-detect"
            await self._transport.send_text(ci.chat, f"Voice language set to: {label}")
            return
        current = getattr(self._transcriber, "_language", "") or "auto-detect"
        btns = [
            Button(
                label=f"{'● ' if (code or '') == getattr(self._transcriber, '_language', '') else ''}{label}",
                value=f"lang_set_{code}",
            )
            for code, label in self.LANGUAGES
        ]
        rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
        await self._transport.send_text(
            ci.chat,
            f"Current language: {current}\nSelect voice language:",
            buttons=Buttons(rows=rows),
        )

    # State-changing button prefixes / exact values — used by the role gate
    # in _on_button. A button click matching any of these requires executor
    # role. Plugin-registered buttons go through _dispatch_plugin_button BEFORE
    # this check, so they are responsible for their own gating per the spec.
    _STATE_CHANGING_BUTTON_PREFIXES = (
        "model_set_",
        "effort_set_",
        "thinking_set_",
        "permissions_set_",
        "backend_set_",
        "task_cancel_",
        "lang_set_",
        "ask_",
        "skill_scope_",
        "pick_skill_",
        "skill_delete_confirm_",
        "persona_scope_",
        "pick_persona_",
        "persona_delete_confirm_",
    )
    _STATE_CHANGING_BUTTON_EXACT = frozenset({
        "reset_confirm",
        "reset_cancel",
    })

    def _is_state_changing_button(self, value: str) -> bool:
        if value in self._STATE_CHANGING_BUTTON_EXACT:
            return True
        return any(value.startswith(p) for p in self._STATE_CHANGING_BUTTON_PREFIXES)

    async def _on_button(self, click) -> None:
        """Transport-native button-click handler. Replaces legacy _on_callback."""
        if not self._auth_identity(click.sender):
            return
        assert self._transport is not None
        value = click.value
        msg_ref = click.message
        chat = click.chat
        if await self._dispatch_plugin_button(click):
            return

        # Role gate for state-changing buttons. Read-only branches (task_info_*,
        # task_log_*, tasks_back, show_thinking_*, skill_delete_cancel,
        # persona_delete_cancel) fall through.
        if self._is_state_changing_button(value):
            if not await self._guard_executor(click):
                return

        if value.startswith("model_set_"):
            name = value[len("model_set_"):]
            valid = {m[0] for m in self.task_manager.backend.MODEL_OPTIONS}
            if name in valid:
                self.task_manager.backend.model = name
                self.task_manager.backend.model_display = None
                self._patch_backend_config({"model": name})
            await self._transport.edit_text(
                msg_ref,
                f"Select model\nCurrent: {self._current_model()}",
                buttons=self._model_buttons(),
            )
        elif value.startswith("backend_set_"):
            requested = value[len("backend_set_"):]
            reply = await self._switch_backend(requested)
            await self._transport.edit_text(
                msg_ref,
                f"{reply}\nActive backend: {self.task_manager.backend.name}",
                buttons=self._backend_buttons(),
            )
        elif value.startswith("effort_set_"):
            backend = self.task_manager.backend
            if not backend.capabilities.supports_effort:
                await self._transport.edit_text(msg_ref, "This backend doesn't support /effort.")
                return
            level = value[len("effort_set_"):]
            if level in backend.capabilities.effort_levels:
                backend.effort = level
                self._patch_backend_config({"effort": level})
            await self._transport.edit_text(
                msg_ref,
                f"Effort: {self._current_effort()}",
                buttons=self._effort_buttons(),
            )
        elif value.startswith("thinking_set_"):
            if not self.task_manager.backend.capabilities.supports_thinking:
                await self._transport.edit_text(msg_ref, "This backend doesn't support /thinking.")
                return
            val = value[len("thinking_set_"):]
            if val in ("on", "off"):
                self.show_thinking = val == "on"
                self._patch_backend_config({"show_thinking": self.show_thinking})
            await self._transport.edit_text(
                msg_ref,
                f"Live thinking: {self._current_thinking()}",
                buttons=self._thinking_buttons(),
            )
        elif value.startswith("permissions_set_"):
            if not self.task_manager.backend.capabilities.supports_permissions:
                await self._transport.edit_text(msg_ref, "This backend doesn't support /permissions.")
                return
            mode = value[len("permissions_set_"):]
            if mode == "dangerously-skip-permissions" or mode in PERMISSION_MODES:
                self.task_manager.backend.set_permission(mode)
                self._patch_backend_config({"permissions": mode if mode != "default" else None})
            await self._transport.edit_text(
                msg_ref,
                f"Permissions: {self._current_permission()}",
                buttons=self._permissions_buttons(),
            )
        elif value.startswith("lang_set_"):
            code = value[len("lang_set_"):]
            if code == "auto":
                code = ""
            if self._transcriber:
                self._transcriber._language = code
            label = code or "auto-detect"
            btns = [
                Button(
                    label=f"{'● ' if (c or '') == code else ''}{l}",
                    value=f"lang_set_{c}",
                )
                for c, l in self.LANGUAGES
            ]
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
            await self._transport.edit_text(
                msg_ref,
                f"Voice language set to: {label}",
                buttons=Buttons(rows=rows),
            )
        elif value == "reset_confirm":
            live_task_ids = list({*self._live_text.keys(), *self._live_thinking.keys()})
            for tid in live_task_ids:
                await self._cancel_live_for(tid)
            self.task_manager.cancel_all()
            self.task_manager.backend.session_id = None
            self._active_skill = None
            self._active_persona = None
            if self._backend_supports_prompt_customization():
                self._claude.append_system_prompt = None
            clear_session(
                self.name,
                self._effective_config_path(),
                team_name=self.team_name,
                role=self.role,
            )
            # Drop the per-chat conversation log so the user gets a true fresh
            # start. Leaving prior turns would defeat the purpose of /reset
            # since the next prompt would still inject them.
            await self.conversation_log.clear_async(chat)
            await self._transport.edit_text(msg_ref, "Session reset.")
        elif value == "reset_cancel":
            await self._transport.edit_text(msg_ref, "Reset cancelled.")
        # Skill callbacks
        elif value.startswith("skill_delete_confirm_"):
            rest = value[len("skill_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_skill as _delete_skill
            _delete_skill(name, self.path, scope=scope)
            if self._active_skill == name:
                self._active_skill = None
                if self._backend_supports_prompt_customization():
                    self._claude.append_system_prompt = None
            await self._transport.edit_text(msg_ref, f"Skill '{name}' deleted.")
        elif value == "skill_delete_cancel":
            await self._transport.edit_text(msg_ref, "Cancelled.")
        elif value.startswith("skill_scope_"):
            rest = value[len("skill_scope_"):]
            scope, _, name = rest.partition("_")
            self._pending_skill_name = name
            self._pending_skill_scope = scope
            await self._transport.edit_text(msg_ref, f"Send the content for skill '{name}':")
        elif value.startswith("pick_skill_"):
            name = value[len("pick_skill_"):]
            if not self._backend_supports_prompt_customization():
                await self._transport.edit_text(
                    msg_ref, "This backend doesn't support skills yet (personas still work)."
                )
                return
            from .skills import load_skill
            skill = load_skill(name, self.path)
            if not skill:
                await self._transport.edit_text(msg_ref, f"Skill '{name}' not found.")
                return
            self._claude.append_system_prompt = skill.content
            self._active_skill = name
            await self._transport.edit_text(
                msg_ref, f"🧠 Skill '{name}' activated.\nUse /stop_skill to deactivate."
            )
        # Persona callbacks
        elif value.startswith("persona_delete_confirm_"):
            rest = value[len("persona_delete_confirm_"):]
            scope, _, name = rest.partition("_")
            from .skills import delete_persona as _delete_persona
            _delete_persona(name, self.path, scope=scope)
            if self._active_persona == name:
                self._active_persona = None
                self._on_persona_change()
            await self._transport.edit_text(msg_ref, f"Persona '{name}' deleted.")
        elif value == "persona_delete_cancel":
            await self._transport.edit_text(msg_ref, "Cancelled.")
        elif value.startswith("persona_scope_"):
            rest = value[len("persona_scope_"):]
            scope, _, name = rest.partition("_")
            self._pending_persona_name = name
            self._pending_persona_scope = scope
            await self._transport.edit_text(msg_ref, f"Send the content for persona '{name}':")
        elif value.startswith("pick_persona_"):
            name = value[len("pick_persona_"):]
            from .skills import load_persona
            persona = load_persona(name, self.path)
            if not persona:
                await self._transport.edit_text(msg_ref, f"Persona '{name}' not found.")
                return
            self._active_persona = name
            self._on_persona_change()
            self._persist_active_persona(name)
            await self._transport.edit_text(
                msg_ref, f"💬 Persona '{name}' activated.\nUse /stop_persona to deactivate."
            )
        elif value.startswith("ask_"):
            parts = value.split("_")
            if len(parts) != 4:
                return
            try:
                task_id = int(parts[1])
                q_idx = int(parts[2])
                opt_idx = int(parts[3])
            except ValueError:
                return
            task = self.task_manager.get(task_id)
            if not task or task.status != TaskStatus.WAITING_INPUT:
                # Drop the buttons by editing the text without buttons.
                return
            if q_idx >= len(task.pending_questions):
                return
            question = task.pending_questions[q_idx]
            if opt_idx >= len(question.options):
                return
            label = question.options[opt_idx].label
            if self.task_manager.submit_answer(task_id, label):
                # Annotate the selection into the existing message body.
                try:
                    original_html = self._render_question_html(question)
                    escaped = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    await self._transport.edit_text(
                        msg_ref,
                        f"{original_html}\n\n<i>Selected:</i> {escaped}",
                        html=True,
                    )
                except Exception:
                    logger.debug("could not annotate selected option", exc_info=True)
        elif value.startswith("task_info_"):
            task_id = _parse_task_id(value)
            task = self.task_manager.get(task_id)
            if not task:
                await self._transport.edit_text(msg_ref, f"Task #{task_id} not found.")
                return
            elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
            text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:200]}"
            rows: list[list[Button]] = []
            if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                rows.append([Button(label="Cancel", value=f"task_cancel_{task_id}")])
            if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                rows.append([Button(label="Log", value=f"task_log_{task_id}")])
            if task_id in self._thinking_store:
                rows.append([Button(label="Thinking", value=f"show_thinking_{task_id}")])
            rows.append([Button(label="« Back", value="tasks_back")])
            await self._transport.edit_text(msg_ref, text, buttons=Buttons(rows=rows))
        elif value == "tasks_back":
            await self._render_tasks(chat, msg_ref)
        elif value.startswith("task_cancel_"):
            task_id = _parse_task_id(value)
            await self._cancel_live_for(task_id)
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await self._transport.edit_text(msg_ref, f"#{task_id} cancelled.")
            else:
                await self._transport.edit_text(
                    msg_ref, f"#{task_id} not found or already finished."
                )
        elif value.startswith("task_log_"):
            task_id = _parse_task_id(value)
            task = self.task_manager.get(task_id)
            if not task:
                await self._transport.edit_text(msg_ref, f"Task #{task_id} not found.")
                return
            if task._log:
                output = "\n".join(task._log)
            else:
                output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            rows = [[Button(label="« Back", value=f"task_info_{task_id}")]]
            await self._transport.edit_text(
                msg_ref, f"#{task_id} log:\n{output}", buttons=Buttons(rows=rows),
            )
        elif value.startswith("show_thinking_"):
            try:
                task_id = int(value[len("show_thinking_"):])
            except ValueError:
                return
            thinking = self._thinking_store.get(task_id)
            if not thinking:
                return
            await self._send_to_chat(chat, f"💭 {thinking}")

    def _compose_status(self) -> str:
        uptime = time.monotonic() - self._started_at
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)

        backend = self.task_manager.backend
        st = backend.status
        caps = backend.capabilities
        # Reuse _current_model so the friendly-label / wire-alias lookup
        # matches what /model shows. _current_model falls back to
        # model_display / raw / 'default' when no MODEL_OPTIONS row hits.
        lines = [
            f"Project: {self.name}",
            f"Path: {self.path}",
            f"Backend: {backend.name}",
            f"Model: {self._current_model()}",
        ]
        # Effort is capability-driven (Phase 4 slice 1). Skip line when the
        # backend doesn't support it (e.g. FakeBackend in tests).
        if caps.supports_effort:
            lines.append(f"Effort: {backend.effort or 'medium'}")
        if caps.supports_permissions:
            lines.append(f"Permissions: {st.get('permission') or backend.current_permission()}")
        if caps.supports_allowed_tools:
            allowed_tools = st.get("allowed_tools") or []
            disallowed_tools = st.get("disallowed_tools") or []
            lines.append(f"Allowed tools: {', '.join(allowed_tools) if allowed_tools else 'none'}")
            lines.append(
                f"Disallowed tools: {', '.join(disallowed_tools) if disallowed_tools else 'none'}"
            )
        lines.extend([
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st.get('session_id') or 'none'}",
            f"Agent: {'RUNNING' if st.get('running') else 'idle'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
        ])
        # Per-backend usage stats. Each backend's `.status` exposes a slightly
        # different dict — surface only the keys that are actually present and
        # non-trivial. Phase 4 slice 2: makes /status useful for both Claude
        # (last_duration, total_requests) and Codex (last_usage tokens).
        if st.get("total_requests", 0) > 0:
            lines.append(f"Requests: {st['total_requests']}")
        last_duration = st.get("last_duration")
        if last_duration is not None:
            lines.append(f"Last duration: {last_duration}s")
        last_usage = st.get("last_usage")
        if last_usage:
            in_t = last_usage.get("input_tokens", 0)
            out_t = last_usage.get("output_tokens", 0)
            lines.append(f"Last tokens: {in_t} in / {out_t} out")
        if caps.supports_usage_cap_detection:
            lines.append(f"Usage capped: {'yes' if st.get('usage_capped') else 'no'}")
        last_error = st.get("last_error")
        if last_error:
            lines.append(f"Last error: {self._short_status_value(str(last_error))}")
        authority = getattr(backend, "team_authority", None)
        if authority is not None:
            consecutive = getattr(self._transport, "team_relay_consecutive_bot_turns", 0)
            max_turns = getattr(self._transport, "team_relay_max_autonomous_turns", 5)
            lines.append("")
            lines.append(_render_team_safety_block(authority, consecutive, max_turns))
        lines.extend([
            f"Skill: {self._active_skill or 'none'}",
            f"Persona: {self._active_persona or 'none'}",
        ])
        return "\n".join(lines)

    @staticmethod
    def _short_status_value(value: str, limit: int = 240) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3] + "..."

    async def _on_status_t(self, ci) -> None:
        if not self._auth_identity(ci.sender):
            return
        assert self._transport is not None
        await self._send_to_chat(ci.chat, self._compose_status())

    async def _on_file_from_transport(self, incoming) -> None:
        """Transport-native file handler. Copies each incoming file from the
        transport's temp dir into the platform temp root under
        link-project-to-chat/<project>/uploads/ and submits it to Claude (or
        the waiting-input task)."""
        import shutil

        if not self._auth_identity(incoming.sender):
            return
        if not await self._guard_executor(incoming):
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            assert self._transport is not None
            await self._transport.send_text(incoming.chat, "Rate limited. Try again shortly.")
            return
        if not incoming.files:
            return
        # Record the caption (if any) into per-room history. No-op for DMs.
        self._chat_history.record(
            chat=incoming.chat,
            msg_id=incoming.message.native_id,
            sender=(
                incoming.sender.handle
                or incoming.sender.display_name
                or "unknown"
            ),
            text=incoming.text,
            is_own_bot=False,
        )

        uploads_dir = (
            Path(tempfile.gettempdir())
            / "link-project-to-chat"
            / self.name
            / "uploads"
        )
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Single-file behavior identical to legacy _on_file.
        f = incoming.files[0]
        # Sanitize the original name.
        raw_name = f.original_name or f"file_{int(time.monotonic() * 1000)}"
        filename = "".join(
            c for c in raw_name.replace("/", "_").replace("\\", "_")
            if c.isalnum() or c in "._- "
        )[:200] or "file"

        dest = uploads_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 2
            while dest.exists():
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        shutil.copyfile(f.path, dest)

        caption = incoming.text or ""
        prompt = f"[User uploaded {dest}]"
        if caption:
            prompt += f"\n\n{caption}"

        waiting = self.task_manager.waiting_input_task(incoming.chat)
        if waiting:
            self.task_manager.submit_answer(waiting.id, prompt)
            return

        self.task_manager.submit_agent(
            chat=incoming.chat,
            message=incoming.message,
            prompt=prompt,
            recent_discussion=self._resolve_recent_discussion(incoming),
        )

    async def _on_voice_from_transport(self, incoming) -> None:
        if not self._auth_identity(incoming.sender):
            return
        if not await self._guard_executor(incoming):
            return
        if self._rate_limited(self._identity_key(incoming.sender)):
            assert self._transport is not None
            await self._transport.send_text(incoming.chat, "Rate limited. Try again shortly.")
            return
        # Record the caption (if any) for per-room history. The transcribed
        # voice text isn't available yet — only what the user typed alongside
        # the audio. No-op for DMs.
        self._chat_history.record(
            chat=incoming.chat,
            msg_id=incoming.message.native_id,
            sender=(
                incoming.sender.handle
                or incoming.sender.display_name
                or "unknown"
            ),
            text=incoming.text,
            is_own_bot=False,
        )
        assert self._transport is not None

        if not self._transcriber:
            await self._transport.send_text(
                incoming.chat,
                "Voice messages aren't configured. "
                "Set up STT with: link-project-to-chat setup --stt-backend whisper-api",
            )
            return

        if not incoming.files:
            return
        audio = incoming.files[0]

        MAX_VOICE_BYTES = 20 * 1024 * 1024
        if audio.size_bytes > MAX_VOICE_BYTES:
            size_mb = audio.size_bytes // (1024 * 1024)
            await self._transport.send_text(
                incoming.chat, f"Audio too large ({size_mb} MB). 20 MB limit.",
            )
            return

        status_ref = await self._transport.send_text(incoming.chat, "🎤 Transcribing...")

        try:
            text = await self._transcriber.transcribe(audio.path)

            if not text or not text.strip():
                await self._transport.edit_text(
                    status_ref, "Could not transcribe the voice message (empty result).",
                )
                return

            display = text if len(text) <= 200 else text[:200] + "..."
            await self._transport.edit_text(status_ref, f'🎤 "{display}"')

            waiting = self.task_manager.waiting_input_task(incoming.chat)
            if waiting:
                self.task_manager.submit_answer(waiting.id, text)
                return

            async with self._backend_lock():
                prompt = await self._build_user_prompt(
                    incoming.chat,
                    text,
                    reply_to_text=incoming.reply_to_text,
                )
                await self._log_user_turn(incoming.chat, text)

                task = self.task_manager.submit_agent(
                    chat=incoming.chat,
                    message=incoming.message,
                    prompt=prompt,
                    recent_discussion=self._resolve_recent_discussion(incoming),
                )
            if self._synthesizer:
                self._voice_tasks.add(task.id)

        except Exception as e:
            logger.exception("Voice transcription failed")
            error_summary = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            await self._transport.edit_text(
                status_ref, f"Transcription failed: {error_summary}",
            )

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath

        return PurePosixPath(path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _send_image(
        self, chat: ChatRef, file_path: str, reply_to: MessageRef | None = None,
    ) -> None:
        path = (
            self.path / file_path if not file_path.startswith("/") else Path(file_path)
        )
        try:
            resolved = path.resolve()
        except (OSError, ValueError):
            logger.warning("Invalid image path: %s", file_path)
            return
        if not resolved.is_relative_to(self.path.resolve()):
            logger.warning("Image path traversal blocked: %s", file_path)
            return
        if not resolved.exists():
            logger.warning("Image file not found: %s", resolved)
            return
        assert self._transport is not None
        try:
            # Oversized (>10MB) or SVG — Transport.send_file picks document-mode
            # for non-image suffixes automatically; SVG has .svg suffix not in
            # _IMAGE_SUFFIXES, so it's routed to send_document. Size-based fall-
            # back for large PNG/JPG files: send_file uses send_photo which may
            # reject >10MB — legacy code switched to send_document. For now we
            # rely on transport's suffix heuristic; rare-case large images fall
            # back via exception handling below.
            await self._transport.send_file(chat, path, caption=path.name)
        except Exception:
            logger.warning("Failed to send image %s", path, exc_info=True)

    @staticmethod
    async def _keep_typing(transport, chat_ref: ChatRef) -> None:
        try:
            while True:
                try:
                    await transport.send_typing(chat_ref)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    async def _dispatch_plugin_on_message(self, msg) -> bool:
        """Fire on_message for each plugin. Return True if ANY plugin consumed.

        Tolerates ``ProjectBot.__new__``-built stubs that skip plugin-state
        initialization — they implicitly behave as "no plugins loaded".
        """
        consumed = False
        for plugin in getattr(self, "_plugins", ()):
            try:
                if await plugin.on_message(msg):
                    consumed = True
            except Exception:
                logger.warning("plugin %s on_message failed", plugin.name, exc_info=True)
        return consumed

    async def _dispatch_plugin_button(self, click) -> bool:
        """Fire on_button for each plugin. Return True if ANY plugin consumed."""
        for plugin in getattr(self, "_plugins", ()):
            try:
                if await plugin.on_button(click):
                    return True
            except Exception:
                logger.warning("plugin %s on_button failed", plugin.name, exc_info=True)
        return False

    async def _dispatch_plugin_tool_use(self, event) -> None:
        for plugin in getattr(self, "_plugins", ()):
            try:
                await plugin.on_tool_use(event.tool, event.path)
            except Exception:
                logger.warning("plugin %s on_tool_use failed", plugin.name, exc_info=True)

    async def _dispatch_plugin_task_complete(self, task) -> None:
        if task.status == TaskStatus.CANCELLED:
            return
        for plugin in getattr(self, "_plugins", ()):
            try:
                await plugin.on_task_complete(task)
            except Exception:
                logger.warning("plugin %s on_task_complete failed", plugin.name, exc_info=True)

    def _plugin_context_prepend(self, prompt: str) -> str:
        """Prepend get_context() outputs to a Claude prompt.

        Only active when the current backend is Claude. Non-Claude backends
        (Codex, future Gemini) don't accept arbitrary system-prompt prepends
        the same way, so plugins that care should branch on ctx.backend_name.
        """
        if getattr(self, "_backend_name", None) != "claude":
            return prompt
        parts: list[str] = []
        for plugin in getattr(self, "_plugins", ()):
            try:
                ctx = plugin.get_context()
            except Exception:
                logger.warning("plugin %s get_context failed", plugin.name, exc_info=True)
                continue
            if ctx:
                parts.append(ctx)
        if not parts:
            return prompt
        return "\n\n".join(parts) + "\n\n---\n\n" + prompt

    def _wrap_plugin_command(self, plugin, bc):
        """Wrap a plugin command handler with auth + role gating + persist.

        Three guards:
        1. **Active-plugin check.** If `plugin.start()` failed (we removed
           the plugin from `self._plugins`), the registered command handler
           silently no-ops. Without this, a failed plugin would still serve
           half-initialized state via its commands.
        2. **Auth + role gate.** `_auth_identity` then `_require_executor`
           (unless `bc.viewer_ok=True`). Same pattern as `_guard_executor`,
           inlined here because `_guard_executor` expects an `IncomingMessage`
           or `CommandInvocation` shape that matches the core handler chain.
        3. **try/finally persist.** Plugin commands can append a first-
           contact identity (via `_auth_identity` → `_get_user_role`). The
           `finally` block calls `_persist_auth_if_dirty` so that lock isn't
           lost when the plugin handler exits — including the viewer-denied
           and exception paths.
        """
        from functools import wraps
        handler = bc.handler
        viewer_ok = bc.viewer_ok
        plugin_ref = plugin  # captured for the active-plugin check

        @wraps(handler)
        async def _wrapped(invocation):
            try:
                # 1. Active-plugin check — plugin may have been removed from
                #    self._plugins after its start() failed.
                if plugin_ref not in self._plugins:
                    logger.debug(
                        "Plugin %s command %r invoked after start failure; "
                        "ignoring",
                        plugin_ref.name, bc.command,
                    )
                    return
                # 2a. Auth (defense-in-depth; transport's authorizer already gated).
                if not self._auth_identity(invocation.sender):
                    return
                # 2b. Role gate (unless viewer_ok).
                if not viewer_ok and not self._require_executor(invocation.sender):
                    assert self._transport is not None
                    await self._transport.send_text(
                        invocation.chat,
                        "Read-only access — your role is viewer.",
                        reply_to=invocation.message,
                    )
                    return
                await handler(invocation)
            finally:
                # 3. Always persist any first-contact lock the auth checks
                #    above may have appended.
                await self._persist_auth_if_dirty()

        return _wrapped

    async def _init_plugins(self) -> None:
        """Instantiate, register, and start plugins. Called from _after_ready."""
        if not self._plugin_configs or self._transport is None:
            return
        # PluginContext field provenance:
        #   bot_name / project_path / bot_username / backend_name / transport
        #     — sourced from ProjectBot state (set in __init__ / _after_ready).
        #   data_dir — resolved via ``resolve_project_meta_dir`` against
        #     ``self._config.meta_dir`` so operators can relocate storage to
        #     a different volume by setting ``meta_dir`` in config.json.
        #     Defaults to ``~/.link-project-to-chat/meta/<bot_name>``.
        #   _identity_resolver — live bound method (see comment inline).
        #   web_port / public_url / register_in_app_web_handler — INTENTIONALLY
        #     left at their None defaults in v1.0.0. They're API surface
        #     reserved for the future external in-app-web-server plugin and
        #     would be populated by a follow-up spec that wires bot.py to the
        #     Web transport's port / public URL and registers an HTTP-route
        #     callback. Plugins that need them must check for None and
        #     degrade gracefully (documented in PluginContext's docstring).
        plugin_data_root = resolve_project_meta_dir(self._config.meta_dir, self.name)
        self._shared_ctx = PluginContext(
            bot_name=self.name,
            project_path=self.path,
            bot_username=self.bot_username,
            data_dir=plugin_data_root,
            backend_name=self._backend_name,
            transport=self._transport,
            # LIVE identity resolver. Plugins call ctx.is_allowed(identity) /
            # ctx.is_executor(identity); the helpers consult the bot's
            # _allowed_users at call time, so locks added AFTER plugin init
            # (e.g., a user first-contacting from a new transport later)
            # are visible. The earlier draft snapshotted allowed_identities /
            # executor_identities as flat lists here — that went stale.
            _identity_resolver=self._get_user_role,
        )

        for cfg in self._plugin_configs:
            pname = cfg.get("name")
            if not pname:
                logger.warning("skipping plugin entry without 'name': %r", cfg)
                continue
            plugin = load_plugin(pname, self._shared_ctx, cfg)
            if plugin:
                self._plugins.append(plugin)

        # Core command names plugins are NOT allowed to shadow. Sourced from
        # `ported_commands` in bot.py:build()/setup; keep in sync if a new
        # core command is added.
        CORE_COMMAND_NAMES: set[str] = {
            "help", "version", "status", "tasks", "backend", "model", "effort",
            "thinking", "context", "permissions", "compact", "reset", "skills",
            "stop_skill", "create_skill", "delete_skill", "persona",
            "stop_persona", "create_persona", "delete_persona", "lang", "start",
            "run", "voice", "halt", "resume",
        }

        # Track which plugin owns which command so we can detect duplicates
        # across plugins (last-load-wins is silently wrong — plugins should
        # not clobber each other).
        registered_command_owner: dict[str, str] = {}

        # Register each plugin's commands on the transport.
        for plugin in self._plugins:
            try:
                cmds = plugin.commands()
            except Exception:
                logger.warning("plugin %s commands() failed; skipping plugin", plugin.name, exc_info=True)
                continue
            for bc in cmds:
                name = bc.command
                if name in CORE_COMMAND_NAMES:
                    logger.warning(
                        "Plugin %s tried to register reserved core command /%s; "
                        "ignoring this command (other commands from this plugin "
                        "remain registered)",
                        plugin.name, name,
                    )
                    continue
                prior_owner = registered_command_owner.get(name)
                if prior_owner is not None:
                    logger.warning(
                        "Plugin %s tried to register /%s already claimed by "
                        "plugin %s; ignoring",
                        plugin.name, name, prior_owner,
                    )
                    continue
                wrapped = self._wrap_plugin_command(plugin, bc)
                self._transport.on_command(name, wrapped)
                self._plugin_command_handlers.setdefault(plugin.name, []).append(name)
                registered_command_owner[name] = plugin.name

        # Start plugins in dependency order; on failure, "unregister" by
        # removing from _plugins so further dispatch skips them. Transport
        # doesn't expose remove-handler, so the registered command stays
        # wired but its plugin is dead — log clearly so this is visible.
        for plugin in _topo_sort(list(self._plugins)):
            try:
                await plugin.start()
            except Exception:
                logger.warning(
                    "plugin %s start failed; removing from dispatch (commands %s remain inert)",
                    plugin.name, self._plugin_command_handlers.get(plugin.name, []),
                    exc_info=True,
                )
                if plugin in self._plugins:
                    self._plugins.remove(plugin)

    async def _shutdown_plugins(self) -> None:
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.warning("plugin %s stop failed", plugin.name, exc_info=True)

    @staticmethod
    def _is_expected_startup_delivery_failure(exc: BaseException) -> bool:
        message = (getattr(exc, "message", "") or str(exc) or "").lower()
        markers = (
            "chat not found",
            "bot was blocked by the user",
            "bot can't initiate conversation",
            "bot can't send messages to bots",
            "forbidden",
        )
        return any(marker in message for marker in markers)

    async def _after_ready(self, self_identity) -> None:
        """Called once after Transport.start() completes platform post-init.

        The Transport has already run delete_webhook, get_me, and
        set_my_commands. This callback does the bot-specific post-ready
        work: backfill missing team metadata, refresh the Claude system
        note with the discovered @handle, and send startup pings to
        trusted users.
        """
        self.bot_username = self_identity.handle or ""
        if self.team_name and self.role and self.bot_username:
            self._backfill_own_bot_username()
        self._refresh_team_system_note()
        await self._init_plugins()
        # The transport's initial command-menu push ran before plugin commands
        # were registered. Refresh through the transport abstraction so bot.py
        # stays platform-free; transports without command menus may no-op.
        if self._plugins:
            try:
                command_menu = list(COMMANDS)
                seen = {name for name, _desc in command_menu}
                for plugin in self._plugins:
                    for bc in plugin.commands():
                        if bc.command in seen:
                            continue
                        command_menu.append((bc.command, bc.description or bc.command))
                        seen.add(bc.command)
                await self._transport.set_command_menu(command_menu)
            except Exception:
                logger.warning("failed to refresh command menu", exc_info=True)

        # Startup ping to trusted users. Skipped for team bots: they live in
        # the team supergroup and have no DM with trusted users, so every send
        # would fail with Forbidden / Chat not found.
        assert self._transport is not None
        if self.team_name and self.role:
            return
        # Each AllowedUser's locked_identities holds entries in the new
        # "transport_id:native_id" shape (Task 3). For the startup ping we
        # only want identities matching the bot's active transport.
        active_prefix = f"{self._transport.TRANSPORT_ID}:"
        for au in self._allowed_users:
            for ident_key in au.locked_identities:
                if not ident_key.startswith(active_prefix):
                    continue
                native_id = ident_key[len(active_prefix):]
                if self._transport.TRANSPORT_ID == "google_chat":
                    # Google Chat user identities (`users/<id>`) are not valid
                    # spaces — `POST /v1/users/.../messages` returns 404. The
                    # bot is discovered via DM/space, so a startup ping isn't
                    # useful here either way; skip cleanly.
                    continue
                if self._transport.TRANSPORT_ID == "telegram":
                    try:
                        int(native_id)
                    except ValueError:
                        logger.warning(
                            "Skipping startup message to invalid Telegram identity %s",
                            ident_key,
                        )
                        continue
                chat = ChatRef(
                    transport_id=self._transport.TRANSPORT_ID,
                    native_id=native_id,
                    kind=ChatKind.DM,
                )
                try:
                    await self._transport.send_text(
                        chat, f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                    )
                except Exception as exc:
                    if self._is_expected_startup_delivery_failure(exc):
                        logger.warning(
                            "Startup message not delivered to %s. If this is a new Telegram bot, "
                            "the user must open it and send /start once before it can DM them.",
                            native_id,
                        )
                    else:
                        logger.error(
                            "Failed to send startup message to %s",
                            native_id,
                            exc_info=True,
                        )

    def _make_web_revocation_check(self):
        """Return a callable the web transport calls on every auth check.

        Reads the live config under load_config's existing lock discipline so
        manager-side mutations (`/remove_user`, `/reset_user_identity`,
        `/demote_user`) take effect on the next web request without a project
        restart. Fails closed on read errors so a corrupt config can't keep
        a revoked user authenticated.
        """
        from .config import (
            _normalize_username,
            load_config,
            resolve_project_allowed_users,
        )

        config_path = self._effective_config_path()
        project_name = self.name

        def check(handle: str) -> bool:
            try:
                config = load_config(config_path)
            except Exception:
                logger.exception(
                    "web revocation check: failed to load config %s; failing closed",
                    config_path,
                )
                return False
            project = config.projects.get(project_name)
            if project is None:
                return False
            users, _ = resolve_project_allowed_users(project, config)
            normalized = _normalize_username(handle)
            return any(_normalize_username(u.username) == normalized for u in users)

        return check

    def build(self) -> None:
        if self.transport_kind == "web":
            from .web.transport import WebTransport
            bot_identity = Identity(
                transport_id="web",
                native_id="bot1",
                display_name=self.name,
                handle=self.name.lower(),
                is_bot=True,
            )
            db_path = Path.home() / ".link-project-to-chat" / "web" / f"{self.name}.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            web_auth_token = None
            web_authenticated_handle = None
            web_authenticated_handles = None
            if len(self._allowed_users) == 1:
                web_auth_token = secrets.token_urlsafe(32)
                web_authenticated_handle = self._allowed_users[0].username
            else:
                web_authenticated_handles = {
                    secrets.token_urlsafe(32): user.username
                    for user in self._allowed_users
                }
            self._transport = WebTransport(
                db_path=db_path,
                bot_identity=bot_identity,
                port=self.web_port,
                authenticated_handle=web_authenticated_handle,
                auth_token=web_auth_token,
                authenticated_handles=web_authenticated_handles,
                revocation_check=self._make_web_revocation_check(),
            )
            self._app = None  # WebTransport has no PTB Application
        elif self.transport_kind == "google_chat":
            from .google_chat.transport import GoogleChatTransport

            transport = GoogleChatTransport(config=self._config.google_chat)
            self._transport = transport
            self._app = None  # GoogleChatTransport has no PTB Application
        elif self.transport_kind == "telegram":
            from .transport.telegram import TelegramTransport
            self._transport = TelegramTransport.build(self.token, menu=COMMANDS)
            self._app = self._transport.app  # PTB Application
        else:
            raise ValueError(f"unknown transport: {self.transport_kind}")
        self._transport.on_ready(self._after_ready)
        self._transport.on_stop(self._shutdown_plugins)

        async def _pre_authorize(identity) -> bool:
            return self._auth_identity(identity)

        self._transport.set_authorizer(_pre_authorize)

        # All commands consume CommandInvocation directly — no legacy shim.
        ported_commands = (
            ("help", self._on_help_t),
            ("version", self._on_version_t),
            ("status", self._on_status_t),
            ("tasks", self._on_tasks),
            ("backend", self._on_backend),
            ("model", self._on_model),
            ("effort", self._on_effort),
            ("thinking", self._on_thinking),
            ("context", self._on_context),
            ("permissions", self._on_permissions),
            ("compact", self._on_compact),
            ("reset", self._on_reset),
            ("skills", self._on_skills),
            ("stop_skill", self._on_stop_skill),
            ("create_skill", self._on_create_skill),
            ("delete_skill", self._on_delete_skill),
            ("persona", self._on_persona),
            ("stop_persona", self._on_stop_persona),
            ("create_persona", self._on_create_persona),
            ("delete_persona", self._on_delete_persona),
            ("lang", self._on_lang),
            ("start", self._on_start),
            ("run", self._on_run),
            ("voice", self._on_voice_status),
            ("halt", self._on_halt),
            ("resume", self._on_resume),
        )
        # Wrap every top-level handler in a try/finally that fires
        # _persist_auth_if_dirty on every exit path (success, deny, plugin
        # consume, exception). Without this, first-contact identity locks
        # added by the transport authorizer would be lost on bot restart
        # whenever the path skipped _guard_executor (read-only commands,
        # plugin-consumed messages, error paths).
        def _wrap_with_persist(handler):
            async def _wrapped(arg):
                try:
                    await handler(arg)
                finally:
                    await self._persist_auth_if_dirty()
            return _wrapped

        self._transport.on_message(_wrap_with_persist(self._on_text_from_transport))
        self._transport.on_button(_wrap_with_persist(self._on_button))
        for _name, _handler in ported_commands:
            self._transport.on_command(_name, _wrap_with_persist(_handler))

        if self.transport_kind == "telegram":
            # Main routing — registers MessageHandler/CommandHandler/CallbackQueryHandler.
            self._transport.attach_telegram_routing(
                group_mode=self.group_mode,
                command_names=[n for n, _ in ported_commands],
                respond_in_groups=self._respond_in_groups,
            )

            # Team-mode bot: if the manager passed a Telethon session, wire the
            # bot-to-bot relay. Spec #0c: project bot owns its relay; the manager
            # exposes credentials via env vars when spawning team-mode subprocesses
            # (see manager/process.py). Two env vars are recognised:
            #   LP2C_TELETHON_SESSION_STRING — preferred (spec D′): an in-memory
            #     StringSession seeded by the manager, no SQLite file lock.
            #   LP2C_TELETHON_SESSION — fallback: absolute path to the on-disk
            #     telethon.session file. Only used when the StringSession export
            #     was unavailable (e.g. unauthorized session, or telethon import
            #     failed in the manager).
            if self.team_name and self.group_chat_id:
                session_string_env = os.environ.get("LP2C_TELETHON_SESSION_STRING")
                session_path_env = os.environ.get("LP2C_TELETHON_SESSION")
                if session_string_env or session_path_env:
                    cfg_path = self._effective_config_path()
                    config = load_config(cfg_path)
                    teams = load_teams(cfg_path)
                    team = teams.get(self.team_name)
                    team_bot_usernames = {
                        b.bot_username for b in team.bots.values() if b.bot_username
                    } if team else set()
                    # Extract the bot's authenticated Telegram user ID from the
                    # AllowedUser-locked-identities model (post-v1.0 auth). The
                    # relay uses this to identify which group messages are the
                    # authenticated user's vs peer-bot forwards. This block is
                    # inside a Telethon-only path, so we look for the "telegram:"
                    # prefix directly rather than reading TRANSPORT_ID from
                    # self._transport (which tests sometimes patch with a Mock).
                    authenticated_user_id = next(
                        (
                            ident_key[len("telegram:"):]
                            for au in self._allowed_users
                            for ident_key in au.locked_identities
                            if ident_key.startswith("telegram:")
                        ),
                        None,
                    )
                    if not team_bot_usernames:
                        logger.warning(
                            "Team %r missing from config or has no bot usernames; "
                            "skipping enable_team_relay (bot will operate without relay).",
                            self.team_name,
                        )
                    elif session_string_env:
                        self._transport.enable_team_relay_from_session_string(
                            session_string=session_string_env,
                            api_id=config.telegram_api_id,
                            api_hash=config.telegram_api_hash,
                            team_bot_usernames=team_bot_usernames,
                            group_chat_id=self.group_chat_id,
                            team_name=self.team_name,
                            max_autonomous_turns=team.max_autonomous_turns if team else 5,
                            team_authority=self._team_authority,
                            authenticated_user_id=authenticated_user_id,
                        )
                    else:
                        self._transport.enable_team_relay_from_session(
                            session_path=session_path_env,
                            api_id=config.telegram_api_id,
                            api_hash=config.telegram_api_hash,
                            team_bot_usernames=team_bot_usernames,
                            group_chat_id=self.group_chat_id,
                            team_name=self.team_name,
                            max_autonomous_turns=team.max_autonomous_turns if team else 5,
                            team_authority=self._team_authority,
                            authenticated_user_id=authenticated_user_id,
                        )

    def run(self) -> None:
        """Run the transport's main loop. Owns the lifecycle from here on.

        Synchronous: matches the underlying Transport.run() contract.
        """
        assert self._transport is not None
        self._transport.run()


def run_bot(
    name: str,
    path: Path,
    token: str,
    username: str = "",
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    allowed_usernames: list[str] | None = None,
    transcriber: "Transcriber | None" = None,
    synthesizer: "Synthesizer | None" = None,
    team_name: str | None = None,
    active_persona: str | None = None,
    show_thinking: bool = False,
    group_chat_id: int | None = None,
    room: RoomBinding | None = None,
    role: str | None = None,
    peer_bot_username: str = "",
    config_path: Path | None = None,
    transport_kind: str = "telegram",
    web_port: int = 8080,
    backend_name: str = "claude",
    backend_state: dict[str, dict] | None = None,
    context_enabled: bool = True,
    context_history_limit: int = 10,
    plugins: list[dict] | None = None,
    allowed_users: list | None = None,
    auth_source: str = "project",
    respond_in_groups: bool = False,
    config: Config | None = None,
) -> None:
    # Determine effective auth — prefer the modern allowed_users; fall back
    # to synthesizing executor entries from the legacy usernames so the
    # transition shim in ProjectBot.__init__ has something to consume.
    effective_usernames = allowed_usernames or ([username] if username else [])
    if not allowed_users and not effective_usernames:
        raise SystemExit(
            "No allowed user configured. For one-shot --path/--token starts, "
            "pass --username USER. For configured projects, run "
            "'configure --add-user USER[:ROLE]'."
        )
    if session_id:
        save_session(
            name,
            session_id,
            config_path or DEFAULT_CONFIG,
            team_name=team_name,
            role=role,
        )
    bot = ProjectBot(
        name, path, token,
        allowed_usernames=effective_usernames,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        transcriber=transcriber,
        synthesizer=synthesizer,
        active_persona=active_persona,
        show_thinking=show_thinking,
        team_name=team_name,
        group_chat_id=group_chat_id,
        room=room,
        role=role,
        peer_bot_username=peer_bot_username,
        config_path=config_path,
        transport_kind=transport_kind,
        web_port=web_port,
        backend_name=backend_name,
        backend_state=backend_state,
        context_enabled=context_enabled,
        context_history_limit=context_history_limit,
        plugins=plugins,
        allowed_users=allowed_users,
        auth_source=auth_source,
        respond_in_groups=respond_in_groups,
        config=config,
    )
    bot.task_manager.backend.session_id = session_id or load_session(
        name,
        config_path or DEFAULT_CONFIG,
        team_name=team_name,
        role=role,
    )
    if model:
        bot.task_manager.backend.model = model
    if effort:
        bot.task_manager.backend.effort = effort
    bot.build()
    logger.info("Bot '%s' started at %s", name, path)
    bot.run()


def run_bots(
    config: Config,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    config_path: Path | None = None,
    transcriber: "Transcriber | None" = None,
    synthesizer: "Synthesizer | None" = None,
    transport_kind: str = "telegram",
    web_port: int = 8080,
) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        effective_allowed_users, project_auth_source = resolve_project_allowed_users(proj, config)
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        project_state = proj.backend_state.get(proj.backend, {})
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            model=resolve_start_model(
                proj.backend,
                explicit_model=model,
                backend_model=project_state.get("model"),
                legacy_claude_model=proj.model,
            ),
            effort=project_state.get("effort") or proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            transcriber=transcriber,
            synthesizer=synthesizer,
            active_persona=proj.active_persona,
            show_thinking=bool(project_state.get("show_thinking", proj.show_thinking)),
            config_path=config_path,
            transport_kind=transport_kind,
            web_port=web_port,
            backend_name=proj.backend,
            backend_state=proj.backend_state,
            context_enabled=proj.context_enabled,
            context_history_limit=proj.context_history_limit,
            plugins=getattr(proj, "plugins", None) or None,
            allowed_users=effective_allowed_users or None,
            auth_source=project_auth_source,
            respond_in_groups=proj.respond_in_groups,
            config=config,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
