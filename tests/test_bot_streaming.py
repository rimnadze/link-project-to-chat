from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.stream import TextDelta, ThinkingDelta
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.fake import FakeTransport
from link_project_to_chat.transport.streaming import StreamingMessage


@dataclass
class FakeChat:
    id: int
    type: str = "private"


@dataclass
class FakeMessage:
    message_id: int
    chat: FakeChat = field(default_factory=lambda: FakeChat(id=99))


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    next_id: int = 500

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        mid = self.next_id
        self.next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_to": reply_to_message_id, "mid": mid, **kw})
        return FakeMessage(message_id=mid, chat=FakeChat(id=chat_id))

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kw})


def _fake_task(task_id: int = 1) -> Task:
    t = Task.__new__(Task)
    t.id = task_id
    chat = ChatRef(transport_id="telegram", native_id="99", kind=ChatKind.DM)
    t.chat = chat
    t.message = MessageRef(transport_id="telegram", native_id="7", chat=chat)
    t.status = TaskStatus.RUNNING
    t.type = TaskType.AGENT
    t.input = "hello"
    t.name = "agent"
    t.result = ""
    t.error = None
    t.exit_code = None
    t.created_at = 0.0
    t.started_at = None
    t.finished_at = None
    t.pending_questions = []
    t._compact = False
    t._log = []
    return t


