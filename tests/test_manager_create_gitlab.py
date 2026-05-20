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


def _make_command_update(text: str, user_id: int = 1, username: str = "tester"):
    """Build a minimal PTB-style text Update for wizard entry points."""
    from unittest.mock import MagicMock

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


def _flatten_button_labels(buttons):
    return [button.label for row in buttons.rows for button in row]


def _fake_message_ref():
    from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef

    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    return MessageRef(transport_id="fake", native_id="1", chat=chat)


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
    edited = mb._transport.edited_messages[-1]
    assert "GitHub" not in edited.text
    assert "GitHub" not in " ".join(_flatten_button_labels(edited.buttons))


@pytest.mark.asyncio
async def test_project_create_entry_allows_gitlab_only_config_to_pick_provider(tmp_path, monkeypatch):
    """A GitLab-only manager should reach provider selection, not fail GitHub preflight."""
    from unittest.mock import AsyncMock

    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    monkeypatch.setattr("link_project_to_chat.github_client._gh_available", lambda: False)

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            telegram_api_id=1,
            telegram_api_hash="x",
            gitlab_pat="glpat-x",
            gitlab_host="gitlab.com",
        ),
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

    ctx = MagicMock()
    ctx.user_data = {}

    next_state = await mb._on_create_project(_make_command_update("/create_project"), ctx)

    assert next_state == ManagerBot.CREATE_PROVIDER_PICK
    assert ctx.user_data["create"]["config_path"] == str(cfg_path)
    assert "GitHub not configured" not in mb._transport.sent_messages[-1].text


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


# ─── Task 18: provider picker wired into create-team flow ──────────────────


@pytest.mark.asyncio
async def test_team_create_entry_routes_through_provider_pick(tmp_path):
    """`/create_team` entry must first prompt for provider, returning CREATE_PROVIDER_PICK."""
    from unittest.mock import AsyncMock as _AM
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

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
    # Bypass executor guard for the unit test.
    mb._guard_executor = _AM(return_value=True)

    # Text update (the /create_team command itself comes via update.message).
    user = MagicMock()
    user.id = 1
    user.username = "op"
    user.full_name = "op"
    user.is_bot = False
    chat = MagicMock()
    chat.id = 1
    chat.type = "private"
    msg = MagicMock()
    msg.message_id = 1
    msg.chat = chat
    msg.text = "/create_team"
    update = MagicMock()
    update.message = msg
    update.callback_query = None
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = msg

    ctx = MagicMock()
    ctx.user_data = {}

    next_state = await mb._on_create_team(update, ctx)

    assert next_state == ManagerBot.CREATE_PROVIDER_PICK
    assert ctx.user_data["create_team"]["config_path"] == str(cfg_path)


@pytest.mark.asyncio
async def test_team_create_entry_allows_gitlab_only_config_to_pick_provider(tmp_path, monkeypatch):
    """A GitLab-only manager should not be blocked by GitHub auth before provider pick."""
    from unittest.mock import AsyncMock as _AM

    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    monkeypatch.setattr("link_project_to_chat.github_client._gh_available", lambda: False)

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            telegram_api_id=1,
            telegram_api_hash="x",
            gitlab_pat="glpat-x",
            gitlab_host="gitlab.com",
        ),
        cfg_path,
    )
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()
    mb._guard_executor = _AM(return_value=True)

    ctx = MagicMock()
    ctx.user_data = {}

    next_state = await mb._on_create_team(_make_command_update("/create_team"), ctx)

    assert next_state == ManagerBot.CREATE_PROVIDER_PICK
    assert ctx.user_data["create_team"]["config_path"] == str(cfg_path)


