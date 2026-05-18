from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import secrets
import socket
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.transport.base import (
    ButtonClick,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)

if TYPE_CHECKING:
    from .auth import VerifiedGoogleChatRequest
    from .client import GoogleChatClient

logger = logging.getLogger(__name__)

PROMPT_CANCEL_OPTION = "__cancel__"
PROMPT_TIMEOUT_OPTION = "__timeout__"
SERVER_START_TIMEOUT_SECONDS = 5.0
SERVER_STOP_TIMEOUT_SECONDS = 5.0
EVENT_DRAIN_TIMEOUT_SECONDS = 5.0
PENDING_EVENT_QUEUE_MAX_SIZE = 256


@dataclass
class PendingPrompt:
    prompt: PromptRef
    chat: ChatRef
    sender: Identity | None
    kind: PromptKind
    expires_at: float


def _chat_from_space(space: dict) -> ChatRef:
    space_type = space.get("spaceType") or space.get("type")
    kind = ChatKind.DM if space_type in {"DM", "DIRECT_MESSAGE"} else ChatKind.ROOM
    return ChatRef("google_chat", space["name"], kind)


def _identity_from_user(user: dict) -> Identity:
    return Identity(
        transport_id="google_chat",
        native_id=user["name"],
        display_name=user.get("displayName") or user["name"],
        handle=user.get("email"),
        is_bot=user.get("type") == "BOT",
    )


def _mentions_from_annotations(annotations: list) -> list[Identity]:
    """Extract `Identity` entries from Google Chat `message.annotations`.

    Per spec §4.9, USER_MENTION annotations are the authoritative source for
    @-mention targets — text parsing is fallback-only. Non-USER_MENTION
    annotations (SLASH_COMMAND, RICH_LINK, etc.) and malformed entries are
    silently skipped.
    """
    result: list[Identity] = []
    for annotation in annotations or []:
        if not isinstance(annotation, dict) or annotation.get("type") != "USER_MENTION":
            continue
        user_mention = annotation.get("userMention") or {}
        user = user_mention.get("user") or {}
        name = user.get("name")
        if not name:
            continue
        result.append(Identity(
            transport_id="google_chat",
            native_id=name,
            display_name=user.get("displayName") or name,
            handle=user.get("email"),
            is_bot=user.get("type") == "BOT",
        ))
    return result


def _safe_attachment_name(content_name: object, *, fallback: str = "attachment") -> str:
    raw_name = str(content_name or fallback).replace("\\", "/")
    leaf = raw_name.rsplit("/", 1)[-1].strip()
    if leaf in {"", ".", ".."}:
        return fallback
    return leaf


def _unique_destination(directory: Path, name: str, used_names: set[str]) -> Path:
    candidate = name
    stem = Path(name).stem or "attachment"
    suffix = Path(name).suffix
    counter = 1
    while candidate in used_names:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used_names.add(candidate)
    return directory / candidate


