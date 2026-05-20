"""Manager wizard-handler hardening tests (audit findings 1-10).

Covers groups A-F from the manager-bot audit:

* Group A: wizard-handler construction + cleanup hardening (findings 1, 2, 5, 7)
* Group B: state-loss-before-persist in google_chat wizards (finding 3)
* Group C: KeyError-prone team-flow access (finding 6)
* Group D: Telethon client leak in managed-project cleanup (finding 8)
* Group E: Telethon timeouts (findings 4 + 9)
* Group F: silent button-branch fallthroughs (finding 10)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─── helpers ────────────────────────────────────────────────────────────────


def _make_text_update(text: str, user_id: int = 1, username: str = "tester"):
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.full_name = username
    user.is_bot = False
    chat = MagicMock()
    chat.id = user_id
    chat.type = "private"
    msg = MagicMock()
    msg.message_id = 1
    msg.chat = chat
    msg.text = text
    update = MagicMock()
    update.callback_query = None
    update.message = msg
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = msg
    return update


def _make_button_update(callback_data: str, user_id: int = 1, username: str = "tester"):
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.full_name = username
    user.is_bot = False
    chat = MagicMock()
    chat.id = user_id
    chat.type = "private"
    msg = MagicMock()
    msg.message_id = 1
    msg.chat = chat
    query = MagicMock()
    query.data = callback_data
    query.message = msg
    query.answer = AsyncMock()
    query.from_user = user
    update = MagicMock()
    update.callback_query = query
    update.message = None
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = msg
    return update


def _fake_msg_ref():
    from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef

    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    return MessageRef(transport_id="fake", native_id="1", chat=chat)


def _fake_chat():
    from link_project_to_chat.transport import ChatKind, ChatRef

    return ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)


def _make_manager(tmp_path, cfg_path=None):
    """Build a fresh ManagerBot with a FakeTransport and the executor-guard
    bypassed (the wizard guards are not the subject under test here)."""
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    if cfg_path is None:
        cfg_path = tmp_path / "config.json"
        save_config(
            Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"),
            cfg_path,
        )
        (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()
    mb._guard_executor = AsyncMock(return_value=True)
    mb._guard_executor_invocation = AsyncMock(return_value=True)
    return mb, cfg_path


def _make_invocation():
    from link_project_to_chat.transport.base import (
        ChatKind,
        ChatRef,
        CommandInvocation,
        Identity,
        MessageRef,
    )

    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="telegram",
        native_id="1",
        display_name="op",
        handle="op",
        is_bot=False,
    )
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    return CommandInvocation(
        chat=chat,
        sender=sender,
        name="setup",
        args=[],
        raw_text="",
        message=msg,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Group A — wizard-handler construction + cleanup hardening (1, 2, 5, 7)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_create_project_load_config_raises_sends_user_error(tmp_path, monkeypatch):
    """Finding 5: _on_create_project must surface an in-chat error when
    load_config raises rather than crashing the callback silently."""
    from telegram.ext import ConversationHandler

    mb, _ = _make_manager(tmp_path)

    def boom(_path):
        raise ValueError("malformed config")

    monkeypatch.setattr("link_project_to_chat.manager.bot.load_config", boom, raising=False)
    # Also patch the local import path used inside the handler.
    monkeypatch.setattr("link_project_to_chat.config.load_config", boom)

    ctx = MagicMock()
    ctx.user_data = {}
    state = await mb._on_create_project(_make_text_update("/create_project"), ctx)

    assert state == ConversationHandler.END
    assert mb._transport.sent_messages, "user must receive an error message"
    last = mb._transport.sent_messages[-1].text.lower()
    assert "config" in last and ("error" in last or "could not" in last)


@pytest.mark.asyncio
async def test_on_setup_from_transport_load_config_raises_sends_user_error(tmp_path, monkeypatch):
    """Finding 5: _on_setup_from_transport must surface an in-chat error
    when load_config raises (config is malformed on disk)."""
    mb, _ = _make_manager(tmp_path)

    def boom(_path):
        raise ValueError("malformed config")

    monkeypatch.setattr("link_project_to_chat.config.load_config", boom)

    inv = _make_invocation()
    await mb._on_setup_from_transport(inv)

    assert mb._transport.sent_messages, "user must receive an error message"
    last = mb._transport.sent_messages[-1].text.lower()
    assert "config" in last and ("error" in last or "could not" in last)


@pytest.mark.asyncio
async def test_execute_bot_creation_botfather_import_error_sends_message(tmp_path, monkeypatch):
    """Finding 1: BotFatherClient construction (and the local import that
    feeds it) must be inside the try/except so an ImportError surfaces to
    the user instead of bubbling up and killing the wizard silently."""
    from telegram.ext import ConversationHandler

    mb, cfg_path = _make_manager(tmp_path)

    # Patch the botfather module to raise on import access. Because the
    # handler does `from ..botfather import BotFatherClient` inside the
    # function body, we hijack the actual symbol so accessing it during
    # import raises ImportError.
    import link_project_to_chat.botfather as botfather

    real_BotFatherClient = botfather.BotFatherClient

    def boom(*a, **kw):
        raise ImportError("telethon not installed")

    monkeypatch.setattr(botfather, "BotFatherClient", boom)

    ctx = MagicMock()
    ctx.user_data = {
        "create": {
            "config_path": str(cfg_path),
            "name": "project",
        }
    }

    state = await mb._execute_bot_creation(_fake_chat(), ctx, "project")

    # Should NOT have propagated the ImportError.
    assert state == ConversationHandler.END or state == mb.CREATE_BOT
    assert mb._transport.sent_messages, "user must receive a failure message"
    last = mb._transport.sent_messages[-1].text.lower()
    assert "bot" in last and ("fail" in last or "error" in last)


@pytest.mark.asyncio
async def test_execute_bot_creation_disconnect_failure_does_not_mask_original(
    tmp_path, monkeypatch,
):
    """Findings 1, 7: when create_bot raises and bf.disconnect() ALSO raises
    in the finally block, the original error must be the one the user sees
    (and the disconnect error must be logged, not propagated)."""
    mb, cfg_path = _make_manager(tmp_path)

    class FakeBF:
        def __init__(self, *a, **kw):
            pass

        async def create_bot(self, *a, **kw):
            raise RuntimeError("create_bot failed badly")

        async def disconnect(self):
            raise RuntimeError("disconnect also failed")

    monkeypatch.setattr(
        "link_project_to_chat.botfather.BotFatherClient", FakeBF,
    )
    monkeypatch.setattr(
        "link_project_to_chat.botfather.sanitize_bot_username", lambda s: f"{s}_bot",
    )

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path), "name": "p"}}

    # Should NOT raise — both errors are swallowed by the handler.
    await mb._execute_bot_creation(_fake_chat(), ctx, "p")

    # User must see the ORIGINAL create_bot error, not the disconnect error.
    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text
    assert "create_bot failed badly" in last
    assert "disconnect also failed" not in last


@pytest.mark.asyncio
async def test_delete_team_execute_load_config_raises_sends_error(tmp_path, monkeypatch):
    """Finding 5: _delete_team_execute must surface load_config errors
    instead of silently dying."""
    mb, _ = _make_manager(tmp_path)

    def boom(_path):
        raise ValueError("malformed config")

    # _load_team_delete_dependencies must succeed (it's the first risky call).
    fake_bfc_cls = MagicMock()
    fake_delete = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.manager.bot._load_team_delete_dependencies",
        lambda: (fake_bfc_cls, fake_delete),
    )
    monkeypatch.setattr("link_project_to_chat.config.load_config", boom)

    await mb._delete_team_execute(_fake_chat(), "myteam")

    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text.lower()
    assert "config" in last and ("error" in last or "could not" in last)


# ═══════════════════════════════════════════════════════════════════════════
# Group B — state-loss-before-persist in google_chat wizards (3)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_finalize_add_gchat_persist_failure_preserves_wizard_state(
    tmp_path, monkeypatch,
):
    """Finding 3: if _patch_json raises during finalize, the wizard state
    must NOT be lost (operator can retry) AND the user gets an error message."""
    mb, cfg_path = _make_manager(tmp_path)

    # _patch_json raises mid-save.
    def boom(_patch, _path):
        raise OSError("disk full")

    monkeypatch.setattr("link_project_to_chat.config._patch_json", boom)

    ctx = MagicMock()
    wizard = {
        "name": "alpha",
        "data": {
            "port": 8080,
            "service_account_file": "/etc/sa.json",
            "public_url": "https://alpha.example.com",
            "root_command_id": "ROOT",
        },
    }
    ctx.user_data = {"gchat_wizard": wizard}

    await mb._finalize_add_google_chat_wizard(ctx, wizard, _fake_chat())

    # Wizard state must still be present after the failed write.
    assert "gchat_wizard" in ctx.user_data
    # User must see an error message.
    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text.lower()
    assert "error" in last or "fail" in last


@pytest.mark.asyncio
async def test_finalize_edit_gchat_persist_failure_preserves_wizard_state(
    tmp_path, monkeypatch,
):
    """Finding 3: same as above for _finalize_edit_gchat_wizard."""
    mb, cfg_path = _make_manager(tmp_path)

    def boom(_patch, _path):
        raise OSError("disk full")

    monkeypatch.setattr("link_project_to_chat.config._patch_json", boom)

    ctx = MagicMock()
    wizard = {
        "name": "alpha",
        "data": {
            "port": 8080,
            "service_account_file": "/etc/sa.json",
            "public_url": "https://alpha.example.com",
            "root_command_id": "ROOT",
        },
    }
    ctx.user_data = {"gchat_wizard": wizard}

    await mb._finalize_edit_gchat_wizard(ctx, wizard, _fake_chat())

    assert "gchat_wizard" in ctx.user_data
    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text.lower()
    assert "error" in last or "fail" in last


@pytest.mark.asyncio
async def test_finalize_add_gchat_missing_project_surfaces_error(tmp_path, monkeypatch):
    """Finding 3: when the project disappears between wizard start and
    finalize, the user must get a clear error (no silent no-op)."""
    mb, cfg_path = _make_manager(tmp_path)

    # Write a config with no matching project so _patch's inner check
    # ``project = projects.get(name)`` returns None.
    with cfg_path.open("w") as f:
        json.dump({}, f)

    ctx = MagicMock()
    wizard = {
        "name": "nonexistent",
        "data": {
            "port": 8080,
            "service_account_file": "/etc/sa.json",
            "public_url": "https://x.example.com",
            "root_command_id": "ROOT",
        },
    }
    ctx.user_data = {"gchat_wizard": wizard}

    await mb._finalize_add_google_chat_wizard(ctx, wizard, _fake_chat())

    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text.lower()
    assert "not found" in last or "missing" in last or "no such project" in last


# ═══════════════════════════════════════════════════════════════════════════
# Group C — KeyError-prone team-flow access (6)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_team_name_missing_session_exits_gracefully(tmp_path):
    """Finding 6: _create_team_name must NOT KeyError when
    ctx.user_data['create_team'] is missing — it should say session expired."""
    from telegram.ext import ConversationHandler

    mb, _ = _make_manager(tmp_path)

    ctx = MagicMock()
    ctx.user_data = {}  # no create_team slot

    state = await mb._create_team_name(_make_text_update("myteam"), ctx)

    assert state == ConversationHandler.END
    assert mb._transport.sent_messages
    last = mb._transport.sent_messages[-1].text.lower()
    assert "session" in last or "create_team" in last


@pytest.mark.asyncio
async def test_create_team_persona_mgr_missing_session_exits_gracefully(tmp_path):
    """Finding 6: same for _create_team_persona_mgr_callback."""
    from telegram.ext import ConversationHandler

    mb, _ = _make_manager(tmp_path)

    ctx = MagicMock()
    ctx.user_data = {}

    state = await mb._create_team_persona_mgr_callback(
        _make_button_update("ct_persona_mgr:developer"), ctx,
    )

    assert state == ConversationHandler.END
    assert mb._transport.edited_messages or mb._transport.sent_messages


@pytest.mark.asyncio
async def test_create_team_persona_dev_missing_session_exits_gracefully(tmp_path):
    """Finding 6: same for _create_team_persona_dev_callback."""
    from telegram.ext import ConversationHandler

    mb, _ = _make_manager(tmp_path)

    ctx = MagicMock()
    ctx.user_data = {}

    state = await mb._create_team_persona_dev_callback(
        _make_button_update("ct_persona_dev:developer"), ctx,
    )

    assert state == ConversationHandler.END


@pytest.mark.asyncio
async def test_create_team_execute_missing_session_exits_gracefully(tmp_path):
    """Finding 6: _create_team_execute must NOT KeyError when state is gone."""
    from telegram.ext import ConversationHandler

    mb, cfg_path = _make_manager(tmp_path)

    ctx = MagicMock()
    ctx.user_data = {}

    state = await mb._create_team_execute(_make_text_update("anything"), ctx)
    assert state == ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════
# Group D — BotFather leak in cleanup (8)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cleanup_managed_project_resources_disconnects_bfc(tmp_path, monkeypatch):
    """Finding 8: _cleanup_managed_project_resources must call bfc.disconnect()
    after delete_bot, even on success — currently the Telethon client leaks."""
    mb, cfg_path = _make_manager(tmp_path)

    disconnect_calls = []

    class FakeBFC:
        def __init__(self, **kwargs):
            pass

        async def delete_bot(self, username):
            return None

        async def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(
        "link_project_to_chat.manager.bot._load_botfather_dependency",
        lambda: FakeBFC,
    )

    project = {
        "managed_by_manager": True,
        "managed_bot_username": "@victim_bot",
    }

    await mb._cleanup_managed_project_resources(project)

    assert disconnect_calls, "bfc.disconnect must be called after delete_bot"


@pytest.mark.asyncio
async def test_cleanup_managed_project_resources_disconnects_after_delete_error(
    tmp_path, monkeypatch,
):
    """Finding 8: even if delete_bot raises, disconnect must still happen."""
    mb, cfg_path = _make_manager(tmp_path)

    disconnect_calls = []

    class FakeBFC:
        def __init__(self, **kwargs):
            pass

        async def delete_bot(self, username):
            raise RuntimeError("delete failed")

        async def disconnect(self):
            disconnect_calls.append(True)

    monkeypatch.setattr(
        "link_project_to_chat.manager.bot._load_botfather_dependency",
        lambda: FakeBFC,
    )

    project = {
        "managed_by_manager": True,
        "managed_bot_username": "@victim_bot",
    }

    await mb._cleanup_managed_project_resources(project)

    assert disconnect_calls, "bfc.disconnect must run via finally after delete_bot error"


# ═══════════════════════════════════════════════════════════════════════════
# Group E — Telethon timeouts (4, 9)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_botfather_create_bot_send_message_timeout_raises_timeout(tmp_path, monkeypatch):
    """Finding 4: BotFatherClient.create_bot must wrap each Telethon call
    in asyncio.wait_for with a 30s timeout and convert TimeoutError into
    a domain-specific BotFatherTimeout."""
    from link_project_to_chat.botfather import BotFatherClient

    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.is_user_authorized = AsyncMock(return_value=True)
    client_mock.send_message = AsyncMock()  # never resolves quickly
    client_mock.get_entity = AsyncMock(return_value=MagicMock())
    client_mock.get_messages = AsyncMock(return_value=[])

    captured_timeouts = []

    real_wait_for = asyncio.wait_for

    async def spy_wait_for(coro, timeout):
        captured_timeouts.append(timeout)
        # If the timeout is the one we care about, raise TimeoutError directly
        # to simulate a hang without actually sleeping.
        if timeout is not None and timeout <= 60.0:
            # Cancel the coroutine so we don't leak it.
            if asyncio.iscoroutine(coro):
                coro.close()
            raise asyncio.TimeoutError()
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.wait_for", spy_wait_for)
    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.sleep", AsyncMock())

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    from link_project_to_chat.botfather import BotFatherTimeout

    with pytest.raises(BotFatherTimeout):
        await bfc.create_bot("Acme Dev", "acme_dev_bot")

    assert captured_timeouts, "asyncio.wait_for must be invoked"
    # At least one call must use a 30s timeout.
    assert any(t == 30.0 for t in captured_timeouts)


@pytest.mark.asyncio
async def test_botfather_delete_bot_uses_wait_for(tmp_path, monkeypatch):
    """Finding 4: BotFatherClient.delete_bot must wrap send_message in
    asyncio.wait_for(30s)."""
    from link_project_to_chat.botfather import BotFatherClient

    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.send_message = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=MagicMock())
    client_mock.get_messages = AsyncMock(return_value=[])

    captured = []
    real_wait_for = asyncio.wait_for

    async def spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.wait_for", spy)
    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.sleep", AsyncMock())

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    await bfc.delete_bot("acme_bot")

    assert any(t == 30.0 for t in captured), "delete_bot must wrap Telethon calls in wait_for(30.0)"


@pytest.mark.asyncio
async def test_botfather_disable_privacy_uses_wait_for(tmp_path, monkeypatch):
    """Finding 4: BotFatherClient.disable_privacy must wrap send_message in wait_for."""
    from link_project_to_chat.botfather import BotFatherClient

    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.send_message = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=MagicMock())

    captured = []
    real_wait_for = asyncio.wait_for

    async def spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.wait_for", spy)
    monkeypatch.setattr("link_project_to_chat.botfather.asyncio.sleep", AsyncMock())

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    await bfc.disable_privacy("acme_bot")

    assert any(t == 30.0 for t in captured), "disable_privacy must wrap Telethon calls in wait_for(30.0)"


@pytest.mark.asyncio
async def test_telegram_group_helpers_use_wait_for(monkeypatch):
    """Finding 9: TL helpers in _telegram_group must wrap their async calls
    in asyncio.wait_for(30.0)."""
    from link_project_to_chat.transport import _telegram_group as tg

    captured = []
    real_wait_for = asyncio.wait_for

    async def spy(coro, timeout):
        captured.append(timeout)
        return await real_wait_for(coro, timeout)

    monkeypatch.setattr(tg.asyncio, "wait_for", spy)

    mock_chat = MagicMock()
    mock_chat.id = 99
    mock_resp = MagicMock(chats=[mock_chat])
    client = AsyncMock(return_value=mock_resp)

    await tg.create_supergroup(client, "acme team")
    assert any(t == 30.0 for t in captured), (
        "create_supergroup must wrap its TL request in wait_for(30.0)"
    )


@pytest.mark.asyncio
async def test_setup_phone_branch_timeout_resets_setup_awaiting(tmp_path, monkeypatch):
    """Finding 9: when send_code_request times out, setup_awaiting must
    be cleared so the user's next text isn't misinterpreted as a code."""
    mb, cfg_path = _make_manager(tmp_path)

    # Construct a fake BotFatherClient whose _ensure_client raises TimeoutError.
    class FakeBF:
        def __init__(self, *a, **kw):
            pass

        async def _ensure_client(self):
            client = MagicMock()
            client.send_code_request = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            return client

    monkeypatch.setattr("link_project_to_chat.botfather.BotFatherClient", FakeBF)

    # Mock asyncio.wait_for to actually raise TimeoutError synchronously.
    async def fake_wait_for(coro, timeout):
        if asyncio.iscoroutine(coro):
            coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr("link_project_to_chat.manager.bot.asyncio.wait_for", fake_wait_for, raising=False)
    # Also catch the asyncio.wait_for reference in case the production code
    # imports it locally.
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    ctx = MagicMock()
    ctx.user_data = {
        "setup_awaiting": "phone",
        "setup_config_path": str(cfg_path),
    }
    update = _make_text_update("+15555550100")

    await mb._handle_setup_input(update, ctx, "phone")

    # On timeout, setup_awaiting must be cleared so subsequent input is not
    # treated as a verification code.
    assert "setup_awaiting" not in ctx.user_data, (
        "setup_awaiting must be cleared on timeout"
    )
    assert mb._transport.sent_messages


# ═══════════════════════════════════════════════════════════════════════════
# Group F — silent button-branch fallthroughs (10)
# ═══════════════════════════════════════════════════════════════════════════


def _click(value: str):
    from link_project_to_chat.transport.base import (
        ButtonClick,
        ChatKind,
        ChatRef,
        Identity,
        MessageRef,
    )

    chat = ChatRef(transport_id="telegram", native_id="42", kind=ChatKind.DM)
    sender = Identity(
        transport_id="telegram",
        native_id="1",
        display_name="op",
        handle="op",
        is_bot=False,
    )
    msg = MessageRef(transport_id="telegram", native_id="100", chat=chat)
    return ButtonClick(chat=chat, message=msg, sender=sender, value=value)


def _make_manager_with_user(tmp_path):
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.allowed_users = [
        AllowedUser(username="op", role="executor", locked_identities=["telegram:1"]),
    ]
    save_config(cfg, cfg_path)

    bot = ManagerBot.__new__(ManagerBot)
    bot._project_config_path = cfg_path
    bot._allowed_users = list(cfg.allowed_users)
    bot._init_auth()
    bot._transport = FakeTransport()
    bot._pm = MagicMock()
    bot._pm.status = MagicMock(return_value="stopped")
    return bot, cfg_path


@pytest.mark.asyncio
async def test_proj_ptog_missing_pipe_shows_error(tmp_path):
    """Finding 10: proj_ptog_<plugin>|<name> with missing pipe must
    surface an error to the user, not silently return."""
    bot, _ = _make_manager_with_user(tmp_path)

    click = _click("proj_ptog_malformedpayload")
    await bot._on_button_from_transport(click)

    assert bot._transport.edited_messages, (
        "Malformed plugin-toggle payload must trigger a visible error edit"
    )
    last = bot._transport.edited_messages[-1].text.lower()
    assert "invalid" in last or "no longer" in last or "refresh" in last


@pytest.mark.asyncio
async def test_proj_ptog_unknown_project_shows_error(tmp_path):
    """Finding 10: proj_ptog with unknown project must surface 'project
    no longer exists', not silently return."""
    bot, _ = _make_manager_with_user(tmp_path)

    click = _click("proj_ptog_someplugin|ghostproject")
    await bot._on_button_from_transport(click)

    assert bot._transport.edited_messages
    last = bot._transport.edited_messages[-1].text.lower()
    assert "no longer" in last or "not found" in last or "refresh" in last


@pytest.mark.asyncio
async def test_proj_rig_unknown_project_shows_error(tmp_path):
    """Finding 10: proj_rig_on_<missingname> must surface 'project no
    longer exists', not silently return."""
    bot, _ = _make_manager_with_user(tmp_path)

    click = _click("proj_rig_on_ghostproject")
    await bot._on_button_from_transport(click)

    assert bot._transport.edited_messages
    last = bot._transport.edited_messages[-1].text.lower()
    assert "no longer" in last or "not found" in last or "refresh" in last


@pytest.mark.asyncio
async def test_proj_model_unknown_project_shows_error(tmp_path):
    """Finding 10: proj_model_<modelid>_<missingname> must surface an
    error, not silently no-op."""
    from link_project_to_chat.manager.bot import MODEL_OPTIONS

    bot, _ = _make_manager_with_user(tmp_path)

    model_id = MODEL_OPTIONS[0][0]
    click = _click(f"proj_model_{model_id}_ghostproject")
    await bot._on_button_from_transport(click)

    assert bot._transport.edited_messages
    last = bot._transport.edited_messages[-1].text.lower()
    assert "no longer" in last or "not found" in last or "refresh" in last