@pytest.mark.asyncio
async def test_team_create_provider_pick_advances_to_team_source(tmp_path):
    """Picking a provider in the team flow advances to CREATE_TEAM_SOURCE,
    not CREATE_SOURCE (which is project-only)."""
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
    # The team-create entry sets create_team (not create) in user_data.
    ctx.user_data = {"create_team": {"config_path": str(cfg_path)}}

    update = _make_button_update("provider:gitlab")
    next_state = await mb._create_provider_pick_callback(
        update, ctx, user_data_key="create_team",
    )

    assert ctx.user_data["create_team"]["provider"] == "gitlab"
    assert next_state == ManagerBot.CREATE_TEAM_SOURCE
    # Sanity: the `create` key was NOT polluted.
    assert "create" not in ctx.user_data


@pytest.mark.asyncio
async def test_gitlab_project_paste_prompt_is_provider_neutral(tmp_path):
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(Config(gitlab_pat="glpat-x"), cfg_path)

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path), "provider": "gitlab"}}

    state = await mb._create_source_callback(_make_button_update("create_paste_url"), ctx)

    assert state == ManagerBot.CREATE_REPO_URL
    assert "GitHub" not in mb._transport.edited_messages[-1].text


@pytest.mark.asyncio
async def test_gitlab_team_paste_prompt_is_provider_neutral(tmp_path):
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(Config(gitlab_pat="glpat-x"), cfg_path)

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()

    ctx = MagicMock()
    ctx.user_data = {"create_team": {"config_path": str(cfg_path), "provider": "gitlab"}}

    state = await mb._create_team_source_callback(_make_button_update("ct_source:url"), ctx)

    assert state == ManagerBot.CREATE_TEAM_REPO_URL
    assert "GitHub" not in mb._transport.edited_messages[-1].text


@pytest.mark.asyncio
async def test_gitlab_browse_unconfigured_edits_error(tmp_path, monkeypatch):
    """Browse should show an actionable error when GitLab provider construction fails."""
    from telegram.ext import ConversationHandler

    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr("link_project_to_chat.gitlab_client._glab_available", lambda host="gitlab.com": False)

    cfg_path = tmp_path / "config.json"
    save_config(Config(gitlab_host="gitlab.com"), cfg_path)

    bot = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path), "provider": "gitlab"}}

    state = await bot._show_repo_page(_fake_message_ref(), ctx, page=1)

    assert state == ConversationHandler.END
    assert bot._transport.edited_messages
    text = bot._transport.edited_messages[-1].text
    assert "GitLab" in text
    assert "/setup" in text


@pytest.mark.asyncio
async def test_gitlab_paste_url_unconfigured_sends_error(tmp_path, monkeypatch):
    """Pasted URL validation should also report unconfigured GitLab."""
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr("link_project_to_chat.gitlab_client._glab_available", lambda host="gitlab.com": False)

    cfg_path = tmp_path / "config.json"
    save_config(Config(gitlab_host="gitlab.com"), cfg_path)

    bot = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path), "provider": "gitlab"}}

    state = await bot._create_repo_url(
        _make_text_update_for_setup("https://gitlab.com/acme/project"),
        ctx,
    )

    assert state == ManagerBot.CREATE_REPO_URL
    assert bot._transport.sent_messages
    text = bot._transport.sent_messages[-1].text
    assert "GitLab" in text
    assert "/setup" in text


@pytest.mark.asyncio
async def test_project_clone_provider_construction_failure_sends_retry(tmp_path, monkeypatch):
    """Clone should show Retry/Cancel even when provider construction fails before clone_repo."""
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport import ChatKind, ChatRef
    from link_project_to_chat.transport.fake import FakeTransport

    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr("link_project_to_chat.gitlab_client._glab_available", lambda host="gitlab.com": False)

    cfg_path = tmp_path / "config.json"
    save_config(Config(gitlab_host="gitlab.com"), cfg_path)

    bot = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    ctx = MagicMock()
    ctx.user_data = {
        "create": {
            "config_path": str(cfg_path),
            "provider": "gitlab",
            "name": "project",
            "repo": {
                "name": "project",
                "full_name": "acme/project",
                "html_url": "https://gitlab.com/acme/project",
                "clone_url": "https://gitlab.com/acme/project.git",
                "description": "",
                "private": True,
            },
        },
    }
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)

    state = await bot._execute_clone(chat, ctx)

    assert state == ManagerBot.CREATE_CLONE
    assert bot._transport.sent_messages
    sent = bot._transport.sent_messages[-1]
    assert "Clone failed" in sent.text
    assert sent.buttons is not None


