"""TelegramTransport — python-telegram-bot adapter for the Transport Protocol.

This module is the ONLY place in the codebase that imports `telegram` after
spec #0 step 9 (lockout). bot.py talks to the interface; everything
Telegram-specific lives behind it.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from .base import (
    AuthorizerCallback,
    ButtonHandler,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    Identity,
    MessageHandler,
    MessageRef,
    TransportRetryAfter,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..team_safety import TeamAuthority
    from ._telegram_relay import TeamRelay

TRANSPORT_ID = "telegram"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _as_retry_after(exc: BaseException) -> TransportRetryAfter | None:
    """Map a PTB exception carrying `retry_after` into a portable TransportRetryAfter.

    Non-retry errors return None so callers can re-raise or swallow as appropriate.
    """
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        return None
    return TransportRetryAfter(float(retry_after))

# Matches the prefix the Telethon relay prepends to forwarded bot-to-bot messages.
# Format: "[auto-relay from <handle>]\n\n" — note there is no '@' on the handle.
# That is intentional: '@handle' would make peer bots re-process the relayed
# message as a self-mention. See transport/_telegram_relay.py for where the
# prefix is written.
from ._telegram_relay import _RELAY_HANDLE_PATTERN
_RELAY_PREFIX_RE = re.compile(rf"^\[auto-relay from {_RELAY_HANDLE_PATTERN}\]\n\n")


def _safe_basename(raw: str | None, fallback: str) -> str:
    """Reduce an attacker-controlled filename to a safe basename for tempdir use.

    Strips path separators and parent-dir tokens. Falls back to `fallback` only
    when the result is empty or is a pure traversal token ('.' or '..'). After
    PurePath(...).name extraction, dotfile names like '.bashrc' are NOT
    traversal vectors, so they pass through unchanged.
    """
    name = (raw or "").strip()
    if not name:
        return fallback
    candidate = PurePath(name.replace("\\", "/")).name
    if not candidate or candidate in (".", ".."):
        return fallback
    return candidate


def _buttons_to_inline_keyboard(buttons: Buttons | None) -> Any:
    """Convert a Buttons primitive to telegram's InlineKeyboardMarkup (or None)."""
    if buttons is None:
        return None
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text=b.label, callback_data=b.value) for b in row]
        for row in buttons.rows
    ])


def chat_ref_from_telegram(chat: Any) -> ChatRef:
    """Map a telegram.Chat (or duck-typed equivalent with .id and .type) to ChatRef."""
    kind = ChatKind.DM if chat.type == "private" else ChatKind.ROOM
    return ChatRef(transport_id=TRANSPORT_ID, native_id=str(chat.id), kind=kind)


def identity_from_telegram_user(user: Any) -> Identity:
    """Map a telegram.User (or duck-typed equivalent) to Identity."""
    return Identity(
        transport_id=TRANSPORT_ID,
        native_id=str(user.id),
        display_name=user.full_name,
        handle=user.username,
        is_bot=user.is_bot,
    )


def message_ref_from_telegram(msg: Any) -> MessageRef:
    """Map a telegram.Message to MessageRef."""
    return MessageRef(
        transport_id=TRANSPORT_ID,
        native_id=str(msg.message_id),
        chat=chat_ref_from_telegram(msg.chat),
    )


