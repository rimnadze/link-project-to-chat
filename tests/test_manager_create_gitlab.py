"""Manager — GitLab provider picker + wizard wiring."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_build_repo_provider_returns_github_client_by_default(tmp_path):
    from link_project_to_chat.config import Config
    from link_project_to_chat.github_client import GitHubClient
    from link_project_to_chat.manager.bot import _build_repo_provider

    config = Config(github_pat="ghp_x")
    ctx = MagicMock()
    ctx.user_data = {"create": {"provider": "github"}}

    provider = _build_repo_provider(ctx, config)
    assert isinstance(provider, GitHubClient)


def test_build_repo_provider_returns_gitlab_client_when_picked(monkeypatch, tmp_path):
    from link_project_to_chat.config import Config
    from link_project_to_chat.gitlab_client import GitLabClient
    from link_project_to_chat.manager.bot import _build_repo_provider
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client._glab_available", lambda: False
    )

    config = Config(gitlab_pat="glpat-x", gitlab_host="gitlab.example.com")
    ctx = MagicMock()
    ctx.user_data = {"create": {"provider": "gitlab"}}

    provider = _build_repo_provider(ctx, config)
    assert isinstance(provider, GitLabClient)
    assert provider._host == "gitlab.example.com"


def test_provider_pick_state_constant_is_unique():
    from link_project_to_chat.manager.bot import ManagerBot
    state_attrs = [
        a for a in dir(ManagerBot)
        if a.startswith("CREATE_") and isinstance(getattr(ManagerBot, a), int)
    ]
    state_values = [getattr(ManagerBot, a) for a in state_attrs]
    assert "CREATE_PROVIDER_PICK" in state_attrs
    assert len(state_values) == len(set(state_values)), "duplicate state ints"


def test_build_repo_provider_defaults_to_github_when_provider_missing(tmp_path):
    """Defensive: legacy ctx.user_data without ['create']['provider'] should default to GitHub."""
    from link_project_to_chat.config import Config
    from link_project_to_chat.github_client import GitHubClient
    from link_project_to_chat.manager.bot import _build_repo_provider

    config = Config(github_pat="ghp_x")
    ctx = MagicMock()
    ctx.user_data = {}

    provider = _build_repo_provider(ctx, config)
    assert isinstance(provider, GitHubClient)


# ─── Task 17: provider picker wired into create-project flow ───────────────


def _make_button_update(callback_data: str, user_id: int = 1, username: str = "tester"):
    """Build a minimal PTB-style Update with a CallbackQuery and ``.message``.

    The manager's wizard callbacks expect ``update.callback_query`` with
    ``.data``, ``.message`` (used by ``_msg_ref_from_query``), and ``.answer``;
    they also touch ``update.effective_user`` / ``update.effective_chat``.
    """
    from unittest.mock import AsyncMock, MagicMock

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


@pytest.mark.asyncio
async def test_project_create_provider_pick_gitlab_stores_choice_and_advances(tmp_path):
    """After picking GitLab, the wizard stores ``provider="gitlab"`` and shows
    the existing repo-source picker."""
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"),
        cfg_path,
    )

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path)}}

    update = _make_button_update("provider:gitlab")
    next_state = await mb._create_provider_pick_callback(update, ctx)

    assert ctx.user_data["create"]["provider"] == "gitlab"
    # Picking a provider transitions to the repo-source picker (CREATE_SOURCE).
    assert next_state == ManagerBot.CREATE_SOURCE
    # The picker prompt was edited in place into the repo-source prompt.
    assert mb._transport.edited_messages, "provider pick must edit_text the message"


@pytest.mark.asyncio
async def test_project_create_provider_pick_github_stores_choice_and_advances(tmp_path):
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"),
        cfg_path,
    )

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path)}}

    update = _make_button_update("provider:github")
    next_state = await mb._create_provider_pick_callback(update, ctx)

    assert ctx.user_data["create"]["provider"] == "github"
    assert next_state == ManagerBot.CREATE_SOURCE


@pytest.mark.asyncio
async def test_project_create_provider_pick_cancel_returns_end(tmp_path):
    from telegram.ext import ConversationHandler

    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"),
        cfg_path,
    )

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path)}}

    update = _make_button_update("provider:cancel")
    next_state = await mb._create_provider_pick_callback(update, ctx)

    assert next_state == ConversationHandler.END
    # The create wizard state must be cleaned up.
    assert "create" not in ctx.user_data
    assert "provider" not in ctx.user_data.get("create", {})
