"""Bot-level tests for the voice flow through the Transport abstraction."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_project_bot_stub(with_synthesizer: bool = False):
    """Minimal ProjectBot stub with FakeTransport + mock transcriber."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.chat_history import ChatHistory
    from link_project_to_chat.config import AllowedUser
    bot = ProjectBot.__new__(ProjectBot)
    bot._transport = FakeTransport()
    bot._app = SimpleNamespace(bot=None)
    bot._allowed_users = [
        AllowedUser(username="alice", role="executor", locked_identities=["fake:42"]),
    ]
    bot._auth_source = "project"
    bot._auth_dirty = False
    bot._rate_limits = {}
    bot._failed_auth_counts = {}
    bot.group_mode = False
    bot.path = Path(".")
    bot.name = "proj"
    bot._active_persona = None
    bot._voice_tasks = set()
    bot._transcriber = AsyncMock()
    bot._transcriber.transcribe = AsyncMock(return_value="transcribed text")
    bot._synthesizer = object() if with_synthesizer else None
    bot._chat_history = ChatHistory()
    # task_manager stub — submit_agent is sync in the real TaskManager,
    # so we use MagicMock here (AsyncMock would return a coroutine that
    # breaks ``task.id`` access in the synthesizer path).
    bot.task_manager = SimpleNamespace(
        waiting_input_task=lambda chat_id: None,
        submit_answer=MagicMock(),
        submit_agent=MagicMock(return_value=SimpleNamespace(id=99)),
    )
    return bot


def _audio_incoming(tmp_path, text: str = "") -> IncomingMessage:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"fake ogg bytes")
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    return IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[IncomingFile(
            path=audio_path, original_name="voice.ogg",
            mime_type="audio/ogg", size_bytes=100,
        )],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
    )


def _file_incoming(tmp_path, text: str = "") -> IncomingMessage:
    upload_path = tmp_path / "report.txt"
    upload_path.write_text("report body", encoding="utf-8")
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    return IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[IncomingFile(
            path=upload_path, original_name="report.txt",
            mime_type="text/plain", size_bytes=11,
        )],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="7", chat=chat),
    )