async def _stub_bot(show_thinking: bool = False):
    """Construct a minimal ProjectBot-like object just for the stream event tests."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport.telegram import TelegramTransport
    bot = ProjectBot.__new__(ProjectBot)
    fake_bot = FakeBot()
    bot._app = SimpleNamespace(bot=fake_bot)
    bot._transport = TelegramTransport(bot._app)
    bot._typing_tasks = {}
    bot._live_text = {}
    bot._live_thinking = {}
    bot._live_text_failed = set()
    bot._live_thinking_failed = set()
    bot._thinking_buf = {}
    bot._thinking_store = {}
    bot._voice_tasks = set()
    bot.show_thinking = show_thinking
    # Non-team (single-project) bot: livestream enabled. Team bots set
    # group_mode=True, which skips LiveMessage creation in _on_stream_event.
    bot.group_mode = False
    return bot


@pytest.mark.asyncio
async def test_text_delta_starts_live_message():
    bot = await _stub_bot()
    task = _fake_task()
    await bot._on_stream_event(task, TextDelta(text="hello "))
    await bot._on_stream_event(task, TextDelta(text="world"))
    # A LiveMessage exists for the task.
    assert task.id in bot._live_text
    live = bot._live_text[task.id]
    # The first delta triggered start() which sent the placeholder.
    assert len(bot._app.bot.sent) == 1
    # The buffer contains both deltas.
    assert live._buffer == "hello world"


@pytest.mark.asyncio
async def test_text_delta_in_group_mode_skips_live_message():
    """Team bots (group_mode=True) must not livestream: streaming edits would
    be picked up by team_relay mid-message and forwarded partial to the peer
    bot. task.result assembled in task_manager is sent once at finalize.
    """
    bot = await _stub_bot()
    bot.group_mode = True
    task = _fake_task(task_id=50)

    await bot._on_stream_event(task, TextDelta(text="hello "))
    await bot._on_stream_event(task, TextDelta(text="world"))

    # No LiveMessage was ever created and no placeholder was sent.
    assert task.id not in bot._live_text
    assert task.id not in bot._live_text_failed
    assert len(bot._app.bot.sent) == 0


@pytest.mark.asyncio
async def test_team_bot_sends_placeholder_at_task_start():
    """Team bots must post a placeholder immediately at `_on_task_started` so
    `team_relay._delete_pending_for_peer` runs on the placeholder's NewMessage
    and drops the forwarded trigger before the 60s fallback timer deletes it.
    Without an early placeholder, long finalize calls crash with
    `BadRequest: Message to be replied not found`.
    """
    bot = await _stub_bot()
    bot.group_mode = True
    bot._app.bot.get_chat = AsyncMock(return_value=MagicMock())
    bot._keep_typing = AsyncMock()
    task = _fake_task(task_id=60)

    await bot._on_task_started(task)

    assert task.id in bot._live_text
    assert len(bot._app.bot.sent) == 1
    assert bot._app.bot.sent[0]["reply_to"] == int(task.message.native_id)


@pytest.mark.asyncio
async def test_non_team_bot_does_not_send_placeholder_at_task_start():
    """The early placeholder is a team-mode workaround. Regular bots keep
    the lazy behavior: LiveMessage is created on the first TextDelta."""
    bot = await _stub_bot()
    bot.group_mode = False
    bot._app.bot.get_chat = AsyncMock(return_value=MagicMock())
    bot._keep_typing = AsyncMock()
    task = _fake_task(task_id=61)

    await bot._on_task_started(task)

    assert task.id not in bot._live_text
    assert len(bot._app.bot.sent) == 0


@pytest.mark.asyncio
async def test_team_bot_finalize_edits_placeholder_no_new_send():
    """End-to-end: placeholder at task start, no edits during stream, finalize
    *edits* the placeholder in place. Because it's an edit (not a send),
    reply_to is never re-validated against a possibly-deleted forward.
    """
    bot = await _stub_bot()
    bot.group_mode = True
    bot._is_image = lambda p: False
    bot._synthesizer = None
    bot._app.bot.get_chat = AsyncMock(return_value=MagicMock())
    bot._keep_typing = AsyncMock()
    task = _fake_task(task_id=62)
    task.status = TaskStatus.DONE
    task.result = "final delegation text"

    await bot._on_task_started(task)
    sent_after_start = len(bot._app.bot.sent)
    # TextDeltas during group_mode are no-ops on the stream side — task.result
    # is what reaches finalize.
    await bot._on_stream_event(task, TextDelta(text="streamed chunk "))
    await bot._on_stream_event(task, TextDelta(text="not appended"))

    await bot._finalize_claude_task(task)

    # No new send_message during finalize — only the placeholder was sent.
    assert len(bot._app.bot.sent) == sent_after_start
    assert any("final delegation text" in e["text"] for e in bot._app.bot.edits)
    assert task.id not in bot._live_text


# NOTE: the historic `test_send_html_retries_without_reply_to_when_target_deleted`
# bot-layer test is intentionally not present here. The retry now lives inside
# `TelegramTransport.send_text` (commit reinstating spec #0 finding I1) and is
# covered by `tests/transport/test_telegram_transport.py::
# test_send_text_retries_without_reply_to_when_target_deleted_preserving_html`.
# `_send_html` makes a single transport call; the retry is invisible to the bot.


@pytest.mark.asyncio
async def test_send_html_chains_chunks_replying_to_previous_chunk():
    """When a long HTML message splits into multiple chunks, chunk 1 replies
    to the user's message, chunk 2 replies to chunk 1, chunk 3 replies to
    chunk 2, etc. Gives every transport a native sense of 'these messages
    belong together' (threads on GChat/Slack, reply chains on Telegram/Discord).
    """
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot.__new__(ProjectBot)
    fake = FakeTransport()
    bot._transport = fake

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    user_message = MessageRef(transport_id="fake", native_id="user-1", chat=chat)

    # Force a 3-chunk split: build HTML well over fake's 4096-char limit.
    # Use long paragraphs so split_html cuts at paragraph boundaries cleanly.
    paragraph = "<p>" + ("x" * 3500) + "</p>"
    html = paragraph + paragraph + paragraph  # ~10500 chars / 4096 limit = 3 chunks

    last_ref = await bot._send_html(chat, html, reply_to=user_message)

    sent = fake.sent_messages
    assert len(sent) >= 2, f"expected multi-chunk send, got {len(sent)} chunk(s)"

    # Chunk 1 replies to the user's message.
    assert sent[0].reply_to == user_message, (
        f"chunk 0 should reply to user message, got {sent[0].reply_to!r}"
    )
    # Chunk 2 replies to chunk 1's MessageRef.
    assert sent[1].reply_to == sent[0].message, (
        f"chunk 1 should reply to chunk 0's MessageRef ({sent[0].message!r}), "
        f"got {sent[1].reply_to!r}"
    )
    # If there are more chunks, each replies to its immediate predecessor.
    for i in range(2, len(sent)):
        assert sent[i].reply_to == sent[i - 1].message, (
            f"chunk {i} should reply to chunk {i-1}'s MessageRef, "
            f"got {sent[i].reply_to!r}"
        )
    # Last sent chunk's MessageRef is what `_send_html` returns.
    assert last_ref == sent[-1].message


@pytest.mark.asyncio
async def test_send_html_single_chunk_uses_original_reply_to():
    """Sanity: short messages (one chunk) still reply to the original target."""
    from link_project_to_chat.bot import ProjectBot

    bot = ProjectBot.__new__(ProjectBot)
    fake = FakeTransport()
    bot._transport = fake

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    user_message = MessageRef(transport_id="fake", native_id="user-1", chat=chat)

    await bot._send_html(chat, "<p>short</p>", reply_to=user_message)

    assert len(fake.sent_messages) == 1
    assert fake.sent_messages[0].reply_to == user_message


@pytest.mark.asyncio
async def test_thinking_delta_in_group_mode_uses_buffer_not_livestream():
    """Even with show_thinking=True, team bots skip the thinking livestream
    and fall through to `_thinking_buf` (which feeds the Thinking button).
    """
    bot = await _stub_bot(show_thinking=True)
    bot.group_mode = True
    task = _fake_task(task_id=51)

    await bot._on_stream_event(task, ThinkingDelta(text="step A"))
    await bot._on_stream_event(task, ThinkingDelta(text="step B"))

    assert task.id not in bot._live_thinking
    assert bot._thinking_buf[task.id] == "step A\n\nstep B"
    assert len(bot._app.bot.sent) == 0


@pytest.mark.asyncio
async def test_thinking_delta_with_toggle_on_streams_separate_message():
    bot = await _stub_bot(show_thinking=True)
    task = _fake_task(task_id=2)
    await bot._on_stream_event(task, ThinkingDelta(text="first thought"))
    assert task.id in bot._live_thinking
    # The first thinking delta produces its own separate placeholder send.
    assert len(bot._app.bot.sent) == 1
    assert bot._app.bot.sent[0]["text"].startswith("💭 ")
    # `_thinking_buf` is NOT used when live thinking is on.
    assert task.id not in bot._thinking_buf


@pytest.mark.asyncio
async def test_thinking_delta_with_toggle_off_uses_buffer():
    bot = await _stub_bot(show_thinking=False)
    task = _fake_task(task_id=3)
    await bot._on_stream_event(task, ThinkingDelta(text="step 1"))
    await bot._on_stream_event(task, ThinkingDelta(text="step 2"))
    assert task.id not in bot._live_thinking
    assert bot._thinking_buf[task.id] == "step 1\n\nstep 2"
    # No Telegram messages were sent for thinking.
    assert len(bot._app.bot.sent) == 0


@pytest.mark.asyncio
async def test_finalize_with_live_text_sends_completion_notice():
    """Live-text path edits the stream in place, then sends a fresh completion ping."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=10)
    task.status = TaskStatus.DONE
    task.started_at = 10.0
    task.finished_at = 14.0
    # task.result contains only the LAST assistant text block; the streamed buffer
    # has every text delta (narration + final). The finalized message must preserve
    # the buffer's content, not clobber it with task.result.
    task.result = "final answer"
    await bot._on_stream_event(task, TextDelta(text="narration before tool use"))
    await bot._on_stream_event(task, TextDelta(text=" — final answer"))
    sent_before = len(bot._app.bot.sent)

    await bot._finalize_claude_task(task)

    # The answer stays in the live message edit, and a short new message gives
    # Telegram a final notification + visible elapsed time.
    assert len(bot._app.bot.sent) == sent_before + 1
    assert bot._app.bot.sent[-1]["text"] == "Done in 4s."
    assert task.id not in bot._live_text
    # Full streamed buffer preserved (narration survives, not just task.result).
    assert any("narration before tool use" in e["text"] for e in bot._app.bot.edits)


