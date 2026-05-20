"""Transport Protocol and primitive types.

See docs/superpowers/specs/2026-04-20-transport-abstraction-design.md section 4.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ChatKind(Enum):
    DM = "dm"
    ROOM = "room"


@dataclass(frozen=True)
class ChatRef:
    """Opaque reference to a conversation target."""
    transport_id: str
    native_id: str
    kind: ChatKind


@dataclass(frozen=True)
class Identity:
    """Who sent a message. Transport-agnostic."""
    transport_id: str
    native_id: str
    display_name: str
    handle: str | None
    is_bot: bool


@dataclass(frozen=True)
class MessageRef:
    """Opaque reference to a sent message."""
    transport_id: str
    native_id: str
    chat: ChatRef
    native: Any = field(default=None, compare=False, hash=False, repr=False)


class ButtonStyle(Enum):
    DEFAULT = "default"
    PRIMARY = "primary"
    DANGER = "danger"


@dataclass(frozen=True)
class Button:
    label: str
    value: str
    style: ButtonStyle = ButtonStyle.DEFAULT


@dataclass(frozen=True)
class Buttons:
    rows: list[list[Button]]


@dataclass(frozen=True)
class ButtonClick:
    chat: ChatRef
    message: MessageRef
    sender: Identity
    value: str
    native: Any = None


@dataclass(frozen=True)
class IncomingFile:
    """An attachment already downloaded to local disk.

    Lifetime: cleaned up by the Transport after the IncomingMessage handler returns.
    """
    path: Path
    original_name: str
    mime_type: str | None
    size_bytes: int


@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    message: MessageRef  # Required: every transport-emitted message has a back-ref.
    native: Any = None
    is_relayed_bot_to_bot: bool = False
    # Optional platform-neutral fields populated by transports that have them.
    # `reply_to_text`/`reply_to_sender` let group-routing and
    # "[Replying to: ...]" prefixing stay transport-free.
    reply_to_text: str | None = None
    reply_to_sender: Identity | None = None
    has_unsupported_media: bool = False  # True if the platform delivered a
    # video/sticker/location/contact/video-note that the transport can't decode.
    # Bot SHOULD reject with a "media type not supported" reply rather than
    # treating any caption as a normal prompt.
    mentions: list[Identity] = field(default_factory=list)
    # Identities @-mentioned in this message. Lets the bot route group
    # messages without parsing platform-specific entity offsets.


@dataclass(frozen=True)
class CommandInvocation:
    chat: ChatRef
    sender: Identity
    name: str
    args: list[str]
    raw_text: str
    message: MessageRef
    native: Any = None


class PromptKind(Enum):
    DISPLAY = "display"
    TEXT = "text"
    SECRET = "secret"
    CHOICE = "choice"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class PromptOption:
    value: str
    label: str
    description: str | None = None
    style: ButtonStyle = ButtonStyle.DEFAULT


@dataclass(frozen=True)
class PromptSpec:
    key: str
    title: str
    body: str
    kind: PromptKind
    placeholder: str = ""
    initial_text: str = ""
    submit_label: str = "Continue"
    allow_cancel: bool = True
    options: list[PromptOption] = field(default_factory=list)


@dataclass(frozen=True)
class PromptRef:
    transport_id: str
    native_id: str
    chat: ChatRef
    key: str


@dataclass(frozen=True)
class PromptSubmission:
    chat: ChatRef
    sender: Identity
    prompt: PromptRef
    text: str | None = None
    option: str | None = None
    native: Any = None


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]
CommandHandler = Callable[[CommandInvocation], Awaitable[None]]
ButtonHandler = Callable[[ButtonClick], Awaitable[None]]
OnReadyCallback = Callable[["Identity"], Awaitable[None]]
AuthorizerCallback = Callable[[Identity], Awaitable[bool]]
PromptHandler = Callable[[PromptSubmission], Awaitable[None]]


class TransportRetryAfter(Exception):
    """Raised when a Transport's underlying platform asks the caller to back off.

    Transports should catch platform-specific rate-limit errors internally and
    re-raise as this if the caller needs to know (e.g., for throttle adaptation).
    `retry_after` is in seconds.
    """

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after}s")
        self.retry_after = retry_after


class Transport(Protocol):
    """A concrete chat platform. See spec #0 for implementation rules.

    `html` on send_text/edit_text is a portable rich-text hint: when True, the
    text contains platform-agnostic HTML-like markup (a subset telegram supports
    natively — see formatting.md_to_telegram). Transports that don't natively
    render HTML strip or convert it. Required floor: every Transport MUST accept
    html=True without error, even if the rendering degrades.

    `reply_to` on send_text attaches the new message as a reply to an earlier
    one. Transports that don't support thread-style replies MAY ignore the hint.
    """

    max_text_length: int  # Largest single-message text length the platform accepts.

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def run(self) -> None:
        """Synchronously run the transport's main loop until cancelled.

        Implementations own their event loop. PTB's Application.run_polling()
        is sync and creates its own loop; async-native transports (Discord
        client.start, uvicorn.serve) wrap with asyncio.run inside this method.
        Returns when the transport stops.
        """
        ...

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef: ...

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None: ...

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef: ...

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        """Send an audio file as a platform-appropriate voice message.

        Telegram renders as a voice note with waveform UI; Discord as an audio
        attachment; other transports render per their platform conventions.
        Transports that don't distinguish voice from file MAY delegate to send_file.
        """
        ...

    async def send_typing(self, chat: ChatRef) -> None:
        """Emit a typing indicator. One-shot; caller loops if a sustained
        indicator is needed. Transports that don't support it MAY no-op.
        """
        ...

    def on_message(self, handler: MessageHandler) -> None: ...
    def on_command(self, name: str, handler: CommandHandler) -> None: ...
    def on_button(self, handler: ButtonHandler) -> None: ...

    def set_authorizer(self, authorizer: AuthorizerCallback | None) -> None:
        """Pre-message authorization gate. Called by the transport BEFORE any
        expensive platform work (file downloads, etc.). Returning False causes
        the transport to silently drop the message — no handlers, no downloads.

        This is a DoS-defense layer; the bot SHOULD still re-auth in its message
        handlers as defense-in-depth. Pass None to disable gating.
        """
        ...

    def on_ready(self, callback: OnReadyCallback) -> None:
        """Register a callback fired after the Transport completes platform-specific
        startup (e.g., identity discovery, menu registration). Called once per process
        with the bot's own Identity as argument.
        """
        ...

    async def set_command_menu(self, commands: list[tuple[str, str]]) -> None:
        """Replace the platform's slash-command autocomplete/menu if supported.

        Called after late command registration, such as plugin commands loaded
        during on_ready. Transports without a platform command menu MAY no-op.
        """
        ...

    def on_stop(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback fired during the Transport's shutdown sequence,
        BEFORE the platform actually tears down. Plugins use this to release
        resources, send a final message, etc. Multiple callbacks fire in
        registration order; exceptions are logged but do not block other
        callbacks or the transport shutdown.
        """
        ...

    def render_markdown(self, md: str) -> str:
        """Render markdown into the platform's native rich-text dialect.

        Synchronous — this must never do I/O. The returned string is what the
        Transport will accept when `send_text(html=True)` is called; the
        `StreamingMessage` helper uses it to pretty-print the sealed head on
        overflow. Transports that can't render rich text MAY return the input
        unchanged.
        """
        ...

    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef: ...

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None: ...

    async def close_prompt(
        self,
        prompt: PromptRef,
        *,
        final_text: str | None = None,
    ) -> None: ...

    def on_prompt_submit(self, handler: PromptHandler) -> None: ...