async def test_voice_message_sends_transcribing_status(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("Transcribing" in m.text for m in bot._transport.sent_messages)


async def test_voice_message_edits_status_with_transcript(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("transcribed text" in e.text for e in bot._transport.edited_messages)


async def test_voice_message_submits_to_claude(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    bot.task_manager.submit_agent.assert_called_once()
    kwargs = bot.task_manager.submit_agent.call_args.kwargs
    assert kwargs["prompt"] == "transcribed text"


async def test_voice_task_added_to_voice_tasks_when_synthesizer_set(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=True)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 in bot._voice_tasks


async def test_voice_task_not_tracked_when_synthesizer_unset(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=False)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 not in bot._voice_tasks


async def test_voice_unauthorized_sender_ignored(tmp_path):
    bot = _make_project_bot_stub()
    bot._allowed_users = []  # fail-closed
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    # No status message sent; no task submitted.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_agent.assert_not_called()
    bot._transcriber.transcribe.assert_not_called()


async def test_voice_no_transcriber_replies_with_setup_instructions(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber = None
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Voice messages aren't configured" in m.text
        for m in bot._transport.sent_messages
    )


async def test_voice_empty_transcription_shows_error(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(return_value="")
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "empty result" in e.text
        for e in bot._transport.edited_messages
    )


async def test_voice_transcription_error_shows_message(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(side_effect=RuntimeError("api down"))
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Transcription failed" in e.text
        for e in bot._transport.edited_messages
    )


async def test_voice_with_reply_prefixes_prompt(tmp_path):
    """When incoming has a reply_to with text, the prompt is prefixed with '[Replying to: ...]'."""
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    chat = incoming.chat
    reply_ref = MessageRef(transport_id="fake", native_id="42", chat=chat)
    incoming_with_reply = IncomingMessage(
        chat=incoming.chat,
        sender=incoming.sender,
        text=incoming.text,
        files=incoming.files,
        reply_to=reply_ref,
        message=incoming.message,
        reply_to_text="earlier message",
    )
    await bot._on_voice_from_transport(incoming_with_reply)
    kwargs = bot.task_manager.submit_agent.call_args.kwargs
    assert kwargs["prompt"].startswith("[Replying to: earlier message]")


async def test_voice_with_active_persona_formats_prompt(tmp_path, monkeypatch):
    """When _active_persona is set, load_persona + format_persona_prompt run on the transcript."""
    bot = _make_project_bot_stub()
    bot._active_persona = "reviewer"

    # Stub the persona-loading helpers by monkeypatching the skills module.
    from link_project_to_chat import skills as skills_module
    fake_persona = SimpleNamespace(content="You are a reviewer.")
    monkeypatch.setattr(skills_module, "load_persona", lambda name, path: fake_persona)
    monkeypatch.setattr(
        skills_module, "format_persona_prompt",
        lambda persona, prompt: f"[persona={persona.content}] {prompt}",
    )

    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    kwargs = bot.task_manager.submit_agent.call_args.kwargs
    assert "[persona=You are a reviewer.]" in kwargs["prompt"]
    assert "transcribed text" in kwargs["prompt"]


async def test_unified_dispatch_routes_audio_to_voice_handler(tmp_path):
    """When IncomingMessage has audio file, _on_text_from_transport routes it
    to the voice handler."""
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_text_from_transport(incoming)
    # Voice flow fires: status message sent, transcript edited, task submitted.
    assert any("Transcribing" in m.text for m in bot._transport.sent_messages)
    bot.task_manager.submit_agent.assert_called_once()


async def test_unified_dispatch_unsupported_fallback():
    """When IncomingMessage has no text and no files, generic 'not supported' reply."""
    bot = _make_project_bot_stub()
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="", files=[], reply_to=None,
        message=MessageRef(transport_id="fake", native_id="1", chat=chat), native=None,
    )
    await bot._on_text_from_transport(incoming)
    assert any(
        "not supported" in m.text.lower()
        for m in bot._transport.sent_messages
    )


async def test_unified_dispatch_unsupported_unauthorized_ignored():
    """Unsupported messages from unauthorized users are silently dropped."""
    bot = _make_project_bot_stub()
    bot._allowed_users = []  # fail-closed
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="", files=[], reply_to=None,
        message=MessageRef(transport_id="fake", native_id="1", chat=chat), native=None,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._transport.sent_messages == []


async def test_unsupported_media_caption_routes_to_specific_rejection():
    """Caption-bearing unsupported media (e.g. video with caption) hits the
    specific 'Unsupported media type' rejection in `_on_text`, not the generic
    fallback. The caption is NOT submitted to Claude.
    """
    bot = _make_project_bot_stub()
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="check this out", files=[],
        reply_to=None, message=MessageRef(transport_id="fake", native_id="1", chat=chat),
        native=None, has_unsupported_media=True,
    )
    await bot._on_text_from_transport(incoming)
    assert any(
        "Unsupported media type" in m.text
        for m in bot._transport.sent_messages
    ), f"expected specific rejection, got: {[m.text for m in bot._transport.sent_messages]}"
    bot.task_manager.submit_agent.assert_not_called()


async def test_unsupported_media_no_caption_routes_to_specific_rejection():
    """Caption-LESS unsupported media (e.g. sticker, muted video, location) must
    also hit the specific 'Unsupported media type' rejection — not the generic
    fallback. Bug regression test for caption-less bypass of the new flag.
    """
    bot = _make_project_bot_stub()
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="", files=[],
        reply_to=None, message=MessageRef(transport_id="fake", native_id="1", chat=chat),
        native=None, has_unsupported_media=True,
    )
    await bot._on_text_from_transport(incoming)
    assert any(
        "Unsupported media type" in m.text
        for m in bot._transport.sent_messages
    ), f"expected specific rejection, got: {[m.text for m in bot._transport.sent_messages]}"
    bot.task_manager.submit_agent.assert_not_called()


async def test_file_upload_uses_platform_temp_root(tmp_path, monkeypatch):
    bot = _make_project_bot_stub()
    incoming = _file_incoming(tmp_path, text="please review")
    temp_root = tmp_path / "platform-temp"

    from link_project_to_chat import bot as bot_module

    monkeypatch.setattr(bot_module.tempfile, "gettempdir", lambda: str(temp_root))

    await bot._on_file_from_transport(incoming)

    expected = temp_root / "link-project-to-chat" / "proj" / "uploads" / "report.txt"
    assert expected.read_text(encoding="utf-8") == "report body"

    kwargs = bot.task_manager.submit_agent.call_args.kwargs
    assert kwargs["chat"].native_id == "12345"
    assert kwargs["message"].native_id == "7"
    assert str(expected) in kwargs["prompt"]
    assert kwargs["prompt"].endswith("please review")