@pytest.mark.asyncio
async def test_browse_close_failure_does_not_mask_provider_error(tmp_path, monkeypatch):
    """Provider close errors should not replace the user-visible provider error."""
    from telegram.ext import ConversationHandler

    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager import bot as manager_bot
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)

    class BrokenProvider:
        async def list_repos(self, *, page: int, per_page: int):
            raise RuntimeError("list failed")

        async def close(self):
            raise RuntimeError("close failed")

    monkeypatch.setattr(manager_bot, "_build_repo_provider", lambda *a, **kw: BrokenProvider())

    bot = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg_path), "provider": "github"}}

    state = await bot._show_repo_page(_fake_message_ref(), ctx, page=1)

    assert state == ConversationHandler.END
    assert "list failed" in bot._transport.edited_messages[-1].text


@pytest.mark.asyncio
async def test_build_repo_provider_reads_create_team_key(tmp_path):
    """`_build_repo_provider` must accept user_data_key="create_team"."""
    from link_project_to_chat.config import Config
    from link_project_to_chat.gitlab_client import GitLabClient
    from link_project_to_chat.manager.bot import _build_repo_provider

    config = Config(gitlab_pat="glpat-x", gitlab_host="gitlab.example.com")
    ctx = MagicMock()
    ctx.user_data = {"create_team": {"provider": "gitlab"}}

    provider = _build_repo_provider(ctx, config, user_data_key="create_team")
    assert isinstance(provider, GitLabClient)


@pytest.mark.asyncio
async def test_team_create_execute_clones_via_provider_factory(tmp_path, monkeypatch):
    """When provider=gitlab is stored under create_team, the clone uses GitLabClient."""
    from unittest.mock import AsyncMock as _AM, patch
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            telegram_api_id=1,
            telegram_api_hash="x",
            github_pat="ghp_x",
            gitlab_pat="glpat-x",
            gitlab_host="gitlab.example.com",
        ),
        cfg_path,
    )

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()
    mb._telethon_client = MagicMock()
    mb._pm = MagicMock()

    ctx = MagicMock()
    ctx.user_data = {
        "create_team": {
            "config_path": str(cfg_path),
            "project_prefix": "myteam",
            "persona_mgr": "developer",
            "persona_dev": "developer",
            "provider": "gitlab",
            "repo": {
                "name": "p",
                "full_name": "u/p",
                "description": "",
                "private": True,
                "html_url": "https://gitlab.com/u/p",
                "clone_url": "https://gitlab.com/u/p.git",
            },
        }
    }

    user = MagicMock()
    user.id = 1
    user.username = "op"
    user.full_name = "op"
    user.is_bot = False
    chat = MagicMock()
    chat.id = 1
    chat.type = "private"
    msg = MagicMock()
    msg.message_id = 1
    msg.chat = chat
    update = MagicMock()
    update.callback_query = None
    update.message = msg
    update.effective_user = user
    update.effective_chat = chat
    update.effective_message = msg

    # Fake GitLabClient instance — both clone_repo + close are awaited.
    fake_gitlab = MagicMock()
    fake_gitlab.clone_repo = _AM()
    fake_gitlab.close = _AM()

    # Patch GitLabClient construction so we capture the call without network.
    def fake_gitlab_factory(**kwargs):
        return fake_gitlab

    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        fake_gitlab_factory,
    )

    # Stub the BotFather + group orchestration — we only care about the clone step.
    bfc_stub = MagicMock()
    bfc_stub.disable_privacy = _AM()
    bfc_stub._ensure_client = _AM(return_value=MagicMock())

    async def fake_create_bot_with_retry(*args, **kwargs):
        # Return (token, username) — matches signature in code.
        return ("FAKETOKEN", "bot_user")

    with patch(
        "link_project_to_chat.manager.bot._load_team_create_dependencies",
    ) as mock_deps, patch(
        "link_project_to_chat.manager.bot._create_bot_with_retry",
        new=fake_create_bot_with_retry,
    ):
        # Mock the dependency tuple. Returns (BotFatherClient cls, GitHubClient, RepoInfo,
        # add_bot, create_supergroup, invite_user, promote_admin, sanitize_bot_username).
        from link_project_to_chat.github_client import RepoInfo as _RealRepoInfo

        # We must short-circuit the rest of the orchestration after clone, so
        # raise a sentinel after clone to halt execution.
        class _StopHere(Exception):
            pass

        async def boom(*a, **kw):
            raise _StopHere()

        mock_deps.return_value = (
            lambda **kw: bfc_stub,           # BotFatherClient
            MagicMock(),                     # GitHubClient (unused now)
            _RealRepoInfo,                   # RepoInfo
            boom,                            # add_bot — raise to stop after clone
            _AM(return_value=-100),          # create_supergroup
            _AM(),                           # invite_user
            _AM(),                           # promote_admin
            lambda s: s,                     # sanitize_bot_username
        )

        # _create_team_execute returns ConversationHandler.END on error;
        # we just need clone_repo to have been awaited.
        await mb._create_team_execute(update, ctx)

    fake_gitlab.clone_repo.assert_awaited()
    fake_gitlab.close.assert_awaited()


