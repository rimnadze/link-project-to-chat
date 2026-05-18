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
        "proj_add_gchat_alpha",
        "proj_edit_gchat_alpha",
        "proj_restart_gchat_alpha",
        "proj_remove_gchat_alpha",
    ],
)
async def test_clicking_gchat_buttons_does_not_misroute_to_edit_remove(
    tmp_path, callback_value
):
    """Until Tasks 12-13 land, clicking a google_chat button must NOT trigger
    the regular edit/remove handler against a project named 'gchat_<name>'.

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
