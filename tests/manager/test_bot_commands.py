from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager
from link_project_to_chat.transport import (
    ButtonClick,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_update(args: list[str] | None = None, user_id: int = 1, username: str = "testuser", text: str = ""):
    user = MagicMock()
    user.id = user_id
    user.username = username
    user.full_name = username
    user.is_bot = False
    chat = MagicMock()
    chat.id = user_id
    chat.type = "private"
    message = AsyncMock()
    message.reply_text = AsyncMock()
    message.text = text
    message.chat = chat
    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat
    update.message = message
    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.user_data = {}
    return update, ctx


def _make_invocation(
    name: str,
    *,
    args: list[str] | None = None,
    user_id: int = 1,
    username: str = "testuser",
) -> CommandInvocation:
    chat = ChatRef(transport_id="fake", native_id=str(user_id), kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake",
        native_id=str(user_id),
        display_name=username,
        handle=username,
        is_bot=False,
    )
    return CommandInvocation(
        chat=chat,
        sender=sender,
        name=name,
        args=list(args or []),
        raw_text=f"/{name}",
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
    )


def _swap_fake_transport(bot: ManagerBot) -> FakeTransport:
    """Replace the bot's transport with a FakeTransport for assertions."""
    fake = FakeTransport()
    bot._transport = fake
    return fake


def _sleep_cmd() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def _make_button_click(
    value: str,
    *,
    user_id: int = 1,
    username: str = "testuser",
    user_data: dict | None = None,
) -> tuple[ButtonClick, dict]:
    """Build a ButtonClick suitable for _on_button_from_transport.

    Returns (click, user_data) where user_data is a real dict that mirrors
    what PTB's per-user storage provides via click.native[1].user_data.
    The caller can mutate it to seed pending_edit / setup_awaiting before the
    handler runs and read it after to assert state mutations.
    """
    chat = ChatRef(transport_id="fake", native_id=str(user_id), kind=ChatKind.DM)
    msg = MessageRef(transport_id="fake", native_id="1", chat=chat)
    sender = Identity(
        transport_id="fake",
        native_id=str(user_id),
        display_name=username,
        handle=username,
        is_bot=False,
    )
    state = user_data if user_data is not None else {}
    ctx = MagicMock()
    ctx.user_data = state
    update = MagicMock()
    click = ButtonClick(
        chat=chat, message=msg, sender=sender, value=value, native=(update, ctx),
    )
    return click, state


@pytest.fixture
def bot_env(tmp_path: Path):
    proj_cfg = tmp_path / "projects.json"
    proj_cfg.write_text(json.dumps({"projects": {}}))
    pm = ProcessManager(project_config_path=proj_cfg)
    from link_project_to_chat.config import AllowedUser
    bot = ManagerBot(
        "TOKEN", pm,
        allowed_users=[
            AllowedUser(username="testuser", role="executor", locked_identities=["telegram:1"]),
        ],
        project_config_path=proj_cfg,
    )
    return bot, pm, proj_cfg


async def _run_add_dialogue(bot, tmp_path, name="myproj", token="TOKEN", username="/skip", model="/skip"):
    proj_path = tmp_path / name
    proj_path.mkdir(exist_ok=True)
    fake = _swap_fake_transport(bot)

    update, ctx = _make_update()
    result = await bot._on_add_project(update, ctx)
    assert result == bot.ADD_NAME

    for step_text, expected_state in [
        (name, bot.ADD_PATH),
        (str(proj_path), bot.ADD_TOKEN),
        (token, bot.ADD_USERNAME),
        (username, bot.ADD_MODEL),
    ]:
        u, _ = _make_update(text=step_text)
        step_ctx = MagicMock()
        step_ctx.user_data = ctx.user_data
        handler = {
            bot.ADD_PATH: bot._add_name,
            bot.ADD_TOKEN: bot._add_path,
            bot.ADD_USERNAME: bot._add_token,
            bot.ADD_MODEL: bot._add_username,
        }[expected_state]
        result = await handler(u, step_ctx)
        assert result == expected_state

    u, _ = _make_update(text=model)
    final_ctx = MagicMock()
    final_ctx.user_data = ctx.user_data
    result = await bot._add_model(u, final_ctx)
    return result, fake, str(proj_path)


@pytest.mark.asyncio
async def test_addproject_success(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler
    bot, pm, proj_cfg = bot_env
    result, fake, proj_path = await _run_add_dialogue(bot, tmp_path)
    assert result == ConversationHandler.END
    assert "Added" in fake.sent_messages[-1].text
    assert "myproj" in json.loads(proj_cfg.read_text())["projects"]


@pytest.mark.asyncio
async def test_addproject_rejects_skipped_token(bot_env, tmp_path: Path):
    bot, _pm, _proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    ctx = MagicMock()
    ctx.user_data = {"new_project": {"name": "myproj", "path": str(tmp_path)}}

    update, _ = _make_update(text="/skip")
    result = await bot._add_token(update, ctx)

    assert result == bot.ADD_TOKEN
    assert "token is required" in fake.sent_messages[-1].text.lower()
    assert "telegram_bot_token" not in ctx.user_data["new_project"]


@pytest.mark.asyncio
async def test_addproject_with_all_options(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler
    bot, pm, proj_cfg = bot_env
    result, _, _ = await _run_add_dialogue(bot, tmp_path, name="fullproj", token="MYTOKEN", username="myuser", model="opus")
    assert result == ConversationHandler.END
    proj = json.loads(proj_cfg.read_text())["projects"]["fullproj"]
    assert proj["telegram_bot_token"] == "MYTOKEN"
    # P1 #2: the wizard now writes the modern allowed_users shape rather than
    # the legacy flat ``username`` key (which would silently lose to any
    # pre-existing allowed_users on next load).
    assert "username" not in proj
    assert proj["allowed_users"] == [{"username": "myuser", "role": "executor"}]
    # v1.0.0 dropped the legacy top-level mirror; canonical home is
    # backend_state["claude"]["model"].
    assert proj["backend_state"]["claude"]["model"] == "opus"
    assert "model" not in proj


@pytest.mark.asyncio
async def test_finalize_create_stores_manager_cleanup_metadata(bot_env, tmp_path: Path):
    from telegram.ext import ConversationHandler

    bot, _pm, proj_cfg = bot_env
    _swap_fake_transport(bot)
    ctx = MagicMock()
    ctx.user_data = {
        "create": {
            "name": "myproj",
            "repo": {"html_url": "https://github.com/acme/myproj"},
            "clone_path": str(tmp_path / "repos" / "myproj"),
            "bot_token": "TOKEN",
            "bot_username": "myproj_bot",
        }
    }

    result = await bot._finalize_create(_make_invocation("create_project").chat, ctx)

    assert result == ConversationHandler.END
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    assert proj["managed_by_manager"] is True
    assert proj["managed_repo_path"] == str(tmp_path / "repos" / "myproj")
    assert proj["managed_bot_username"] == "myproj_bot"


@pytest.mark.asyncio
async def test_addproject_already_exists(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    existing = tmp_path / "existing"
    existing.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(existing)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u, _ = _make_update(text="myproj")
    step_ctx = MagicMock()
    step_ctx.user_data = ctx.user_data
    result = await bot._add_name(u, step_ctx)
    assert result == bot.ADD_NAME
    assert "already exists" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_addproject_invalid_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update()
    await bot._on_add_project(update, ctx)
    u1, _ = _make_update(text="newproj")
    c1 = MagicMock(); c1.user_data = ctx.user_data
    await bot._add_name(u1, c1)
    u2, _ = _make_update(text="/nonexistent/xyz")
    c2 = MagicMock(); c2.user_data = ctx.user_data
    result = await bot._add_path(u2, c2)
    assert result == bot.ADD_PATH
    assert "not exist" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_editproject_rename(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"oldname": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["oldname", "name", "newname"])
    await bot._on_edit_project(update, ctx)
    assert "Renamed" in fake.sent_messages[-1].text
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "newname" in projects and "oldname" not in projects


@pytest.mark.asyncio
async def test_editproject_change_path(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    new_path = tmp_path / "new"; new_path.mkdir()
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "path", str(new_path)])
    await bot._on_edit_project(update, ctx)
    assert "Updated" in fake.sent_messages[-1].text
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"]["path"] == str(new_path)


@pytest.mark.asyncio
async def test_editproject_rename_conflict(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"a": {"path": str(tmp_path)}, "b": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["a", "name", "b"])
    await bot._on_edit_project(update, ctx)
    assert "already exists" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_editproject_invalid_field(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "color", "blue"])
    await bot._on_edit_project(update, ctx)
    assert "Unknown field" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_info(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_info_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert "myproj" in fake.edited_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_start(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_start_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert pm.status("myproj") == "running"
    pm.stop("myproj")


@pytest.mark.asyncio
async def test_callback_proj_stop(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    pm.start("myproj")
    assert pm.status("myproj") == "running"
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_stop_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_remove(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm._command_builder = lambda name, cfg: _sleep_cmd()
    pm.start("myproj")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_remove_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    assert "myproj" not in json.loads(proj_cfg.read_text())["projects"]
    assert pm.status("myproj") == "stopped"


@pytest.mark.asyncio
async def test_callback_proj_remove_managed_project_runs_cleanup(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": str(tmp_path),
                        "managed_by_manager": True,
                        "managed_repo_path": str(tmp_path),
                        "managed_bot_username": "myproj_bot",
                    }
                }
            }
        )
    )
    cleanup = AsyncMock(return_value=(["deleted repo"], []))
    bot._cleanup_managed_project_resources = cleanup
    fake = _swap_fake_transport(bot)

    click, _ = _make_button_click("proj_remove_myproj")
    await bot._on_button_from_transport(click)

    cleanup.assert_awaited_once()
    assert "cleaned up manager-owned resources" in fake.edited_messages[-1].text


@pytest.mark.asyncio
async def test_create_team_execute_missing_dependencies_returns_install_hint(bot_env, monkeypatch):
    from telegram.ext import ConversationHandler

    bot, _pm, _proj_cfg = bot_env
    fake = _swap_fake_transport(bot)

    def _boom():
        raise ImportError("missing")

    monkeypatch.setattr("link_project_to_chat.manager.bot._load_team_create_dependencies", _boom)

    update, ctx = _make_update()
    ctx.user_data = {"create_team": {}}
    result = await bot._create_team_execute(update, ctx)

    assert result == ConversationHandler.END
    assert "Missing dependencies" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_delete_team_execute_missing_dependencies_returns_install_hint(bot_env, monkeypatch):
    bot, _pm, _proj_cfg = bot_env
    fake = _swap_fake_transport(bot)

    def _boom():
        raise ImportError("missing")

    monkeypatch.setattr("link_project_to_chat.manager.bot._load_team_delete_dependencies", _boom)

    await bot._delete_team_execute(_make_invocation("delete_team").chat, "acme")

    assert "Missing dependencies" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_back(bot_env):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_back")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1


@pytest.mark.asyncio
async def test_callback_unauthorized(bot_env):
    """Unauthorized button clicks are silent — no edit, no reveal of dispatch
    structure. Behaviour shifted from the legacy popup ('Unauthorized.') because
    Transport doesn't expose answer-with-text; transport.on_button auto-answers
    silently before the handler runs. See spec #0c Task 10 self-review."""
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_back", user_id=999, username="hacker")
    await bot._on_button_from_transport(click)
    assert fake.edited_messages == []
    assert fake.sent_messages == []


@pytest.mark.asyncio
async def test_projects_header_shows_count(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    await bot._on_projects_from_transport(_make_invocation("projects"))
    assert "0/1" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_proj_edit_shows_fields(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_edit_myproj")
    await bot._on_button_from_transport(click)
    assert len(fake.edited_messages) == 1
    edited = fake.edited_messages[-1]
    assert "myproj" in edited.text
    assert edited.buttons is not None
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "proj_efld_path_myproj" in button_values
    assert "proj_efld_model_myproj" in button_values
    assert "proj_info_myproj" in button_values  # Back button


@pytest.mark.asyncio
async def test_edit_field_prompt_and_save(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)

    # Clicking the "model" field button shows a model picker (not pending_edit)
    click, state = _make_button_click("proj_efld_model_myproj")
    await bot._on_button_from_transport(click)
    assert "pending_edit" not in state
    assert len(fake.edited_messages) == 1
    assert "Select model" in fake.edited_messages[-1].text

    # Clicking a model option saves it
    click2, _ = _make_button_click("proj_model_opus_myproj")
    await bot._on_button_from_transport(click2)
    # v1.0.0 dropped the legacy top-level mirror; canonical home is backend_state.
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    assert proj["backend_state"]["claude"]["model"] == "opus"
    assert "model" not in proj


@pytest.mark.asyncio
async def test_edit_field_rename_via_button(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    click, state = _make_button_click("proj_efld_name_myproj")
    await bot._on_button_from_transport(click)

    # pending_edit persists in the same user_data dict, so route the followup text
    # through _edit_field_save with the same state to complete the rename.
    save_update, save_ctx = _make_update(text="renamed")
    save_ctx.user_data = state
    await bot._edit_field_save(save_update, save_ctx)
    projects = json.loads(proj_cfg.read_text())["projects"]
    assert "renamed" in projects and "myproj" not in projects


@pytest.mark.asyncio
async def test_edit_cancel(bot_env):
    bot, pm, proj_cfg = bot_env
    _swap_fake_transport(bot)
    update, ctx = _make_update()
    ctx.user_data = {"pending_edit": {"name": "myproj", "field": "model"}}
    await bot._edit_cancel(update, ctx)
    assert "pending_edit" not in ctx.user_data


@pytest.mark.asyncio
async def test_button_click_cancels_pending_edit(bot_env, tmp_path: Path):
    bot, pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    _swap_fake_transport(bot)

    # Start a non-model edit (e.g. "name") — this still uses pending_edit
    click, state = _make_button_click("proj_efld_name_myproj")
    await bot._on_button_from_transport(click)
    assert "pending_edit" in state

    # Click back — clears pending_edit (reusing the same user_data dict)
    click2, _ = _make_button_click("proj_back", user_data=state)
    await bot._on_button_from_transport(click2)
    assert "pending_edit" not in state


@pytest.mark.asyncio
async def test_edit_field_save_noop_without_pending(bot_env):
    bot, pm, proj_cfg = bot_env
    update, ctx = _make_update(text="some text")
    ctx.user_data = {}
    await bot._edit_field_save(update, ctx)  # should not raise or reply
    update.effective_message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_setup_code_strips_non_digits_before_sign_in(bot_env):
    """Telegram invalidates login codes typed verbatim into any chat, so the
    setup wizard tells the user to obfuscate (e.g. 1 2 3 4 5). The handler
    must strip non-digits before calling sign_in or the code will be rejected.
    """
    bot, _, _ = bot_env
    _swap_fake_transport(bot)

    fake_client = MagicMock()
    fake_client.sign_in = AsyncMock()
    fake_bf = MagicMock()
    fake_bf._ensure_client = AsyncMock(return_value=fake_client)

    update, ctx = _make_update(text="7 0 3 9 8")
    ctx.user_data = {
        "setup_awaiting": "code",
        "setup_phone": "+995511166693",
        "setup_bf_client": fake_bf,
    }
    await bot._edit_field_save(update, ctx)

    fake_client.sign_in.assert_awaited_once_with("+995511166693", "70398")


@pytest.mark.asyncio
async def test_post_stop_terminates_project_subprocesses(bot_env):
    """PTB's post_stop runs on Ctrl+C. If it doesn't call _pm.stop_all(),
    every project bot subprocess gets orphaned and keeps polling Telegram
    with its token — the next manager start hits "Conflict: terminated by
    other getUpdates request" until the user manually kills the strays.
    """
    bot, pm, _ = bot_env
    pm.stop_all = MagicMock(return_value=0)
    bot._telethon_client = None

    await bot._post_stop(MagicMock())

    pm.stop_all.assert_called_once()


@pytest.mark.asyncio
async def test_setup_promotes_authenticated_client_to_manager(bot_env):
    """After /setup successfully signs in, the freshly-authenticated
    TelegramClient must be promoted to ``self._telethon_client`` so that
    /create_project reuses it instead of opening a second SQLite connection
    against telethon.session (which would error with "database is locked").
    """
    bot, _, _ = bot_env
    _swap_fake_transport(bot)
    bot._telethon_client = None

    fake_client = MagicMock(name="setup_telethon_client")
    fake_client.sign_in = AsyncMock()
    fake_bf = MagicMock()
    fake_bf._ensure_client = AsyncMock(return_value=fake_client)
    fake_bf._owns_client = True

    update, ctx = _make_update(text="7 0 3 9 8")
    ctx.user_data = {
        "setup_awaiting": "code",
        "setup_phone": "+995511166693",
        "setup_bf_client": fake_bf,
    }
    await bot._edit_field_save(update, ctx)

    assert bot._telethon_client is fake_client
    # The wizard's BotFatherClient must surrender ownership so disconnect
    # paths don't double-close the now-shared client.
    assert fake_bf._owns_client is False


@pytest.mark.asyncio
async def test_execute_bot_creation_reuses_managers_telethon_client(
    bot_env, tmp_path: Path, monkeypatch
):
    """The manager keeps a persistent Telethon client connected to the
    telethon.session SQLite file. Constructing a new TelegramClient against
    the same file in /create_project would raise "database is locked", so
    BotFatherClient must adopt the manager's client when one exists.
    """
    bot, _, proj_cfg = bot_env
    _swap_fake_transport(bot)

    from link_project_to_chat.config import Config, save_config
    save_config(Config(telegram_api_id=1, telegram_api_hash="x"), proj_cfg)

    constructor_kwargs: dict = {}
    create_bot_kwargs: dict = {}

    class FakeBotFatherClient:
        def __init__(self, api_id, api_hash, session_path, client=None):
            constructor_kwargs["client"] = client

        async def create_bot(self, display_name: str, username: str) -> str:
            create_bot_kwargs["display_name"] = display_name
            create_bot_kwargs["username"] = username
            raise RuntimeError("stop here — we only care about constructor args")

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(
        "link_project_to_chat.botfather.BotFatherClient", FakeBotFatherClient
    )

    sentinel_client = MagicMock(name="manager_telethon_client")
    bot._telethon_client = sentinel_client

    update, ctx = _make_update()
    ctx.user_data = {"create": {"config_path": str(proj_cfg), "name": "myproj"}}
    await bot._execute_bot_creation(
        ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM), ctx, "myproj"
    )

    assert constructor_kwargs["client"] is sentinel_client
    assert create_bot_kwargs == {
        "display_name": "myproj Agent",
        "username": "myproj_bot",
    }


def _write_team(proj_cfg: Path, team: str, bots: dict, group_chat_id: int = -1001) -> None:
    raw = json.loads(proj_cfg.read_text())
    raw.setdefault("teams", {})[team] = {
        "path": str(proj_cfg.parent),
        "group_chat_id": group_chat_id,
        "bots": bots,
    }
    proj_cfg.write_text(json.dumps(raw))


@pytest.mark.asyncio
async def test_on_teams_lists_one_button_per_team(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    _write_team(proj_cfg, "beta", {"manager": {"telegram_bot_token": "t3"}})
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    assert len(fake.sent_messages) == 1
    buttons = fake.sent_messages[-1].buttons
    assert buttons is not None
    button_values = [btn.value for row in buttons.rows for btn in row]
    assert button_values == ["team_info_acme", "team_info_beta"]


@pytest.mark.asyncio
async def test_on_teams_button_label_shows_running_count(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    buttons = fake.sent_messages[-1].buttons
    assert buttons is not None
    labels = [btn.label for row in buttons.rows for btn in row]
    assert any("0/2" in label and "acme" in label for label in labels)


@pytest.mark.asyncio
async def test_on_teams_empty_no_markup(bot_env):
    bot, pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    await bot._on_teams_from_transport(_make_invocation("teams"))
    assert "No teams" in fake.sent_messages[-1].text


@pytest.mark.asyncio
async def test_callback_team_info_shows_start_and_per_bot_status(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_info_acme")
    await bot._on_button_from_transport(click)
    edited = fake.edited_messages[-1]
    assert "acme" in edited.text
    assert "manager" in edited.text and "dev" in edited.text
    assert edited.buttons is not None
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_start_acme" in button_values
    assert "team_back" in button_values


@pytest.mark.asyncio
async def test_callback_team_start_invokes_start_team_for_each_bot(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.start_team = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="running")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_start_acme")
    await bot._on_button_from_transport(click)
    calls = {c.args for c in pm.start_team.call_args_list}
    assert calls == {("acme", "manager"), ("acme", "dev")}
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_stop_acme" in button_values


@pytest.mark.asyncio
async def test_callback_team_stop_invokes_stop_for_each_bot(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.stop = MagicMock(return_value=True)
    pm.status = MagicMock(return_value="stopped")
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_stop_acme")
    await bot._on_button_from_transport(click)
    stopped_keys = {c.args[0] for c in pm.stop.call_args_list}
    assert stopped_keys == {"team:acme:manager", "team:acme:dev"}
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_start_acme" in button_values


@pytest.mark.asyncio
async def test_callback_team_back_relists_teams(bot_env):
    bot, pm, proj_cfg = bot_env
    _write_team(proj_cfg, "acme", {"manager": {"telegram_bot_token": "t1"}})
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_back")
    await bot._on_button_from_transport(click)
    edited = fake.edited_messages[-1]
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "team_info_acme" in button_values


@pytest.mark.asyncio
async def test_guard_returns_false_when_effective_user_is_none(bot_env):
    """Regression: _guard must not crash when update.effective_user is None
    (anonymous channel admins, service messages, etc.)."""
    from types import SimpleNamespace

    bot, _pm, _cfg = bot_env
    fake = _swap_fake_transport(bot)

    update = SimpleNamespace(
        effective_user=None,
        effective_chat=SimpleNamespace(id=12345, type="private"),
        effective_message=SimpleNamespace(text=""),
    )

    allowed = await bot._guard(update)
    assert allowed is False
    assert any("Unauthorized" in m.text for m in fake.sent_messages)


# --- P1 #1: viewer cannot run state-changing manager buttons ---

@pytest.fixture
def viewer_bot_env(tmp_path: Path):
    """Manager bot with a single viewer user. Used to verify P1 #1 button gating.

    Locks the viewer's identity to ``fake:1`` and ``telegram:1`` so both the
    button (FakeTransport) and the wizard (telegram-shaped Update from
    ``_make_update``) code paths authenticate the same caller as a viewer.
    Without the telegram lock, viewer-driven `/edit_project` etc. would fall
    out before hitting the executor gate (the wizard's ``_guard`` path uses
    ``transport_id="telegram"`` via ``identity_from_telegram_user``).
    """
    proj_cfg = tmp_path / "projects.json"
    proj_cfg.write_text(json.dumps({"projects": {}}))
    pm = ProcessManager(project_config_path=proj_cfg)
    from link_project_to_chat.config import AllowedUser
    bot = ManagerBot(
        "TOKEN", pm,
        allowed_users=[
            AllowedUser(
                username="viewer-bob",
                role="viewer",
                locked_identities=["fake:1", "telegram:1"],
            ),
        ],
        project_config_path=proj_cfg,
    )
    return bot, pm, proj_cfg


@pytest.mark.asyncio
async def test_viewer_cannot_start_project_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: viewer pressing the Start button on a project
    detail card receives a read-only reply and the ProcessManager is NOT
    asked to spawn the subprocess."""
    bot, pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm.start = MagicMock()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_start_myproj")
    await bot._on_button_from_transport(click)
    pm.start.assert_not_called()
    # No state-changing edit; we instead expect a read-only reply.
    assert fake.edited_messages == []
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_stop_project_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: viewer pressing Stop is blocked."""
    bot, pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    pm.stop = MagicMock()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_stop_myproj")
    await bot._on_button_from_transport(click)
    pm.stop.assert_not_called()
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_remove_project_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: viewer pressing Remove cannot delete the project."""
    bot, pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_remove_myproj")
    await bot._on_button_from_transport(click)
    # Project still present on disk.
    assert "myproj" in json.loads(proj_cfg.read_text())["projects"]
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_open_edit_keyboard_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: the Edit button itself is gated — the keyboard
    it would render leads straight into write operations."""
    bot, pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_edit_myproj")
    await bot._on_button_from_transport(click)
    assert fake.edited_messages == []
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_pick_proj_model_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: viewer cannot write a per-project model via the
    inline picker."""
    bot, pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_model_opus_myproj")
    await bot._on_button_from_transport(click)
    assert json.loads(proj_cfg.read_text())["projects"]["myproj"].get("model") != "opus"
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_set_global_model_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: viewer cannot set the global default model
    through the model picker keyboard."""
    bot, pm, proj_cfg = viewer_bot_env
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("global_model_opus")
    await bot._on_button_from_transport(click)
    raw = json.loads(proj_cfg.read_text()) if proj_cfg.exists() else {}
    assert raw.get("default_model_claude") != "opus"
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_start_team_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: team_start_* must gate to executor."""
    bot, pm, proj_cfg = viewer_bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.start_team = MagicMock()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_start_acme")
    await bot._on_button_from_transport(click)
    pm.start_team.assert_not_called()
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_stop_team_via_button(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: team_stop_* must gate to executor."""
    bot, pm, proj_cfg = viewer_bot_env
    _write_team(proj_cfg, "acme", {
        "manager": {"telegram_bot_token": "t1"},
        "dev":     {"telegram_bot_token": "t2"},
    })
    pm.stop = MagicMock()
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("team_stop_acme")
    await bot._on_button_from_transport(click)
    pm.stop.assert_not_called()
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_arm_setup_gh_via_button(viewer_bot_env):
    """Regression for P1 #1: setup_* buttons must gate to executor. These
    arm a setup_awaiting state whose follow-up text input writes the
    GitHub PAT / API credentials to config.json — a viewer must not be
    able to start that flow."""
    bot, _pm, _proj_cfg = viewer_bot_env
    fake = _swap_fake_transport(bot)
    click, state = _make_button_click("setup_gh")
    await bot._on_button_from_transport(click)
    assert "setup_awaiting" not in state
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_complete_pending_edit_via_text(viewer_bot_env, tmp_path: Path):
    """Defense-in-depth: even if pending_edit somehow ends up in user_data
    for a viewer (e.g. carried over from a prior executor session in the
    same PTB process), the text-save path must refuse the write."""
    bot, _pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(text="newname")
    ctx.user_data = {"pending_edit": {"name": "myproj", "field": "name"}}
    await bot._edit_field_save(update, ctx)
    # Project was not renamed.
    assert "myproj" in json.loads(proj_cfg.read_text())["projects"]
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_complete_setup_input_via_text(viewer_bot_env, tmp_path: Path):
    """Defense-in-depth: even if setup_awaiting is leaked into a viewer's
    user_data slot, the text path must refuse to write."""
    bot, _pm, proj_cfg = viewer_bot_env
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(text="ghp_secret_pat_abc123")
    ctx.user_data = {
        "setup_awaiting": "github_pat",
        "setup_config_path": str(proj_cfg),
    }
    await bot._edit_field_save(update, ctx)
    raw = json.loads(proj_cfg.read_text()) if proj_cfg.exists() else {}
    assert raw.get("github_pat") in (None, "", False)
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_complete_setup_input_via_text(bot_env, tmp_path: Path):
    """Regression for the post-v1.0 P1: an unauthenticated caller with a
    stale/leaked setup_awaiting state must NOT be able to write setup
    values like github_pat.

    Previous guard at manager/bot.py:1159 was
    ``self._auth_identity(identity) and not self._require_executor(identity)``
    — which only blocks AUTHENTICATED viewers. Unauthenticated callers fell
    through to _handle_setup_input and successfully wrote ghp_BAD into the
    config. Fix requires authenticated executor before _handle_setup_input;
    clears setup_awaiting and replies Unauthorized./Read-only on every
    non-executor branch so a single bad reply can't keep collecting writes.
    """
    bot, _pm, proj_cfg = bot_env
    fake = _swap_fake_transport(bot)
    # mallory is NOT in the allow-list (bot_env locks testuser to telegram:1
    # as the only allowed executor). Picking user_id=999 + username=mallory
    # guarantees _auth_identity returns False.
    update, ctx = _make_update(user_id=999, username="mallory", text="ghp_BAD")
    ctx.user_data = {
        "setup_awaiting": "github_pat",
        "setup_config_path": str(proj_cfg),
    }
    await bot._edit_field_save(update, ctx)
    raw = json.loads(proj_cfg.read_text()) if proj_cfg.exists() else {}
    assert raw.get("github_pat") in (None, "", False), (
        f"unauthorized user wrote github_pat: {raw.get('github_pat')!r}"
    )
    # setup_awaiting must be cleared so a stale state can't keep collecting writes.
    assert "setup_awaiting" not in ctx.user_data
    # Reply is "Unauthorized." (the viewer-blocked path would say "read-only").
    text = fake.sent_messages[-1].text.lower()
    assert "unauthorized" in text


@pytest.mark.asyncio
async def test_viewer_cannot_run_add_project_wizard(viewer_bot_env):
    """Regression for P1 #1: /add_project wizard entry must gate to executor."""
    from telegram.ext import ConversationHandler
    bot, _pm, _proj_cfg = viewer_bot_env
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(username="viewer-bob")
    result = await bot._on_add_project(update, ctx)
    assert result == ConversationHandler.END
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_viewer_cannot_run_edit_project_command(viewer_bot_env, tmp_path: Path):
    """Regression for P1 #1: /edit_project must gate to executor."""
    bot, _pm, proj_cfg = viewer_bot_env
    proj_cfg.write_text(json.dumps({"projects": {"myproj": {"path": str(tmp_path)}}}))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "token", "X"], username="viewer-bob")
    await bot._on_edit_project(update, ctx)
    # Project token must remain untouched.
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    assert proj.get("telegram_bot_token") != "X"
    text = fake.sent_messages[-1].text.lower()
    assert "read-only" in text or "executor" in text


# --- P1 #2: manager username writes must translate to allowed_users ---

@pytest.mark.asyncio
async def test_apply_edit_username_replaces_allowed_users(bot_env, tmp_path: Path):
    """Regression for P1 #2: `/edit_project NAME username X` must produce an
    actually-authorizing allowed_users entry — historically wrote a legacy
    flat ``username`` key that loses to any pre-existing allowed_users on
    next load.
    """
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "T",
                "allowed_users": [{"username": "alice", "role": "executor"}],
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "username", "bob"])
    await bot._on_edit_project(update, ctx)
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    # bob fully replaces alice.
    assert proj["allowed_users"] == [{"username": "bob", "role": "executor"}]
    # Legacy flat key removed.
    assert "username" not in proj
    # Operator gets the deprecation hint pointing to /add_user / /remove_user.
    last_text = fake.sent_messages[-1].text
    assert "allowed_users" in last_text or "/add_user" in last_text


@pytest.mark.asyncio
async def test_apply_edit_username_normalizes_handle(bot_env, tmp_path: Path):
    """Manager-side normalization matches CLI: lowercase + strip leading @."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {"path": str(tmp_path), "telegram_bot_token": "T"}
        }
    }))
    _swap_fake_transport(bot)
    update, ctx = _make_update(["myproj", "username", "@BobUpper"])
    await bot._on_edit_project(update, ctx)
    proj = json.loads(proj_cfg.read_text())["projects"]["myproj"]
    assert proj["allowed_users"] == [{"username": "bobupper", "role": "executor"}]


@pytest.mark.asyncio
async def test_remove_user_revokes_trusted_binding_immediately(bot_env):
    from link_project_to_chat.config import AllowedUser
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(
        json.dumps(
            {
                "allowed_users": [
                    {"username": "testuser", "role": "executor", "locked_identities": ["fake:1"]},
                    {"username": "alice", "role": "executor", "locked_identities": ["telegram:42"]},
                ],
                "projects": {},
            }
        )
    )
    bot._allowed_users = [
        AllowedUser(username="testuser", role="executor", locked_identities=["fake:1"]),
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:42"]),
    ]
    fake = _swap_fake_transport(bot)

    # Task 6: /remove_user now goes through _on_remove_user, which gates to
    # executor role and replies with the full updated /users listing.
    invocation = _make_invocation("remove_user", args=["alice"])
    await bot._on_remove_user(invocation)

    last_text = fake.sent_messages[-1].text
    assert "alice" not in last_text and "testuser" in last_text
    raw = json.loads(proj_cfg.read_text())
    # Legacy keys never appear post-Task-5.
    assert "allowed_usernames" not in raw
    assert "trusted_users" not in raw
    surviving = {u["username"] for u in raw.get("allowed_users", [])}
    assert surviving == {"testuser"}
    # Alice's locked identity is gone — she can no longer auth.
    revoked = Identity(
        transport_id="telegram",
        native_id="42",
        display_name="alice",
        handle="alice",
        is_bot=False,
    )
    assert bot._auth_identity(revoked) is False


@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_true(bot_env, tmp_path: Path):
    """Manager wizard: setting respond_in_groups to a truthy string flips
    the per-project flag on, persists to disk."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "true")
    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["myproj"]["respond_in_groups"] is True
    text = fake.sent_messages[-1].text.lower()
    assert "respond_in_groups" in text or "updated" in text


@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_false_strips_key(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    }))
    _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "false")
    raw = json.loads(proj_cfg.read_text())
    assert "respond_in_groups" not in raw["projects"]["myproj"]


def test_editable_fields_include_respond_in_groups():
    """Manager has TWO related tuples (verified at manager/bot.py:53-54):
    - _EDITABLE_FIELDS: consumed by /edit_project's help text + unknown-field
      error path, plus the project-edit text wizard.
    - _BUTTON_EDIT_FIELDS: consumed by the project-detail keyboard generator
      that auto-creates the per-field edit button.

    Both must include the new field so respond_in_groups is reachable from
    BOTH the CommandHandler (/edit_project NAME respond_in_groups VALUE) AND
    the inline keyboard.
    """
    from link_project_to_chat.manager.bot import (  # type: ignore[attr-defined]
        _BUTTON_EDIT_FIELDS,
        _EDITABLE_FIELDS,
    )
    assert "respond_in_groups" in _EDITABLE_FIELDS
    assert "respond_in_groups" in _BUTTON_EDIT_FIELDS


@pytest.mark.asyncio
async def test_apply_edit_respond_in_groups_invalid_value_replies_error(bot_env, tmp_path: Path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "respond_in_groups", "maybe")
    raw = json.loads(proj_cfg.read_text())
    # File unchanged.
    assert "respond_in_groups" not in raw["projects"]["myproj"]
    text = fake.sent_messages[-1].text.lower()
    assert "invalid" in text or "true" in text  # error mentions accepted values


@pytest.mark.asyncio
async def test_edit_respond_in_groups_button_renders_on_off_picker(
    bot_env, tmp_path: Path,
):
    """Clicking the respond_in_groups field button shows an On/Off picker
    (not a text-input prompt). Default state Off → 'Off' shows the ● glyph.
    """
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": str(tmp_path)}}
    }))
    fake = _swap_fake_transport(bot)
    click, state = _make_button_click("proj_efld_respond_in_groups_myproj")
    await bot._on_button_from_transport(click)
    # No pending_edit armed — picker is button-driven, not text-driven.
    assert "pending_edit" not in state
    edited = fake.edited_messages[-1]
    assert "Respond in groups" in edited.text
    assert "Off" in edited.text  # current state shown
    assert edited.buttons is not None
    button_values = [btn.value for row in edited.buttons.rows for btn in row]
    assert "proj_rig_on_myproj" in button_values
    assert "proj_rig_off_myproj" in button_values
    # Off is default state → ● glyph appears on the Off label.
    labels = [btn.label for row in edited.buttons.rows for btn in row]
    assert any("● Off" in l for l in labels)
    assert all("● On" not in l for l in labels)