# ─── Task 19: /setup wizard fields for gitlab_pat and gitlab_host ──────────


def _make_text_update_for_setup(text: str, *, user_id: int = 42, username: str = "op"):
    """Minimal PTB-style Update for the setup-wizard text-input handler.

    ``_handle_setup_input`` reads ``update.effective_message.text`` (via
    ``_incoming_from_update``) and ``update.effective_user``/``effective_chat``
    for identity/chat resolution. Match what ``_make_text_update`` does in
    tests/test_manager_create_google_chat.py.
    """
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
    message.message_id = 1
    update = _MM()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat
    update.message = message
    return update


def _make_manager_bot_for_setup_test(config_path):
    """Build a ManagerBot with a FakeTransport wired in for setup-wizard tests.

    Mirrors ``_make_bot`` in tests/test_manager_create_google_chat.py but
    drops the executor/identity allowlist (the setup-input handler under test
    is reached after the auth/executor guards in ``_edit_field_save`` — the
    tests call ``_handle_setup_input`` directly, so guards are not on the
    path).
    """
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport

    bot = ManagerBot("TOKEN", MagicMock(), project_config_path=config_path)
    bot._transport = FakeTransport()
    return bot


async def test_setup_wizard_persists_gitlab_pat(tmp_path):
    """Setting gitlab_pat via the /setup wizard writes config.json."""
    import json

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_setup_test(cfg)

    ctx = MagicMock()
    ctx.user_data = {
        "setup_awaiting": "gitlab_pat",
        "setup_config_path": str(cfg),
    }
    update = _make_text_update_for_setup("glpat-newvalue")
    await bot._handle_setup_input(update, ctx, "gitlab_pat")

    raw = json.loads(cfg.read_text())
    assert raw["gitlab_pat"] == "glpat-newvalue"


async def test_setup_wizard_persists_gitlab_host(tmp_path):
    """Setting gitlab_host via the /setup wizard writes config.json."""
    import json

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_setup_test(cfg)

    ctx = MagicMock()
    ctx.user_data = {
        "setup_awaiting": "gitlab_host",
        "setup_config_path": str(cfg),
    }
    update = _make_text_update_for_setup("gitlab.example.com")
    await bot._handle_setup_input(update, ctx, "gitlab_host")

    raw = json.loads(cfg.read_text())
    assert raw["gitlab_host"] == "gitlab.example.com"


