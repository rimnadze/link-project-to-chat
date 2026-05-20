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

    Walks: provider:gitlab → source=From GitHub → list page → pick repo →
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

    # Step 2: source pick → "From GitHub" (the label is platform-agnostic now —
    # the underlying provider was already chosen). Should land on CREATE_REPO_LIST.
    source = _make_button_update("create_from_gh")
    state = await bot._create_source_callback(source, ctx)
    fake_client.list_repos.assert_awaited()
    assert state == ManagerBot.CREATE_REPO_LIST
    # The repos dict was cached in user_data for the pick step.
    assert "acme/my-app" in ctx.user_data["create"]["repos"]

    # Step 3: repo pick → "create_repo_acme/my-app". Should land on CREATE_NAME
    # and have stored the repo on ctx.user_data["create"]["repo"].
    repo_pick = _make_button_update("create_repo_acme/my-app")
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
    assert "u/r" in ctx.user_data["create"]["repos"]

    # Step 3: pick the repo.
    repo_pick = _make_button_update("create_repo_u/r")
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