async def test_voice_long_transcript_truncated_in_status(tmp_path):
    """Long transcripts are truncated to 200 chars in the status-edit display."""
    bot = _make_project_bot_stub()
    long_text = "x" * 500
    bot._transcriber.transcribe = AsyncMock(return_value=long_text)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    # The status edit should contain truncated text ending with "..."
    edit_texts = [e.text for e in bot._transport.edited_messages]
    truncated_edits = [t for t in edit_texts if "..." in t]
    assert truncated_edits, "expected at least one edit with truncation marker"
    # The truncated edit should be shorter than the full transcript wrapped in quotes.
    for t in truncated_edits:
        # "🎤 "text..."" format: the inner text portion should be 200 chars max
        assert len(t) < 300  # 200 chars + wrapper/ellipsis


async def test_finalize_voice_task_skips_synthesizer_on_google_chat(monkeypatch):
    """Outbound TTS upload is unavailable on google_chat with default chat.bot
    auth, so the synthesizer path must be skipped — the operator should get
    a plain text reply instead of a '[file upload not available]' fallback."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.task_manager import Task, TaskStatus, TaskType

    bot = _make_project_bot_stub(with_synthesizer=True)
    bot._transport.TRANSPORT_ID = "google_chat"  # mock the channel

    # Minimal _finalize_claude_task scaffolding.
    bot._live_text = {}
    bot._live_thinking = {}
    bot._live_text_failed = set()
    bot._live_thinking_failed = set()
    bot._thinking_buf = {}
    bot._thinking_store = {}

    voice_response_calls: list[tuple] = []

    async def _spy_send_voice_response(chat, text, reply_to):
        voice_response_calls.append((chat, text, reply_to))

    text_send_calls: list[tuple] = []

    async def _spy_send_to_chat(chat, text, reply_to=None):
        text_send_calls.append((chat, text, reply_to))

    monkeypatch.setattr(bot, "_send_voice_response", _spy_send_voice_response)
    monkeypatch.setattr(bot, "_send_to_chat", _spy_send_to_chat)

    chat = ChatRef(transport_id="google_chat", native_id="spaces/AAAA", kind=ChatKind.DM)
    msg = MessageRef(transport_id="google_chat", native_id="m1", chat=chat)
    task = Task(
        id=42, chat=chat, message=msg, type=TaskType.AGENT, input="hi", name="agent",
        status=TaskStatus.DONE, result="Hello back.",
    )
    bot._voice_tasks.add(task.id)

    await ProjectBot._finalize_claude_task(bot, task)

    assert voice_response_calls == [], (
        f"_send_voice_response must not be called on google_chat; got {voice_response_calls!r}"
    )
    assert any(call[1] == "Hello back." for call in text_send_calls), (
        f"expected plain text reply, got {text_send_calls!r}"
    )


async def test_finalize_voice_task_still_synthesizes_on_other_transports(monkeypatch):
    """Sanity: the google_chat gate doesn't accidentally suppress TTS on
    transports that DO support voice upload (Telegram, Fake)."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.task_manager import Task, TaskStatus, TaskType

    bot = _make_project_bot_stub(with_synthesizer=True)
    # FakeTransport.TRANSPORT_ID is already "fake" — leave it alone.

    bot._live_text = {}
    bot._live_thinking = {}
    bot._live_text_failed = set()
    bot._live_thinking_failed = set()
    bot._thinking_buf = {}
    bot._thinking_store = {}

    voice_response_calls: list[tuple] = []

    async def _spy_send_voice_response(chat, text, reply_to):
        voice_response_calls.append((chat, text, reply_to))

    async def _spy_send_to_chat(chat, text, reply_to=None):
        pass

    monkeypatch.setattr(bot, "_send_voice_response", _spy_send_voice_response)
    monkeypatch.setattr(bot, "_send_to_chat", _spy_send_to_chat)

    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    msg = MessageRef(transport_id="fake", native_id="m1", chat=chat)
    task = Task(
        id=43, chat=chat, message=msg, type=TaskType.AGENT, input="hi", name="agent",
        status=TaskStatus.DONE, result="Hello back.",
    )
    bot._voice_tasks.add(task.id)

    await ProjectBot._finalize_claude_task(bot, task)

    assert len(voice_response_calls) == 1, (
        f"expected _send_voice_response on non-google_chat transport, got {voice_response_calls!r}"
    )
