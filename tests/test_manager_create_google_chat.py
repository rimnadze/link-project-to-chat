"""Wizard-state-machine tests for the new Google Chat manager flows.

The manager bot uses the transport-portable ``Buttons``/``Button`` model
(``link_project_to_chat.transport.base``), not raw Telegram
``InlineKeyboardMarkup``. The per-project view is built by
``_proj_detail_buttons(name, status)``.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.config import AllowedUser
from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.transport import (
    ButtonClick,
    ChatKind,
    ChatRef,
    Identity,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _write_project_config(
    tmp_path: Path, projects: dict, gchat_top: dict | None = None
) -> Path:
    cfg_path = tmp_path / "config.json"
    raw: dict = {"projects": projects}
    if gchat_top is not None:
        raw["google_chat"] = gchat_top
    cfg_path.write_text(json.dumps(raw))
    return cfg_path


def _make_bot(cfg_path: Path) -> ManagerBot:
    return ManagerBot(
        "TOKEN",
        MagicMock(),
        allowed_users=[
            AllowedUser(
                username="op", role="executor", locked_identities=["telegram:1"]
            ),
        ],
        project_config_path=cfg_path,
    )


def _labels(buttons) -> list[str]:
    return [b.label for row in buttons.rows for b in row]


def _values(buttons) -> list[str]:
    return [b.value for row in buttons.rows for b in row]


def test_project_view_shows_add_google_chat_button_when_no_override(tmp_path):
    """Per-project view of a project without a google_chat override must
    include an 'Add Google Chat' button."""
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )

    bot = _make_bot(cfg_path)
    buttons = bot._proj_detail_buttons("alpha", "stopped")

    labels = _labels(buttons)
    values = _values(buttons)
    assert any("Add Google Chat" in label for label in labels)
    assert any(v == "proj_add_gchat_alpha" for v in values)


def test_project_view_shows_edit_remove_restart_when_override_exists(tmp_path):
    cfg_path = _write_project_config(
        tmp_path,
        {
            "alpha": {
                "path": str(tmp_path),
                "telegram_bot_token": "tok",
                "google_chat": {
                    "port": 8091,
                    "service_account_file": "/keys/a.json",
                    "public_url": "https://a.example",
                    "root_command_id": 7,
                },
            },
        },
    )

    bot = _make_bot(cfg_path)
    buttons = bot._proj_detail_buttons("alpha", "stopped")
    labels = _labels(buttons)
    values = _values(buttons)

    assert any("Edit Google Chat" in label for label in labels)
    assert any("Remove Google Chat" in label for label in labels)
    assert any("Restart Google Chat" in label for label in labels)

    assert any(v == "proj_edit_gchat_alpha" for v in values)
    assert any(v == "proj_remove_gchat_alpha" for v in values)
    assert any(v == "proj_restart_gchat_alpha" for v in values)
    # "Add" should not appear when an override exists
    assert not any("Add Google Chat" in label for label in labels)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_value",
    [
        # ``proj_add_gchat_alpha`` is covered by the wizard tests below — it
        # now opens the 4-step Add wizard rather than the Task 11 stub.
        "proj_edit_gchat_alpha",
        "proj_restart_gchat_alpha",
        "proj_remove_gchat_alpha",
    ],
)
async def test_clicking_gchat_buttons_does_not_misroute_to_edit_remove(
    tmp_path, callback_value
):
    """Until Task 13 lands, edit/restart/remove google_chat buttons must NOT
    trigger the regular edit/remove handler against a project named
    'gchat_<name>'.

    The dispatch chain in ``_dispatch_button_click`` uses prefix matching,
    so without the catch-all stub ``proj_edit_gchat_alpha`` would match
    ``proj_edit_`` first and route to the legacy edit flow for a phantom
    project named ``gchat_alpha``. This test pins the stub's behaviour.
    """
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )
    bot = _make_bot(cfg_path)

    # Swap in a FakeTransport so we can both observe edits and drive the
    # dispatch via the registered button handler.
    fake = FakeTransport()
    bot._transport = fake
    fake.on_button(bot._on_button_from_transport)

    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    msg = MessageRef(transport_id="fake", native_id="1", chat=chat)
    sender = Identity(
        transport_id="telegram",
        native_id="1",
        display_name="op",
        handle="op",
        is_bot=False,
    )
    ctx = MagicMock()
    ctx.user_data = {}
    update = MagicMock()
    click = ButtonClick(
        chat=chat,
        message=msg,
        sender=sender,
        value=callback_value,
        native=(update, ctx),
    )

    await bot._dispatch_button_click(click)

    # The stub edits the clicked message with a "coming soon" notice and
    # returns early. If misrouting happened, the legacy proj_edit_/proj_remove_
    # branches would have rendered an edit menu or a removal confirmation
    # for a phantom project named "gchat_alpha".
    assert fake.edited_messages, "stub must edit_text the clicked message"
    final = fake.edited_messages[-1]
    assert "Google Chat" in final.text
    # The legacy edit flow renders "Edit 'gchat_alpha' — choose field:" and
    # the legacy remove flow lists projects — neither phrase should appear.
    assert "gchat_alpha" not in final.text
    assert "choose field" not in final.text


# ─── Add-Google-Chat wizard (Task 12) ───────────────────────────────────────


def _make_wizard_click(value: str, *, user_data: dict | None = None):
    """Build a ButtonClick that mirrors the PTB-native (update, ctx) pair the
    real handler expects. ``user_data`` is the dict carried across steps via
    ctx.user_data (PTB's per-user storage)."""
    from unittest.mock import MagicMock as _MM

    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    msg = MessageRef(transport_id="fake", native_id="1", chat=chat)
    sender = Identity(
        transport_id="telegram",
        native_id="1",
        display_name="op",
        handle="op",
        is_bot=False,
    )
    state = user_data if user_data is not None else {}
    ctx = _MM()
    ctx.user_data = state
    update = _MM()
    return ButtonClick(
        chat=chat,
        message=msg,
        sender=sender,
        value=value,
        native=(update, ctx),
    ), state


def _make_text_update(text: str, *, user_id: int = 1, username: str = "op"):
    from unittest.mock import AsyncMock as _AM, MagicMock as _MM

    user = _MM()
    user.id = user_id
    user.username = username
    user.full_name = username
    user.is_bot = False
    chat = _MM()
    chat.id = user_id
    chat.type = "private"
    message = _AM()
    message.reply_text = _AM()
    message.text = text
    message.chat = chat
    update = _MM()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat
    update.message = message
    return update


@pytest.mark.asyncio
async def test_add_google_chat_wizard_collects_four_fields_and_persists(tmp_path):
    """End-to-end wizard: tap [+ Add Google Chat], answer four prompts,
    config gets persisted, ProcessManager.start_google_chat_subprocess fires."""
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )

    bot = _make_bot(cfg_path)
    bot._pm.start_google_chat_subprocess.return_value = True
    fake = FakeTransport()
    bot._transport = fake

    # Step 0: tap [+ Add Google Chat]
    click, state = _make_wizard_click("proj_add_gchat_alpha")
    await bot._dispatch_button_click(click)

    # Wizard prompts for the SA file first
    assert fake.edited_messages, "wizard entry must prompt the operator"
    first_prompt = fake.edited_messages[-1].text.lower()
    assert "service-account" in first_prompt or "service account" in first_prompt
    assert state.get("gchat_wizard", {}).get("name") == "alpha"
    assert state["gchat_wizard"]["step"] == "sa_file"

    # Step 1: send the service-account JSON path
    update = _make_text_update("/home/botuser/keys/alpha.json")
    from unittest.mock import MagicMock as _MM
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "port"
    assert state["gchat_wizard"]["data"]["service_account_file"] == "/home/botuser/keys/alpha.json"

    # Step 2: send the port
    update = _make_text_update("8091")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "public_url"
    assert state["gchat_wizard"]["data"]["port"] == 8091

    # Step 3: send the public URL
    update = _make_text_update("https://alpha.example.com")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "root_command_id"

    # Step 4: send the slash-command ID and finalize
    update = _make_text_update("7")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)

    # Wizard popped from user_data after finalize.
    assert "gchat_wizard" not in state

    # Config persisted with the override.
    from link_project_to_chat.config import load_config
    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].google_chat is not None
    assert loaded.projects["alpha"].google_chat.port == 8091
    assert (
        loaded.projects["alpha"].google_chat.service_account_file
        == "/home/botuser/keys/alpha.json"
    )
    assert (
        loaded.projects["alpha"].google_chat.public_url
        == "https://alpha.example.com"
    )
    assert loaded.projects["alpha"].google_chat.root_command_id == 7

    # ProcessManager.start_google_chat_subprocess fired exactly once.
    bot._pm.start_google_chat_subprocess.assert_called_once_with("alpha")

    # The finalizer printed an nginx vhost snippet with the public host.
    # FakeTransport.send_text records the body in `sent_messages`.
    snippets = [m.text for m in fake.sent_messages]
    assert any("nginx" in s.lower() or "server_name" in s for s in snippets), \
        f"finalizer must print an nginx vhost snippet, got: {snippets}"
    final_snippet = next(s for s in snippets if "server_name" in s)
    assert "alpha.example.com" in final_snippet
    assert "127.0.0.1:8091" in final_snippet


