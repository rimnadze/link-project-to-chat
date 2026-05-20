"""Transport-agnostic streaming-edit helper.

Owns one editable message and throttles updates. The bot calls `append(delta)`
as Claude's deltas arrive; this class handles rate-limiting, HTML rendering on
finalize, overflow rotation into new messages, and back-off when the transport
signals RetryAfter. Rich rendering goes through `transport.render_markdown` so
this module stays platform-free.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .base import ChatRef, MessageRef, Transport, TransportRetryAfter

logger = logging.getLogger(__name__)

_DEFAULT_THROTTLE = 1.2  # seconds between edits per message
_DEFAULT_MAX_CHARS = 3800  # Telegram hard cap is 4096; leave room for prefix + ellipsis
_MAX_THROTTLE = 5.0  # cap when backing off from rate-limit hint


class StreamingMessage:
    """A single Transport message that is edited in place as deltas arrive.

    Public API mirrors the legacy `LiveMessage` so bot.py can swap in place:
      start(initial), append(delta), finalize(final_text=None, render=True), cancel(note)
    """

    def __init__(
        self,
        transport: Transport,
        chat: ChatRef,
        *,
        reply_to: MessageRef | None = None,
        prefix: str = "",
        throttle: float = _DEFAULT_THROTTLE,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        if len(prefix) >= max_chars:
            raise ValueError(
                f"StreamingMessage prefix ({len(prefix)} chars) must be shorter than max_chars "
                f"({max_chars}); otherwise overflow rotation cannot make progress."
            )
        self._transport = transport
        self._chat = chat
        self._reply_to = reply_to
        self._prefix = prefix
        self._throttle = throttle
        self._effective_throttle = throttle
        self._max_chars = max_chars
        self._buffer: str = ""
        self._sealed_text: str = ""
        self._last_rendered: str = ""
        self._last_edit_ts: float = 0.0
        self._pending: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._finalized = False
        self._current_ref: MessageRef | None = None

    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def full_text(self) -> str:
        """The complete streamed text, including chunks already sealed by rotation."""
        return self._sealed_text + self._buffer

    @property
    def message_id(self) -> str | None:
        """Native-id of the current underlying message, or None before start().

        Kept as a string to match the Transport's opaque MessageRef.native_id.
        Legacy LiveMessage returned int; callers that need int-typed IDs can
        cast explicitly.
        """
        return self._current_ref.native_id if self._current_ref is not None else None

    async def start(self, initial: str = "…") -> None:
        self._current_ref = await self._transport.send_text(
            self._chat, self._prefix + initial, reply_to=self._reply_to,
        )
        self._last_rendered = self._prefix + initial
        self._last_edit_ts = time.monotonic()

    async def append(self, delta: str) -> None:
        if self._finalized:
            logger.debug("append after finalize ignored")
            return
        if not delta:
            return
        self._buffer += delta
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._flush_soon())

    async def _flush_soon(self) -> None:
        try:
            wait = self._effective_throttle - (time.monotonic() - self._last_edit_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self._edit_current()
            except TransportRetryAfter as e:
                hint = float(e.retry_after or 1.0)
                sleep_for = max(hint, self._throttle)
                self._effective_throttle = min(sleep_for, _MAX_THROTTLE)
                logger.warning(
                    "Transport RetryAfter; sleeping %.2fs, cadence now %.2fs",
                    sleep_for, self._effective_throttle,
                )
                await asyncio.sleep(sleep_for)
                try:
                    await self._edit_current()
                except Exception:
                    logger.exception("StreamingMessage retry edit failed")
            if self._prefix + self._buffer != self._last_rendered and not self._finalized:
                self._pending = asyncio.create_task(self._flush_soon())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("StreamingMessage flush failed")

    async def _edit_current(self) -> None:
        async with self._lock:
            if self._finalized or self._current_ref is None:
                return
            while len(self._prefix + self._buffer) > self._max_chars:
                await self._rotate_once()
            text = self._prefix + self._buffer
            if text == self._last_rendered:
                return
            if not text.strip():
                return
            await self._transport.edit_text(self._current_ref, text)
            self._last_rendered = text
            self._last_edit_ts = time.monotonic()
            self._effective_throttle = self._throttle

    async def _rotate_once(self) -> None:
        """Seal the current message at max_chars and open a new one.

        Mirrors LiveMessage: tries to render the sealed head as HTML for pretty
        formatting; shrinks the head size on overflow; falls back to plain on
        failure.
        """
        assert self._current_ref is not None
        room = self._max_chars - len(self._prefix)
        if room <= 0:
            room = 0
        head_size = room
        rendered: str | None = None
        for _ in range(5):
            candidate = self._prefix + self._transport.render_markdown(self._buffer[:head_size])
            if len(candidate) <= self._max_chars:
                rendered = candidate
                break
            head_size = max(1, head_size * 3 // 4)
        if rendered is None:
            head_size = room
        head = self._buffer[:head_size]
        tail = self._buffer[head_size:]

        edited_html = False
        if rendered is not None:
            try:
                await self._transport.edit_text(self._current_ref, rendered, html=True)
                edited_html = True
            except Exception:
                logger.warning(
                    "StreamingMessage seal-edit HTML failed; falling back to plain",
                    exc_info=True,
                )
        if not edited_html:
            try:
                await self._transport.edit_text(self._current_ref, self._prefix + head)
            except Exception:
                logger.warning("StreamingMessage seal-edit plain failed", exc_info=True)

        if tail and len(self._prefix + tail) <= self._max_chars:
            initial = tail
        else:
            initial = "…"
        self._current_ref = await self._transport.send_text(
            self._chat, self._prefix + initial, reply_to=self._reply_to,
        )
        self._sealed_text += head
        self._buffer = tail
        self._last_rendered = self._prefix + initial
        self._last_edit_ts = time.monotonic()
        self._effective_throttle = self._throttle

    async def finalize(
        self,
        final_text: str | None = None,
        *,
        render: bool = True,
    ) -> None:
        if self._finalized or self._current_ref is None:
            return
        self._finalized = True
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            try:
                await self._pending
            except (asyncio.CancelledError, Exception):
                pass
        self._pending = None

        if final_text is not None and final_text != "":
            self._buffer = final_text

        while len(self._prefix + self._buffer) > self._max_chars:
            try:
                await self._rotate_once()
            except Exception:
                logger.exception("StreamingMessage.finalize rotation failed")
                break

        text = self._prefix + self._buffer
        if not text.strip():
            return

        use_html = render
        rendered = text
        if render:
            rendered = self._prefix + self._transport.render_markdown(self._buffer)

        if rendered != self._last_rendered:
            try:
                await self._transport.edit_text(self._current_ref, rendered, html=use_html)
                self._last_rendered = rendered
            except Exception:
                logger.warning(
                    "StreamingMessage.finalize edit failed; falling back to plain",
                    exc_info=True,
                )
                try:
                    await self._transport.edit_text(self._current_ref, text)
                    self._last_rendered = text
                except Exception:
                    logger.exception("StreamingMessage.finalize plain fallback failed")

    async def cancel(self, note: str = "(cancelled)") -> None:
        if self._finalized:
            return
        suffix = f"\n{note}" if self._buffer else note
        await self.finalize(self._buffer + suffix, render=False)