@pytest.mark.asyncio
async def test_proj_rig_on_button_flips_field_true_and_persists(
    bot_env, tmp_path: Path,
):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": str(tmp_path)}}
    }))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_rig_on_myproj")
    await bot._on_button_from_transport(click)
    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["myproj"]["respond_in_groups"] is True
    # Picker re-rendered with ● On.
    edited = fake.edited_messages[-1]
    labels = [btn.label for row in edited.buttons.rows for btn in row]
    assert any("● On" in l for l in labels)
    assert "Restart" in edited.text


@pytest.mark.asyncio
async def test_proj_rig_off_button_strips_key_from_config(
    bot_env, tmp_path: Path,
):
    """Off click drops the key (emit-only-when-True policy) rather than
    writing an explicit false, keeping on-disk configs tidy."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "respond_in_groups": True,
            }
        }
    }))
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_rig_off_myproj")
    await bot._on_button_from_transport(click)
    raw = json.loads(proj_cfg.read_text())
    assert "respond_in_groups" not in raw["projects"]["myproj"]
    edited = fake.edited_messages[-1]
    labels = [btn.label for row in edited.buttons.rows for btn in row]
    assert any("● Off" in l for l in labels)


@pytest.mark.asyncio
async def test_proj_rig_buttons_gated_to_executor(bot_env, tmp_path: Path):
    """Viewer-role users can't toggle the picker — same role gate that
    protects every other state-changing button.

    Uses the same handle as bot_env's default AllowedUser ("testuser") so
    auth succeeds via username fallback; the role check then trips.
    """
    from link_project_to_chat.config import AllowedUser

    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": str(tmp_path)}}
    }))
    # Demote the bot_env user to viewer (handle/identity unchanged so auth
    # still succeeds; only role gate trips).
    bot._allowed_users = [
        AllowedUser(username="testuser", role="viewer", locked_identities=["fake:1"]),
    ]
    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_rig_on_myproj")
    await bot._on_button_from_transport(click)
    # Field on disk unchanged.
    raw = json.loads(proj_cfg.read_text())
    assert "respond_in_groups" not in raw["projects"]["myproj"]
    # User informed it's read-only.
    text = (fake.sent_messages[-1].text if fake.sent_messages else "").lower()
    assert "read-only" in text or "executor" in text


@pytest.mark.asyncio
async def test_apply_edit_safety_prompt_custom_string(bot_env, tmp_path):
    """Manager wizard: setting safety_prompt to a custom string persists it."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": str(tmp_path), "telegram_bot_token": "t"}}
    }))
    fake = _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "safety_prompt", "custom guardrail")
    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["myproj"]["safety_prompt"] == "custom guardrail"


@pytest.mark.asyncio
async def test_apply_edit_safety_prompt_default_strips_key(bot_env, tmp_path):
    """Manager wizard: 'default' restores the built-in safety prompt by
    stripping the key from disk."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {
            "path": str(tmp_path),
            "telegram_bot_token": "t",
            "safety_prompt": "old custom",
        }}
    }))
    _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "safety_prompt", "default")
    raw = json.loads(proj_cfg.read_text())
    assert "safety_prompt" not in raw["projects"]["myproj"]


@pytest.mark.asyncio
async def test_apply_edit_safety_prompt_empty_disables(bot_env, tmp_path):
    """Manager wizard: empty string writes the explicit-disable signal."""
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": str(tmp_path), "telegram_bot_token": "t"}}
    }))
    _swap_fake_transport(bot)
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    await bot._apply_edit(chat, "myproj", "safety_prompt", "")
    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["myproj"]["safety_prompt"] == ""


def test_editable_fields_include_safety_prompt():
    from link_project_to_chat.manager.bot import _BUTTON_EDIT_FIELDS, _EDITABLE_FIELDS
    assert "safety_prompt" in _EDITABLE_FIELDS
    assert "safety_prompt" in _BUTTON_EDIT_FIELDS
