"""Unit tests for StreamingMessage — transport-agnostic streaming-edit helper.

Covers:
- start() sends initial placeholder
- append() accumulates and throttles edits
- finalize() performs a final render-with-HTML edit
- cancel() finalizes with a note appended
- overflow rotates into a new message
- TransportRetryAfter triggers back-off
"""
from __future__ import annotations

import asyncio

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef, TransportRetryAfter
from link_project_to_chat.transport.fake import FakeTransport
from link_project_to_chat.transport.streaming import StreamingMessage


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


async def test_start_sends_initial_placeholder():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    assert len(t.sent_messages) == 1
    assert t.sent_messages[0].text == "…"


async def test_start_with_custom_initial():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start("thinking…")
    assert t.sent_messages[0].text == "thinking…"


async def test_append_schedules_edit_after_throttle():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    await sm.append("hello ")
    await sm.append("world")
    # Allow the pending flush task to run.
    await asyncio.sleep(0.02)
    assert any(e.text == "hello world" for e in t.edited_messages)


async def test_finalize_renders_html_on_final_edit():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    await sm.append("hello **bold**")
    await sm.finalize()
    # Final edit goes out with html=True.
    assert any(e.html for e in t.edited_messages)


async def test_finalize_with_final_text_overrides_buffer():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    await sm.append("interim")
    await sm.finalize(final_text="done", render=False)
    assert any(e.text == "done" for e in t.edited_messages)


async def test_cancel_appends_note():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    await sm.append("part1")
    await sm.cancel(note="(stopped)")
    last = t.edited_messages[-1]
    assert "part1" in last.text
    assert "(stopped)" in last.text


async def test_overflow_rotates_into_new_message():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0, max_chars=20)
    await sm.start()
    # Push well over max_chars.
    await sm.append("x" * 50)
    await sm.finalize()
    # At least one rotation — so >1 send_text call.
    assert len(t.sent_messages) >= 2


async def test_exact_limit_does_not_rotate_into_new_message():
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0, max_chars=20)
    await sm.start()
    await sm.append("x" * 20)
    await sm.finalize(render=False)

    assert len(t.sent_messages) == 1
    assert t.edited_messages[-1].text == "x" * 20


async def test_full_text_preserves_content_across_rotations():
    """After rotation seals an earlier chunk, ``full_text`` must still expose
    the full accumulated stream — otherwise the bot's conversation_log only
    captures the tail (the live buffer is truncated on every rotation).
    """
    t = FakeTransport()
    sm = StreamingMessage(t, _chat(), throttle=0, max_chars=20)
    await sm.start()
    payload = "x" * 50
    await sm.append(payload)
    await sm.finalize(render=False)
    # At least one rotation happened (>1 sent message).
    assert len(t.sent_messages) >= 2
    # The full accumulated stream must still be reachable post-rotation.
    assert sm.full_text == payload


def test_prefix_must_be_shorter_than_max_chars():
    with pytest.raises(ValueError, match="prefix"):
        StreamingMessage(FakeTransport(), _chat(), prefix="x" * 5, max_chars=5)


async def test_retry_after_triggers_throttle_backoff():
    """If the Transport raises TransportRetryAfter, StreamingMessage retries after the hint."""
    t = FakeTransport()
    # Monkey-patch edit_text to raise once, then succeed.
    calls = {"count": 0}
    original_edit = t.edit_text

    async def flaky_edit(msg, text, *, buttons=None, html=False):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TransportRetryAfter(0.01)
        return await original_edit(msg, text, buttons=buttons, html=html)

    t.edit_text = flaky_edit  # type: ignore[assignment]

    sm = StreamingMessage(t, _chat(), throttle=0)
    await sm.start()
    await sm.append("hello")
    await asyncio.sleep(0.1)  # let the retry happen
    # Should have retried and eventually landed an edit.
    assert calls["count"] >= 2
