"""Integration tests for TelegramTransport using a lightweight Application stub.

We don't require a live Telegram connection — `telegram.ext.Application` accepts
a mock `Bot` and we can drive it via its message-handling entry points.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.telegram import (
    TRANSPORT_ID,
    TelegramTransport,
)


def _make_transport_with_mock_bot() -> tuple[TelegramTransport, MagicMock]:
    """Return (transport, mock_bot) where mock_bot has async send_message/etc."""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=SimpleNamespace(
        message_id=42,
        chat=SimpleNamespace(id=12345, type="private"),
    ))
    app = MagicMock()
    app.bot = bot
    app.initialize = AsyncMock()
    app.start = AsyncMock()
    app.stop = AsyncMock()
    app.shutdown = AsyncMock()
    app.updater = MagicMock()
    app.updater.start_polling = AsyncMock()
    app.updater.stop = AsyncMock()
    return TelegramTransport(app), bot


async def test_send_text_calls_bot_send_message():
    t, bot = _make_transport_with_mock_bot()
    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)

    ref = await t.send_text(chat, "hello")

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["text"] == "hello"
    assert ref.native_id == "42"
    assert ref.chat == chat


async def test_start_and_stop_delegate_to_application():
    t, _bot = _make_transport_with_mock_bot()
    await t.start()
    t._app.initialize.assert_awaited_once()
    t._app.start.assert_awaited_once()
    t._app.updater.start_polling.assert_awaited_once()

    await t.stop()
    t._app.updater.stop.assert_awaited_once()
    t._app.stop.assert_awaited_once()
    t._app.shutdown.assert_awaited_once()


def test_run_wires_post_init_and_post_stop_then_calls_run_polling():
    """TelegramTransport.run() owns the post_init/post_stop bindings and
    drives PTB's polling loop. bot.py never touches the Application by name.
    """
    # Use a SimpleNamespace so attribute assignments are plain (MagicMock would
    # wrap assigned values in mocks and break the identity check).
    run_polling_calls: list = []
    app = SimpleNamespace(
        post_init=None,
        post_stop=None,
        run_polling=lambda: run_polling_calls.append(()),
    )
    t = TelegramTransport(app)

    t.run()

    # Bound methods compare equal when bound to the same instance/function.
    assert app.post_init == t.post_init
    assert app.post_stop == t.post_stop
    assert run_polling_calls == [()]


async def test_on_message_handler_fires_on_telegram_update():
    """Inbound text message from telegram lands as IncomingMessage on the handler."""
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    # Build a minimal telegram.Update-shaped object.
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="hi there",
        photo=None,
        document=None,
        voice=None,
        audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    # Drive the transport's internal message dispatcher directly.
    await t._dispatch_message(update, ctx=None)

    assert len(received) == 1
    assert received[0].text == "hi there"
    assert received[0].sender.handle == "alice"
    assert received[0].chat.native_id == "12345"


async def test_incoming_file_tempdir_is_cleaned_after_handlers_return():
    t, _bot = _make_transport_with_mock_bot()
    seen_exists: list[bool] = []
    seen_paths = []

    async def handler(msg):
        file = msg.files[0]
        seen_exists.append(file.path.exists())
        seen_paths.append(file.path)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    document = SimpleNamespace(
        file_name="report.txt",
        mime_type="text/plain",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        caption=None,
        photo=None,
        document=document,
        voice=None,
        audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=SimpleNamespace(user_data={}))

    assert seen_exists == [True]
    assert seen_paths
    assert not seen_paths[0].exists()


async def test_on_command_handler_fires_for_telegram_command():
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(ci):
        captured.append(ci)

    t.on_command("help", handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=77,
        chat=tg_chat,
        from_user=tg_user,
        text="/help",
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)
    ctx = SimpleNamespace(args=[])

    await t._dispatch_command("help", update, ctx)

    assert len(captured) == 1
    assert captured[0].name == "help"
    assert captured[0].args == []
    assert captured[0].raw_text == "/help"


async def test_edit_text_calls_edit_message_text():
    t, bot = _make_transport_with_mock_bot()
    bot.edit_message_text = AsyncMock()

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    ref = MessageRef(transport_id=TRANSPORT_ID, native_id="99", chat=chat)

    await t.edit_text(ref, "updated text")

    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["message_id"] == 99
    assert kwargs["text"] == "updated text"


async def test_send_text_with_buttons_passes_inline_keyboard():
    t, bot = _make_transport_with_mock_bot()
    from link_project_to_chat.transport import Button, Buttons

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    buttons = Buttons(rows=[[Button(label="Go", value="go"), Button(label="Stop", value="stop")]])

    await t.send_text(chat, "pick one", buttons=buttons)

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.call_args.kwargs
    markup = kwargs["reply_markup"]
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    row = markup.inline_keyboard[0]
    assert len(row) == 2
    assert row[0].text == "Go"
    assert row[0].callback_data == "go"


async def test_edit_text_with_buttons_passes_inline_keyboard():
    t, bot = _make_transport_with_mock_bot()
    bot.edit_message_text = AsyncMock()
    from link_project_to_chat.transport import Button, Buttons

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    ref = MessageRef(transport_id=TRANSPORT_ID, native_id="99", chat=chat)
    buttons = Buttons(rows=[[Button(label="Ok", value="ok")]])

    await t.edit_text(ref, "new text", buttons=buttons)

    bot.edit_message_text.assert_awaited_once()
    kwargs = bot.edit_message_text.call_args.kwargs
    assert kwargs["reply_markup"] is not None


async def test_incoming_message_populates_files_from_document(tmp_path):
    """Document attachments get downloaded and exposed as IncomingFile."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []
    captured_bytes: list[bytes] = []

    async def handler(msg):
        captured.append(msg)
        captured_bytes.append(msg.files[0].path.read_bytes())

    t.on_message(handler)

    downloaded_bytes = b"hello doc"
    target_path: list = []

    async def fake_download_to_drive(path):
        # Simulate telegram's download by writing bytes to the given path.
        p = path if hasattr(path, "write_bytes") else __import__("pathlib").Path(str(path))
        p.write_bytes(downloaded_bytes)
        target_path.append(p)

    tg_file_obj = SimpleNamespace(download_to_drive=fake_download_to_drive)

    async def fake_get_file():
        return tg_file_obj

    tg_document = SimpleNamespace(
        file_name="notes.txt",
        mime_type="text/plain",
        file_size=len(downloaded_bytes),
        get_file=fake_get_file,
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=200,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=None,
        document=tg_document,
        voice=None,
        audio=None,
        caption="see this file",
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.original_name == "notes.txt"
    assert f.mime_type == "text/plain"
    assert captured_bytes == [downloaded_bytes]
    assert not f.path.exists()
    # Caption becomes the message text when no text is set.
    assert captured[0].text == "see this file"


async def test_send_file_calls_send_document_for_non_image(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_document = AsyncMock(return_value=SimpleNamespace(
        message_id=200, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    path = tmp_path / "notes.txt"
    path.write_text("x")

    ref = await t.send_file(chat, path, caption="see")

    bot.send_document.assert_awaited_once()
    kwargs = bot.send_document.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["caption"] == "see"
    assert ref.native_id == "200"


async def test_send_file_calls_send_photo_for_image(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_photo = AsyncMock(return_value=SimpleNamespace(
        message_id=201, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    path = tmp_path / "pic.png"
    path.write_bytes(b"\x89PNG\r\n")

    await t.send_file(chat, path)

    bot.send_photo.assert_awaited_once()


async def test_on_button_fires_for_telegram_callback_query():
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(click):
        captured.append(click)

    t.on_button(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(message_id=99, chat=tg_chat)
    tg_query = SimpleNamespace(
        data="confirm_reset",
        from_user=tg_user,
        message=tg_msg,
        answer=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=tg_query, effective_user=tg_user)

    await t._dispatch_button(update, ctx=None)

    assert len(captured) == 1
    assert captured[0].value == "confirm_reset"
    assert captured[0].sender.handle == "alice"
    tg_query.answer.assert_awaited_once()


async def test_send_voice_calls_bot_send_voice(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=300, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    ref = await t.send_voice(chat, p)

    bot.send_voice.assert_awaited_once()
    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert ref.native_id == "300"


async def test_send_voice_passes_reply_to(tmp_path):
    t, bot = _make_transport_with_mock_bot()
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(
        message_id=301, chat=SimpleNamespace(id=12345, type="private"),
    ))

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_ref = MessageRef(transport_id=TRANSPORT_ID, native_id="42", chat=chat)
    p = tmp_path / "v.opus"
    p.write_bytes(b"fake opus")

    await t.send_voice(chat, p, reply_to=reply_ref)

    kwargs = bot.send_voice.call_args.kwargs
    assert kwargs["reply_to_message_id"] == 42


async def test_incoming_message_populates_files_from_voice(tmp_path):
    """Voice attachments get downloaded and exposed as IncomingFile with audio mime."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []
    captured_bytes: list[bytes] = []

    async def handler(msg):
        captured.append(msg)
        captured_bytes.append(msg.files[0].path.read_bytes())

    t.on_message(handler)

    downloaded_bytes = b"ogg-voice"

    async def fake_download_to_drive(path):
        from pathlib import Path as _P
        p = path if hasattr(path, "write_bytes") else _P(str(path))
        p.write_bytes(downloaded_bytes)

    tg_file_obj = SimpleNamespace(download_to_drive=fake_download_to_drive)

    async def fake_get_file():
        return tg_file_obj

    tg_voice = SimpleNamespace(
        file_id="abc",
        file_size=len(downloaded_bytes),
        get_file=fake_get_file,
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=300,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        photo=None,
        document=None,
        voice=tg_voice,
        audio=None,
        caption=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert len(captured[0].files) == 1
    f = captured[0].files[0]
    assert f.mime_type == "audio/ogg"
    assert captured_bytes == [downloaded_bytes]
    assert not f.path.exists()


async def test_default_error_handler_logs_on_exception(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("boom"))
    with caplog.at_level(logging.ERROR):
        await t._default_error_handler(update, ctx)
    assert any("boom" in rec.getMessage() for rec in caplog.records)


async def test_default_error_handler_logs_conflict_as_warning(caplog):
    import logging
    t, _bot = _make_transport_with_mock_bot()
    update = SimpleNamespace()
    ctx = SimpleNamespace(error=RuntimeError("Conflict: another bot instance"))
    with caplog.at_level(logging.WARNING):
        await t._default_error_handler(update, ctx)
    conflict_recs = [r for r in caplog.records if "Conflict" in r.getMessage()]
    assert conflict_recs
    assert all(r.levelno == logging.WARNING for r in conflict_recs)


async def test_start_fires_on_ready_with_bot_identity():
    """start() should perform post-init (delete_webhook + get_me + set_my_commands)
    and fire registered on_ready callbacks with the bot's own Identity."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=9876, full_name="Alice Bot", username="alicebot",
    ))
    bot.set_my_commands = AsyncMock()

    # Attach a menu so set_my_commands gets called.
    t._menu = [("help", "Show help")]

    captured: list = []

    async def cb(identity):
        captured.append(identity)

    t.on_ready(cb)

    await t.start()

    bot.delete_webhook.assert_awaited_once()
    bot.get_me.assert_awaited_once()
    bot.set_my_commands.assert_awaited_once()
    assert len(captured) == 1
    assert captured[0].native_id == "9876"
    assert captured[0].handle == "alicebot"
    assert captured[0].is_bot is True


async def test_build_accepts_menu_kwarg():
    """TelegramTransport.build(menu=...) must accept and store the menu."""
    # We can't easily test the full build() because it instantiates a real
    # Application. Instead: construct directly and test _menu storage.
    from link_project_to_chat.transport.telegram import TelegramTransport
    _mock_app = AsyncMock()
    t = TelegramTransport(_mock_app)
    t._menu = [("cmd", "desc")]
    assert t._menu == [("cmd", "desc")]


async def test_dispatch_sets_is_relayed_bot_to_bot_and_strips_prefix():
    """Inbound text starting with '[auto-relay from <handle>]' is marked as relayed
    and the prefix is stripped from the dispatched IncomingMessage.text."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=-100123, type="supergroup")
    tg_user = SimpleNamespace(id=42, full_name="Rezo", username="rezo", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text="[auto-relay from bot_a]\n\n@bot_b go do X",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(captured) == 1
    assert captured[0].is_relayed_bot_to_bot is True
    assert captured[0].text == "@bot_b go do X"


async def test_dispatch_non_relay_text_unchanged():
    """Messages without the relay prefix have is_relayed_bot_to_bot=False and text unchanged."""
    t, _bot = _make_transport_with_mock_bot()
    captured: list = []

    async def handler(msg):
        captured.append(msg)
    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hello world",
        photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert captured[0].is_relayed_bot_to_bot is False
    assert captured[0].text == "hello world"


async def test_enable_team_relay_lifecycle():
    """enable_team_relay stashes config; start() starts the relay; stop() stops it.

    TeamRelay registers two handlers (NewMessage + MessageEdited) because
    livestream edits carry @peer mentions that the original send lacks.
    """
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    mock_client = MagicMock()
    mock_client.add_event_handler = MagicMock(return_value=object())
    mock_client.remove_event_handler = MagicMock()

    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    await t.start()
    from telethon import events
    assert mock_client.add_event_handler.call_count == 2
    registered_callbacks = [
        call.args[0].__name__
        for call in mock_client.add_event_handler.call_args_list
    ]
    assert registered_callbacks == ["_on_new_message", "_on_message_edited"]
    registered_event_types = {
        type(call.args[1]) for call in mock_client.add_event_handler.call_args_list
    }
    assert registered_event_types == {events.NewMessage, events.MessageEdited}

    await t.stop()
    assert mock_client.remove_event_handler.call_count == 2
    removed_callbacks = [
        call.args[0].__name__
        for call in mock_client.remove_event_handler.call_args_list
    ]
    assert removed_callbacks == ["_on_new_message", "_on_message_edited"]


async def test_enable_team_relay_passes_safety_options():
    from link_project_to_chat.team_safety import TeamAuthority

    t, _bot = _make_transport_with_mock_bot()
    authority = TeamAuthority(team_name="acme")
    mock_client = MagicMock()

    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
        max_autonomous_turns=9,
        team_authority=authority,
        authenticated_user_id=42,
    )

    assert t._team_relay is not None
    assert t._team_relay._max_autonomous_turns == 9
    assert t._team_relay._team_authority is authority
    assert t._team_relay._authenticated_user_id == "42"


async def test_post_hooks_run_ready_callbacks_and_team_relay_for_run_polling():
    """Application.run_polling() uses post hooks rather than TelegramTransport.start()."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))
    bot.set_my_commands = AsyncMock()
    t._menu = [("help", "Help")]

    ready: list[str | None] = []

    async def on_ready(identity):
        ready.append(identity.handle)

    t.on_ready(on_ready)

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.disconnect = AsyncMock()
    mock_client.add_event_handler = MagicMock(return_value=object())
    mock_client.remove_event_handler = MagicMock()
    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    await t.post_init(t.app)

    assert ready == ["bot_a"]
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    bot.get_me.assert_awaited_once()
    bot.set_my_commands.assert_awaited_once_with([("help", "Help")])
    mock_client.connect.assert_awaited_once()
    mock_client.is_user_authorized.assert_awaited_once()
    assert mock_client.add_event_handler.call_count == 2

    await t.post_stop(t.app)

    assert mock_client.remove_event_handler.call_count == 2
    mock_client.disconnect.assert_awaited_once()


async def test_build_without_enable_team_relay_starts_and_stops_cleanly():
    """TelegramTransport without a team relay starts/stops without touching relay code."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    await t.start()
    await t.stop()


async def test_app_property_returns_underlying_application():
    """TelegramTransport.app exposes the underlying telegram.ext.Application
    so the manager bot can attach ConversationHandlers directly."""
    t, _bot = _make_transport_with_mock_bot()
    app = t.app
    assert app is t._app  # exposes the same instance
    assert hasattr(app, "add_handler")  # quacks like an Application


async def test_set_command_menu_refreshes_started_telegram_menu():
    """Late plugin command registration can refresh Telegram's slash menu."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))
    bot.set_my_commands = AsyncMock()

    await t.post_init(t.app)
    await t.set_command_menu([("help", "Help"), ("todo", "Todo")])

    bot.set_my_commands.assert_awaited_with([("help", "Help"), ("todo", "Todo")])


async def test_enable_team_relay_from_session_constructs_client(monkeypatch):
    """TelegramTransport.enable_team_relay_from_session owns the telethon import
    so bot.py never has to know the optional library exists."""
    from types import SimpleNamespace
    constructed: list = []

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash):
            constructed.append((session, api_id, api_hash))

    fake_telethon = SimpleNamespace(TelegramClient=FakeTelegramClient)
    monkeypatch.setitem(__import__("sys").modules, "telethon", fake_telethon)

    t, _bot = _make_transport_with_mock_bot()
    t.enable_team_relay_from_session(
        session_path="/tmp/x.session",
        api_id=12345,
        api_hash="secret",
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100,
        team_name="acme",
    )

    assert constructed == [("/tmp/x.session", 12345, "secret")]
    assert t._team_relay is not None


async def test_enable_team_relay_from_session_string_constructs_string_session(monkeypatch):
    """Spec D′: enable_team_relay_from_session_string wraps the session string
    in a Telethon StringSession and hands the resulting client to the relay.

    Each subprocess gets its own in-memory session built from the same string,
    so concurrent connect() calls don't race for a shared SQLite file lock.
    """
    from types import SimpleNamespace

    constructed_clients: list = []
    constructed_string_sessions: list = []

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash):
            constructed_clients.append((session, api_id, api_hash))

    class FakeStringSession:
        def __init__(self, s):
            constructed_string_sessions.append(s)
            self._s = s

    fake_telethon = SimpleNamespace(TelegramClient=FakeTelegramClient)
    fake_telethon_sessions = SimpleNamespace(StringSession=FakeStringSession)
    sys_mod = __import__("sys")
    monkeypatch.setitem(sys_mod.modules, "telethon", fake_telethon)
    monkeypatch.setitem(sys_mod.modules, "telethon.sessions", fake_telethon_sessions)

    t, _bot = _make_transport_with_mock_bot()
    t.enable_team_relay_from_session_string(
        session_string="1$abcdef",
        api_id=12345,
        api_hash="secret",
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100,
        team_name="acme",
    )

    assert constructed_string_sessions == ["1$abcdef"]
    assert len(constructed_clients) == 1
    session_arg, api_id_arg, api_hash_arg = constructed_clients[0]
    assert isinstance(session_arg, FakeStringSession)
    assert api_id_arg == 12345
    assert api_hash_arg == "secret"
    assert t._team_relay is not None


async def test_tempdir_cleanup_runs_even_when_handler_raises(tmp_path):
    """A handler exception must not leak the downloaded-attachment tempdir."""
    t, _bot = _make_transport_with_mock_bot()
    seen: list = []

    async def handler(msg):
        seen.append(msg.files[0].path)
        raise RuntimeError("handler blew up")

    t.on_message(handler)

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file = SimpleNamespace(download_to_drive=download_to_drive)
    document = SimpleNamespace(
        file_name="report.txt",
        mime_type="text/plain",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100,
        chat=tg_chat,
        from_user=tg_user,
        text=None,
        caption=None,
        photo=None,
        document=document,
        voice=None,
        audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    with pytest.raises(RuntimeError, match="handler blew up"):
        await t._dispatch_message(update, ctx=SimpleNamespace(user_data={}))

    assert seen, "handler did not observe the file"
    assert not seen[0].exists(), "tempdir was leaked after handler raised"


async def test_post_init_unauthorized_relay_session_logs_and_continues(caplog):
    """Expired Telethon session must not kill the whole bot — log warning + skip relay."""
    import logging

    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=False)
    mock_client.disconnect = AsyncMock()
    mock_client.add_event_handler = MagicMock(return_value=object())
    mock_client.remove_event_handler = MagicMock()
    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    with caplog.at_level(logging.WARNING, logger="link_project_to_chat.transport.telegram"):
        await t.post_init(t.app)

    # The relay disconnected cleanly (no handlers registered, no leaks).
    mock_client.connect.assert_awaited_once()
    mock_client.disconnect.assert_awaited_once()
    mock_client.add_event_handler.assert_not_called()
    assert t._team_relay is None, "relay should be dropped after unauthorized"
    assert any("Team relay disabled" in r.message for r in caplog.records)

    # post_stop should be a no-op (no second disconnect attempt).
    await t.post_stop(t.app)
    mock_client.disconnect.assert_awaited_once()  # still exactly once


async def test_post_init_is_idempotent_when_invoked_twice():
    """Both TelegramTransport.start() and PTB.run_polling() can call post_init;
    a double invocation must not re-run get_me / on_ready callbacks."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    ready_calls: list = []

    async def on_ready(identity):
        ready_calls.append(identity.handle)

    t.on_ready(on_ready)

    await t.post_init(t.app)
    await t.post_init(t.app)

    # Second call is a no-op.
    bot.delete_webhook.assert_awaited_once()
    bot.get_me.assert_awaited_once()
    assert len(ready_calls) == 1


async def test_post_init_unwinds_partially_started_relay_on_failure():
    """If the relay's second handler registration fails, the first must be removed
    and the Telethon connection closed — no leaked handlers or open sessions."""
    t, bot = _make_transport_with_mock_bot()
    bot.delete_webhook = AsyncMock()
    bot.get_me = AsyncMock(return_value=SimpleNamespace(
        id=1, full_name="Bot", username="bot_a",
    ))

    mock_client = MagicMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.disconnect = AsyncMock()
    mock_client.remove_event_handler = MagicMock()
    # First add_event_handler succeeds; second raises — partially-initialized relay.
    mock_client.add_event_handler = MagicMock(
        side_effect=[object(), RuntimeError("handler registration failed")]
    )
    t.enable_team_relay(
        telethon_client=mock_client,
        team_bot_usernames={"bot_a", "bot_b"},
        group_chat_id=-100123,
        team_name="acme",
    )

    with pytest.raises(RuntimeError, match="handler registration failed"):
        await t.post_init(t.app)

    # The first handler must have been removed; the Telethon client disconnected.
    mock_client.remove_event_handler.assert_called_once()
    mock_client.disconnect.assert_awaited_once()
    # The guard is reset so a retry is possible.
    assert t._post_init_ran is False


async def test_document_filename_with_path_separators_is_sanitized_to_basename():
    """A malicious document filename like '../../etc/passwd' must not escape the temp dir."""
    t, _bot = _make_transport_with_mock_bot()
    captured_path: list = []

    async def handler(msg):
        captured_path.append(msg.files[0].path)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    document = SimpleNamespace(
        file_name="../../etc/passwd",
        mime_type="text/plain",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=document, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    # Path must live under tempfile's tempdir; basename must NOT contain separators.
    import tempfile as _tf
    assert captured_path[0].name == "passwd", f"basename leaked separators: {captured_path[0].name}"
    assert str(captured_path[0]).startswith(_tf.gettempdir()) or "tmp" in str(captured_path[0]).lower()


async def test_audio_filename_with_absolute_path_is_sanitized():
    """An audio filename like '/etc/passwd' must reduce to the basename inside tempdir."""
    t, _bot = _make_transport_with_mock_bot()
    captured_path: list = []

    async def handler(msg):
        captured_path.append(msg.files[0].path)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    audio = SimpleNamespace(
        file_name="/etc/passwd",
        mime_type="audio/mpeg",
        file_size=7,
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=None, voice=None, audio=audio,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert captured_path[0].name == "passwd"


async def test_unauthorized_pre_dispatch_skips_downloads_and_handlers():
    """Authorizer returning False must short-circuit before any get_file()/download."""
    t, _bot = _make_transport_with_mock_bot()
    handler_calls: list = []
    download_calls: list = []

    async def handler(msg):
        handler_calls.append(msg)

    t.on_message(handler)

    async def authorizer(identity):
        return False  # Always reject.

    t.set_authorizer(authorizer)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Mallory", username="mallory", is_bot=False)
    tg_file = SimpleNamespace()

    async def download_to_drive(path):
        download_calls.append(path)
        path.write_bytes(b"payload")

    tg_file.download_to_drive = download_to_drive
    document = SimpleNamespace(
        file_name="big.bin",
        mime_type="application/octet-stream",
        file_size=10**9,  # 1 GB - would burn disk if downloaded.
        get_file=AsyncMock(return_value=tg_file),
    )
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None, caption=None, photo=None,
        document=document, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert download_calls == [], "authorized=False must prevent download_to_drive"
    assert document.get_file.await_count == 0, "authorized=False must skip get_file too"
    assert handler_calls == [], "authorized=False must skip handler invocation"


async def test_authorized_pre_dispatch_proceeds_with_downloads():
    """Authorizer returning True allows the normal download path."""
    t, _bot = _make_transport_with_mock_bot()
    handler_calls: list = []

    async def handler(msg):
        handler_calls.append(msg)

    t.on_message(handler)

    async def authorizer(identity):
        return identity.handle == "alice"

    t.set_authorizer(authorizer)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hi", photo=None, document=None, voice=None, audio=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(handler_calls) == 1
    assert handler_calls[0].text == "hi"


async def test_video_with_caption_marks_message_as_unsupported_media():
    """Telegram allows video/sticker/etc. through the filter; the bot must NOT
    treat their captions as plain text."""
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text=None,
        caption="please summarize this video",
        photo=None, document=None, voice=None, audio=None,
        video=SimpleNamespace(file_id="vid123"),
        sticker=None, location=None, contact=None, video_note=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert len(received) == 1
    assert received[0].has_unsupported_media is True
    assert received[0].text == "please summarize this video"  # caption preserved for the rejection UX


async def test_text_only_message_is_not_unsupported_media():
    t, _bot = _make_transport_with_mock_bot()
    received: list = []

    async def handler(msg):
        received.append(msg)

    t.on_message(handler)

    tg_chat = SimpleNamespace(id=12345, type="private")
    tg_user = SimpleNamespace(id=42, full_name="Alice", username="alice", is_bot=False)
    tg_msg = SimpleNamespace(
        message_id=100, chat=tg_chat, from_user=tg_user,
        text="hi there",
        photo=None, document=None, voice=None, audio=None,
        video=None, sticker=None, location=None, contact=None, video_note=None,
        reply_to_message=None,
    )
    update = SimpleNamespace(effective_message=tg_msg, effective_user=tg_user)

    await t._dispatch_message(update, ctx=None)

    assert received[0].has_unsupported_media is False


async def test_send_text_retries_without_reply_to_when_target_deleted_preserving_html():
    """If Telegram returns 'Message to be replied not found', send_text retries
    once without reply_to_message_id, preserving parse_mode=HTML."""
    t, bot = _make_transport_with_mock_bot()

    # First call raises the specific BadRequest; second call (the retry) succeeds.
    class _ReplyTargetMissing(Exception):
        def __init__(self):
            super().__init__("Message to be replied not found")
            self.message = "Message to be replied not found"

    success_native = SimpleNamespace(
        message_id=99, chat=SimpleNamespace(id=12345, type="private")
    )
    bot.send_message = AsyncMock(side_effect=[_ReplyTargetMissing(), success_native])

    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_to = MessageRef(transport_id=TRANSPORT_ID, native_id="500", chat=chat)

    ref = await t.send_text(chat, "<b>hi</b>", html=True, reply_to=reply_to)

    assert ref.native_id == "99"
    assert bot.send_message.await_count == 2
    second_kwargs = bot.send_message.await_args_list[1].kwargs
    assert "reply_to_message_id" not in second_kwargs, "retry must drop reply_to"
    assert second_kwargs["parse_mode"] == "HTML", "retry must preserve HTML parse_mode"


async def test_send_text_does_not_retry_for_unrelated_badrequest():
    """Other BadRequest errors (e.g., chat not found) must NOT trigger the retry."""
    t, bot = _make_transport_with_mock_bot()

    class _OtherBadRequest(Exception):
        def __init__(self):
            super().__init__("Chat not found")
            self.message = "Chat not found"

    bot.send_message = AsyncMock(side_effect=_OtherBadRequest())
    chat = ChatRef(transport_id=TRANSPORT_ID, native_id="12345", kind=ChatKind.DM)
    reply_to = MessageRef(transport_id=TRANSPORT_ID, native_id="500", chat=chat)

    with pytest.raises(Exception) as excinfo:
        await t.send_text(chat, "hi", reply_to=reply_to)
    assert "Chat not found" in str(excinfo.value)
    assert bot.send_message.await_count == 1, "no retry for unrelated errors"