@pytest.mark.asyncio
async def test_task_complete_logs_full_live_text_after_finalize():
    """Conversation history must capture the full streamed text the user saw."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    bot._effective_config_path = lambda: None
    logged: list[tuple[str, str]] = []

    class _Log:
        async def append_async(self, chat, role, text, backend=None):
            logged.append((role, text))

    bot.conversation_log = _Log()
    bot.task_manager = SimpleNamespace(
        backend=SimpleNamespace(session_id=None, name="claude"),
    )
    task = _fake_task(task_id=13)
    task.status = TaskStatus.DONE
    task.started_at = 10.0
    task.finished_at = 12.0
    task.result = "final answer only"
    await bot._on_stream_event(task, TextDelta(text="narration before tool use"))
    await bot._on_stream_event(task, TextDelta(text=" — final answer"))

    await bot._on_task_complete(task)

    assert logged == [("assistant", "narration before tool use — final answer")]


@pytest.mark.asyncio
async def test_finalize_with_live_error_sends_failure_notice():
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=14)
    task.status = TaskStatus.FAILED
    task.error = "boom"
    task.started_at = 20.0
    task.finished_at = 23.0
    await bot._on_stream_event(task, TextDelta(text="working"))
    sent_before = len(bot._app.bot.sent)

    await bot._finalize_claude_task(task)

    assert len(bot._app.bot.sent) == sent_before + 1
    assert bot._app.bot.sent[-1]["text"] == "Failed in 3s."
    assert any("Error: boom" in e["text"] for e in bot._app.bot.edits)


@pytest.mark.asyncio
async def test_finalize_with_empty_buffer_falls_back_to_task_result():
    """If the stream dropped before any deltas arrived, use task.result as the message body."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=11)
    task.status = TaskStatus.DONE
    task.result = "fallback answer"

    # Create an empty-buffer StreamingMessage (no TextDelta ever fired).
    sm = StreamingMessage(bot._transport, task.chat, reply_to=task.message)
    await sm.start()
    bot._live_text[task.id] = sm

    await bot._finalize_claude_task(task)
    # With empty buffer, the fallback kicks in and the final edit contains task.result.
    assert any("fallback answer" in e["text"] for e in bot._app.bot.edits)