@pytest.mark.asyncio
async def test_add_google_chat_wizard_rejects_invalid_port(tmp_path):
    """Sending '99999' to the port prompt must not advance the wizard."""
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )

    bot = _make_bot(cfg_path)
    fake = FakeTransport()
    bot._transport = fake

    # Tap the button to start the wizard.
    click, state = _make_wizard_click("proj_add_gchat_alpha")
    await bot._dispatch_button_click(click)

    # Step 1: SA file
    from unittest.mock import MagicMock as _MM
    update = _make_text_update("/home/botuser/keys/alpha.json")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "port"

    # Step 2: send an invalid port
    update = _make_text_update("99999")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)

    # Wizard must stay on the port step.
    assert state["gchat_wizard"]["step"] == "port"
    # Reply must mention the valid port range.
    last = fake.sent_messages[-1].text if fake.sent_messages else ""
    assert "1" in last and "65535" in last

    # Also reject port 0 and non-numeric input.
    for bad in ("0", "abc", "-1"):
        update = _make_text_update(bad)
        ctx = _MM()
        ctx.user_data = state
        await bot._edit_field_save(update, ctx)
        assert state["gchat_wizard"]["step"] == "port"


@pytest.mark.asyncio
async def test_add_google_chat_wizard_rejects_non_https_url(tmp_path):
    """The public-URL step must require an https:// scheme."""
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )

    bot = _make_bot(cfg_path)
    fake = FakeTransport()
    bot._transport = fake

    click, state = _make_wizard_click("proj_add_gchat_alpha")
    await bot._dispatch_button_click(click)

    from unittest.mock import MagicMock as _MM
    # Advance to public_url step.
    for text in ("/home/botuser/keys/alpha.json", "8091"):
        update = _make_text_update(text)
        ctx = _MM()
        ctx.user_data = state
        await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "public_url"

    # http:// — must be rejected.
    update = _make_text_update("http://alpha.example.com")
    ctx = _MM()
    ctx.user_data = state
    await bot._edit_field_save(update, ctx)
    assert state["gchat_wizard"]["step"] == "public_url"
    assert "https" in fake.sent_messages[-1].text.lower()


@pytest.mark.asyncio
async def test_add_google_chat_wizard_viewer_cannot_start(tmp_path):
    """A viewer-role user must not be able to start the Add wizard."""
    cfg_path = _write_project_config(
        tmp_path,
        {"alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"}},
    )

    # Viewer-only allowed_users.
    bot = ManagerBot(
        "TOKEN",
        MagicMock(),
        allowed_users=[
            AllowedUser(
                username="op", role="viewer", locked_identities=["telegram:1"]
            ),
        ],
        project_config_path=cfg_path,
    )
    fake = FakeTransport()
    bot._transport = fake

    click, state = _make_wizard_click("proj_add_gchat_alpha")
    await bot._dispatch_button_click(click)

    # Wizard must not have armed itself for a viewer.
    assert "gchat_wizard" not in state