async def test_setup_wizard_rejects_gitlab_host_with_scheme(tmp_path):
    """A host containing scheme or path is rejected; no persistence happens
    and a guidance message is sent so the operator can correct it."""
    import json

    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    bot = _make_manager_bot_for_setup_test(cfg)

    ctx = MagicMock()
    ctx.user_data = {
        "setup_awaiting": "gitlab_host",
        "setup_config_path": str(cfg),
    }
    update = _make_text_update_for_setup("https://gitlab.example.com")
    await bot._handle_setup_input(update, ctx, "gitlab_host")

    raw = json.loads(cfg.read_text())
    assert "gitlab_host" not in raw
    # A rejection message must be sent so the operator can fix the value.
    assert bot._transport.sent_messages, "rejection must emit a guidance message"
    last = bot._transport.sent_messages[-1].text.lower()
    assert "scheme" in last or "host" in last or "invalid" in last


# ─── Task 20: end-to-end create-project flow ───────────────────────────────


def _make_e2e_manager_bot(config_path):
    """Build a ManagerBot wired to FakeTransport for the E2E wizard tests.

    Mirrors ``_make_manager_bot_for_setup_test`` above but additionally sets
    ``_project_config_path`` so that ``_finalize_create``'s projects.json
    writes land under tmp_path.
    """
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport

    bot = ManagerBot("TOKEN", MagicMock(), project_config_path=config_path)
    bot._transport = FakeTransport()
    return bot


async def test_e2e_create_project_with_gitlab(tmp_path, monkeypatch):
    """End-to-end project-create wizard with provider=gitlab.

    Walks: provider:gitlab → browse repos → list page → pick repo →
    set name → _execute_clone. Asserts ``GitLabClient.clone_repo`` was awaited
    with a dest path under ``repos/<name>/``.
    """
    import json
    from unittest.mock import AsyncMock, patch
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "gitlab_pat": "glpat-x", "gitlab_host": "gitlab.com",
        "github_pat": "ghp_x",
    }))
    bot = _make_e2e_manager_bot(cfg)

    fake_repos = [
        RepoInfo(
            name="my-app",
            full_name="acme/my-app",
            html_url="https://gitlab.com/acme/my-app",
            clone_url="https://gitlab.com/acme/my-app.git",
            description="An app",
            private=True,
        ),
    ]
    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(fake_repos, False))
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg)}}

    # Step 1: provider pick → gitlab. Should land on CREATE_SOURCE.
    pick = _make_button_update("provider:gitlab")
    state = await bot._create_provider_pick_callback(pick, ctx)
    assert ctx.user_data["create"]["provider"] == "gitlab"
    assert state == ManagerBot.CREATE_SOURCE

    # Step 2: source pick → browse list. The underlying provider was already chosen.
    source = _make_button_update("create_from_gh")
    state = await bot._create_source_callback(source, ctx)
    fake_client.list_repos.assert_awaited()
    assert state == ManagerBot.CREATE_REPO_LIST
    # The repos list was cached in user_data for the pick step.
    assert any(
        r["full_name"] == "acme/my-app" for r in ctx.user_data["create"]["repos"]
    )

    # Step 3: repo pick → index 0 in the cached page. Should land on
    # CREATE_NAME and have stored the repo on ctx.user_data["create"]["repo"].
    repo_pick = _make_button_update("create_repo_0")
    state = await bot._create_repo_list_callback(repo_pick, ctx)
    assert state == ManagerBot.CREATE_NAME
    assert ctx.user_data["create"]["repo"]["full_name"] == "acme/my-app"

    # Step 4: short-circuit name + bot-creation by setting fields directly,
    # then invoke _execute_clone (the step under test). Asserts clone_repo
    # was called with a dest under repos/myproj/.
    ctx.user_data["create"]["name"] = "myproj"
    ctx.user_data["create"]["bot_token"] = "FAKETOKEN"
    ctx.user_data["create"]["bot_username"] = "bot_user"

    # _execute_clone calls _finalize_create on success; stub that to halt here.
    with patch.object(bot, "_finalize_create", new=AsyncMock(return_value=0)) as fin:
        from link_project_to_chat.transport import ChatRef as _CR
        from link_project_to_chat.transport.base import ChatKind as _CK
        chat = _CR(transport_id="telegram", native_id="1", kind=_CK.DM)
        await bot._execute_clone(chat, ctx)
        fin.assert_awaited()

    fake_client.clone_repo.assert_awaited_once()
    call_args = fake_client.clone_repo.await_args
    # _execute_clone signature: clone_repo(repo, dest)
    dest = call_args.args[1]
    assert "repos" in str(dest)
    assert "myproj" in str(dest)