@pytest.mark.asyncio
async def test_finalize_empty_buffer_and_empty_result_replaces_placeholder():
    """If the turn produced no text (only tool_use), replace the '…' placeholder with
    a short notice instead of leaving it stuck forever."""
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    task = _fake_task(task_id=12)
    task.status = TaskStatus.DONE
    task.result = ""  # Claude turn ended with only tool_use blocks

    sm = StreamingMessage(bot._transport, task.chat, reply_to=task.message)
    await sm.start()
    bot._live_text[task.id] = sm

    await bot._finalize_claude_task(task)
    # Placeholder must be replaced with the "no text response" notice (not left as "…").
    notice_edits = [e for e in bot._app.bot.edits if "no text response" in e["text"]]
    assert notice_edits, f"Expected 'no text response' edit, got: {[e['text'] for e in bot._app.bot.edits]}"


@pytest.mark.asyncio
async def test_finalize_without_live_text_falls_back_to_send_to_chat():
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None
    sent_chats: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, reply_to=None):
        sent_chats.append((chat_id, text))

    bot._send_to_chat = fake_send
    task = _fake_task(task_id=11)
    task.status = TaskStatus.DONE
    task.result = "tool-only answer"

    await bot._finalize_claude_task(task)

    assert sent_chats == [(task.chat, "tool-only answer")]


