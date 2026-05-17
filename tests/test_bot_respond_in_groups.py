"""Solo project bot in Telegram group — routing gate tests.

Verifies the `respond_in_groups=True` elif branch in
ProjectBot._on_text_from_transport:
  - mention → process
  - reply-to-bot → process
  - drive-by → silent
  - self → silent
  - peer bot → silent
  - mention-strip happens before _on_text
  - DMs still work (filter is PRIVATE | GROUPS, not GROUPS only)
  - captioned file with @mention → process
  - DM behavior unchanged when flag is False
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot(*, respond_in_groups: bool, bot_username: str = "MyBot"):
    """Build a minimal ProjectBot suitable for routing-gate tests.

    Bypasses __init__ via __new__ and sets only the fields the gate touches.
    Stubs _on_text so tests can assert called-with cleanly.
    """
    bot = ProjectBot.__new__(ProjectBot)
    bot.bot_username = bot_username
    bot._respond_in_groups = respond_in_groups
    bot.group_mode = False
    bot.team_name = None
    bot.role = None
    bot._allowed_users = [AllowedUser(username="alice", role="executor",
                                       locked_identities=["fake:42"])]
    bot._auth_dirty = False
    bot._on_text = AsyncMock()
    bot._transport = FakeTransport()
    from link_project_to_chat.chat_history import ChatHistory
    bot._chat_history = ChatHistory()
    return bot


def _make_group_incoming(
    text: str,
    *,
    sender_handle: str = "alice",
    sender_id: str = "42",
    sender_is_bot: bool = False,
    mentions: list[Identity] | None = None,
    reply_to_sender: Identity | None = None,
    reply_to_text: str | None = None,
    files: list[IncomingFile] | None = None,
) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="100", kind=ChatKind.ROOM)
    sender = Identity(
        transport_id="fake", native_id=sender_id,
        display_name=sender_handle, handle=sender_handle, is_bot=sender_is_bot,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=files or [],
        reply_to=None, message=msg,
        reply_to_sender=reply_to_sender,
        reply_to_text=reply_to_text,
        mentions=mentions or [],
    )


def _make_dm_incoming(text: str) -> IncomingMessage:
    chat = ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42",
        display_name="alice", handle="alice", is_bot=False,
    )
    msg = MessageRef(transport_id="fake", native_id="200", chat=chat)
    return IncomingMessage(
        chat=chat, sender=sender, text=text, files=[],
        reply_to=None, message=msg,
    )


def _bot_mention(handle: str = "MyBot") -> Identity:
    return Identity(
        transport_id="fake", native_id="bot-self",
        display_name=handle, handle=handle, is_bot=True,
    )


@pytest.mark.asyncio
async def test_group_mention_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # Mention stripped before reaching _on_text.
    assert forwarded.text == " do X"


@pytest.mark.asyncio
async def test_group_reply_to_bot_reaches_on_text():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "follow-up question",
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # No mention to strip; text unchanged.
    assert forwarded.text == "follow-up question"


@pytest.mark.asyncio
async def test_group_drive_by_message_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming("chatter between humans")
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_self_is_silent():
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "any text",
        sender_handle="MyBot",  # same as bot_username
        sender_is_bot=True,
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_message_from_peer_bot_with_mention_is_silent():
    """Peer-bot defense: solo bot in group must NEVER respond to another bot,
    even when @mentioned. Avoids bot-to-bot loops; team workflows are
    explicitly opt-in via team mode."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_group_incoming(
        "@MyBot ping",
        sender_handle="OtherBot",
        sender_is_bot=True,
        mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_captioned_file_with_mention_reaches_file_dispatch():
    """Captioned files in groups route through the existing file
    dispatch path (NOT directly to _on_text). Mention is stripped from
    the caption (which carries on incoming.text) so the agent sees the
    cleaned caption alongside the uploaded file payload."""
    bot = _make_bot(respond_in_groups=True)
    bot._on_file_from_transport = AsyncMock()  # mock the file path too
    files = [IncomingFile(
        path=Path("/tmp/x.png"),
        original_name="x.png",
        mime_type="image/png",
        size_bytes=1024,
    )]
    incoming = _make_group_incoming(
        "@MyBot analyze this",
        mentions=[_bot_mention("MyBot")],
        files=files,
    )
    await bot._on_text_from_transport(incoming)
    # Files route to _on_file_from_transport, NOT _on_text.
    assert bot._on_file_from_transport.await_count == 1
    forwarded = bot._on_file_from_transport.await_args.args[0]
    assert forwarded.files == files
    assert forwarded.text == " analyze this"
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_text_when_flag_is_false_is_silent_via_filter():
    """With respond_in_groups=False, group messages don't reach _on_text
    even if they would have been addressed-at-me. (In production this is
    enforced by the PTB filter — here we verify the bot-side gate also
    refuses to process them defensively.)"""
    bot = _make_bot(respond_in_groups=False)
    incoming = _make_group_incoming(
        "@MyBot do X", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_message_unaffected_by_flag():
    """DM messages reach _on_text regardless of respond_in_groups setting."""
    bot = _make_bot(respond_in_groups=True)
    incoming = _make_dm_incoming("just a DM")
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # DM text isn't mention-stripped (no @mention typically).
    assert forwarded.text == "just a DM"


@pytest.mark.asyncio
async def test_group_explicit_mention_alongside_other_mention_reaches_on_text():
    """`@MyBot @SomeoneElse do X` → processed. An explicit `@MyBot` mention
    always wins in is_directed_at_me, regardless of other handles mentioned
    in the same message. Pins design-doc Edge cases table row 4."""
    bot = _make_bot(respond_in_groups=True)
    other = Identity(
        transport_id="fake", native_id="999",
        display_name="SomeoneElse", handle="SomeoneElse", is_bot=False,
    )
    incoming = _make_group_incoming(
        "@MyBot @SomeoneElse do X",
        mentions=[_bot_mention("MyBot"), other],
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # Only @MyBot is stripped; @SomeoneElse survives verbatim so the agent
    # sees who else was tagged in the prompt.
    assert forwarded.text == " @SomeoneElse do X"


@pytest.mark.asyncio
async def test_group_reply_to_bot_with_other_mention_is_silent():
    """Reply to bot + simultaneously @-mentions someone else → silent.
    Matches team-mode semantics in is_directed_at_me."""
    bot = _make_bot(respond_in_groups=True)
    other = Identity(
        transport_id="fake", native_id="999",
        display_name="OtherUser", handle="OtherUser", is_bot=False,
    )
    incoming = _make_group_incoming(
        "@OtherUser the bot said X",
        mentions=[other],  # mentions OtherUser, NOT MyBot
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_empty_text_after_strip_does_not_reach_on_text():
    """User types just '@MyBot' with nothing else AND no reply context.
    After stripping, the incoming has empty text and no files. The gate's
    fall-through routes to the existing 'Nothing actionable — unsupported'
    branch (which sends 'This message type is not supported.'). It does
    NOT call _on_text. (We don't tightly assert the unsupported reply
    text here — that's existing behavior under test elsewhere.)"""
    bot = _make_bot(respond_in_groups=True)
    # Stub _auth_identity so the unsupported-branch auth check doesn't
    # explode on missing brute-force-counter state we didn't set up.
    bot._auth_identity = lambda _identity: True
    incoming = _make_group_incoming(
        "@MyBot", mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_bare_mention_as_reply_uses_replied_to_text_as_prompt():
    """User replies to someone else's message with just '@MyBot' and no
    other text. Without this fix the bot bails out with 'unsupported
    media' because the stripped text is empty. With the fix, the bot
    treats the replied-to message content as the user's prompt and
    forwards it to _on_text.

    Validates the design call: when @bot has no payload after strip but
    the message is a reply to something else, the user's intent is
    'look at the replied-to message and respond to it.' The replied-to
    text becomes the prompt; reply_to_text is cleared on the forwarded
    IncomingMessage so _build_user_prompt doesn't double-prepend the
    same content under a '[Replying to: ...]' header.
    """
    bot = _make_bot(respond_in_groups=True)
    bot._auth_identity = lambda _identity: True
    incoming = _make_group_incoming(
        "@MyBot",
        mentions=[_bot_mention("MyBot")],
        reply_to_text="what's the meaning of life?",
        reply_to_sender=Identity(
            transport_id="fake", native_id="99",
            display_name="Alice", handle="alice_other",
            is_bot=False,
        ),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.text == "what's the meaning of life?"
    # reply_to_text cleared so _build_user_prompt doesn't double-prepend.
    assert forwarded.reply_to_text is None


@pytest.mark.asyncio
async def test_group_bare_mention_as_reply_to_bot_uses_replied_to_text():
    """Same lift as the previous test but the replied-to message is from
    the bot itself. is_directed_at_me already returned True via the
    mentions_bot path (top of the function); the reply-context promotion
    must also fire here so the user can answer their own ping-pong style
    flow with bare @mentions.
    """
    bot = _make_bot(respond_in_groups=True)
    bot._auth_identity = lambda _identity: True
    incoming = _make_group_incoming(
        "@MyBot",
        mentions=[_bot_mention("MyBot")],
        reply_to_text="pong",
        reply_to_sender=_bot_mention("MyBot"),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.text == "pong"
    assert forwarded.reply_to_text is None


@pytest.mark.asyncio
async def test_group_mention_with_text_in_reply_preserves_reply_to_text():
    """When the user DOES write something after @MyBot in a reply, the
    promotion-to-prompt path must NOT fire — the user-supplied text is
    the prompt, and reply_to_text stays populated so _build_user_prompt
    can prepend the '[Replying to: ...]' header (existing behavior).
    """
    bot = _make_bot(respond_in_groups=True)
    bot._auth_identity = lambda _identity: True
    incoming = _make_group_incoming(
        "@MyBot can you help with this?",
        mentions=[_bot_mention("MyBot")],
        reply_to_text="some earlier message",
        reply_to_sender=Identity(
            transport_id="fake", native_id="99",
            display_name="Alice", handle="alice_other",
            is_bot=False,
        ),
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    # @MyBot stripped; user-supplied text preserved.
    assert "can you help with this?" in forwarded.text
    # reply_to_text untouched — _build_user_prompt will prepend it.
    assert forwarded.reply_to_text == "some earlier message"


@pytest.mark.asyncio
async def test_group_edited_message_newly_including_mention_reaches_on_text():
    """When a user edits a previously plain group message to add `@MyBot`,
    PTB delivers it via the EDITED_MESSAGE filter, the transport adapter
    converts it into a normal IncomingMessage (no edited-vs-fresh marker),
    and the bot's gate processes it exactly like a fresh @mention.

    Pins the design-doc Edge cases row: "Edited message that newly
    includes @MyBot → Processed". From the bot's perspective an edited
    message is indistinguishable from a fresh one — both arrive as the
    same IncomingMessage shape — so the gate behavior is identical.
    """
    bot = _make_bot(respond_in_groups=True)
    # Simulate the IncomingMessage we'd get from `_dispatch_message` after
    # a user edits "hi all" → "hi all @MyBot do X". Same shape as a fresh
    # mention; the transport doesn't surface an is_edited flag.
    incoming = _make_group_incoming(
        "hi all @MyBot do X",
        mentions=[_bot_mention("MyBot")],
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_text.await_count == 1
    forwarded = bot._on_text.await_args.args[0]
    assert forwarded.text == "hi all  do X"  # @MyBot stripped


@pytest.mark.asyncio
async def test_group_voice_with_caption_mention_routes_to_voice_dispatch():
    """Voice notes in groups with @mention (via caption surfaced as text)
    route through _on_voice_from_transport, NOT _on_text. Same gate logic
    as captioned files."""
    bot = _make_bot(respond_in_groups=True)
    bot._on_voice_from_transport = AsyncMock()
    voice = [IncomingFile(
        path=Path("/tmp/v.ogg"),
        original_name="v.ogg",
        mime_type="audio/ogg",
        size_bytes=4096,
    )]
    incoming = _make_group_incoming(
        "@MyBot transcribe",
        mentions=[_bot_mention("MyBot")],
        files=voice,
    )
    await bot._on_text_from_transport(incoming)
    assert bot._on_voice_from_transport.await_count == 1
    bot._on_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_mention_by_native_id_when_bot_username_is_empty():
    """Transports without @handles (Google Chat) leave bot_username empty
    because self_identity.handle is None. Routing must still recognise that
    we were @-mentioned by matching `IncomingMessage.mentions[*]` against
    the transport's self_identity by (transport_id, native_id).
    """
    bot = _make_bot(respond_in_groups=True, bot_username="")
    # Stand in for a Google-Chat-style transport with a learned identity.
    bot._transport.self_identity = Identity(
        transport_id="fake",
        native_id="users/bot-app-1",
        display_name="LPTC",
        handle=None,
        is_bot=True,
    )
    bot_mention = Identity(
        transport_id="fake",
        native_id="users/bot-app-1",
        display_name="LPTC",
        handle=None,
        is_bot=True,
    )
    incoming = _make_group_incoming("@LPTC ping", mentions=[bot_mention])

    await bot._on_text_from_transport(incoming)

    assert bot._on_text.await_count == 1


@pytest.mark.asyncio
async def test_group_message_without_id_match_silent_when_bot_username_is_empty():
    """Negative: the id-match fallback only fires when mentions actually
    contain the bot. A drive-by message with no relevant mentions stays silent.
    """
    bot = _make_bot(respond_in_groups=True, bot_username="")
    bot._transport.self_identity = Identity(
        transport_id="fake",
        native_id="users/bot-app-1",
        display_name="LPTC",
        handle=None,
        is_bot=True,
    )
    incoming = _make_group_incoming("just chatter")

    await bot._on_text_from_transport(incoming)

    bot._on_text.assert_not_awaited()