async def test_e2e_create_project_with_github_still_works(tmp_path, monkeypatch):
    """Regression: provider=github still wires the GitHubClient through the
    same wizard transitions and reaches the clone step."""
    import json
    from unittest.mock import AsyncMock, patch
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"github_pat": "ghp-x"}))
    bot = _make_e2e_manager_bot(cfg)

    fake_repos = [
        RepoInfo(
            name="r",
            full_name="u/r",
            html_url="https://github.com/u/r",
            clone_url="https://github.com/u/r.git",
            description="",
            private=False,
        ),
    ]
    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(fake_repos, False))
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.github_client.GitHubClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg)}}

    # Step 1: provider pick → github.
    pick = _make_button_update("provider:github")
    state = await bot._create_provider_pick_callback(pick, ctx)
    assert ctx.user_data["create"]["provider"] == "github"
    assert state == ManagerBot.CREATE_SOURCE

    # Step 2: source pick → browse list. Calls GitHubClient.list_repos.
    source = _make_button_update("create_from_gh")
    state = await bot._create_source_callback(source, ctx)
    fake_client.list_repos.assert_awaited()
    assert state == ManagerBot.CREATE_REPO_LIST
    assert any(r["full_name"] == "u/r" for r in ctx.user_data["create"]["repos"])

    # Step 3: pick the repo by index.
    repo_pick = _make_button_update("create_repo_0")
    state = await bot._create_repo_list_callback(repo_pick, ctx)
    assert state == ManagerBot.CREATE_NAME
    assert ctx.user_data["create"]["repo"]["full_name"] == "u/r"

    # Step 4: kick off the clone — GitHub path still uses GitHubClient.
    ctx.user_data["create"]["name"] = "rg"
    ctx.user_data["create"]["bot_token"] = "FAKETOKEN"
    ctx.user_data["create"]["bot_username"] = "bot_user"
    with patch.object(bot, "_finalize_create", new=AsyncMock(return_value=0)):
        from link_project_to_chat.transport import ChatRef as _CR
        from link_project_to_chat.transport.base import ChatKind as _CK
        chat = _CR(transport_id="telegram", native_id="1", kind=_CK.DM)
        await bot._execute_clone(chat, ctx)

    fake_client.clone_repo.assert_awaited_once()
    dest = fake_client.clone_repo.await_args.args[1]
    assert "repos" in str(dest)
    assert "rg" in str(dest)


# ─── Task 21: end-to-end create-team flow with GitLab ──────────────────────