class TelegramTransport:
    """python-telegram-bot adapter. All Protocol methods raise NotImplementedError
    until populated in subsequent tasks (spec strangler steps 3–8).
    """

    TRANSPORT_ID = TRANSPORT_ID
    max_text_length: int = 4096  # Telegram's hard cap.

    def __init__(self, application: Any) -> None:
        """Construct from an already-built telegram.ext.Application.

        Prefer the `build()` classmethod for production use; this constructor
        exists so tests can inject a mocked Application.
        """
        self._app = application
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list = []
        self._on_stop_callbacks: list = []
        self._authorizer: AuthorizerCallback | None = None
        self._menu: Any = None
        self._team_relay: "TeamRelay | None" = None  # Set by enable_team_relay; lifecycle-tied to start/stop.
        self._post_init_ran: bool = False
        self._routing_attached: bool = False
        self._group_mode_attached: bool = False
        self._respond_in_groups_attached: bool = False

    @property
    def team_relay_consecutive_bot_turns(self) -> int:
        return int(getattr(self._team_relay, "_consecutive_bot_turns", 0))

    @property
    def team_relay_max_autonomous_turns(self) -> int:
        return int(getattr(self._team_relay, "_max_autonomous_turns", 5))

    @classmethod
    def build(
        cls,
        token: str,
        *,
        concurrent_updates: bool = True,
        menu: Any = None,
    ) -> "TelegramTransport":
        """Construct a TelegramTransport with a polling-mode Application.

        Post-init work (delete_webhook, get_me, set_my_commands) runs inside
        start() — see TelegramTransport.start().
        """
        from telegram.ext import ApplicationBuilder
        app = ApplicationBuilder().token(token).concurrent_updates(concurrent_updates).build()
        instance = cls(app)
        instance._menu = menu
        return instance

    def enable_team_relay(
        self,
        telethon_client: Any,
        team_bot_usernames: set[str],
        group_chat_id: int,
        team_name: str,
        max_autonomous_turns: int = 5,
        team_authority: TeamAuthority | None = None,
        authenticated_user_id: int | str | None = None,
    ) -> None:
        """Activate the Telethon-user-session relay for a team group chat.

        Required because Telegram Bot API never delivers bot-to-bot messages.
        Other transports (Discord, Slack, Web) don't need this and don't
        implement it — this method is TelegramTransport-specific, not on the
        Transport Protocol.

        Call once after build(), before start(). Relay lifecycle is tied to
        start()/stop() thereafter.
        """
        from ._telegram_relay import TeamRelay
        self._team_relay = TeamRelay(
            client=telethon_client,
            team_name=team_name,
            group_chat_id=group_chat_id,
            bot_usernames=team_bot_usernames,
            max_autonomous_turns=max_autonomous_turns,
            team_authority=team_authority,
            authenticated_user_id=authenticated_user_id,
        )

    def enable_team_relay_from_session(
        self,
        *,
        session_path: str,
        api_id: int,
        api_hash: str,
        team_bot_usernames: set[str],
        group_chat_id: int,
        team_name: str,
        max_autonomous_turns: int = 5,
        team_authority: TeamAuthority | None = None,
        authenticated_user_id: int | str | None = None,
    ) -> None:
        """Construct a Telethon client from session credentials and enable the relay.

        Keeps the telethon import inside this module so bot.py stays platform-free
        (only TelegramTransport knows the underlying library). Missing optional
        deps raise ImportError so the caller can degrade gracefully.
        """
        from telethon import TelegramClient  # telethon is an optional extra
        client = TelegramClient(session_path, api_id, api_hash)
        self.enable_team_relay(
            telethon_client=client,
            team_bot_usernames=team_bot_usernames,
            group_chat_id=group_chat_id,
            team_name=team_name,
            max_autonomous_turns=max_autonomous_turns,
            team_authority=team_authority,
            authenticated_user_id=authenticated_user_id,
        )

    def enable_team_relay_from_session_string(
        self,
        *,
        session_string: str,
        api_id: int,
        api_hash: str,
        team_bot_usernames: set[str],
        group_chat_id: int,
        team_name: str,
        max_autonomous_turns: int = 5,
        team_authority: TeamAuthority | None = None,
        authenticated_user_id: int | str | None = None,
    ) -> None:
        """Like ``enable_team_relay_from_session`` but seeds the Telethon
        client from an in-memory ``StringSession`` instead of a SQLite file.

        Multiple subprocesses can each call this with the same string and
        connect concurrently — Telethon treats them as parallel connections
        of the same authorized account, with no shared SQLite write lock to
        fight over. Spec D′ uses this to fix the ``database is locked`` race
        that kills team-bot autostart when several bots open the same
        ``telethon.session`` file at once.
        """
        from telethon import TelegramClient  # telethon is an optional extra
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.enable_team_relay(
            telethon_client=client,
            team_bot_usernames=team_bot_usernames,
            group_chat_id=group_chat_id,
            team_name=team_name,
            max_autonomous_turns=max_autonomous_turns,
            team_authority=team_authority,
            authenticated_user_id=authenticated_user_id,
        )

    def attach_telegram_routing(
        self,
        *,
        group_mode: bool,
        command_names: list[str],
        respond_in_groups: bool = False,
    ) -> None:
        """Wire telegram's MessageHandler/CommandHandler/CallbackQueryHandler
        so all incoming updates route through our _dispatch_* methods.

        ``group_mode``: team-mode bot — restrict to GROUPS only.
        ``respond_in_groups``: solo-mode bot that wants to also answer in groups.
        The two flags are mutually exclusive at the filter level: when both are
        True, group_mode wins (team workflow takes precedence).
        """
        from telegram.ext import (
            CallbackQueryHandler,
            CommandHandler,
            MessageHandler,
            filters,
        )

        if group_mode:
            chat_filter = filters.ChatType.GROUPS
            incoming_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & filters.TEXT
                & ~filters.COMMAND
            )
        elif respond_in_groups:
            chat_filter = filters.ChatType.PRIVATE | filters.ChatType.GROUPS
            incoming_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & (
                    filters.TEXT
                    | filters.Document.ALL
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.VIDEO_NOTE
                    | filters.Sticker.ALL
                    | filters.VIDEO
                    | filters.LOCATION
                    | filters.CONTACT
                )
                & ~filters.COMMAND
            )
        else:
            chat_filter = filters.ChatType.PRIVATE
            incoming_filter = (
                chat_filter
                & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
                & (
                    filters.TEXT
                    | filters.Document.ALL
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.VIDEO_NOTE
                    | filters.Sticker.ALL
                    | filters.VIDEO
                    | filters.LOCATION
                    | filters.CONTACT
                )
                & ~filters.COMMAND
            )

        self._app.add_handler(MessageHandler(incoming_filter, self._dispatch_message))

        for name in command_names:
            self._app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._dispatch_command(_n, u, c),
                filters=chat_filter,
            ))

        self._app.add_handler(CallbackQueryHandler(self._dispatch_button))
        self._app.add_error_handler(self._default_error_handler)

        self._routing_attached = True
        self._group_mode_attached = group_mode
        self._respond_in_groups_attached = respond_in_groups

    @property
    def app(self) -> Any:
        """Expose the underlying telegram.ext.Application.

        TelegramTransport-specific accessor — NOT on the Transport Protocol.
        Used by callers that need to attach legacy handlers (e.g.,
        ConversationHandler) that don't yet have a Transport equivalent.
        """
        return self._app

    def bridge_command(self, name: str):
        """Return a PTB CommandHandler callback that forwards to `on_command`.

        The manager bot still registers PTB handlers directly (its
        ConversationHandler wizards can't live inside attach_telegram_routing)
        but should never reach into _dispatch_command. Use this helper instead.
        """
        async def _bridge(update: Any, ctx: Any) -> None:
            await self._dispatch_command(name, update, ctx)
        return _bridge

    def bridge_button(self):
        """Return a PTB CallbackQueryHandler callback that forwards to `on_button`.

        See `bridge_command` for why the manager needs this.
        """
        async def _bridge(update: Any, ctx: Any) -> None:
            await self._dispatch_button(update, ctx)
        return _bridge

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        await self._app.initialize()
        await self.post_init(self._app)
        await self._app.start()
        await self._app.updater.start_polling()

    async def post_init(self, _app: Any = None) -> None:
        # Platform post-init: drain pending updates + discover own identity +
        # register /commands menu. Runs between initialize() and start() so the
        # Application is configured before polling begins. Idempotent — PTB may
        # invoke us both from TelegramTransport.start() (tests/scaffolding) and
        # from Application.run_polling(); the guard lets both paths coexist.
        if self._post_init_ran:
            return
        self._post_init_ran = True

        from ._telegram_relay import RelayUnauthorizedError

        try:
            try:
                await self._app.bot.delete_webhook(drop_pending_updates=True)
            except Exception:
                pass  # non-fatal
            try:
                me = await self._app.bot.get_me()
                from .base import Identity
                self_identity = Identity(
                    transport_id=TRANSPORT_ID,
                    native_id=str(me.id),
                    display_name=me.full_name or me.username or "bot",
                    handle=(me.username or "").lower() or None,
                    is_bot=True,
                )
            except Exception:
                from .base import Identity
                self_identity = Identity(
                    transport_id=TRANSPORT_ID, native_id="0",
                    display_name="bot", handle=None, is_bot=True,
                )
            if self._menu:
                try:
                    await self._app.bot.set_my_commands(self._menu)
                except Exception:
                    pass

            # Fire caller-registered callbacks with the bot's identity.
            for cb in self._on_ready_callbacks:
                await cb(self_identity)

            if self._team_relay is not None:
                try:
                    await self._team_relay.start()
                except RelayUnauthorizedError as e:
                    # Missing Telethon auth disables bot-to-bot relay but the rest
                    # of the bot still works; re-linking re-enables it at next start.
                    logger.warning("Team relay disabled: %s", e)
                    self._team_relay = None
        except Exception:
            # Undo anything we started so we don't leak Telethon handlers or
            # half-wired menu state — post_stop only runs after a successful start.
            if self._team_relay is not None:
                try:
                    await self._team_relay.stop()
                except Exception:
                    logger.exception("post_init failure: unwinding partial startup")
            self._post_init_ran = False
            raise

    def on_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

    async def set_command_menu(self, commands: list[tuple[str, str]]) -> None:
        self._menu = list(commands)
        if not self._post_init_ran:
            return
        await self._app.bot.set_my_commands(self._menu)

    def on_stop(self, callback) -> None:
        self._on_stop_callbacks.append(callback)

    async def post_stop(self, _app: Any = None) -> None:
        for cb in self._on_stop_callbacks:
            try:
                await cb()
            except Exception:
                logger.exception("on_stop callback failed")
        if self._team_relay is not None:
            try:
                await self._team_relay.stop()
            except Exception:
                logger.exception("post_stop team_relay.stop failed")
        # Reset the guard so a rebuilt transport can re-init if the caller
        # drives the start/stop sequence manually (tests + CLI scaffolding).
        self._post_init_ran = False

    async def stop(self) -> None:
        await self.post_stop(self._app)
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()

    def run(self) -> None:
        """Run PTB's polling loop. Owns post_init/post_stop wiring so bot.py
        never touches the Application by name.

        Synchronous: PTB's run_polling creates and manages its own event loop.

        ``start()`` and ``run()`` are alternative entry points. ``run()`` is
        the standalone path (CLI orchestration); ``start()`` is for tests
        that drive the lifecycle manually. Both invoke ``post_init`` exactly
        once via the ``_post_init_ran`` guard, so calling ``start()`` then
        ``run()`` is safe but unusual.
        """
        self._app.post_init = self.post_init
        self._app.post_stop = self.post_stop
        self._app.run_polling()

    # ── Outbound ──────────────────────────────────────────────────────────
    _DELETED_REPLY_TARGET_MARKERS = (
        "message to be replied not found",
        "replied message not found",
        "reply message not found",
    )

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        kwargs: dict[str, Any] = {
            "chat_id": int(chat.native_id),
            "text": text,
        }
        if html:
            kwargs["parse_mode"] = "HTML"
        if reply_to is not None:
            kwargs["reply_to_message_id"] = int(reply_to.native_id)
        if buttons is not None:
            kwargs["reply_markup"] = _buttons_to_inline_keyboard(buttons)
        try:
            native_msg = await self._app.bot.send_message(**kwargs)
        except Exception as e:
            remapped = _as_retry_after(e)
            if remapped is not None:
                raise remapped from e
            # Retry once without reply_to if the target message was deleted —
            # preserving HTML/buttons. This restores the behavior previously
            # hand-rolled in bot.py._send_html (commit 4b4c08d).
            if reply_to is not None and self._is_deleted_reply_target(e):
                logger.info(
                    "send_text retry without reply_to: target message deleted",
                )
                # Safe to retry: Bot API validates reply_to_message_id before
                # delivery, so the original send was rejected, not partially
                # completed.
                kwargs.pop("reply_to_message_id", None)
                native_msg = await self._app.bot.send_message(**kwargs)
                return message_ref_from_telegram(native_msg)
            if reply_to is not None:
                logger.warning(
                    "send_text BadRequest with reply_to but markers did not match — "
                    "Telegram wording may have changed: %r",
                    getattr(e, "message", None) or str(e),
                )
            raise
        return message_ref_from_telegram(native_msg)

    @classmethod
    def _is_deleted_reply_target(cls, exc: BaseException) -> bool:
        """Recognize the BadRequest variants Telegram uses when the reply
        target has been deleted (covers slight wording differences across
        PTB versions)."""
        message = (getattr(exc, "message", "") or str(exc) or "").lower()
        return any(marker in message for marker in cls._DELETED_REPLY_TARGET_MARKERS)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None:
        # buttons handled in Task 17; ignore here.
        kwargs: dict[str, Any] = {
            "chat_id": int(msg.chat.native_id),
            "message_id": int(msg.native_id),
            "text": text,
        }
        if html:
            kwargs["parse_mode"] = "HTML"
        if buttons is not None:
            kwargs["reply_markup"] = _buttons_to_inline_keyboard(buttons)
        try:
            await self._app.bot.edit_message_text(**kwargs)
        except Exception as e:
            remapped = _as_retry_after(e)
            if remapped is not None:
                raise remapped from e
            raise

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        suffix = path.suffix.lower()
        chat_id = int(chat.native_id)
        try:
            if suffix in _IMAGE_SUFFIXES:
                with path.open("rb") as fh:
                    native = await self._app.bot.send_photo(
                        chat_id=chat_id, photo=fh, caption=caption,
                    )
            else:
                with path.open("rb") as fh:
                    native = await self._app.bot.send_document(
                        chat_id=chat_id, document=fh, caption=caption, filename=display_name,
                    )
        except Exception as e:
            remapped = _as_retry_after(e)
            if remapped is not None:
                raise remapped from e
            raise
        return message_ref_from_telegram(native)

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        kwargs: dict[str, Any] = {
            "chat_id": int(chat.native_id),
        }
        if reply_to is not None:
            kwargs["reply_to_message_id"] = int(reply_to.native_id)
        try:
            with path.open("rb") as fh:
                native = await self._app.bot.send_voice(voice=fh, **kwargs)
        except Exception as e:
            remapped = _as_retry_after(e)
            if remapped is not None:
                raise remapped from e
            raise
        return message_ref_from_telegram(native)

    def render_markdown(self, md: str) -> str:
        """Render markdown into Telegram's HTML subset (see formatting.md_to_telegram)."""
        from ..formatting import md_to_telegram
        return md_to_telegram(md)

    async def send_typing(self, chat: ChatRef) -> None:
        try:
            await self._app.bot.send_chat_action(
                chat_id=int(chat.native_id), action="typing",
            )
        except Exception as e:
            remapped = _as_retry_after(e)
            if remapped is not None:
                # Rate-limit hint matters — surface it so the caller can back off.
                raise remapped from e
            # Other errors: typing indicators are best-effort; never fatal.

    # ── Inbound dispatch ──────────────────────────────────────────────────
    async def _dispatch_message(self, update: Any, ctx: Any) -> None:
        """Convert a telegram Update into IncomingMessage and invoke handlers.

        Downloads photo/document attachments to per-handler temp directories,
        then removes them after all handlers return. If an authorizer is set,
        it is consulted BEFORE any download work; rejection drops the update.
        """
        import tempfile

        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return

        # Pre-download authorization gate (defends against unauth-DoS via large attachments).
        if self._authorizer is not None:
            sender = identity_from_telegram_user(user)
            if not await self._authorizer(sender):
                return

        from .base import IncomingFile, IncomingMessage

        files: list[IncomingFile] = []
        tmpdirs: list[tempfile.TemporaryDirectory] = []

        photo = getattr(msg, "photo", None)
        if photo:
            largest = photo[-1]
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            path = Path(tmpdir.name) / "photo.jpg"
            tg_file = await largest.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name="photo.jpg",
                mime_type="image/jpeg",
                size_bytes=getattr(largest, "file_size", 0) or 0,
            ))

        doc = getattr(msg, "document", None)
        if doc is not None:
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            raw_name = getattr(doc, "file_name", None)
            safe_name = _safe_basename(raw_name, "document")
            path = Path(tmpdir.name) / safe_name
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=safe_name,
                mime_type=getattr(doc, "mime_type", None),
                size_bytes=getattr(doc, "file_size", 0) or 0,
            ))

        voice = getattr(msg, "voice", None)
        if voice is not None:
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            path = Path(tmpdir.name) / "voice.ogg"
            tg_file = await voice.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name="voice.ogg",
                mime_type="audio/ogg",
                size_bytes=getattr(voice, "file_size", 0) or 0,
            ))

        audio = getattr(msg, "audio", None)
        if audio is not None:
            tmpdir = tempfile.TemporaryDirectory()
            tmpdirs.append(tmpdir)
            raw_name = getattr(audio, "file_name", None)
            safe_name = _safe_basename(raw_name, "audio")
            path = Path(tmpdir.name) / safe_name
            tg_file = await audio.get_file()
            await tg_file.download_to_drive(path)
            files.append(IncomingFile(
                path=path,
                original_name=safe_name,
                mime_type=getattr(audio, "mime_type", None) or "audio/mpeg",
                size_bytes=getattr(audio, "file_size", 0) or 0,
            ))

        # Detect unsupported attachments delivered by the filter
        # (filters.VIDEO | filters.Sticker.ALL | filters.LOCATION | filters.CONTACT | filters.VIDEO_NOTE).
        # We surface a flag so the bot can reject instead of treating the caption as a prompt.
        has_unsupported_media = (
            len(files) == 0
            and any(
                getattr(msg, attr, None) is not None
                for attr in ("video", "sticker", "location", "contact", "video_note")
            )
        )

        text = msg.text or getattr(msg, "caption", None) or ""
        is_relayed = False
        # The Telethon relay posts messages with this prefix (no '@' on the handle —
        # intentional, see transport/_telegram_relay.py comment). Detect and strip.
        relay_match = _RELAY_PREFIX_RE.match(text)
        if relay_match:
            is_relayed = True
            text = text[relay_match.end():]

        reply_native = msg.reply_to_message if msg.reply_to_message is not None else None
        reply_to_text = (
            reply_native.text or getattr(reply_native, "caption", None) or None
            if reply_native is not None else None
        )
        reply_to_sender = (
            identity_from_telegram_user(reply_native.from_user)
            if reply_native is not None and getattr(reply_native, "from_user", None) is not None
            else None
        )
        incoming = IncomingMessage(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            text=text,
            files=files,
            reply_to=(
                message_ref_from_telegram(reply_native)
                if reply_native is not None else None
            ),
            native=msg,
            is_relayed_bot_to_bot=is_relayed,
            message=message_ref_from_telegram(msg),
            reply_to_text=reply_to_text,
            reply_to_sender=reply_to_sender,
            has_unsupported_media=has_unsupported_media,
        )
        try:
            for h in self._message_handlers:
                await h(incoming)
        finally:
            for tmpdir in tmpdirs:
                tmpdir.cleanup()

    async def _dispatch_button(self, update: Any, ctx: Any) -> None:
        """Convert a telegram CallbackQuery into ButtonClick and invoke handlers.

        Also answers the query to dismiss the client-side loading spinner.
        """
        query = update.callback_query
        if query is None:
            return
        try:
            await query.answer()
        except Exception:
            pass  # already answered or expired; not fatal
        from .base import ButtonClick
        click = ButtonClick(
            chat=chat_ref_from_telegram(query.message.chat),
            message=message_ref_from_telegram(query.message),
            sender=identity_from_telegram_user(query.from_user),
            value=query.data or "",
            native=(update, ctx),
        )
        for h in self._button_handlers:
            await h(click)

    async def _default_error_handler(self, update: Any, ctx: Any) -> None:
        """Default handler for unhandled telegram update errors.

        Specially treats 'Conflict' errors (another bot instance) as WARNING
        rather than ERROR since they're usually operational, not bugs.
        """
        import logging
        logger_ = logging.getLogger(__name__)
        err = str(ctx.error)
        if "Conflict" in err:
            logger_.warning(
                "Conflict error (another instance?): %s | update=%s", err, update,
            )
        else:
            logger_.error("Update error: %s | update=%s", ctx.error, update)

    async def _dispatch_command(self, name: str, update: Any, ctx: Any) -> None:
        """Convert a telegram command Update into CommandInvocation and invoke the handler."""
        msg = update.effective_message
        user = update.effective_user
        if msg is None or user is None:
            return
        from .base import CommandInvocation
        ci = CommandInvocation(
            chat=chat_ref_from_telegram(msg.chat),
            sender=identity_from_telegram_user(user),
            name=name,
            args=list(getattr(ctx, "args", []) or []),
            raw_text=msg.text or "",
            message=message_ref_from_telegram(msg),
            native=(update, ctx),
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            await handler(ci)

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler) -> None:
        self._command_handlers[name] = handler
        # If routing is already attached, also register a PTB CommandHandler
        # so this command name reaches our dispatcher. Without this, late
        # registrations (e.g., plugin commands wired in _after_ready, which
        # fires AFTER attach_telegram_routing) silently fail — PTB drops the
        # update at the filter level.
        if self._app is not None and self._routing_attached:
            from telegram.ext import CommandHandler as _PTBCommandHandler
            from telegram.ext import filters as _filters

            if self._group_mode_attached:
                chat_filter = _filters.ChatType.GROUPS
            elif self._respond_in_groups_attached:
                chat_filter = _filters.ChatType.PRIVATE | _filters.ChatType.GROUPS
            else:
                chat_filter = _filters.ChatType.PRIVATE
            self._app.add_handler(_PTBCommandHandler(
                name,
                lambda u, c, _n=name: self._dispatch_command(_n, u, c),
                filters=chat_filter,
            ))

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def set_authorizer(self, authorizer: AuthorizerCallback | None) -> None:
        self._authorizer = authorizer