@pytest.mark.asyncio
async def test_finalize_command_task_includes_elapsed_duration():
    bot = await _stub_bot()
    sent: list[str] = []

    async def fake_send_raw(chat, text, reply_to=None):
        sent.append(text)

    bot._send_raw = fake_send_raw
    task = _fake_task(task_id=15)
    task.type = TaskType.COMMAND
    task.status = TaskStatus.DONE
    task.result = "compiled"
    task.started_at = 30.0
    task.finished_at = 36.0

    await bot._finalize_command_task(task)

    assert sent == ["compiled\n[exit 0 | 6s]"]


@pytest.mark.asyncio
async def test_on_task_complete_still_finalizes_when_session_persist_fails(caplog):
    bot = await _stub_bot()
    task = _fake_task(task_id=13)
    task.status = TaskStatus.DONE
    bot.task_manager = SimpleNamespace(backend=SimpleNamespace(session_id="sess-123"))
    bot._patch_config = MagicMock(side_effect=RuntimeError("disk full"))
    bot._finalize_claude_task = AsyncMock(return_value=None)

    with caplog.at_level("ERROR", logger="link_project_to_chat.bot"):
        await bot._on_task_complete(task)

    bot._finalize_claude_task.assert_awaited_once_with(task)
    assert "Failed to persist session_id for task #13" in caplog.text


@pytest.mark.asyncio
async def test_finalize_with_toggle_off_stores_thinking_for_button():
    bot = await _stub_bot(show_thinking=False)
    bot._is_image = lambda p: False
    bot._synthesizer = None

    async def fake_send(chat_id, text, reply_to=None):
        pass

    bot._send_to_chat = fake_send
    task = _fake_task(task_id=12)
    task.status = TaskStatus.DONE
    task.result = "ok"
    await bot._on_stream_event(task, ThinkingDelta(text="hidden reasoning"))

    await bot._finalize_claude_task(task)

    assert bot._thinking_store[task.id] == "hidden reasoning"


@pytest.mark.asyncio
async def test_waiting_input_seals_live_text():
    bot = await _stub_bot()
    bot._is_image = lambda p: False
    bot._synthesizer = None

    # We don't exercise the question rendering here — stub it.
    async def fake_send(chat_id, text, reply_to=None):
        pass

    bot._send_to_chat = fake_send

    async def fake_render_questions(task):
        pass

    # _on_waiting_input will try to render questions; give it an empty list so that path is a no-op.
    task = _fake_task(task_id=20)
    task.pending_questions = []
    task.result = ""

    await bot._on_stream_event(task, TextDelta(text="mid-stream"))
    assert task.id in bot._live_text

    await bot._on_waiting_input(task)

    # Live text was finalised and popped.
    assert task.id not in bot._live_text


@pytest.mark.asyncio
async def test_thinking_command_handlers_exist_and_register():
    # Sanity check that the ProjectBot class exposes the new command handler.
    from link_project_to_chat.bot import ProjectBot, COMMANDS
    assert any(c[0] == "thinking" for c in COMMANDS)
    assert hasattr(ProjectBot, "_on_thinking")


@pytest.mark.asyncio
async def test_cancel_live_for_seals_both_messages():
    bot = await _stub_bot(show_thinking=True)
    task = _fake_task(task_id=30)
    # Create both a live-text and a live-thinking message by feeding deltas.
    await bot._on_stream_event(task, TextDelta(text="answer so far"))
    await bot._on_stream_event(task, ThinkingDelta(text="reasoning"))
    assert task.id in bot._live_text
    assert task.id in bot._live_thinking

    await bot._cancel_live_for(task.id, "(cancelled)")

    assert task.id not in bot._live_text
    assert task.id not in bot._live_thinking
    # Both messages received a final edit containing the cancellation note.
    cancel_texts = [e["text"] for e in bot._app.bot.edits if "(cancelled)" in e["text"]]
    assert len(cancel_texts) >= 2, f"expected 2 cancel edits, got edits={bot._app.bot.edits}"