async def test_e2e_create_team_with_gitlab(tmp_path, monkeypatch):
    """End-to-end team-create wizard with provider=gitlab, stopping at clone.

    Walks: provider:gitlab → ct_source:github → list page → pick repo →
    pre-set name + personas → ``_create_team_execute``. Stubs BotFather +
    Telethon paths and halts execution right after the clone step by raising
    from ``add_bot``. Asserts ``GitLabClient.clone_repo`` was awaited with a
    dest path under ``repos/<prefix>/``.

    Mirrors ``test_e2e_create_project_with_gitlab`` for the team flow. The
    subsequent BotFather/supergroup orchestration is orthogonal and provider-
    agnostic, so it's stubbed out.
    """
    import json
    from unittest.mock import AsyncMock, patch
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "telegram_api_id": 1,
        "telegram_api_hash": "x",
        "gitlab_pat": "glpat-x",
        "gitlab_host": "gitlab.com",
        "github_pat": "ghp_x",
    }))
    (cfg.parent / "telethon.session").write_text("x")
    bot = _make_e2e_manager_bot(cfg)
    # ``_create_team_execute`` reads ``self._telethon_client`` (via getattr)
    # and ``self._pm.start_team(...)`` — provide harmless stubs.
    bot._telethon_client = MagicMock()
    bot._pm = MagicMock()

    fake_repos = [
        RepoInfo(
            name="my-app",
            full_name="acme/my-app",
            html_url="https://gitlab.com/acme/my-app",
            clone_url="https://gitlab.com/acme/my-app.git",
            description="An app",
            private=True,
        ),
    ]
    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(fake_repos, False))
    fake_client.clone_repo = AsyncMock()
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {"create_team": {"config_path": str(cfg)}}

    # Step 1: provider pick → gitlab. Should land on CREATE_TEAM_SOURCE.
    pick = _make_button_update("provider:gitlab")
    state = await bot._create_provider_pick_callback(
        pick, ctx, user_data_key="create_team",
    )
    assert ctx.user_data["create_team"]["provider"] == "gitlab"
    assert state == ManagerBot.CREATE_TEAM_SOURCE

    # Step 2: team-source pick → ``ct_source:github`` (browse list). Should
    # land on CREATE_TEAM_REPO_LIST and call GitLabClient.list_repos.
    source = _make_button_update("ct_source:github")
    state = await bot._create_team_source_callback(source, ctx)
    fake_client.list_repos.assert_awaited()
    assert state == ManagerBot.CREATE_TEAM_REPO_LIST
    assert any(
        r["full_name"] == "acme/my-app"
        for r in ctx.user_data["create_team"]["repos"]
    )

    # Step 3: repo pick → index 0. Team flow lands on CREATE_TEAM_NAME (not
    # the project's CREATE_NAME) and stores the repo.
    repo_pick = _make_button_update("create_repo_0")
    state = await bot._create_repo_list_callback(
        repo_pick, ctx, user_data_key="create_team",
    )
    assert state == ManagerBot.CREATE_TEAM_NAME
    assert ctx.user_data["create_team"]["repo"]["full_name"] == "acme/my-app"

    # Step 4: pre-set name + personas (skipping the prefix-validation +
    # persona-pick callbacks; those are unit-tested elsewhere) and invoke
    # ``_create_team_execute``. Stub BotFather + supergroup orchestration so
    # execution halts right after the clone step.
    ctx.user_data["create_team"]["project_prefix"] = "myteam"
    ctx.user_data["create_team"]["persona_mgr"] = "developer"
    ctx.user_data["create_team"]["persona_dev"] = "developer"

    async def fake_create_bot_with_retry(*args, **kwargs):
        return ("FAKETOKEN", "bot_user")

    bfc_stub = MagicMock()
    bfc_stub.disable_privacy = AsyncMock()
    bfc_stub._ensure_client = AsyncMock(return_value=MagicMock())
    bfc_stub.disconnect = AsyncMock()

    class _StopAfterClone(Exception):
        pass

    async def boom_add_bot(*a, **kw):
        # Raised from add_bot — after clone_repo completed and group_id was
        # captured, before any config commit. Falls into the partial-failure
        # handler in ``_create_team_execute``, which is non-fatal for the test.
        raise _StopAfterClone()

    from link_project_to_chat.repo_provider import RepoInfo as _RealRepoInfo

    with patch(
        "link_project_to_chat.manager.bot._load_team_create_dependencies",
    ) as mock_deps, patch(
        "link_project_to_chat.manager.bot._create_bot_with_retry",
        new=fake_create_bot_with_retry,
    ):
        mock_deps.return_value = (
            lambda **kw: bfc_stub,           # BotFatherClient
            MagicMock(),                     # GitHubClient (unused — provider factory wins)
            _RealRepoInfo,                   # RepoInfo (re-hydrates dict → dataclass)
            boom_add_bot,                    # add_bot — raise to halt after clone
            AsyncMock(return_value=-100),    # create_supergroup
            AsyncMock(),                     # invite_user
            AsyncMock(),                     # promote_admin
            lambda s: s,                     # sanitize_bot_username (identity)
        )

        update = _make_button_update("provider:gitlab")  # any Update-shaped mock works
        await bot._create_team_execute(update, ctx)

    # The clone step ran via the GitLab provider with dest under repos/myteam/.
    fake_client.clone_repo.assert_awaited_once()
    call_dest = fake_client.clone_repo.await_args.args[1]
    assert "repos" in str(call_dest)
    assert "myteam" in str(call_dest)
    fake_client.close.assert_awaited()


