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

from link_project_to_chat.config import AllowedUser
from link_project_to_chat.manager.bot import ManagerBot


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