@dataclass
class FailingStartBot(FakeBot):
    """FakeBot that raises on every send_message call carrying a 💭 prefix.

    Simulates the case where the thinking placeholder send fails (e.g. Telegram
    rate limit / transient error), so the regular text placeholder still
    succeeds but the thinking one does not.
    """

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        if text.startswith("💭"):
            raise RuntimeError("simulated Telegram failure for thinking placeholder")
        return await super().send_message(
            chat_id, text, reply_to_message_id=reply_to_message_id, **kw
        )


async def _stub_bot_with_bot(fake_bot, show_thinking: bool = False):
    """Same as _stub_bot but with a caller-provided FakeBot variant."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.transport.telegram import TelegramTransport
    bot = ProjectBot.__new__(ProjectBot)
    bot._app = SimpleNamespace(bot=fake_bot)
    bot._transport = TelegramTransport(bot._app)
    bot._typing_tasks = {}
    bot._live_text = {}
    bot._live_thinking = {}
    bot._live_text_failed = set()
    bot._live_thinking_failed = set()
    bot._thinking_buf = {}
    bot._thinking_store = {}
    bot._voice_tasks = set()
    bot.show_thinking = show_thinking
    # Non-team (single-project) bot: livestream enabled. Team bots set
    # group_mode=True, which skips LiveMessage creation in _on_stream_event.
    bot.group_mode = False
    return bot


@pytest.mark.asyncio
async def test_thinking_start_failure_falls_back_to_buffer():
    """When LiveMessage.start() raises for the thinking placeholder, the
    ThinkingDelta content must still be captured in _thinking_buf so the
    post-completion 'Thinking' button still works. Without this fallback,
    a transient Telegram error silently eats all subsequent thinking content.
    """
    failing_bot = FailingStartBot()
    bot = await _stub_bot_with_bot(failing_bot, show_thinking=True)
    task = _fake_task(task_id=40)

    # First ThinkingDelta — start() will raise because the bot refuses the 💭 send.
    await bot._on_stream_event(task, ThinkingDelta(text="first thought"))
    # Second ThinkingDelta — we should not keep hammering start() and losing content.
    await bot._on_stream_event(task, ThinkingDelta(text="second thought"))

    # No dead LiveMessage should be left in _live_thinking.
    assert task.id not in bot._live_thinking
    # Both thinking chunks captured via the toggle-off fallback path.
    assert bot._thinking_buf.get(task.id) == "first thought\n\nsecond thought"


@dataclass
class TextFailingBot(FakeBot):
    """FakeBot whose send_message always raises — simulates Telegram refusing
    the text placeholder. The bot should not crash; finalize should
    gracefully fall back to _send_to_chat with task.result."""

    async def send_message(self, chat_id, text, reply_to_message_id=None, **kw):
        raise RuntimeError("simulated Telegram failure for text placeholder")


@pytest.mark.asyncio
async def test_text_start_failure_does_not_crash_and_finalize_falls_back():
    failing_bot = TextFailingBot()
    bot = await _stub_bot_with_bot(failing_bot, show_thinking=False)
    bot._is_image = lambda p: False
    bot._synthesizer = None
    fallback_sends: list[tuple[int, str]] = []

    async def fake_send(chat_id, text, reply_to=None):
        fallback_sends.append((chat_id, text))

    bot._send_to_chat = fake_send
    task = _fake_task(task_id=42)
    task.status = TaskStatus.DONE
    task.result = "the final answer"

    # Multiple TextDeltas — first fails start(), rest should be no-ops (not keep retrying).
    await bot._on_stream_event(task, TextDelta(text="chunk1"))
    await bot._on_stream_event(task, TextDelta(text="chunk2"))
    await bot._on_stream_event(task, TextDelta(text="chunk3"))

    # No live message was stored, and the failure flag short-circuits retries.
    assert task.id not in bot._live_text
    assert task.id in bot._live_text_failed
    # Only ONE send_message attempt (the first start()), not three.
    # FailingBot raises on every call; the failed-set prevents retries.
    # We can't count raises directly, but we can verify finalize falls back cleanly.

    await bot._finalize_claude_task(task)

    # Fallback path sent task.result via _send_to_chat.
    assert fallback_sends == [(task.chat, "the final answer")]
    # Failed flag cleaned up for subsequent tasks.
    assert task.id not in bot._live_text_failed


@pytest.mark.asyncio
async def test_thinking_start_failure_surfaces_via_thinking_button_after_finalize():
    """End-to-end: after the task finalises, the buffered thinking lands in
    `_thinking_store` so `/tasks → Thinking` shows it as a fallback."""
    failing_bot = FailingStartBot()
    bot = await _stub_bot_with_bot(failing_bot, show_thinking=True)
    bot._is_image = lambda p: False
    bot._synthesizer = None

    async def fake_send(chat_id, text, reply_to=None):
        pass

    bot._send_to_chat = fake_send

    task = _fake_task(task_id=41)
    task.status = TaskStatus.DONE
    task.result = "final answer"

    await bot._on_stream_event(task, ThinkingDelta(text="hidden reasoning"))
    await bot._on_stream_event(task, TextDelta(text="partial"))
    await bot._finalize_claude_task(task)

    # The Thinking button fallback kicks in because live thinking degraded to buffer.
    assert bot._thinking_store.get(task.id) == "hidden reasoning"


@pytest.mark.asyncio
async def test_ask_answer_annotation_preserves_question_html():
    """M5 regression: after user picks an option, the edit contains the original
    question HTML + 'Selected: X', not just the selection suffix."""
    from unittest.mock import MagicMock

    from link_project_to_chat.stream import Question, QuestionOption
    from link_project_to_chat.transport import (
        ButtonClick,
        ChatKind,
        ChatRef,
        Identity,
        MessageRef,
    )

    bot = await _stub_bot()
    bot._auth_identity = lambda _sender: True
    bot._require_executor = lambda _sender: True
    bot._auth_dirty = False

    # Prepare a fake task with one pending question.
    task = _fake_task(task_id=77)
    task.pending_questions = [Question(
        question="Which option?",
        header="Pick one",
        options=[
            QuestionOption(label="Option A", description="desc A"),
            QuestionOption(label="Option B", description="desc B"),
        ],
    )]
    task.status = TaskStatus.WAITING_INPUT
    bot.task_manager = MagicMock()
    bot.task_manager.get = MagicMock(return_value=task)
    bot.task_manager.submit_answer = MagicMock(return_value=True)

    chat = ChatRef(transport_id="telegram", native_id="12345", kind=ChatKind.DM)
    msg_ref = MessageRef(transport_id="telegram", native_id="200", chat=chat)
    sender = Identity(
        transport_id="telegram", native_id="42",
        display_name="Alice", handle="alice", is_bot=False,
    )
    click = ButtonClick(chat=chat, message=msg_ref, sender=sender, value="ask_77_0_0")

    await bot._on_button(click)

    # Assert the edit contains both the question header AND "Selected:" annotation.
    edits = bot._app.bot.edits
    assert edits, "expected at least one edit after option click"
    edit_text = edits[-1]["text"]
    assert "Pick one" in edit_text
    assert "Which option?" in edit_text
    assert "Option A" in edit_text
    assert "<i>Selected:</i> Option A" in edit_text


@pytest.mark.asyncio
async def test_send_image_rejects_sibling_path_with_shared_prefix(tmp_path):
    from link_project_to_chat.bot import ProjectBot

    sibling_dir = tmp_path.parent / f"{tmp_path.name}-secret"
    sibling_dir.mkdir()
    leaked = sibling_dir / "leak.png"
    leaked.write_bytes(b"not really a png")

    bot = ProjectBot(name="proj", path=tmp_path, token="t")
    bot._transport = FakeTransport()

    chat = ChatRef(transport_id="telegram", native_id="123", kind=ChatKind.DM)
    await bot._send_image(chat=chat, file_path=str(leaked))

    assert bot._transport.sent_files == []