class GoogleChatTransport:
    TRANSPORT_ID = "google_chat"
    transport_id = "google_chat"
    # 8 000 is the conservative *character* budget surfaced to callers
    # via the `max_text_length` capability. The hard *byte* ceiling is
    # `config.max_message_bytes` (default 32 000), enforced at send time
    # by `_check_message_bytes()`. 8 000 characters stays under 32 000
    # bytes even for 4-byte UTF-8 graphemes (emoji / non-BMP), so the
    # character cap can never produce an over-byte payload.
    max_text_length = 8000

    def __init__(
        self,
        *,
        config: GoogleChatConfig,
        client: "GoogleChatClient | None" = None,
        credentials_factory: Callable[[str, tuple[str, ...]], Any] | None = None,
        serve: bool = True,
    ) -> None:
        self.config = config
        self.client = client
        self._credentials_factory = credentials_factory
        self._serve = serve
        self._http = None
        self._consumer_task: asyncio.Task | None = None
        self._server_task: asyncio.Task | None = None
        self._uvicorn_server = None
        self._server_socket: socket.socket | None = None
        self._owns_client = False
        self.self_identity = Identity(
            transport_id="google_chat",
            native_id="google_chat:app",
            display_name="Google Chat App",
            handle=None,
            is_bot=True,
        )
        # Bounded so an unresponsive consumer can't grow memory without
        # bound. The dispatch loop attached in `start()` (or driven directly
        # by `inject_message` / `inject_command` in tests) must drain this
        # faster than events arrive at sustained load; on overflow the
        # incoming event is dropped, counted via `_overflowed_events`, and
        # logged at WARNING.
        self._pending_events: asyncio.Queue = asyncio.Queue(maxsize=PENDING_EVENT_QUEUE_MAX_SIZE)
        self._fast_ack_timeouts: int = 0
        self._overflowed_events: int = 0
        self._message_handlers: list = []
        self._command_handlers: dict[str, object] = {}
        self._button_handlers: list = []
        self._stop_callbacks: list = []
        self._on_ready_callbacks: list = []
        self._authorizer = None
        self._pending_prompts: dict[str, PendingPrompt] = {}
        self._pending_prompt_messages: dict[str, MessageRef] = {}
        self._prompt_submit_handlers: list = []
        self._prompt_seq: int = 0
        self._callback_secret: bytes = secrets.token_bytes(32)
        self._seen_event_cache: OrderedDict[str, float] = OrderedDict()
        self._seen_event_cache_max = 4096
        self._seen_event_ttl_seconds = 600.0

    @property
    def pending_event_count(self) -> int:
        return self._pending_events.qsize()

    @property
    def bound_port(self) -> int:
        """Return the active HTTP port, including the OS-assigned port for 0."""
        if self._server_socket is not None:
            sockname = self._server_socket.getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                return int(sockname[1])
        servers = getattr(self._uvicorn_server, "servers", None)
        if servers:
            for server in servers:
                sockets = getattr(server, "sockets", None) or []
                for sock in sockets:
                    sockname = sock.getsockname()
                    if isinstance(sockname, tuple) and len(sockname) >= 2:
                        return int(sockname[1])
        return int(self.config.port)

    def verify_request(self, headers) -> "VerifiedGoogleChatRequest":
        from .auth import verify_google_chat_request  # noqa: PLC0415

        # Workspace add-on Chat apps sign tokens with the project-scoped
        # gsuiteaddons service account, not the standard `chat@system...`
        # signer. When `project_number` is configured, widen both the
        # endpoint_url signer set and the project_number issuer set so the
        # add-on flow verifies correctly without breaking the standalone path.
        signers = {"chat@system.gserviceaccount.com"}
        issuers = {"chat@system.gserviceaccount.com"}
        if self.config.project_number:
            addon_signer = (
                f"service-{self.config.project_number}@gcp-sa-gsuiteaddons.iam.gserviceaccount.com"
            )
            signers.add(addon_signer)
            issuers.add(addon_signer)

        return verify_google_chat_request(
            headers=headers,
            mode=self.config.auth_audience_type,
            audiences=self._effective_allowed_audiences(),
            accepted_signer_emails=frozenset(signers),
            accepted_issuers=frozenset(issuers),
        )

    def _effective_allowed_audiences(self) -> list[str]:
        if self.config.allowed_audiences:
            return self.config.allowed_audiences
        if self.config.auth_audience_type == "project_number" and self.config.project_number:
            return [self.config.project_number]
        return []

    async def enqueue_verified_event(
        self,
        payload: dict,
        verified: "VerifiedGoogleChatRequest",
        *,
        headers: dict,
    ) -> None:
        try:
            self._pending_events.put_nowait({"payload": payload, "verified": verified, "headers": headers})
        except asyncio.QueueFull:
            self._note_queue_overflow()

    def note_fast_ack_timeout(self) -> None:
        self._fast_ack_timeouts += 1
        logger.warning("Google Chat fast-ack budget exceeded; event dropped (total=%d)", self._fast_ack_timeouts)

    def _note_queue_overflow(self) -> None:
        self._overflowed_events += 1
        logger.warning(
            "Google Chat pending-event queue full (maxsize=%d); event dropped (total=%d)",
            self._pending_events.maxsize,
            self._overflowed_events,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Prepare outbound Google Chat API access and fire readiness hooks."""
        from .validators import validate_google_chat_for_start  # noqa: PLC0415

        validate_google_chat_for_start(self.config)
        should_start_server = self._serve and (self._server_task is None or self._server_task.done())
        try:
            if should_start_server and self._server_socket is None:
                # Prove the bind before on_ready, but do not accept HTTP
                # traffic until callbacks have registered plugins/hooks.
                self._server_socket = self._bind_server_socket()
            if self.client is None:
                from .client import GoogleChatClient  # noqa: PLC0415
                from .credentials import build_google_chat_http_client  # noqa: PLC0415

                self._http = build_google_chat_http_client(
                    self.config,
                    credentials_factory=self._credentials_factory,
                )
                self.client = GoogleChatClient(http=self._http)
                self._owns_client = True
            await self._fire_on_ready()
            if self._consumer_task is None or self._consumer_task.done():
                self._consumer_task = asyncio.create_task(
                    self._consume_events(),
                    name="google-chat-consumer",
                )
            if should_start_server:
                await self._start_server()
        except BaseException:
            await self._cleanup_after_failed_start()
            raise

    async def stop(self) -> None:
        """Stop intake, drain queued work, fire callbacks, and clean up state.

        Google Chat shuts down HTTP intake before plugin callbacks so no new
        inbound events arrive during shutdown. Outbound REST resources stay
        alive until after callbacks so plugins can send final messages.
        """
        await self._stop_server()
        if self._consumer_task is not None:
            try:
                await asyncio.wait_for(self._pending_events.join(), timeout=EVENT_DRAIN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("GoogleChatTransport: timed out draining pending events during stop")
        if self._consumer_task is not None:
            consumer_task = self._consumer_task
            self._consumer_task = None
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        for cb in self._stop_callbacks:
            try:
                result = cb()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("GoogleChatTransport: on_stop callback raised")
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_client:
            self.client = None
            self._owns_client = False

    def run(self) -> None:
        """Synchronous entry point for CLI use. Blocks for the transport lifetime."""
        asyncio.run(self._run_with_lifecycle())

    async def _run_with_lifecycle(self) -> None:
        await self.start()
        try:
            if self._server_task is not None:
                await self._server_task
            else:
                await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _start_server(self) -> None:
        from .app import create_google_chat_app  # noqa: PLC0415

        import uvicorn  # noqa: PLC0415

        bound_socket = self._server_socket
        if bound_socket is None:
            bound_socket = self._bind_server_socket()
        self._server_socket = None
        app = create_google_chat_app(self)
        config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            lifespan="off",
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(
            self._uvicorn_server.serve(sockets=[bound_socket]),
            name="google-chat-uvicorn",
        )
        try:
            async with asyncio.timeout(SERVER_START_TIMEOUT_SECONDS):
                while not self._uvicorn_server.started:
                    if self._server_task.done():
                        try:
                            await self._server_task
                        except BaseException as exc:
                            raise self._server_start_error(exc) from exc
                        raise self._server_start_error()
                    await asyncio.sleep(0.01)
            bound_socket = None
        except TimeoutError as exc:
            await self._stop_server()
            raise self._server_start_error("timed out") from exc
        finally:
            if bound_socket is not None:
                bound_socket.close()

    def _bind_server_socket(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.config.host else socket.AF_INET
        sock = socket.socket(family)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.host, self.config.port))
        except OSError as exc:
            sock.close()
            raise self._server_start_error(exc) from exc
        sock.set_inheritable(True)
        return sock

    def _server_start_error(self, reason: object | None = None) -> RuntimeError:
        message = f"Failed to start Google Chat HTTP server on {self.config.host}:{self.config.port}"
        if reason is not None:
            message = f"{message}: {reason}"
        return RuntimeError(message)

    async def _stop_server(self) -> None:
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            server_task = self._server_task
            self._server_task = None
            try:
                await asyncio.wait_for(server_task, timeout=SERVER_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                server_task.cancel()
                try:
                    await server_task
                except BaseException:
                    pass
            except BaseException:
                pass
        self._uvicorn_server = None

    async def _cleanup_after_failed_start(self) -> None:
        await self._stop_server()
        if self._consumer_task is not None:
            consumer_task = self._consumer_task
            self._consumer_task = None
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_client:
            self.client = None
            self._owns_client = False

    def on_stop(self, callback) -> None:
        self._stop_callbacks.append(callback)

    def on_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

    async def _fire_on_ready(self) -> None:
        for cb in self._on_ready_callbacks:
            try:
                result = cb(self.self_identity)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("GoogleChatTransport: on_ready callback raised")

    # ── Inbound registration ──────────────────────────────────────────────

    def on_message(self, handler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler) -> None:
        self._button_handlers.append(handler)

    def set_authorizer(self, authorizer) -> None:
        self._authorizer = authorizer

    # ── Event dispatch ────────────────────────────────────────────────────

    async def dispatch_event(self, payload: dict) -> None:
        # Workspace add-on Chat apps send a nested envelope with
        # `commonEventObject` / `authorizationEventObject` / `chat`, where the
        # actual event is at `chat.{messagePayload,appCommandPayload,...}`.
        # Standalone Chat apps send a flat `{type, space, user, message, ...}`.
        # Detect and rewrite to the flat shape so the rest of the dispatcher
        # (and downstream handlers / fixtures) keep working.
        payload = self._maybe_unwrap_addon_envelope(payload)
        event_type = payload.get("type")
        if event_type in {"MESSAGE", "APP_COMMAND", "CARD_CLICKED"}:
            key = self._event_idempotency_key(payload)
            if key is not None and self._seen_event(key):
                logger.debug("GoogleChatTransport: suppressing duplicate event type=%r", event_type)
                return
        if event_type == "MESSAGE":
            await self._dispatch_message(payload)
        elif event_type == "APP_COMMAND":
            await self._dispatch_app_command(payload)
        elif event_type == "CARD_CLICKED":
            await self._dispatch_card_clicked(payload)
        else:
            # [TRACE v2] Promote to WARNING so we see card-click drops in
            # journald. Strip alongside the addon-envelope diagnostic.
            logger.warning(
                "[TRACE-v2] dropping event type=%r, payload.keys=%s",
                event_type,
                sorted(payload.keys()) if isinstance(payload, dict) else "non-dict",
            )

    def _maybe_unwrap_addon_envelope(self, payload: dict) -> dict:
        """Rewrite a Workspace-add-on envelope to the standalone Chat-app shape."""
        # [TRACE v2] Capture full payload key shape for every add-on event so
        # we can identify the missing card-click payload key. Strip once the
        # CARD_CLICKED dispatch bug is fixed.
        if "commonEventObject" in payload:
            chat_for_log = payload.get("chat") or {}
            common_for_log = payload.get("commonEventObject") or {}
            logger.warning(
                "[TRACE-v2] addon envelope: payload.keys=%s, "
                "chat.keys=%s, "
                "commonEventObject.keys=%s, "
                "commonEventObject.parameters=%s, "
                "commonEventObject.invokedFunction=%s",
                sorted(payload.keys()),
                sorted(chat_for_log.keys()) if isinstance(chat_for_log, dict) else f"non-dict:{type(chat_for_log).__name__}",
                sorted(common_for_log.keys()) if isinstance(common_for_log, dict) else f"non-dict:{type(common_for_log).__name__}",
                common_for_log.get("parameters") if isinstance(common_for_log, dict) else None,
                common_for_log.get("invokedFunction") if isinstance(common_for_log, dict) else None,
            )
        if "commonEventObject" not in payload:
            return payload
        chat = payload.get("chat") or {}
        if not isinstance(chat, dict):
            return payload
        common_user = chat.get("user") or {}
        common_time = chat.get("eventTime")
        if "messagePayload" in chat:
            mp = chat.get("messagePayload") or {}
            return {
                "type": "MESSAGE",
                "eventTime": mp.get("eventTime") or common_time,
                "space": mp.get("space", {}),
                "user": mp.get("user") or common_user,
                "message": mp.get("message", {}),
            }
        if "appCommandPayload" in chat:
            ap = chat.get("appCommandPayload") or {}
            return {
                "type": "APP_COMMAND",
                "eventTime": ap.get("eventTime") or common_time,
                "space": ap.get("space", {}),
                "user": ap.get("user") or common_user,
                "message": ap.get("message", {}),
                "appCommandMetadata": ap.get("appCommandMetadata", {}),
            }
        if "buttonClickedPayload" in chat:
            bp = chat.get("buttonClickedPayload") or {}
            # [TRACE] Diagnostic: capture add-on CARD_CLICKED payload shape so
            # we can confirm where Google routes the callback_token in the
            # nested envelope. Strip once the dispatch bug is fixed.
            common_event = payload.get("commonEventObject") or {}
            logger.warning(
                "[TRACE] addon CARD_CLICKED unwrap: bp.keys=%s, bp.action.keys=%s, "
                "bp.common.keys=%s, commonEventObject.keys=%s, "
                "commonEventObject.parameters.keys=%s, commonEventObject.formInputs.keys=%s",
                sorted(bp.keys()),
                sorted((bp.get("action") or {}).keys()),
                sorted((bp.get("common") or {}).keys()),
                sorted(common_event.keys()),
                sorted((common_event.get("parameters") or {}).keys())
                if isinstance(common_event.get("parameters"), dict)
                else f"non-dict:{type(common_event.get('parameters')).__name__}",
                sorted((common_event.get("formInputs") or {}).keys())
                if isinstance(common_event.get("formInputs"), dict)
                else "absent",
            )
            return {
                "type": "CARD_CLICKED",
                "eventTime": bp.get("eventTime") or common_time,
                "space": bp.get("space", {}),
                "user": bp.get("user") or common_user,
                "message": bp.get("message", {}),
                "action": bp.get("action", {}),
                "common": bp.get("common", {}),
                # [TRACE] Surface the outer commonEventObject too so the
                # dispatcher diagnostic can see it.
                "_commonEventObject": common_event,
            }
        return payload

    async def _consume_events(self) -> None:
        while True:
            envelope = await self._pending_events.get()
            try:
                await self.dispatch_event(envelope["payload"])
            except Exception:
                logger.exception("GoogleChatTransport: queued event dispatch failed")
            finally:
                self._pending_events.task_done()

    def _event_idempotency_key(self, payload: dict) -> str | None:
        parts = {
            "type": payload.get("type"),
            "eventTime": payload.get("eventTime"),
            "space.name": payload.get("space", {}).get("name"),
            "message.name": payload.get("message", {}).get("name"),
            "user.name": payload.get("user", {}).get("name"),
        }
        if all(value is None for value in parts.values()):
            return None
        encoded = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _seen_event(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._seen_event_ttl_seconds
        while self._seen_event_cache:
            oldest_key, oldest_seen_at = next(iter(self._seen_event_cache.items()))
            if oldest_seen_at >= cutoff:
                break
            self._seen_event_cache.pop(oldest_key)
        seen_at = self._seen_event_cache.get(key)
        if seen_at is not None:
            if seen_at < cutoff:
                self._seen_event_cache.pop(key, None)
            else:
                self._seen_event_cache[key] = now
                self._seen_event_cache.move_to_end(key)
                return True
        self._seen_event_cache[key] = now
        while len(self._seen_event_cache) > self._seen_event_cache_max:
            self._seen_event_cache.popitem(last=False)
        return False

    def _maybe_learn_self_identity(self, mentions: list[Identity]) -> None:
        """Adopt the bot's real identity from a USER_MENTION when unambiguous.

        The default ``self_identity`` is a placeholder sentinel
        (``google_chat:app``, ``handle=None``) because Google Chat has no
        get_me() equivalent. Google delivers MESSAGE events only to apps
        targeted by the user, so when a payload contains exactly one BOT-type
        USER_MENTION that bot must be us — record its ``users/<id>`` and
        display name so room routing can match by ID. Multi-bot annotations
        are ambiguous and skipped; once learned, the identity is sticky.
        """
        if self.self_identity.native_id != "google_chat:app":
            return
        bot_mentions = [m for m in mentions if m.is_bot]
        if len(bot_mentions) != 1:
            return
        self.self_identity = bot_mentions[0]

    async def _dispatch_message(self, payload: dict) -> None:
        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                logger.debug("GoogleChatTransport: authorizer rejected sender=%r", sender)
                return
        message_data = payload["message"]
        text = message_data.get("text", "")
        thread_name = message_data.get("thread", {}).get("name")
        message = MessageRef(
            "google_chat",
            message_data["name"],
            chat,
            native={"thread_name": thread_name} if thread_name else {},
        )
        mentions = _mentions_from_annotations(message_data.get("annotations", []))
        self._maybe_learn_self_identity(mentions)
        tempdir: tempfile.TemporaryDirectory | None = None
        try:
            files: list[IncomingFile] = []
            has_unsupported_media = False
            used_names: set[str] = set()
            for attachment in message_data.get("attachment", []):
                data_ref = attachment.get("attachmentDataRef")
                resource_name = data_ref.get("resourceName") if isinstance(data_ref, dict) else None
                if "driveDataRef" in attachment or not resource_name or self.client is None:
                    has_unsupported_media = True
                    continue

                if tempdir is None:
                    tempdir = tempfile.TemporaryDirectory(prefix="lptc-google-chat-")
                original_name = _safe_attachment_name(attachment.get("contentName"))
                destination = _unique_destination(Path(tempdir.name), original_name, used_names)
                try:
                    await self.client.download_attachment(
                        resource_name,
                        destination,
                        max_bytes=self.config.attachment_max_bytes,
                    )
                except Exception:
                    logger.exception("GoogleChatTransport: attachment download failed")
                    has_unsupported_media = True
                    continue
                files.append(
                    IncomingFile(
                        path=destination,
                        original_name=original_name,
                        mime_type=attachment.get("contentType"),
                        size_bytes=destination.stat().st_size,
                    ),
                )

            msg = IncomingMessage(
                chat=chat,
                sender=sender,
                text=text,
                files=[] if has_unsupported_media else files,
                reply_to=None,
                message=message,
                has_unsupported_media=has_unsupported_media,
                mentions=mentions,
            )
            for handler in self._message_handlers:
                result = handler(msg)
                if inspect.isawaitable(result):
                    await result
        finally:
            if tempdir is not None:
                tempdir.cleanup()

    async def _dispatch_app_command(self, payload: dict) -> None:
        app_command_id = payload["appCommandMetadata"]["appCommandId"]
        if self.config.root_command_id is None or app_command_id != self.config.root_command_id:
            logger.debug(
                "GoogleChatTransport: ignoring appCommandId=%d (root_command_id=%s)",
                app_command_id,
                self.config.root_command_id,
            )
            return

        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return

        message_data = payload["message"]
        raw_text = message_data.get("text", "")
        thread_name = message_data.get("thread", {}).get("name")
        message = MessageRef(
            "google_chat",
            message_data["name"],
            chat,
            native={"thread_name": thread_name} if thread_name else {},
        )

        tokens = raw_text.split()
        # tokens[0] is the slash command name (e.g. "/lp2c"), tokens[1] is the subcommand
        name = tokens[1] if len(tokens) > 1 else ""
        args = tokens[2:] if len(tokens) > 2 else []

        ci = CommandInvocation(
            chat=chat,
            sender=sender,
            name=name,
            args=args,
            raw_text=raw_text,
            message=message,
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            result = handler(ci)
            if inspect.isawaitable(result):
                await result

    async def _dispatch_card_clicked(self, payload: dict) -> None:
        from .cards import CallbackTokenError, verify_callback_token  # noqa: PLC0415

        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return

        action = payload.get("action", {})
        params = {param.get("key"): param.get("value") for param in action.get("parameters", [])}
        common = payload.get("common", {})
        common_params = common.get("parameters", {})
        if isinstance(common_params, dict):
            params.update(common_params)
        token = params.get("callback_token")
        if not token:
            # [TRACE] Diagnostic: capture where the token actually lives so we
            # can fix the unwrap. Strip once the dispatch bug is fixed.
            outer_common = payload.get("_commonEventObject") or {}
            outer_params = outer_common.get("parameters") or {}
            outer_form = outer_common.get("formInputs") or {}
            logger.warning(
                "[TRACE] CARD_CLICKED no token in action/common. "
                "action.parameters=%s, common.parameters=%s, "
                "outer commonEventObject.parameters=%s, formInputs.keys=%s",
                action.get("parameters"),
                common_params,
                outer_params,
                sorted(outer_form.keys()) if isinstance(outer_form, dict) else "non-dict",
            )
            logger.warning("CARD_CLICKED missing callback_token; dropping")
            return
        try:
            verified = verify_callback_token(
                secret=self._callback_secret,
                token=token,
                now=int(time.time()),
            )
        except CallbackTokenError as exc:
            logger.warning("CARD_CLICKED callback_token rejected: %s", exc)
            return

        if verified.get("space") != chat.native_id:
            logger.warning("CARD_CLICKED callback_token bound to a different space; dropping")
            return

        kind = verified.get("kind")
        value = verified.get("value")
        if kind == "button":
            message = MessageRef(
                transport_id="google_chat",
                native_id=payload["message"]["name"],
                chat=chat,
            )
            click = ButtonClick(
                chat=chat,
                message=message,
                sender=sender,
                value=value or "",
                native=payload,
            )
            for handler in self._button_handlers:
                result = handler(click)
                if inspect.isawaitable(result):
                    await result
        elif kind == "prompt":
            prompt_id = verified.get("prompt_id")
            pending = self._pending_prompts.get(prompt_id)
            if pending is None:
                logger.debug("CARD_CLICKED prompt_id=%r not pending; dropping", prompt_id)
                return
            expected_sender = verified.get("sender")
            if expected_sender and expected_sender != sender.native_id:
                logger.warning("CARD_CLICKED prompt sender mismatch; dropping")
                return
            if pending.sender is not None and pending.sender.native_id != sender.native_id:
                logger.warning("CARD_CLICKED pending prompt sender mismatch; dropping")
                return
            if pending.expires_at < time.monotonic():
                self._pending_prompts.pop(prompt_id, None)
                self._pending_prompt_messages.pop(prompt_id, None)
                logger.debug("CARD_CLICKED prompt_id=%r expired; dropping", prompt_id)
                return
            self._pending_prompts.pop(prompt_id, None)
            self._pending_prompt_messages.pop(prompt_id, None)
            form_field = params.get("form_field")
            if form_field:
                text = self._extract_form_input(payload, form_field)
                await self.inject_prompt_reply(pending.prompt, sender=sender, text=text)
            else:
                await self.inject_prompt_reply(pending.prompt, sender=sender, option=value)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _new_request_id(self) -> str:
        return f"lp2c-{uuid4().hex}"

    def _check_message_bytes(self, text: str) -> None:
        byte_len = len(text.encode("utf-8"))
        if byte_len > self.config.max_message_bytes:
            raise ValueError(
                f"Message exceeds max_message_bytes limit: {byte_len} > {self.config.max_message_bytes}"
            )

    def render_markdown(self, text: str) -> str:
        """Translate Telegram-flavored HTML and CommonMark markdown into
        Google Chat's text format.

        Google Chat supports:
        - ``*bold*`` (single asterisks; ``**double**`` is literal)
        - ``_italic_`` (single underscores)
        - ``~strikethrough~`` (single tildes)
        - ``` `inline code` ``` (single backticks)
        - ``` ```fenced code``` ``` (triple backticks)
        - ``<URL>`` autolink, ``<URL|displayed text>`` named link
        - Bullet lists (``-`` or ``*`` at line start)

        Google Chat does NOT support tables, headers, blockquotes, or
        HTML tags. Those degrade to readable plain text. This function
        is best-effort idempotent: content already in Google Chat
        format passes through with minor formatting normalization.
        """
        import re as _re

        # 1. Lift fenced code blocks out so subsequent regex passes
        #    don't munge their contents. Handle Telegram HTML
        #    ``<pre><code class="language-X">...</code></pre>`` and
        #    plain ``<pre>...</pre>``.
        code_blocks: list[str] = []

        def _stash_block(lang: str, body: str) -> str:
            # Unescape HTML entities inside the code block.
            body = (
                body.replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&amp;", "&")
            )
            fence = f"```{lang}\n{body}\n```" if lang else f"```\n{body}\n```"
            idx = len(code_blocks)
            code_blocks.append(fence)
            return f"\x00CB{idx}\x00"

        text = _re.sub(
            r'<pre><code class="language-(\w+)">(.+?)</code></pre>',
            lambda m: _stash_block(m.group(1), m.group(2)),
            text,
            flags=_re.DOTALL,
        )
        text = _re.sub(
            r"<pre>(.+?)</pre>",
            lambda m: _stash_block("", m.group(1)),
            text,
            flags=_re.DOTALL,
        )

        # 2. Inline code: <code>...</code> → `...`
        text = _re.sub(
            r"<code>(.+?)</code>",
            lambda m: "`"
            + m.group(1)
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&amp;", "&")
            + "`",
            text,
        )

        # 3. Markdown headers → bold (Google Chat has no header syntax).
        text = _re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=_re.MULTILINE)

        # 4. Markdown tables → plain rows. Strip the divider row, keep
        #    pipes as visual separators. Google Chat doesn't render
        #    tables but the rows stay scannable.
        def _flatten_table(m: _re.Match) -> str:
            lines = m.group(0).strip().splitlines()
            kept = [ln for ln in lines if not _re.fullmatch(r"\|[\s\-:|]+\|", ln.strip())]
            # Trim leading/trailing pipes for readability.
            return "\n".join(ln.strip().strip("|").strip() for ln in kept)

        text = _re.sub(
            r"(?:^\|.+\|[ \t]*\n){2,}",
            _flatten_table,
            text,
            flags=_re.MULTILINE,
        )

        # 5. Telegram HTML bold/italic/strike → Google Chat marks.
        text = _re.sub(r"<b>(.+?)</b>", r"*\1*", text, flags=_re.DOTALL)
        text = _re.sub(r"<strong>(.+?)</strong>", r"*\1*", text, flags=_re.DOTALL)
        text = _re.sub(r"<i>(.+?)</i>", r"_\1_", text, flags=_re.DOTALL)
        text = _re.sub(r"<em>(.+?)</em>", r"_\1_", text, flags=_re.DOTALL)
        text = _re.sub(r"<s>(.+?)</s>", r"~\1~", text, flags=_re.DOTALL)
        text = _re.sub(r"<del>(.+?)</del>", r"~\1~", text, flags=_re.DOTALL)

        # 6. Markdown bold/strike that may have leaked through
        #    (non-html=True call sites).
        text = _re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=_re.DOTALL)
        text = _re.sub(r"__(.+?)__", r"*\1*", text, flags=_re.DOTALL)
        text = _re.sub(r"~~(.+?)~~", r"~\1~", text, flags=_re.DOTALL)

        # 7. Markdown links [text](url) → Google Chat <url|text>.
        text = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
        # Telegram HTML anchors → Google Chat <url|text>.
        text = _re.sub(
            r'<a\s+href="([^"]+)">(.+?)</a>',
            r"<\1|\2>",
            text,
            flags=_re.DOTALL,
        )

        # 8. Blockquotes: Google Chat has no native support. Keep the
        #    body, drop the tag — Telegram-shaped or markdown-shaped.
        text = _re.sub(r"<blockquote>(.+?)</blockquote>", r"\1", text, flags=_re.DOTALL)
        text = _re.sub(r"^&gt;\s?", "", text, flags=_re.MULTILINE)
        text = _re.sub(r"^>\s?", "", text, flags=_re.MULTILINE)

        # 9. Unescape any remaining HTML entities (md_to_telegram used
        #    _escape_html on body text outside code blocks).
        text = (
            text.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&amp;", "&")
        )

        # 10. Re-insert fenced code blocks.
        for i, block in enumerate(code_blocks):
            text = text.replace(f"\x00CB{i}\x00", block)

        return text

    def _extract_form_input(self, payload: dict, form_field: str) -> str:
        value = (
            payload.get("common", {})
            .get("formInputs", {})
            .get(form_field, {})
            .get("stringInputs", {})
            .get("value", [])
        )
        if not value:
            return ""
        return str(value[0])

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send_typing(self, chat: ChatRef) -> None:
        # Google Chat REST has no typing-indicator endpoint. Implementing as a
        # no-op satisfies the Transport protocol so `ProjectBot._on_task_started`
        # doesn't spam best-effort failures.
        return None

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons=None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        # Always run the renderer — Google Chat needs the conversion
        # whether the caller passed Telegram HTML (html=True) or raw
        # markdown (html=False). The renderer is idempotent for content
        # already in Google Chat format.
        rendered = self.render_markdown(text)
        self._check_message_bytes(rendered)
        request_id = self._new_request_id()
        body = {"text": rendered}
        if buttons is not None:
            from .cards import build_buttons_card  # noqa: PLC0415

            body.update(
                build_buttons_card(
                    buttons,
                    secret=self._callback_secret,
                    space=chat.native_id,
                    sender="",
                    message=request_id,
                    now=int(time.time()),
                    ttl_seconds=self.config.callback_token_ttl_seconds,
                )
            )
        native: dict[str, object] = {}
        if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
            native["thread_name"] = reply_to.native["thread_name"]
        result = await self.client.create_message(
            chat.native_id,
            body,
            thread_name=native.get("thread_name"),
            request_id=request_id,
        )
        native["request_id"] = request_id
        native["message_name"] = result["name"]
        native["is_app_created"] = True
        return MessageRef("google_chat", result["name"], chat, native=native)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons=None,
        html: bool = False,
    ) -> None:
        # See send_text — render unconditionally for Google Chat parity.
        rendered = self.render_markdown(text)
        self._check_message_bytes(rendered)
        if isinstance(msg.native, dict) and msg.native.get("is_app_created") is False:
            return
        await self.client.update_message(msg.native_id, {"text": rendered}, update_mask="text", allow_missing=False)

    async def send_file(
        self,
        chat,
        path,
        *,
        caption=None,
        display_name=None,
        reply_to: MessageRef | None = None,
    ):
        # `GoogleChatClient.upload_attachment` exists for callers wiring
        # a user-authenticated HTTP client (see its docstring); under the
        # default service-account `chat.bot` auth the `media.upload`
        # endpoint 403s, so v1 surfaces a text fallback instead of
        # attempting (and failing) the upload.
        file_name = display_name or Path(path).name
        fallback = f"[Google Chat file upload is not available with app authentication: {file_name}]"
        text = f"{caption}\n\n{fallback}" if caption else fallback
        return await self.send_text(chat, text, reply_to=reply_to)

    async def send_voice(self, chat, path, *, reply_to=None):
        return await self.send_file(chat, path, display_name=Path(path).name, reply_to=reply_to)

    # ── Prompt support ────────────────────────────────────────────────────

    def on_prompt_submit(self, handler) -> None:
        self._prompt_submit_handlers.append(handler)

    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
        expected_sender_native_id: str | None = None,
    ) -> PromptRef:
        prompt_id = f"p-{self._prompt_seq}"
        self._prompt_seq += 1
        ref = PromptRef(
            transport_id="google_chat",
            native_id=prompt_id,
            chat=chat,
            key=spec.key,
        )
        expires_at = time.monotonic() + self.config.pending_prompt_ttl_seconds
        self._pending_prompts[prompt_id] = PendingPrompt(
            prompt=ref,
            chat=chat,
            sender=(
                Identity("google_chat", expected_sender_native_id, expected_sender_native_id, None, False)
                if expected_sender_native_id is not None
                else None
            ),
            kind=spec.kind,
            expires_at=expires_at,
        )
        if self.client is None:
            return ref
        if spec.kind is PromptKind.DISPLAY:
            self._pending_prompt_messages[prompt_id] = await self.send_text(chat, spec.body, reply_to=reply_to)
            return ref

        request_id = self._new_request_id()
        body = self._build_prompt_message_body(
            prompt_id=prompt_id,
            spec=spec,
            chat=chat,
            expected_sender_native_id=expected_sender_native_id,
        )
        native: dict[str, object] = {}
        if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
            native["thread_name"] = reply_to.native["thread_name"]
        result = await self.client.create_message(
            chat.native_id,
            body,
            thread_name=native.get("thread_name"),
            request_id=request_id,
        )
        native["request_id"] = request_id
        native["message_name"] = result["name"]
        native["is_app_created"] = True
        self._pending_prompt_messages[prompt_id] = MessageRef("google_chat", result["name"], chat, native=native)
        return ref

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        msg = self._pending_prompt_messages.get(prompt.native_id)
        if msg is None:
            return
        pending = self._pending_prompts.get(prompt.native_id)
        was_posted_with_cards = pending is not None and pending.kind is not PromptKind.DISPLAY
        if self.client is None:
            return
        if spec.kind is PromptKind.DISPLAY and not was_posted_with_cards:
            await self.edit_text(msg, spec.body)
            if pending is not None:
                pending.kind = spec.kind
            return
        if spec.kind is PromptKind.DISPLAY:
            self._check_message_bytes(spec.body)
            await self.client.update_message(
                msg.native_id,
                {"text": spec.body, "cardsV2": []},
                update_mask="text,cardsV2",
                allow_missing=False,
            )
            if pending is not None:
                pending.kind = spec.kind
            return

        expected_sender_native_id = pending.sender.native_id if pending and pending.sender is not None else None
        body = self._build_prompt_message_body(
            prompt_id=prompt.native_id,
            spec=spec,
            chat=prompt.chat,
            expected_sender_native_id=expected_sender_native_id,
        )
        self._check_message_bytes(body["text"])
        await self.client.update_message(
            msg.native_id,
            body,
            update_mask="text,cardsV2",
            allow_missing=False,
        )
        if pending is not None:
            pending.kind = spec.kind

    def _build_prompt_message_body(
        self,
        *,
        prompt_id: str,
        spec: PromptSpec,
        chat: ChatRef,
        expected_sender_native_id: str | None,
    ) -> dict:
        from .cards import build_prompt_card  # noqa: PLC0415

        body = {"text": spec.body}
        body.update(
            build_prompt_card(
                spec,
                secret=self._callback_secret,
                space=chat.native_id,
                prompt_id=prompt_id,
                expected_sender_native_id=expected_sender_native_id,
                now=int(time.time()),
                ttl_seconds=self.config.callback_token_ttl_seconds,
            )
        )
        return body

    async def close_prompt(
        self,
        prompt: PromptRef,
        *,
        final_text: str | None = None,
    ) -> None:
        self._pending_prompts.pop(prompt.native_id, None)
        self._pending_prompt_messages.pop(prompt.native_id, None)

    async def inject_prompt_reply(
        self,
        prompt: PromptRef,
        *,
        sender: Identity,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        """Test helper: synthesize a PromptSubmission and dispatch to handlers."""
        submission = PromptSubmission(
            chat=prompt.chat,
            sender=sender,
            prompt=prompt,
            text=text,
            option=option,
        )
        for handler in self._prompt_submit_handlers:
            result = handler(submission)
            if inspect.isawaitable(result):
                await result

    async def inject_prompt_submit(
        self,
        prompt: PromptRef,
        sender: Identity,
        *,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        """Contract-test alias for inject_prompt_reply (same semantics)."""
        await self.inject_prompt_reply(prompt, sender=sender, text=text, option=option)

    async def inject_message(
        self,
        chat: "ChatRef",
        sender: "Identity",
        text: str,
        *,
        files=None,
        reply_to=None,
        mentions=None,
    ) -> None:
        """Test helper: synthesize an IncomingMessage and dispatch to handlers.

        Bypasses HTTP auth — for use in contract tests only. Respects the
        registered authorizer so the authorizer contract tests work correctly.
        """
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return
        msg_ref = MessageRef(
            transport_id="google_chat",
            native_id="test-msg-001",
            chat=chat,
        )
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=files or [],
            reply_to=reply_to,
            message=msg_ref,
            has_unsupported_media=False,
            mentions=mentions or [],
        )
        for handler in self._message_handlers:
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

    async def inject_command(
        self,
        chat: "ChatRef",
        sender: "Identity",
        name: str,
        *,
        args: list,
        raw_text: str,
    ) -> None:
        """Test helper: synthesize a CommandInvocation and dispatch to handlers."""
        from link_project_to_chat.transport.base import CommandInvocation as _CI  # noqa: PLC0415
        msg_ref = MessageRef(
            transport_id="google_chat",
            native_id="test-cmd-001",
            chat=chat,
        )
        ci = _CI(
            chat=chat,
            sender=sender,
            name=name,
            args=args,
            raw_text=raw_text,
            message=msg_ref,
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            result = handler(ci)
            if inspect.isawaitable(result):
                await result