# ─── Telegram 64-byte callback_data limit on repo-picker buttons ───────────


async def test_show_repo_page_callback_data_fits_telegram_64_byte_limit(
    tmp_path, monkeypatch,
):
    """Telegram caps callback_data at 64 bytes. GitLab namespaced paths can
    exceed 64 chars on their own (subgroups, deletion-scheduled suffixes).

    The repo picker must use a compact identifier (index) in callback_data,
    not full_name — otherwise long GitLab paths (e.g. ones with a
    ``-deletion_scheduled-<id>`` suffix tacked on by GitLab when a project
    is awaiting deletion) blow the 64-byte limit and Telegram rejects the
    keyboard with ``Button_data_invalid``.
    """
    import json
    from unittest.mock import AsyncMock
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.repo_provider import RepoInfo

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"gitlab_pat": "glpat-x", "gitlab_host": "gitlab.com"}))
    bot = _make_e2e_manager_bot(cfg)

    long_repos = [
        RepoInfo(
            name="link-project-to-chat-deletion_scheduled-82372719",
            full_name=(
                "rezochikashua/link-project-to-chat-deletion_scheduled-82372719"
            ),
            html_url=(
                "https://gitlab.com/rezochikashua/"
                "link-project-to-chat-deletion_scheduled-82372719"
            ),
            clone_url=(
                "https://gitlab.com/rezochikashua/"
                "link-project-to-chat-deletion_scheduled-82372719.git"
            ),
            description="",
            private=False,
        ),
    ]
    fake_client = MagicMock()
    fake_client.list_repos = AsyncMock(return_value=(long_repos, False))
    fake_client.close = AsyncMock()
    monkeypatch.setattr(
        "link_project_to_chat.gitlab_client.GitLabClient",
        lambda **kw: fake_client,
    )

    ctx = MagicMock()
    ctx.user_data = {"create": {"config_path": str(cfg), "provider": "gitlab"}}

    await bot._show_repo_page(_fake_message_ref(), ctx, page=1)

    edited = bot._transport.edited_messages[-1]
    payloads = [
        btn.value
        for row in edited.buttons.rows
        for btn in row
        if getattr(btn, "value", None)
    ]
    assert payloads, "no buttons rendered"
    for payload in payloads:
        assert len(payload.encode("utf-8")) <= 64, (
            f"callback_data {payload!r} is "
            f"{len(payload.encode('utf-8'))} bytes, "
            "exceeds Telegram's 64-byte limit"
        )
